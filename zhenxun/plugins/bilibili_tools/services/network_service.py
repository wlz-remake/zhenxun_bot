import urllib.parse
from pathlib import Path

import aiofiles
import httpx

from zhenxun.services.log import logger

from ..model import ArticleInfo, LiveInfo, UserInfo, VideoInfo
from ..utils.exceptions import (
    DownloadError,
    ShortUrlError,
    UnsupportedUrlError,
    UrlParseError,
)
from ..utils.headers import get_bilibili_headers, get_bilibili_video_stream_headers
from ..utils.url_parser import ResourceType, UrlParserRegistry


async def download_bilibili_file(url: str | list[str], file_path: Path) -> bool:
    """下载B站文件"""
    headers = get_bilibili_video_stream_headers()

    url_list = url if isinstance(url, list) else [url]

    logger.info(f"开始下载文件: {file_path.name}")

    try:
        success = False
        for try_url in url_list:
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    async with client.stream(
                        "GET", try_url, headers=headers
                    ) as response:
                        response.raise_for_status()
                        async with aiofiles.open(file_path, "wb") as f:
                            async for chunk in response.aiter_bytes():
                                await f.write(chunk)
                success = True
                logger.info(f"下载文件成功: {file_path.name} (URL: {try_url})")
                break
            except httpx.HTTPError as e:
                # 捕获 httpx 网络/状态码错误，尝试下一个备用URL（等价于原单URL失败回退）
                logger.warning(
                    f"下载文件 {file_path.name} 失败 (URL: {try_url}): "
                    f"{type(e).__name__}: {e}"
                )
                if try_url == url_list[-1]:
                    logger.error(f"下载文件 {file_path.name} 失败，所有URL均不可用")
    except Exception as e:
        # 仅捕获下载过程中未预期的异常（如 OSError 等底层错误）
        logger.error(
            f"下载文件 {file_path.name} 时发生未预期的错误: {type(e).__name__}: {e}"
        )
        raise DownloadError(
            f"下载文件 {file_path.name} 时发生未预期的错误: {type(e).__name__}",
            context={
                "url": url_list[0] if url_list else "",
                "file_path": str(file_path),
            },
            cause=e,
        ) from e

    # success 为 False 表示所有 URL 均下载失败
    if not success:
        raise DownloadError(
            f"下载文件 {file_path.name} 失败，所有URL均不可用",
            context={
                "url": url_list[0] if url_list else "",
                "file_path": str(file_path),
            },
        )

    return success


async def extract_dynamic_images(dynamic_id: str) -> list[str]:
    """从动态中提取图片链接（不包括被转发的动态）"""

    from ..config import get_credential

    dynamic_images = []
    try:
        credential = get_credential()

        headers = get_bilibili_headers(referer="https://t.bilibili.com/")
        api_url = (
            f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id}"
        )

        cookies = credential.get_cookies() if credential else None

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(api_url, headers=headers, cookies=cookies)
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            logger.warning(f"获取动态详情失败: {data.get('message')}")
            return dynamic_images

        item = data.get("data", {}).get("item", {})
        if not item:
            return dynamic_images

        modules = item.get("modules") or {}
        module_dynamic = modules.get("module_dynamic") or {}
        major = module_dynamic.get("major") or {}

        opus = major.get("opus")
        if isinstance(opus, dict):
            for pic in opus.get("pics") or []:
                url = pic.get("url") if isinstance(pic, dict) else None
                if url:
                    dynamic_images.append(url)

        draw = major.get("draw")
        if isinstance(draw, dict):
            for draw_item in draw.get("items") or []:
                src = draw_item.get("src") if isinstance(draw_item, dict) else None
                if src:
                    dynamic_images.append(src)

        logger.debug(f"从动态 {dynamic_id} 中提取到 {len(dynamic_images)} 张图片")

    except Exception as e:
        logger.warning(f"解析动态图片时出错: {e}")

    return dynamic_images


