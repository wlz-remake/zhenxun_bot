"""微博解析器

移植自 AstrBot 万能解析插件 core/parsers/weibo.py:
- m.weibo.cn statuses/show 接口取正文/图集/视频(伪装 XHR, 不带 cookie, 不跟随重定向)
- weibo.com/tv/show 的 mid 通过 base62 算法还原为微博 id
- video.weibo.com fid 与 card.weibo 文章走各自的接口
"""

import calendar
import re
import time
from typing import ClassVar
from uuid import uuid4

from bs4 import BeautifulSoup

from zhenxun.services.log import logger

from ..utils.exceptions import ResourceNotFoundError, UrlParseError
from .base import MAX_REDIRECT_HOPS, PlatformParser
from .http_utils import COMMON_HEADERS, get_json, get_redirect_url, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_TAG_PATTERN = re.compile(r"<[^>]*>")


def _strip_html_tags(text: str) -> str:
    return _TAG_PATTERN.sub("", (text or "").replace("<br />", "\n")).strip()


def _fix_scheme(url: str | None) -> str | None:
    """微博接口返回的部分链接缺少协议前缀(// 或裸路径)"""
    if not url:
        return None
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


class WeiboParser(PlatformParser):
    PLATFORM_NAME = "weibo"
    DISPLAY_NAME = "微博"
    PRIORITY = 20
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://weibo.com/tv/show/1034:5007449447661594?mid=5007452630158934
        (
            "weibo.com/tv",
            re.compile(r"weibo\.com/tv/show/\d{4}:\d+\?mid=(?P<mid>\d+)"),
        ),
        # https://video.weibo.com/show?fid=1034:5145615399845897
        (
            "video.weibo",
            re.compile(r"video\.weibo\.com/show\?fid=(?P<fid>\d+:\d+)"),
        ),
        # https://m.weibo.cn/status/5234367615996775
        # https://m.weibo.cn/detail/4976424138313924
        (
            "weibo.cn",
            re.compile(r"weibo\.cn/(?:status|detail|\d+)/(?P<wid>[0-9a-zA-Z]+)"),
        ),
        # https://weibo.com/7207262816/P5kWdcfDe
        ("weibo.com", re.compile(r"weibo\.com/\d+/(?P<wid2>[0-9a-zA-Z]+)")),
        # https://mapp.api.weibo.cn/fx/233911ddcc6bffea835a55e725fb0ebc.html
        ("mapp.api.weibo", re.compile(r"mapp\.api\.weibo\.cn/fx/[A-Za-z\d]+\.html")),
        # https://weibo.com/ttarticle/p/show?id=2309404962180771742222
        (
            "ttarticle",
            re.compile(r"weibo\.com/ttarticle/.*?id=(?P<article_id>\d+)"),
        ),
        # https://card.weibo.com/article/m/show/id/2309404962180771742222
        (
            "card.weibo",
            re.compile(r"card\.weibo\.com/article/m/(?:show|detail)/id/(?P<article_id>\d+)"),
        ),
    ]

    def __init__(self):
        self.headers = {
            **COMMON_HEADERS,
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
            "referer": "https://weibo.com/",
        }
        self.media_headers = {"Referer": "https://weibo.com/", **COMMON_HEADERS}

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        if _hop > MAX_REDIRECT_HOPS:
            raise UrlParseError(f"短链重定向次数超过上限({MAX_REDIRECT_HOPS}): {url}")
        groups = matched.groupdict()

        if mid := groups.get("mid"):
            return await self._parse_weibo_id(self._mid2id(mid))

        if fid := groups.get("fid"):
            return await self._parse_fid(fid)

        if article_id := groups.get("article_id"):
            return await self._parse_article(article_id)

        if "mapp.api.weibo.cn" in matched.group(0):
            async with new_client(follow_redirects=False) as client:
                redirect_url = await get_redirect_url(
                    client, f"https://{matched.group(0)}", headers=self.headers
                )
            if rematch := self.match(redirect_url):
                return await self.parse(rematch.group(0), rematch, _hop=_hop + 1)
            raise UrlParseError(f"微博分享链接重定向后无法识别: {redirect_url}")

        wid = groups.get("wid") or groups.get("wid2")
        if not wid:
            raise UrlParseError(f"无法从微博链接提取ID: {url}")
        return await self._parse_weibo_id(wid)

    async def _parse_weibo_id(self, weibo_id: str) -> PlatformParseResult:
        """通过 m.weibo.cn 接口解析微博(无 Cookie + 伪装 XHR + 不跟随重定向)"""
        headers = {
            **self.headers,
            "accept": "application/json, text/plain, */*",
            "referer": f"https://m.weibo.cn/detail/{weibo_id}",
            "origin": "https://m.weibo.cn",
            "x-requested-with": "XMLHttpRequest",
            "mweibo-pwa": "1",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
        }
        # 加时间戳参数, 减少被缓存/风控规则命中
        url = (
            f"https://m.weibo.cn/statuses/show?id={weibo_id}"
            f"&_={int(time.time() * 1000)}"
        )

        async with new_client(follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (403, 418):
                raise ResourceNotFoundError(
                    f"微博被风控拦截(HTTP {resp.status_code}), 请稍后重试"
                )
            if resp.status_code == 404:
                raise ResourceNotFoundError("微博不存在或已被删除")
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "application/json" not in content_type:
                raise ResourceNotFoundError("微博接口返回异常数据(可能已被删除)")
            data = (resp.json() or {}).get("data") or {}

        if not data:
            raise ResourceNotFoundError("微博数据为空(可能已被删除)")

        result = self._build_result(data)
        result.url = (
            f"https://weibo.com/{(data.get('user') or {}).get('id', '')}"
            f"/{data.get('bid', '')}"
            if data.get("bid")
            else url
        )
        return result

    def _build_result(self, data: dict, is_repost: bool = False) -> PlatformParseResult:
        """由微博接口数据构建解析结果, 转发微博内容平铺进正文"""
        user = data.get("user") or {}
        author_name = user.get("screen_name") or "未知用户"

        contents: list[PlatformMedia] = []
        # 视频
        page_info = data.get("page_info") or {}
        urls = page_info.get("urls") or {}
        video_url = (
            urls.get("mp4_720p_mp4") or urls.get("mp4_hd_mp4") or urls.get("mp4_ld_mp4")
        )
        if video_url and not video_url.startswith("http"):
            video_url = f"https:{video_url}"
        if video_url:
            cover_url = _fix_scheme((page_info.get("page_pic") or {}).get("url"))
            contents.append(
                PlatformMedia(
                    type=MediaType.VIDEO,
                    url=video_url,
                    cover_url=cover_url,
                    headers=self.media_headers,
                )
            )
        # 图集
        for pic in data.get("pics") or []:
            img_url = _fix_scheme((pic.get("large") or pic).get("url"))
            if img_url:
                contents.append(
                    PlatformMedia(
                        type=MediaType.IMAGE, url=img_url, headers=self.media_headers
                    )
                )

        text_parts: list[str] = []
        if title := (page_info.get("title") or "").strip():
            text_parts.append(title)
        if body_text := _strip_html_tags(data.get("text", "")):
            text_parts.append(body_text)
        # 转发的微博: 文本附加说明, 媒体合并展示
        if repost_data := data.get("retweeted_status"):
            repost_result = self._build_result(repost_data, is_repost=True)
            repost_header = (
                f"转发 @{repost_result.author_name}: {repost_result.text or ''}"
            ).strip()
            text_parts.append(f"——— 转发内容 ———\n{repost_header}")
            contents.extend(repost_result.contents)

        text = "\n".join(text_parts).strip() or None

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=None,
            text=text,
            author_name=author_name,
            author_avatar=_fix_scheme(user.get("profile_image_url")),
            timestamp=self._parse_created_at(data.get("created_at", "")),
            url=None,
            contents=contents,
        )

    async def _parse_fid(self, fid: str) -> PlatformParseResult:
        """解析 video.weibo.com 带 fid 的视频"""
        req_url = f"https://h5.video.weibo.com/api/component?page=/show/{fid}"
        headers = {
            **self.headers,
            "Referer": f"https://h5.video.weibo.com/show/{fid}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        post_content = (
            'data={"Component_Play_Playinfo":{"oid":"' + fid + '"}}'
        )
        async with new_client() as client:
            json_data = await get_json(
                client, req_url, headers=headers, method="POST", data=post_content
            )

        data = (json_data.get("data") or {}).get("Component_Play_Playinfo") or {}
        if not data:
            raise ResourceNotFoundError("微博视频数据为空(可能已被删除)")

        user = (data.get("reward") or {}).get("user") or {}
        contents: list[PlatformMedia] = []
        cover_url = _fix_scheme(data.get("cover_image"))
        if cover_url and not cover_url.startswith("http"):
            cover_url = f"https:{cover_url}"

        # urls 字典第一条码率最高
        video_url_dict = data.get("urls")
        video_url = None
        if isinstance(video_url_dict, dict) and video_url_dict:
            video_url = next(iter(video_url_dict.values()))
        video_url = video_url or data.get("stream_url")
        if video_url:
            if not video_url.startswith("http"):
                video_url = f"https:{video_url}"
            contents.append(
                PlatformMedia(
                    type=MediaType.VIDEO,
                    url=video_url,
                    cover_url=cover_url,
                    headers=self.media_headers,
                )
            )

        text = _strip_html_tags(data.get("text", "")) or None
        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(data.get("title") or "").strip() or None,
            text=text,
            author_name=user.get("name") or "未知用户",
            author_avatar=_fix_scheme(user.get("profile_image_url")),
            timestamp=self._parse_created_at(data.get("real_date", "")),
            url=f"https://h5.video.weibo.com/show/{fid}",
            contents=contents,
        )

    async def _parse_article(self, article_id: str) -> PlatformParseResult:
        """解析微博头条文章"""
        url = "https://card.weibo.com/article/m/aj/detail"
        params = {
            "_rid": str(uuid4()),
            "id": article_id,
            "_t": int(time.time() * 1000),
        }
        async with new_client() as client:
            json_data = await get_json(
                client, url, headers=self.headers, method="POST", data=params
            )

        if json_data.get("msg") != "success":
            raise ResourceNotFoundError("微博文章获取失败")
        data = json_data.get("data") or {}
        userinfo = data.get("userinfo") or {}

        contents: list[PlatformMedia] = []
        text_buffer: list[str] = []
        soup = BeautifulSoup(data.get("content", ""), "html.parser")
        for element in soup.find_all(["p", "img"]):
            if element.name == "p":
                text = element.get_text(strip=True).replace("\u200b", "")
                if text:
                    text_buffer.append(text)
            elif element.name == "img":
                src = element.get("src")
                if isinstance(src, str) and src:
                    contents.append(
                        PlatformMedia(
                            type=MediaType.IMAGE,
                            url=_fix_scheme(src) or src,
                            headers=self.media_headers,
                        )
                    )

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=(data.get("title") or "").strip() or None,
            text="\n\n".join(text_buffer) or None,
            author_name=userinfo.get("screen_name") or "未知用户",
            author_avatar=_fix_scheme(userinfo.get("profile_image_url")),
            timestamp=int(data.get("create_at_unix") or 0) or None,
            url=data.get("url") or None,
            contents=contents,
        )

    @staticmethod
    def _parse_created_at(created_at: str) -> int | None:
        """解析 'Thu Oct 02 14:39:33 +0800 2025' 格式时间"""
        if not created_at:
            return None
        try:
            parsed = time.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
            # timegm 将字段按 UTC 解释, 再减去 %z 偏移得到真实时间戳
            # (替代 mktime 的本地时区解释, 非东八区宿主不再产生时差)
            return int(calendar.timegm(parsed) - (parsed.tm_gmtoff or 0))
        except ValueError:
            logger.debug(f"微博时间解析失败: {created_at}", "多平台解析")
            return None

    @staticmethod
    def _base62_encode(number: int) -> str:
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        if number == 0:
            return "0"
        result = ""
        while number > 0:
            result = alphabet[number % 62] + result
            number //= 62
        return result

    @classmethod
    def _mid2id(cls, mid: str) -> str:
        """将微博 tv/show 链接中的 mid 还原为微博 id"""
        from math import ceil

        mid = str(mid)[::-1]
        size = ceil(len(mid) / 7)
        result = []
        for i in range(size):
            s = mid[i * 7 : (i + 1) * 7][::-1]
            s = cls._base62_encode(int(s))
            if i < size - 1 and len(s) < 4:
                s = "0" * (4 - len(s)) + s
            result.append(s)
        result.reverse()
        return "".join(result)
