"""微信视频号解析器

移植自 AstrBot 万能解析插件 core/parsers/shipinhao.py (纯服务端解析, 无需 MITM):
1. 分享短链 (weixin.qq.com/sph/xxx) 先 301 跟随拿到 finder-preview 长链,
   再 POST 腾讯元宝解析接口换取带 token + eid 的 playable_url(需登录态 Cookie);
2. 用 token + eid 调 finder-preview 的 get_feed_info 接口拿明文视频直链。

浏览器复制的长链(finder-preview ... &eid=...&token=...)自带凭证,
可直接走第 2 步, 无需元宝 Cookie。

元宝 Cookie 通过配置项 SHIPINHAO_YUANBAO_COOKIE 配置(登录 yuanbao.tencent.com
后复制 Cookie 字符串), 失效时会有明确提示。
"""

from random import choice
import re
import time
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from zhenxun.services.log import logger

from ..utils.exceptions import ResourceNotFoundError, UrlParseError
from .base import PlatformParser
from .http_utils import COMMON_HEADERS, get_redirect_url, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

# 腾讯元宝: 视频号分享链接解析接口(借其登录态签发合法 token)
_YUANBAO_PARSE_URL = "https://yuanbao.tencent.com/api/weixin/get_parse_result"
# 视频号网页预览: 带 token 后返回真实视频直链
_FEED_INFO_URL = (
    "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
)

_YUANBAO_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://yuanbao.tencent.com",
    "referer": "https://yuanbao.tencent.com/chat",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    "x-language": "zh-CN",
    "x-platform": "mac",
    "x-requested-with": "XMLHttpRequest",
    "x-source": "web",
}

_MEDIA_HEADERS = {"Referer": "https://channels.weixin.qq.com/", **COMMON_HEADERS}


def _get_yuanbao_cookie() -> str:
    from ..config import base_config

    return str(base_config.get("SHIPINHAO_YUANBAO_COOKIE", "") or "").strip()