class ParserService:
    """URL解析服务"""

    @staticmethod
    async def resolve_short_url(url: str) -> str:
        """解析短链接，返回原始URL"""
        original_url = url.strip()

        if "b23.tv" in original_url:
            logger.debug(f"检测到b23.tv短链接: {original_url}", "B站解析")
            try:
                if not original_url.startswith(("http://", "https://")):
                    original_url = f"https://{original_url}"

                async with httpx.AsyncClient(follow_redirects=True) as client:
                    response = await client.get(
                        original_url, timeout=10, headers=get_bilibili_headers()
                    )
                    response.raise_for_status()
                    resolved_url = str(response.url)

                parsed_url_obj = urllib.parse.urlparse(resolved_url)
                query_params = urllib.parse.parse_qs(parsed_url_obj.query)
                filtered_params = {k: v for k, v in query_params.items() if k in ["p"]}
                new_query = (
                    urllib.parse.urlencode(filtered_params, doseq=True)
                    if filtered_params
                    else ""
                )

                clean_url = urllib.parse.urlunparse(
                    (
                        parsed_url_obj.scheme,
                        parsed_url_obj.netloc,
                        parsed_url_obj.path,
                        parsed_url_obj.params,
                        new_query,
                        "",
                    )
                )

                logger.debug(f"短链接解析结果: {clean_url}", "B站解析")
                return clean_url
            except (ShortUrlError, httpx.HTTPError) as e:
                logger.warning(
                    f"短链接解析失败 {original_url}: {e}，将使用原始链接继续尝试解析",
                    "B站解析",
                )

        return original_url

    @staticmethod
    async def fetch_resource_info(
        resource_type: ResourceType, resource_id: str, parsed_url: str
    ) -> VideoInfo | LiveInfo | ArticleInfo | UserInfo:
        """根据资源类型和ID获取详细信息"""
        from .api_service import BilibiliApiService
        from .utility_service import ScreenshotService

        logger.debug(
            f"获取资源信息: 类型={resource_type.name}, ID={resource_id}",
            "B站解析",
        )

        if resource_type == ResourceType.VIDEO:
            return await BilibiliApiService.get_video_info(
                vid=resource_id, parsed_url=parsed_url
            )
        elif resource_type == ResourceType.LIVE:
            return await BilibiliApiService.get_live_info(
                room_id=int(resource_id), parsed_url=parsed_url
            )
        elif resource_type == ResourceType.ARTICLE:
            return await BilibiliApiService.get_article_info(
                cv_id=resource_id, parsed_url=parsed_url
            )
        elif resource_type == ResourceType.OPUS:
            screenshot_bytes = await ScreenshotService.get_opus_screenshot(
                opus_id=resource_id, url=parsed_url
            )
            # 提取动态中的图片
            dynamic_images = await extract_dynamic_images(resource_id)
            return ArticleInfo(
                id=resource_id,
                type="opus",
                url=parsed_url,
                screenshot_bytes=screenshot_bytes,
                images=dynamic_images if dynamic_images else None,
            )
        elif resource_type == ResourceType.USER:
            return await BilibiliApiService.get_user_info(
                uid=int(resource_id), parsed_url=parsed_url
            )
        else:
            raise UnsupportedUrlError(f"不支持的资源类型: {resource_type}")

    @classmethod
    async def parse(cls, url: str) -> VideoInfo | LiveInfo | ArticleInfo | UserInfo:
        """解析Bilibili URL，返回相应的信息模型"""
        original_url = url.strip()
        logger.debug(f"开始解析URL: {original_url}", "B站解析")

        final_url = await cls.resolve_short_url(original_url)

        try:
            resource_type, resource_id = UrlParserRegistry.parse(final_url)
            logger.debug(
                f"从URL提取资源信息: 类型={resource_type.name}, ID={resource_id}",
                "B站解析",
            )
        except (UrlParseError, UnsupportedUrlError):
            if final_url != original_url:
                logger.debug(
                    f"最终URL解析失败，尝试解析原始URL: {original_url}", "B站解析"
                )
                try:
                    resource_type, resource_id = UrlParserRegistry.parse(original_url)
                    logger.debug(
                        f"从原始URL提取资源信息: 类型={resource_type.name}, ID={resource_id}",
                        "B站解析",
                    )
                except (UrlParseError, UnsupportedUrlError) as e:
                    logger.warning(
                        f"无法从URL确定资源类型或ID: {original_url} (解析为: {final_url})",
                        "B站解析",
                    )
                    raise UrlParseError(
                        f"无法从URL确定资源类型或ID: {original_url} (解析为: {final_url})",
                        cause=e,
                        context={"original_url": original_url, "final_url": final_url},
                    )
            else:
                logger.warning(f"无法解析URL: {original_url}", "B站解析")
                raise

        if resource_type == ResourceType.SHORT_URL:
            resolved_url = await cls.resolve_short_url(original_url)
            if resolved_url == original_url:
                raise ShortUrlError(
                    f"无法解析短链接: {original_url}", context={"url": original_url}
                )

            logger.debug(f"递归解析短链接解析结果: {resolved_url}", "B站解析")
            return await cls.parse(resolved_url)

        parsed_url = final_url if final_url != original_url else original_url

        if resource_type == ResourceType.VIDEO and (
            parsed_url.startswith("av")
            or parsed_url.startswith("AV")
            or parsed_url.startswith("BV")
            or parsed_url.startswith("bv")
        ):
            if parsed_url.upper().startswith("BV"):
                full_url = f"https://www.bilibili.com/video/{parsed_url}"
                logger.debug(f"为纯BV号生成完整URL: {full_url}", "B站解析")
                parsed_url = full_url
            elif parsed_url.lower().startswith("av"):
                full_url = f"https://www.bilibili.com/video/{parsed_url}"
                logger.debug(f"为纯AV号生成完整URL: {full_url}", "B站解析")
                parsed_url = full_url

        return await cls.fetch_resource_info(
            resource_type=resource_type, resource_id=resource_id, parsed_url=parsed_url
        )
