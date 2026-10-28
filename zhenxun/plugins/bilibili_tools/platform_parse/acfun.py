"""AcFun(A站) 解析器

移植自 AstrBot 万能解析插件 core/parsers/acfun.py:
ajaxpipe 接口取视频信息与 m3u8 播放列表 → 顺序下载 TS 分段拼接,
若系统存在 ffmpeg 则进一步 remux 为标准 mp4。
"""

import asyncio
import json
import re
import time
from typing import Any, ClassVar

import aiofiles
import httpx

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR
from ..utils.exceptions import DownloadError, ResourceNotFoundError, UrlParseError
from ..utils.file_utils import check_ffmpeg_available
from .base import PlatformParser
from .http_utils import (
    COMMON_HEADERS,
    MediaTooLargeError,
    config_int,
    new_client,
)
from .models import MediaType, PlatformMedia, PlatformParseResult

_HEADERS = {"Referer": "https://www.acfun.cn/", **COMMON_HEADERS}
_VIDEO_INFO_PATTERN = re.compile(r"window\.videoInfo =(.*?)</script>", re.DOTALL)


class AcfunParser(PlatformParser):
    PLATFORM_NAME = "acfun"
    DISPLAY_NAME = "A站"
    PRIORITY = 60
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://www.acfun.cn/v/ac38888888 (分享文本中也可能出现 ?ac= 形式)
        (
            # ac= 分支加负向后行断言, 防止 "bac=123" 之类闲聊误触发;
            # /ac 分支自带斜杠分隔, 无需额外边界
            "acfun",
            re.compile(r"(?:(?<!\w)ac=|/ac)(?P<acid>\d+)"),
        ),
    ]

    async def parse(self, url: str, matched: re.Match[str]) -> PlatformParseResult:
        acid = int(matched.group("acid"))
        page_url = f"https://www.acfun.cn/v/ac{acid}"
        video_info = await self._fetch_video_info(page_url)

        m3u8_url = self._select_m3u8_url(video_info.get("currentVideoInfo") or {})
        if not m3u8_url:
            raise ResourceNotFoundError("A站视频中未找到可用的播放地址")

        timestamp = None
        if create_time := video_info.get("createTime"):
            try:
                timestamp = int(time.mktime(time.strptime(create_time, "%Y-%m-%d")))
            except ValueError:
                timestamp = None

        # m3u8 需要下载拼接, 在解析阶段落盘, 发送阶段直接使用本地文件
        video_path = await self._download_video(m3u8_url, acid)
        description = (video_info.get("description") or "").strip() or None
        contents = [
            PlatformMedia(
                type=MediaType.VIDEO,
                url=m3u8_url,
                headers=_HEADERS,
                local_path=str(video_path),
            )
        ]

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(video_info.get("title") or "").strip() or None,
            text=f"简介：{description}" if description else None,
            author_name=(video_info.get("user") or {}).get("name") or None,
            timestamp=timestamp,
            url=page_url,
            contents=contents,
        )

    async def _fetch_video_info(self, page_url: str) -> dict[str, Any]:
        async with new_client() as client:
            resp = await client.get(
                f"{page_url}?quickViewId=videoInfo_new&ajaxpipe=1", headers=_HEADERS
            )
            resp.raise_for_status()
            raw = resp.text

        matched = _VIDEO_INFO_PATTERN.search(raw)
        if not matched:
            raise ResourceNotFoundError("A站视频信息解析失败(可能已被删除)")
        # 接口返回的 JSON 经过了转义处理, 需还原
        json_str = matched.group(1).replace('\\\\"', '\\"').replace('\\"', '"')
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise UrlParseError("A站视频信息 JSON 解析失败") from e

    @staticmethod
    def _select_m3u8_url(current_video_info: dict[str, Any]) -> str | None:
        """从 ksPlayJson 的多清晰度表示中取 720p(索引3), 越界时回退到最高可用项"""
        ks_play_json = current_video_info.get("ksPlayJson")
        if not ks_play_json:
            return None
        try:
            ks_play = json.loads(ks_play_json)
            representations = ks_play["adaptationSet"][0]["representation"]
            urls = [d.get("url") for d in representations if d.get("url")]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
            logger.warning(f"A站 m3u8 清晰度解析失败: {e}", "多平台解析")
            return None
        if not urls:
            return None
        # 清晰度从高到低排列, 默认取 720p
        return urls[3] if len(urls) > 3 else urls[-1]

    async def _download_video(self, m3u8_url: str, acid: int):
        """下载 m3u8 全部 TS 分段并拼接为单文件"""
        # 缓存复用: 已有完整产物时跳过重下(避免重复大文件下载长时间占用解析锁)
        mp4_file = PLUGIN_TEMP_DIR / f"platform_video_acfun{acid}.mp4"
        if mp4_file.exists() and mp4_file.stat().st_size > 0:
            logger.debug(f"A站视频缓存命中: {mp4_file.name}", "多平台解析")
            return mp4_file

        ts_urls = await self._parse_m3u8(m3u8_url)
        if not ts_urls:
            raise DownloadError("A站 m3u8 中未找到视频分段")

        video_file = PLUGIN_TEMP_DIR / f"platform_video_acfun{acid}.ts"
        max_size_mb = config_int("MULTI_PLATFORM_MAX_VIDEO_SIZE_MB", 100)
        max_size_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
        total = 0

        try:
            async with aiofiles.open(video_file, "wb") as f:
                async with new_client(timeout=60) as client:
                    for ts_url in ts_urls:
                        async with client.stream(
                            "GET", ts_url, headers=_HEADERS
                        ) as resp:
                            resp.raise_for_status()
                            async for chunk in resp.aiter_bytes():
                                total += len(chunk)
                                if max_size_bytes and total > max_size_bytes:
                                    raise MediaTooLargeError(
                                        f"A站视频超过 {max_size_mb}MB 上限, 已中断下载"
                                    )
                                await f.write(chunk)
        except MediaTooLargeError:
            video_file.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as e:
            video_file.unlink(missing_ok=True)
            raise DownloadError(
                f"A站视频分段下载失败: {type(e).__name__}: {e}",
                context={"m3u8_url": m3u8_url},
            ) from e

        logger.info(
            f"A站视频下载完成: ac{acid} "
            f"({len(ts_urls)} 段, {total / 1024 / 1024:.1f}MB)",
            "多平台解析",
        )

        # 有 ffmpeg 时 remux 为标准 mp4(TS 拼接文件部分播放器兼容性差)
        if await check_ffmpeg_available():
            mp4_file = video_file.with_suffix(".mp4")
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_file),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(mp4_file),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                if await proc.wait() == 0 and mp4_file.exists():
                    video_file.unlink(missing_ok=True)
                    return mp4_file
                # remux 失败: 清理半成品 mp4, 避免缓存命中损坏文件
                mp4_file.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(
                    f"A站视频 remux 失败, 使用原始拼接文件: {e}", "多平台解析"
                )
                mp4_file.unlink(missing_ok=True)
        return video_file

    @staticmethod
    async def _parse_m3u8(m3u8_url: str) -> list[str]:
        """解析 m3u8 播放列表中的全部分段地址(兼容绝对/相对路径)"""
        async with new_client() as client:
            resp = await client.get(m3u8_url, headers=_HEADERS)
            resp.raise_for_status()
            m3u8_text = resp.text

        prefix = m3u8_url.rsplit("/", 1)[0]
        ts_urls: list[str] = []
        for line in m3u8_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("http://") or line.startswith("https://"):
                ts_urls.append(line)
            else:
                ts_urls.append(f"{prefix}/{line}")
        return ts_urls
