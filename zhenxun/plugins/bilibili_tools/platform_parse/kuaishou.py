"""快手解析器

移植自 AstrBot 万能解析插件 core/parsers/kuaishou.py:
短链/分享链 302 → 页面 window.INIT_STATE 数据 → 视频/图集直链。
"""

import json
from random import choice
import re
from typing import Any, ClassVar

from ..utils.exceptions import ResourceNotFoundError, UrlParseError
from .base import PlatformParser
from .http_utils import IOS_HEADERS, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_STATE_PATTERN = re.compile(r"window\.INIT_STATE\s*=\s*(.*?)</script>", re.DOTALL)

_MEDIA_HEADERS = {"Referer": "https://v.kuaishou.com/", **IOS_HEADERS}


def _pick_url(cdn_items: Any) -> str | None:
    """从 [{cdn, url}] 列表中随机取一个可用直链"""
    if not isinstance(cdn_items, list):
        return None
    valid = [item for item in cdn_items if isinstance(item, dict) and item.get("url")]
    if not valid:
        return None
    return choice(valid).get("url")


def _get_any(data: dict, *names: str) -> Any:
    """容错取值: 兼容接口字段大小写差异"""
    if not isinstance(data, dict):
        return None
    for name in names:
        if name in data:
            return data[name]
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


class KuaishouParser(PlatformParser):
    PLATFORM_NAME = "kuaishou"
    DISPLAY_NAME = "快手"
    PRIORITY = 40
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://v.kuaishou.com/2yAnzeZ
        ("v.kuaishou", re.compile(r"v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")),
        # https://www.kuaishou.com/short-video/3xhjgcmir24m4nm
        ("kuaishou", re.compile(r"(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+")),
        # https://v.m.chenzhongtech.com/fw/photo/3xburnkmj3auazc
        (
            "chenzhongtech",
            re.compile(r"(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+"),
        ),
    ]

    async def parse(self, url: str, matched: re.Match[str]) -> PlatformParseResult:
        raw_url = f"https://{matched.group(0)}"
        # 短链会 302 跳转, 带 share 参数的卡片链接则直接 200 返回 SSR 页面,
        # 统一用自动跟随重定向的单次请求兼容两种形态
        async with new_client() as client:
            resp = await client.get(raw_url, headers=_MEDIA_HEADERS)
            resp.raise_for_status()
            html = resp.text
            real_url = str(resp.url)

        # /fw/long-video/ 返回结果不一样, 统一替换为 /fw/photo/ 请求
        if "/fw/long-video/" in real_url:
            real_url = real_url.replace("/fw/long-video/", "/fw/photo/")
            async with new_client() as client:
                resp = await client.get(real_url, headers=_MEDIA_HEADERS)
                resp.raise_for_status()
                html = resp.text

        state_match = _STATE_PATTERN.search(html)
        if not state_match:
            raise ResourceNotFoundError("快手页面数据解析失败(可能已被删除)")
        try:
            init_state = json.loads(state_match.group(1).strip())
        except json.JSONDecodeError as e:
            raise UrlParseError("快手 INIT_STATE JSON 解析失败") from e

        photo = next(
            (
                value.get("photo")
                for value in init_state.values()
                if isinstance(value, dict) and value.get("photo")
            ),
            None,
        )
        if not photo:
            raise ResourceNotFoundError("快手内容中未找到视频或图集数据")

        contents: list[PlatformMedia] = []
        # 视频
        if video_url := _pick_url(_get_any(photo, "mainMvUrls")):
            contents.append(
                PlatformMedia(
                    type=MediaType.VIDEO,
                    url=video_url,
                    cover_url=_pick_url(_get_any(photo, "coverUrls")),
                    duration=int(_get_any(photo, "duration") or 0) / 1000,
                    headers=_MEDIA_HEADERS,
                )
            )
        # 图集
        ext_params = _get_any(photo, "ext_params", "extParams") or {}
        atlas = _get_any(ext_params, "atlas") or {}
        img_routes = _get_any(atlas, "list") or []
        cdn_list = _get_any(atlas, "cdnList") or []
        valid_cdns = [
            c.get("cdn") for c in cdn_list if isinstance(c, dict) and c.get("cdn")
        ]
        if img_routes and valid_cdns:
            cdn = choice(valid_cdns)
            contents.extend(
                PlatformMedia(
                    type=MediaType.IMAGE,
                    url=f"https://{cdn}/{route}",
                    headers=_MEDIA_HEADERS,
                )
                for route in img_routes
            )

        if not contents:
            raise ResourceNotFoundError("快手内容中未找到可用的视频或图片")

        user_name = str(_get_any(photo, "userName") or "未知用户").replace(
            "\u3164", ""
        ).strip() or "未知用户"

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(photo.get("caption") or "").strip() or None,
            author_name=user_name,
            author_avatar=_get_any(photo, "headUrl"),
            timestamp=int(_get_any(photo, "timestamp") or 0) // 1000 or None,
            url=real_url,
            contents=contents,
        )
