"""抖音解析器

移植自 AstrBot 万能解析插件 core/parsers/douyin (其方案无需 a_bogus 签名):
短链 302 → iesdouyin 分享页 SSR 数据 (window._ROUTER_DATA) → 图集/视频直链。
ttwid 为匿名注册的凭证, 模块级缓存并持久化, 失效时自动重新注册。
"""

import json
import re
from typing import Any, ClassVar

import httpx

from zhenxun.services.log import logger

from ..config import PLUGIN_CACHE_DIR
from ..utils.exceptions import DownloadError, ResourceNotFoundError, UrlParseError
from .base import MAX_REDIRECT_HOPS, PlatformParser
from .http_utils import ANDROID_HEADERS, IOS_HEADERS, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_TTWID_FILE = PLUGIN_CACHE_DIR / "douyin_ttwid.txt"
_TTWID_PATTERN = re.compile(r"ttwid=([^;]+)")
_ROUTER_DATA_PATTERN = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL
)

TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
TTWID_REGISTER_PAYLOAD = {
    "region": "cn",
    "aid": 1768,
    "needFid": False,
    "service": "www.iesdouyin.com",
    "union": True,
    "fid": "",
}
PLAY_RATIOS = ("1080p", "720p", "540p", "360p")

_ttwid: str | None = None


def _load_ttwid() -> str | None:
    global _ttwid
    if _ttwid:
        return _ttwid
    try:
        if _TTWID_FILE.exists():
            _ttwid = _TTWID_FILE.read_text(encoding="utf-8").strip() or None
    except Exception as e:
        logger.debug(f"读取抖音 ttwid 缓存失败: {e}", "多平台解析")
    return _ttwid


def _save_ttwid(value: str):
    global _ttwid
    _ttwid = value
    try:
        _TTWID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TTWID_FILE.write_text(value, encoding="utf-8")
    except Exception as e:
        logger.debug(f"保存抖音 ttwid 缓存失败: {e}", "多平台解析")


def _extract_ttwid(set_cookie_list: list[str]) -> str | None:
    for cookie in set_cookie_list:
        if match := _TTWID_PATTERN.search(cookie):
            return match.group(1)
    return None


