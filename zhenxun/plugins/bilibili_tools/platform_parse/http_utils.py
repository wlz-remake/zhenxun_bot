"""多平台解析通用 HTTP 工具(基于 httpx)

参考 AstrBot 万能解析插件 core/download.py 的流式下载与大小限制思路,
统一实现: 重定向获取、页面/JSON 请求、带大小上限的媒体下载。
"""

import hashlib
from pathlib import Path
import re
from typing import Any

import aiofiles
import httpx

from zhenxun.services.log import logger

from ..utils.exceptions import DownloadError


class MediaTooLargeError(DownloadError):
    """媒体文件超出大小上限"""

    pass


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    )
}

IOS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
    )
}

ANDROID_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36 Edg/132.0.0.0"
    )
}

DEFAULT_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 300
"""媒体下载超时(秒), 视频文件较大, 单独放宽"""


def new_client(follow_redirects: bool = True, timeout: int = DEFAULT_TIMEOUT):
    """创建通用 HTTP 客户端"""
    return httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=timeout,
    )


async def get_redirect_url(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> str:
    """获取单次重定向的目标 URL(不自动跟随)

    无 Location 时返回原 URL, 网络错误时抛出 httpx 异常。
    """
    resp = await client.get(
        url, headers=headers or COMMON_HEADERS, follow_redirects=False
    )
    if resp.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"redirect check {resp.status_code}",
            request=resp.request,
            response=resp,
        )
    return resp.headers.get("Location", url) or url


async def get_final_url(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> str:
    """跟随全部重定向, 返回最终 URL"""
    resp = await client.get(url, headers=headers or COMMON_HEADERS)
    resp.raise_for_status()
    return str(resp.url)


async def get_text(
    client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
) -> str:
    """GET 请求并返回文本, 非 2xx 抛出异常"""
    resp = await client.get(url, headers=headers or COMMON_HEADERS)
    resp.raise_for_status()
    return resp.text


async def get_json(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    data: Any = None,
) -> Any:
    """GET/POST 请求并返回 JSON"""
    if method.upper() == "POST":
        resp = await client.post(url, headers=headers or COMMON_HEADERS, data=data)
    else:
        resp = await client.get(url, headers=headers or COMMON_HEADERS)
    resp.raise_for_status()
    return resp.json()


def config_int(key: str, default: int, *, none_as_zero: bool = False) -> int:
    """读取整型配置项, 非法值(手改 yaml 写入字符串等)回退默认值避免持续崩溃"""
    from ..config import base_config

    value = base_config.get(key, default)
    if none_as_zero and (value is None or value == ""):
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            f"配置项 {key} 的值无效: {value!r}, 已回退默认值 {default}", "多平台解析"
        )
        return default


def config_bool(key: str, default: bool) -> bool:
    """读取布尔配置项, 容错解析手改 yaml 写入的字符串形式"""
    from ..config import base_config

    value = base_config.get(key, default)
    if value is None or value == "":
        # yaml 显式写空的键回退默认值, 而非静默当作 False
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "enable"):
            return True
        if lowered in ("0", "false", "no", "off", "disable"):
            return False
        logger.warning(
            f"配置项 {key} 的值无效: {value!r}, 已回退默认值 {default}", "多平台解析"
        )
        return default
    return bool(value)


def generate_file_name(url: str, suffix: str) -> str:
    """根据 URL 生成稳定的缓存文件名(同一 URL 复用同一缓存文件)"""
    name_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    return f"{name_hash}{suffix}"


def guess_url_suffix(url: str, default: str) -> str:
    """从 URL 路径猜测文件后缀"""
    path = url.split("?", 1)[0].split("#", 1)[0]
    match = re.search(r"\.([A-Za-z0-9]{1,5})$", path)
    if match:
        suffix = f".{match.group(1).lower()}"
        # 常见媒体后缀白名单, 避免取到无意义后缀
        if suffix in (
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
            ".gif",
            ".mp4",
            ".mp3",
            ".m4a",
            ".flac",
            ".aac",
        ):
            return suffix
    return default


