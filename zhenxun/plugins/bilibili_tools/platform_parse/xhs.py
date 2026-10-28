"""小红书解析器

移植自 AstrBot 万能解析插件 core/parsers/xhs.py:
短链 302 → explore/discovery 页面 window.__INITIAL_STATE__ 数据。
视频优先取 h265 流(无水印), 图集取 imageList。
"""

import json
import re
from typing import Any, ClassVar

from zhenxun.services.log import logger

from ..utils.exceptions import ResourceNotFoundError, UrlParseError
from .base import MAX_REDIRECT_HOPS, PlatformParser
from .http_utils import COMMON_HEADERS, IOS_HEADERS, get_redirect_url, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_STATE_PATTERN = re.compile(r"window\.__INITIAL_STATE__=(.*?)</script>", re.DOTALL)

_MEDIA_HEADERS = {"Referer": "https://www.xiaohongshu.com/", **COMMON_HEADERS}


def _extract_initial_state(html: str) -> dict[str, Any]:
    matched = _STATE_PATTERN.search(html)
    if not matched:
        raise ResourceNotFoundError("小红书分享链接失效或内容已删除")
    # 页面 JSON 中存在裸 undefined, 需替换为 null 才能被 json 解析
    json_str = matched.group(1).replace("undefined", "null")
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise UrlParseError("小红书页面数据解析失败") from e


def _video_stream_url(note_data: dict[str, Any]) -> str | None:
    """优先 h265(无水印), 回退 h264/av1"""
    streams = (
        ((note_data.get("video") or {}).get("media") or {}).get("stream") or {}
    )
    for codec in ("h265", "h264", "av1", "h266"):
        codec_list = streams.get(codec) or []
        if codec_list and isinstance(codec_list, list):
            master_url = codec_list[0].get("masterUrl")
            if master_url:
                return master_url
    return None