class DouyinParser(PlatformParser):
    PLATFORM_NAME = "douyin"
    DISPLAY_NAME = "抖音"
    PRIORITY = 10
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # 短链: https://v.douyin.com/xxxx / https://jx.douyin.com/xxxx
        ("v.douyin", re.compile(r"v\.douyin\.com/[a-zA-Z0-9_\-]+")),
        ("jx.douyin", re.compile(r"jx\.douyin\.com/[a-zA-Z0-9_\-]+")),
        # https://www.douyin.com/video/7521023890996514083
        # https://www.douyin.com/note/7469411074119322899
        (
            "douyin.com",
            re.compile(r"douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)"),
        ),
        # https://www.iesdouyin.com/share/video/xxxx
        (
            "iesdouyin",
            re.compile(r"iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)"),
        ),
        (
            "m.douyin",
            re.compile(r"m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)"),
        ),
        (
            "jingxuan.douyin",
            re.compile(
                r"jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)"
            ),
        ),
        # 不移植参考插件的 "裸 18-20 位数字ID" 模式:
        # 群聊环境中极易误命中普通长数字, 收益低误报高
    ]

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        if _hop > MAX_REDIRECT_HOPS:
            raise UrlParseError(f"短链重定向次数超过上限({MAX_REDIRECT_HOPS}): {url}")
        # 短链: 先 302 拿真实链接(顺带收集 ttwid), 再重新分发
        if "v.douyin.com" in url or "jx.douyin.com" in url:
            short_url = f"https://{matched.group(0)}"
            async with new_client(follow_redirects=False) as client:
                resp = await client.get(short_url, headers=IOS_HEADERS)
                if resp.status_code >= 400:
                    raise UrlParseError(
                        f"抖音短链请求失败(HTTP {resp.status_code}): {short_url}"
                    )
                redirect_url = resp.headers.get("Location") or short_url
                if ttwid := self._collect_ttwid_from_set_cookies(resp.headers):
                    _save_ttwid(ttwid)
            if redirect_url == short_url:
                raise UrlParseError(f"抖音短链重定向失败: {short_url}")
            logger.debug(f"抖音短链重定向: {redirect_url}", "多平台解析")
            if rematch := self.match(redirect_url):
                return await self.parse(rematch.group(0), rematch, _hop=_hop + 1)
            raise UrlParseError(f"抖音短链重定向后无法识别: {redirect_url}")

        groups = matched.groupdict()
        resource_type = groups.get("ty") or "video"
        vid = groups.get("vid")
        if not vid:
            raise UrlParseError(f"无法从抖音链接提取视频ID: {url}")

        if resource_type == "slides":
            return await self._parse_slides(vid)

        await self._ensure_ttwid()
        share_url = f"https://www.iesdouyin.com/share/{resource_type}/{vid}/"
        try:
            return await self._parse_video(share_url)
        except ResourceNotFoundError:
            raise
        except (DownloadError, UrlParseError) as e:
            logger.warning(
                f"抖音分享页解析失败({share_url}): {e}, 尝试回退 slides 接口",
                "多平台解析",
            )
            # 部分 note/图集内容在 share 页取不到数据, 回退幻灯片接口
            return await self._parse_slides(vid)

    @staticmethod
    def _collect_ttwid_from_set_cookies(headers: httpx.Headers) -> str | None:
        return _extract_ttwid(headers.get_list("set-cookie"))

    async def _ensure_ttwid(self) -> str:
        """确保存在匿名 ttwid 凭证(无账号依赖, 失效自动重注册)"""
        if _load_ttwid():
            return _ttwid  # type: ignore[return-value]

        headers = {
            **IOS_HEADERS,
            "Content-Type": "application/json",
            "Referer": "https://www.iesdouyin.com/",
        }
        try:
            async with new_client(follow_redirects=False) as client:
                resp = await client.post(
                    TTWID_REGISTER_URL, json=TTWID_REGISTER_PAYLOAD, headers=headers
                )
                resp.raise_for_status()
                if ttwid := self._collect_ttwid_from_set_cookies(resp.headers):
                    _save_ttwid(ttwid)
                    logger.info("抖音匿名 ttwid 注册成功", "多平台解析")
                    return ttwid
        except httpx.HTTPError as e:
            logger.warning(f"抖音 ttwid 注册请求失败: {e}", "多平台解析")
        raise UrlParseError("抖音 ttwid 注册失败, 暂无法解析抖音链接")

    def _share_headers(self, referer: str, with_cookie: bool = False) -> dict[str, str]:
        headers = {**IOS_HEADERS, "Referer": referer}
        if with_cookie and _load_ttwid():
            headers["Cookie"] = f"ttwid={_ttwid}"
        return headers

    def _media_headers(self, referer: str) -> dict[str, str]:
        """媒体下载/探测请求头(不带 cookie, 带防盗链 Referer)"""
        return {**IOS_HEADERS, "Referer": referer}

    async def _parse_video(self, share_url: str) -> PlatformParseResult:
        """解析 iesdouyin 分享页 SSR 数据"""
        async with new_client(follow_redirects=False) as client:
            resp = await client.get(
                share_url, headers=self._share_headers(share_url, with_cookie=True)
            )
            if resp.status_code in (403, 404):
                raise ResourceNotFoundError(
                    f"抖音内容不存在或需要验证(HTTP {resp.status_code})"
                )
            resp.raise_for_status()
            html = resp.text
            if ttwid := self._collect_ttwid_from_set_cookies(resp.headers):
                _save_ttwid(ttwid)

        matched = _ROUTER_DATA_PATTERN.search(html)
        if not matched:
            raise UrlParseError("未在抖音分享页中找到 _ROUTER_DATA 数据")

        try:
            router_data = json.loads(matched.group(1).strip())
        except json.JSONDecodeError as e:
            raise UrlParseError("抖音 _ROUTER_DATA JSON 解析失败") from e

        loader_data = router_data.get("loaderData", {})
        page = loader_data.get("video_(id)/page") or loader_data.get("note_(id)/page")
        item_list = (
            page.get("videoInfoRes", {}).get("item_list", []) if page else []
        )
        if not item_list:
            raise ResourceNotFoundError("抖音分享页中未找到内容数据(可能已删除)")

        item: dict[str, Any] = item_list[0]

        author = item.get("author", {}) or {}
        author_name = author.get("nickname") or "未知用户"
        avatar_url = self._first_url(
            (author.get("avatar_thumb") or {}).get("url_list")
        ) or self._first_url((author.get("avatar_medium") or {}).get("url_list"))

        contents: list[PlatformMedia] = []
        # 图集内容
        for image in item.get("images") or []:
            if img_url := self._first_url(image.get("url_list")):
                contents.append(
                    PlatformMedia(
                        type=MediaType.IMAGE, url=img_url, headers=IOS_HEADERS
                    )
                )

        # 视频内容(无图集时)
        if not contents:
            video_info = item.get("video") or {}
            video_url = await self._resolve_video_url(video_info, share_url)
            if video_url:
                contents.append(
                    PlatformMedia(
                        type=MediaType.VIDEO,
                        url=video_url,
                        cover_url=self._first_url(
                            (video_info.get("cover") or {}).get("url_list")
                        ),
                        duration=(video_info.get("duration") or 0) / 1000,
                        headers=self._media_headers(share_url),
                    )
                )

        if not contents:
            raise ResourceNotFoundError("抖音内容中未找到可用的视频或图片")

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=self._clean_desc(item.get("desc", "")),
            author_name=author_name,
            author_avatar=avatar_url,
            timestamp=int(item.get("create_time") or 0) or None,
            url=share_url,
            contents=contents,
        )

    async def _resolve_video_url(
        self, video_info: dict[str, Any], referer: str
    ) -> str | None:
        """优先通过 play_token 探测无水印直链, 失败回退 play_addr"""
        play_addr = video_info.get("play_addr") or {}
        play_token = play_addr.get("uri")
        if not play_token:
            for url in play_addr.get("url_list") or []:
                if match := re.search(r"video_id=([A-Za-z0-9]+)", url):
                    play_token = match.group(1)
                    break

        if play_token:
            if probed_url := await self._probe_play_url(play_token, referer):
                return probed_url

        for url in play_addr.get("url_list") or []:
            # playwm 为带水印端点, 替换为 play 即无水印
            return url.replace("playwm", "play")
        return None

    async def _probe_play_url(self, video_id: str, referer: str) -> str | None:
        """对各级清晰度 play 端点做 Range 探测, 返回体积最大的可用直链"""
        probed: dict[int, str] = {}
        async with new_client(follow_redirects=True) as client:
            for ratio in PLAY_RATIOS:
                play_url = (
                    f"https://aweme.snssdk.com/aweme/v1/play/"
                    f"?video_id={video_id}&ratio={ratio}"
                )
                headers = {**self._media_headers(referer), "Range": "bytes=0-1"}
                try:
                    resp = await client.get(play_url, headers=headers)
                    if resp.status_code >= 400:
                        continue
                    size = self._extract_response_size(resp.headers)
                    if size > 0:
                        probed[size] = str(resp.url)
                except httpx.HTTPError as e:
                    logger.debug(f"抖音 play 端点探测失败(ratio={ratio}): {e}")
        if not probed:
            return None
        return probed[max(probed)]

    @staticmethod
    def _extract_response_size(headers: httpx.Headers) -> int:
        if content_range := headers.get("Content-Range"):
            if match := re.search(r"/(\d+)\s*$", content_range):
                return int(match.group(1))
        if content_length := headers.get("Content-Length"):
            try:
                return int(content_length)
            except ValueError:
                return 0
        return 0

    async def _parse_slides(self, vid: str) -> PlatformParseResult:
        """解析幻灯片/图集接口"""
        api_url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {"aweme_ids": f"[{vid}]", "request_source": "200"}
        async with new_client() as client:
            resp = await client.get(api_url, params=params, headers=ANDROID_HEADERS)
            resp.raise_for_status()
            data = resp.json()

        details = data.get("aweme_details") or []
        if not details:
            raise ResourceNotFoundError("抖音 slides 接口未返回内容(可能已删除)")

        detail: dict[str, Any] = details[0]
        author_name = detail.get("name") or (detail.get("author") or {}).get(
            "nickname"
        ) or "未知用户"

        contents: list[PlatformMedia] = []
        for image in detail.get("images") or []:
            if img_url := self._first_url(image.get("url_list")):
                contents.append(
                    PlatformMedia(
                        type=MediaType.IMAGE, url=img_url, headers=ANDROID_HEADERS
                    )
                )
            # 图集项自带动图(实况)时一并提取
            play_urls = (
                ((image.get("video") or {}).get("play_addr") or {}).get("url_list")
                or []
            )
            if play_url := self._first_url(play_urls):
                contents.append(
                    PlatformMedia(
                        type=MediaType.VIDEO,
                        url=play_url,
                        headers=ANDROID_HEADERS,
                    )
                )

        if not contents:
            raise ResourceNotFoundError("抖音图集中未找到可用内容")

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=self._clean_desc(detail.get("desc", "")),
            author_name=author_name,
            timestamp=int(detail.get("create_time") or 0) or None,
            url=f"https://www.iesdouyin.com/share/slides/{vid}/",
            contents=contents,
        )

    @staticmethod
    def _first_url(url_list: Any) -> str | None:
        if isinstance(url_list, list) and url_list:
            url = url_list[0]
            if isinstance(url, str) and url:
                return url
        return None

    @staticmethod
    def _clean_desc(desc: str) -> str | None:
        desc = (desc or "").replace("\u200b", "").strip()
        return desc or None