async def download_media(
    url: str,
    save_dir: Path,
    headers: dict[str, str] | None = None,
    max_size_mb: int = 0,
    default_suffix: str = ".jpg",
    prefix: str = "platform_img_",
) -> Path:
    """流式下载媒体文件到指定目录(带大小上限与同 URL 缓存复用)

    Args:
        url: 媒体直链
        save_dir: 保存目录
        headers: 下载请求头(防盗链场景必传)
        max_size_mb: 大小上限(MB), <=0 表示不限制
        default_suffix: 无法从 URL 猜测后缀时的默认后缀
        prefix: 缓存文件名前缀

    Returns:
        下载完成的文件路径

    Raises:
        DownloadError: 下载失败或超出大小上限
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    suffix = guess_url_suffix(url, default_suffix)
    file_path = save_dir / f"{prefix}{generate_file_name(url, suffix)}"

    if file_path.exists() and file_path.stat().st_size > 0:
        logger.debug(f"媒体缓存命中: {file_path.name}")
        return file_path

    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
    total_size = 0

    try:
        async with new_client(
            follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
        ) as client:
            async with client.stream(
                "GET", url, headers=headers or COMMON_HEADERS
            ) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if (
                    max_size_bytes
                    and content_length
                    and content_length.isdigit()
                    and int(content_length) > max_size_bytes
                ):
                    raise MediaTooLargeError(
                        f"媒体文件过大({int(content_length) / 1024 / 1024:.1f}MB), "
                        f"超过 {max_size_mb}MB 上限",
                        context={"url": url},
                    )
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        total_size += len(chunk)
                        if max_size_bytes and total_size > max_size_bytes:
                            raise MediaTooLargeError(
                                f"媒体文件超过 {max_size_mb}MB 上限, 已中断下载",
                                context={"url": url},
                            )
                        await f.write(chunk)
    except MediaTooLargeError:
        # 大小超限: 清理半成品文件后原样抛出
        file_path.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as e:
        file_path.unlink(missing_ok=True)
        raise DownloadError(
            f"媒体下载失败: {type(e).__name__}: {e}",
            context={"url": url, "file_path": str(file_path)},
        ) from e
    except Exception as e:
        file_path.unlink(missing_ok=True)
        raise DownloadError(
            f"媒体下载时发生未预期的错误: {type(e).__name__}",
            context={"url": url, "file_path": str(file_path)},
            cause=e,
        ) from e

    if total_size == 0:
        file_path.unlink(missing_ok=True)
        raise DownloadError("媒体下载内容为空", context={"url": url})

    logger.debug(f"媒体下载完成: {file_path.name} ({total_size / 1024:.0f}KB)")
    return file_path


def parse_m3u8_segments(m3u8_url: str, m3u8_text: str) -> list[str]:
    """解析 m3u8 播放列表中的全部分段地址(urljoin 兼容绝对/相对路径)"""
    from urllib.parse import urljoin

    if "#EXT-X-STREAM-INF" in m3u8_text:
        raise DownloadError(
            "暂不支持多码率 master playlist 的 HLS 视频", context={"url": m3u8_url}
        )

    segments: list[str] = []
    for line in m3u8_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        segments.append(urljoin(m3u8_url, line))
    return segments


async def download_m3u8_to_file(
    m3u8_url: str,
    save_dir: Path,
    headers: dict[str, str] | None = None,
    max_size_mb: int = 100,
    prefix: str = "platform_video_",
    file_stem: str | None = None,
) -> Path:
    """下载 m3u8(HLS) 视频并拼接为本地文件

    顺序下载全部分段后拼接为 .ts, 若系统存在 ffmpeg 则 remux 为标准 mp4
    (替代参考插件的 yt-dlp 方案, 复用插件已有的 ffmpeg 系统依赖)。
    未加密 HLS 流可直接处理, 加密流会产出损坏文件并抛出异常。

    Returns:
        下载完成的本地文件路径
    """
    import asyncio as _asyncio

    from ..utils.file_utils import check_ffmpeg_available

    async with new_client(timeout=30) as client:
        resp = await client.get(m3u8_url, headers=headers or COMMON_HEADERS)
        resp.raise_for_status()
        segments = parse_m3u8_segments(m3u8_url, resp.text)
    if not segments:
        raise DownloadError("m3u8 中未找到视频分段", context={"url": m3u8_url})

    save_dir.mkdir(parents=True, exist_ok=True)
    stem = file_stem or generate_file_name(m3u8_url, "")[:16]
    ts_file = save_dir / f"{prefix}{stem}.ts"
    mp4_file = save_dir / f"{prefix}{stem}.mp4"
    if mp4_file.exists() and mp4_file.stat().st_size > 0:
        return mp4_file

    max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
    total = 0
    try:
        async with aiofiles.open(ts_file, "wb") as f:
            async with new_client(timeout=60) as client:
                for seg_url in segments:
                    async with client.stream(
                        "GET", seg_url, headers=headers or COMMON_HEADERS
                    ) as seg_resp:
                        seg_resp.raise_for_status()
                        async for chunk in seg_resp.aiter_bytes():
                            total += len(chunk)
                            if max_size_bytes and total > max_size_bytes:
                                raise MediaTooLargeError(
                                    f"HLS 视频超过 {max_size_mb}MB 上限, 已中断下载",
                                    context={"url": m3u8_url},
                                )
                            await f.write(chunk)
    except MediaTooLargeError:
        ts_file.unlink(missing_ok=True)
        raise
    except httpx.HTTPError as e:
        ts_file.unlink(missing_ok=True)
        raise DownloadError(
            f"HLS 视频分段下载失败: {type(e).__name__}: {e}",
            context={"url": m3u8_url},
        ) from e

    if not await check_ffmpeg_available():
        # 无 ffmpeg 时返回拼接的 ts 文件(部分播放器兼容)
        return ts_file

    try:
        proc = await _asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(ts_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(mp4_file),
            stdout=_asyncio.subprocess.DEVNULL,
            stderr=_asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0 and mp4_file.exists():
            ts_file.unlink(missing_ok=True)
            return mp4_file
        # remux 失败: 清理半成品 mp4, 避免下次缓存命中损坏文件
        mp4_file.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"HLS remux 失败, 使用原始拼接文件: {e}")
        mp4_file.unlink(missing_ok=True)
    return ts_file