class XhsParser(PlatformParser):
    PLATFORM_NAME = "xhs"
    DISPLAY_NAME = "小红书"
    PRIORITY = 30
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://xhslink.com/xxxx (短链)
        ("xhslink", re.compile(r"xhslink\.(?:com|cn)/[A-Za-z0-9._?%&+=/#@-]+")),
        # https://www.xiaohongshu.com/explore/{id}?{query 含 xsec_token}
        # https://www.xiaohongshu.com/discovery/item/{id}?{query}
        (
            "xiaohongshu.com",
            re.compile(
                r"(?:explore|discovery/item)/"
                r"(?P<query>(?P<xhs_id>[0-9a-zA-Z]+)\?[A-Za-z0-9._%&+=/#@-]+)"
            ),
        ),
    ]

    def __init__(self):
        self.page_headers = {
            **COMMON_HEADERS,
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
        }
        self.ios_headers = {
            **IOS_HEADERS,
            "origin": "https://www.xiaohongshu.com",
            "x-requested-with": "XMLHttpRequest",
        }

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        if _hop > MAX_REDIRECT_HOPS:
            raise UrlParseError(f"短链重定向次数超过上限({MAX_REDIRECT_HOPS}): {url}")
        # 短链: 302 后重新分发
        if "xhslink" in matched.group(0):
            short_url = f"https://{matched.group(0)}"
            async with new_client(follow_redirects=False) as client:
                redirect_url = await get_redirect_url(
                    client, short_url, headers=self.ios_headers
                )
            if rematch := self.match(redirect_url):
                return await self.parse(rematch.group(0), rematch, _hop=_hop + 1)
            raise UrlParseError(f"小红书短链重定向后无法识别: {redirect_url}")

        query = matched.group("query")
        xhs_id = matched.group("xhs_id")
        try:
            return await self._parse_explore(
                f"https://www.xiaohongshu.com/explore/{query}", xhs_id
            )
        except (ResourceNotFoundError, UrlParseError) as e:
            logger.warning(
                f"小红书 explore 页解析失败: {e}, 回退 discovery 页", "多平台解析"
            )
            return await self._parse_discovery(
                f"https://www.xiaohongshu.com/discovery/item/{query}"
            )

    async def _parse_explore(self, url: str, xhs_id: str) -> PlatformParseResult:
        async with new_client() as client:
            resp = await client.get(url, headers=self.page_headers)
            resp.raise_for_status()
            html = resp.text

        state = _extract_initial_state(html)
        note_data = (
            state.get("note", {})
            .get("noteDetailMap", {})
            .get(xhs_id, {})
            .get("note", {})
        )
        if not note_data:
            raise ResourceNotFoundError("小红书笔记数据为空(可能已被删除)")

        user = note_data.get("user") or {}
        image_urls = [
            item.get("urlDefault")
            for item in note_data.get("imageList") or []
            if item.get("urlDefault")
        ]

        contents: list[PlatformMedia] = []
        if note_data.get("type") == "video":
            if video_url := _video_stream_url(note_data):
                contents.append(
                    PlatformMedia(
                        type=MediaType.VIDEO,
                        url=video_url,
                        cover_url=image_urls[0] if image_urls else None,
                        headers=_MEDIA_HEADERS,
                    )
                )
        if not contents:
            contents.extend(
                PlatformMedia(type=MediaType.IMAGE, url=u, headers=_MEDIA_HEADERS)
                for u in image_urls
            )
        if not contents:
            raise ResourceNotFoundError("小红书笔记中未找到可用的视频或图片")

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(note_data.get("title") or "").strip() or None,
            text=(note_data.get("desc") or "").strip() or None,
            author_name=user.get("nickname") or "未知用户",
            author_avatar=user.get("avatar"),
            url=f"https://www.xiaohongshu.com/explore/{xhs_id}",
            contents=contents,
        )

    async def _parse_discovery(self, url: str) -> PlatformParseResult:
        """discovery 页回退路径(分享链接无 xsec_token 时 explore 页可能拿不到数据)"""
        async with new_client() as client:
            resp = await client.get(url, headers=self.ios_headers)
            resp.raise_for_status()
            html = resp.text

        state = _extract_initial_state(html)
        note_root = state.get("noteData") or {}
        preload = note_root.get("normalNotePreloadData") or {}
        note_data = (note_root.get("data") or {}).get("noteData") or {}
        if not note_data and not preload:
            raise ResourceNotFoundError("小红书笔记数据为空(可能已被删除)")

        user = note_data.get("user") or {}
        image_urls = [
            item.get("url")
            for item in note_data.get("imageList") or []
            if item.get("url")
        ]
        if not image_urls:
            # 无水印图仅存在于 preload 数据
            image_urls = [
                item.get("urlSizeLarge") or item.get("url")
                for item in preload.get("imagesList") or []
                if item.get("urlSizeLarge") or item.get("url")
            ]

        contents: list[PlatformMedia] = []
        if note_data.get("type") == "video":
            if video_url := _video_stream_url(note_data):
                contents.append(
                    PlatformMedia(
                        type=MediaType.VIDEO,
                        url=video_url,
                        cover_url=image_urls[0] if image_urls else None,
                        headers=_MEDIA_HEADERS,
                    )
                )
        if not contents:
            contents.extend(
                PlatformMedia(type=MediaType.IMAGE, url=u, headers=_MEDIA_HEADERS)
                for u in image_urls
            )

        timestamp = int(note_data.get("time") or 0) // 1000 or None
        if not contents:
            # 纯文字笔记允许无媒体, 但至少保留标题/正文
            if not (note_data.get("desc") or preload.get("desc")):
                raise ResourceNotFoundError("小红书笔记中未找到可用的内容")
        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(
                (note_data.get("title") or preload.get("title") or "").strip() or None
            ),
            text=(
                (note_data.get("desc") or preload.get("desc") or "").strip() or None
            ),
            author_name=user.get("nickName") or "未知用户",
            author_avatar=user.get("avatar"),
            timestamp=timestamp,
            url=url,
            contents=contents,
        )
