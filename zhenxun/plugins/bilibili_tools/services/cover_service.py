import httpx
from nonebot_plugin_alconna import Image, UniMessage, UniMsg

from zhenxun.services.log import logger

from ..model import VideoInfo
from ..utils.exceptions import BilibiliBaseException
from ..utils.headers import get_bilibili_headers
from .network_service import ParserService


class CoverService:
    """获取封面的服务"""

    @staticmethod
    def _extract_cover_url(
        content_info: VideoInfo,
    ) -> str | None:
        """从内容信息中提取封面URL"""
        cover_url = getattr(content_info, "pic", None) or getattr(
            content_info, "cover", None
        )
        if cover_url and "@" in cover_url:
            return cover_url.split("@")[0]
        return cover_url

    @staticmethod
    async def get_cover_message(url: str) -> UniMsg:
        """
        根据URL获取封面，并构建一个包含图片和信息的UniMsg。
        如果失败，则抛出 BilibiliBaseException。
        """
        logger.info(f"开始获取封面: {url}")
        try:
            parsed_content = await ParserService.parse(url)

            if not isinstance(parsed_content, VideoInfo):
                raise BilibiliBaseException(
                    f"该链接内容类型 ({type(parsed_content).__name__}) 不支持获取封面。"
                )

            cover_url = CoverService._extract_cover_url(parsed_content)
            if not cover_url:
                raise BilibiliBaseException("该内容没有可用的封面图片。")

            logger.info(f"获取到封面URL: {cover_url}")

            headers = get_bilibili_headers()
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(cover_url, headers=headers)
                response.raise_for_status()
                image_data = response.content

            if not image_data:
                raise BilibiliBaseException("下载封面图片失败。")

            title = f"《{parsed_content.title}》的封面"
            image_segment = Image(
                raw=image_data,
                name=f"{getattr(parsed_content, 'bvid', None) or 'cover'}.jpg",
            )
            text_segment = f"\n{title}\n原始链接: {cover_url}"
            return UniMessage([image_segment, text_segment])

        except BilibiliBaseException:
            raise
        except Exception as e:
            logger.error(f"获取封面时发生未知错误: {e}", e=e)
            raise BilibiliBaseException("获取封面时发生未知错误，请检查日志。") from e