class ShipinhaoParser(PlatformParser):
    PLATFORM_NAME = "shipinhao"
    DISPLAY_NAME = "微信视频号"
    PRIORITY = 82
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://weixin.qq.com/sph/AuCZlx1A3C (App 复制的短链)
        ("weixin.qq.com/sph", re.compile(r"weixin\.qq\.com/sph/[A-Za-z0-9]+")),
        # https://channels.weixin.qq.com/finder-preview/pages/sph?id=xxx (跳转/长链)
        (
            "finder-preview",
            re.compile(
                r"channels\.weixin\.qq\.com/finder-preview/pages/[A-Za-z0-9_]+"
                r"\?[A-Za-z0-9._=&%/+\-]+"
            ),
        ),
    ]

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        share_url = f"https://{matched.group(0)}"

        # 长链自带 token + eid 时直接走 get_feed_info, 无需元宝
        token, export_id = self._extract_token_eid(share_url)
        if not (token and export_id):
            token, export_id = await self._parse_share_url(share_url)

        feed = await self._get_feed_info(export_id, token)
        return self._build_result(feed, share_url)

    # ---------------- Step 1: 元宝换取 token + eid ----------------

    async def _resolve_short_url(self, url: str) -> str:
        """短链 301 跟随, 转为 finder-preview 长链以提高元宝解析成功率"""
        if "weixin.qq.com/sph" not in url:
            return url
        try:
            ios_wechat_ua = {
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                    "MicroMessenger/8.0.40"
                )
            }
            async with new_client(follow_redirects=False) as client:
                return await get_redirect_url(client, url, headers=ios_wechat_ua)
        except Exception as e:
            logger.debug(f"视频号短链解析失败, 使用原始链接: {e}", "多平台解析")
        return url

    async def _parse_share_url(self, share_url: str) -> tuple[str, str]:
        yuanbao_cookie = _get_yuanbao_cookie()
        if not yuanbao_cookie:
            raise ResourceNotFoundError(
                "微信视频号短链解析需要配置「腾讯元宝」Cookie"
                "(配置项 SHIPINHAO_YUANBAO_COOKIE); "
                "使用浏览器复制的带 token/eid 长链则无需配置"
            )

        target_url = await self._resolve_short_url(share_url)
        payload = {"type": "video_channel_url", "url": target_url, "scene": 1}
        headers = {**_YUANBAO_HEADERS, "cookie": yuanbao_cookie}

        async with new_client() as client:
            resp = await client.post(
                _YUANBAO_PARSE_URL, json=payload, headers=headers
            )
            if resp.status_code == 401:
                raise ResourceNotFoundError("元宝 Cookie 已失效，请重新登录并更新配置")
            resp.raise_for_status()
            data = resp.json()

        result = data.get("data") or {}
        playable_url = result.get("playable_url") or ""
        token, export_id = self._extract_token_eid(playable_url)
        # 兜底: playable_url 里没取到 eid 时, 用返回的 wx_export_id
        export_id = export_id or result.get("wx_export_id") or ""

        if not export_id:
            raise ResourceNotFoundError(
                "元宝未返回视频号 eid，可能是链接失效或非视频内容"
            )
        return token, export_id

    # ---------------- Step 2: get_feed_info 拿视频直链 ----------------

    async def _get_feed_info(self, export_id: str, token: str) -> dict[str, Any]:
        rid = f"{int(time.time()):x}-{self._rand_hex(8)}"
        api_url = (
            f"{_FEED_INFO_URL}?_rid={rid}"
            "&_pageUrl=https%3A%2F%2Fchannels.weixin.qq.com"
            "%2Ffinder-preview%2Fpages%2Ffeed"
        )
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed"
            f"?entry_card_type=48&comment_scene=39&appid=0&token={token}"
            f"&entry_scene=0&eid={export_id}"
        )
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://channels.weixin.qq.com",
            "referer": referer,
            "user-agent": _YUANBAO_HEADERS["user-agent"],
        }
        payload = {"baseReq": {"generalToken": token}, "exportId": export_id}

        async with new_client() as client:
            resp = await client.post(api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        if data.get("errCode") not in (0, None):
            raise UrlParseError(f"视频号预览接口返回错误: {data.get('errMsg')}")
        return data

    # ---------------- 组装解析结果 ----------------

    def _build_result(
        self, feed: dict[str, Any], share_url: str
    ) -> PlatformParseResult:
        data = feed.get("data") or {}
        feed_info: dict[str, Any] = data.get("feedInfo") or {}
        author_info: dict[str, Any] = data.get("authorInfo") or {}

        err = feed_info.get("errMsg") or {}
        if isinstance(err, dict) and err.get("type"):
            raise ResourceNotFoundError(
                str(err.get("title") or "该视频号内容无法解析")
            )

        video_url = self._pick_video_url(feed_info)
        if not video_url:
            raise ResourceNotFoundError(
                "未获取到视频直链（可能是图文动态、已删除，或 Cookie 权限不足）"
            )

        contents = [
            PlatformMedia(
                type=MediaType.VIDEO,
                url=video_url,
                cover_url=feed_info.get("coverUrl") or None,
                duration=self._pick_duration(feed_info),
                headers=_MEDIA_HEADERS,
            )
        ]

        stats = self._build_stats(feed_info)
        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(feed_info.get("description") or "").strip() or None,
            author_name=str(author_info.get("nickname") or "视频号用户"),
            author_avatar=(
                avatar
                if isinstance(avatar := author_info.get("headImgUrl"), str)
                else None
            ),
            timestamp=self._safe_int(feed_info.get("createtime")) or None,
            url=share_url,
            contents=contents,
            extra_text=stats,
        )

    # ---------------- 辅助 ----------------

    @staticmethod
    def _extract_token_eid(url: str) -> tuple[str, str]:
        """从 URL query 中提取 token 与 eid, 取不到返回空串"""
        if not url:
            return "", ""
        try:
            qs = parse_qs(urlparse(url).query)
        except ValueError:
            return "", ""
        token = (qs.get("token") or [""])[0]
        eid = (qs.get("eid") or qs.get("exportId") or [""])[0]
        return token, eid

    @staticmethod
    def _pick_video_url(feed_info: dict[str, Any]) -> str | None:
        for key in ("h264VideoInfo", "h265VideoInfo"):
            info = feed_info.get(key)
            if isinstance(info, dict) and info.get("videoUrl"):
                return info["videoUrl"]
        return feed_info.get("videoUrl") or None

    @staticmethod
    def _pick_duration(feed_info: dict[str, Any]) -> float:
        for key in ("h264VideoInfo", "h265VideoInfo"):
            info = feed_info.get(key)
            if isinstance(info, dict) and info.get("duration"):
                return float(ShipinhaoParser._safe_int(info.get("duration")) or 0)
        return float(ShipinhaoParser._safe_int(feed_info.get("videoDuration")) or 0)

    @staticmethod
    def _build_stats(feed_info: dict[str, Any]) -> str | None:
        pairs = (
            ("赞", feed_info.get("likeCountFmt")),
            ("爱心", feed_info.get("favCountFmt")),
            ("评论", feed_info.get("commentCountFmt")),
            ("转发", feed_info.get("forwardCountFmt")),
        )
        tokens = [f"{label} {value}" for label, value in pairs if value]
        return " · ".join(tokens) if tokens else None

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _rand_hex(length: int) -> str:
        chars = "0123456789abcdef"
        return "".join(choice(chars) for _ in range(length))
