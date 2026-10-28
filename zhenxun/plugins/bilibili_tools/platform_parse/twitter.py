"""Twitter/X 解析器

移植自 AstrBot 万能解析插件 core/parsers/twitter.py:
借助第三方解析站 xdown.app 的公开接口获取推文视频/图片直链。
"""

import re
from typing import Any, ClassVar

from bs4 import BeautifulSoup, Tag

from zhenxun.services.log import logger

from ..utils.exceptions import ResourceNotFoundError
from .base import PlatformParser
from .http_utils import COMMON_HEADERS, get_json, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_XDOWN_URL = "https://xdown.app/api/ajaxSearch"
_HEADERS = {
    **COMMON_HEADERS,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://xdown.app",
    "Referer": "https://xdown.app/",
}


class TwitterParser(PlatformParser):
    PLATFORM_NAME = "twitter"
    DISPLAY_NAME = "推特"
    PRIORITY = 70
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://twitter.com/user/status/123 / https://x.com/user/status/123
        # 负向后行断言防止 x.com 在 fox.com 等域名内被子串误匹配
        (
            "twitter.com",
            re.compile(
                r"(?<![A-Za-z0-9.-])(?:(?:www|mobile)\.)?twitter\.com/"
                r"(?:[A-Za-z0-9_]+/)*status/\d+"
            ),
        ),
        (
            "x.com",
            re.compile(
                r"(?<![A-Za-z0-9.-])(?:www\.)?x\.com/(?:[A-Za-z0-9_]+/)*status/\d+"
            ),
        ),
    ]

    async def parse(self, url: str, matched: re.Match[str]) -> PlatformParseResult:
        tweet_url = f"https://{matched.group(0)}"
        async with new_client() as client:
            resp_data: dict[str, Any] = await get_json(
                client,
                _XDOWN_URL,
                headers=_HEADERS,
                method="POST",
                data={"q": tweet_url, "lang": "zh-cn"},
            )

        if resp_data.get("status") != "ok":
            raise ResourceNotFoundError(
                f"推特解析失败(xdown 返回: {resp_data.get('status')})"
            )

        html_content = resp_data.get("data")
        if not html_content:
            raise ResourceNotFoundError("推特解析失败, 第三方接口返回数据为空")

        return self._parse_html(str(html_content), tweet_url)

    @staticmethod
    def _parse_html(html_content: str, tweet_url: str) -> PlatformParseResult:
        soup = BeautifulSoup(html_content, "html.parser")

        cover_url = None
        if thumb_tag := soup.find("img"):
            if isinstance(thumb_tag, Tag) and thumb_tag.get("src"):
                cover_url = str(thumb_tag.get("src"))

        video_url = None
        image_urls: list[str] = []
        gif_urls: list[str] = []
        for tag in soup.find_all("a", class_=["tw-button-dl", "abutton"]):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if not href:
                continue
            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text and not video_url:
                video_url = href
            elif "下载图片" in text:
                image_urls.append(href)
            elif "下载 gif" in text:
                gif_urls.append(href)

        title = None
        if title_tag := soup.find("h3"):
            title = title_tag.get_text(strip=True)

        contents: list[PlatformMedia] = []
        if video_url:
            contents.append(
                PlatformMedia(type=MediaType.VIDEO, url=video_url, cover_url=cover_url)
            )
        contents.extend(
            PlatformMedia(type=MediaType.IMAGE, url=u) for u in image_urls
        )
        # gif 以视频消息发送(与参考插件 DynamicContent 行为一致)
        contents.extend(
            PlatformMedia(type=MediaType.VIDEO, url=u, cover_url=cover_url)
            for u in gif_urls
        )
        if not contents:
            logger.debug(f"推特内容无可用媒体: {tweet_url}", "多平台解析")

        return PlatformParseResult(
            platform=TwitterParser.PLATFORM_NAME,
            platform_display=TwitterParser.DISPLAY_NAME,
            title=title,
            author_name="推文",
            url=tweet_url,
            contents=contents,
        )
