"""知乎解析器

移植自 AstrBot 万能解析插件 core/parsers/zhihu (精简版):
- 多套 TLS 指纹档位轮询 + 反爬挑战页/登录页识别(需 curl_cffi, 未安装时回退 httpx)
- 支持专栏文章/回答/问题(取默认排序首答)/想法(pin) 四类内容
- 正文 HTML 按原文顺序抽取文本与图片, m3u8 视频经 ffmpeg 落盘后发送
"""

import json
import re
from typing import Any, ClassVar
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR, base_config
from ..utils.common import format_number
from ..utils.exceptions import DownloadError, ResourceNotFoundError, UrlParseError
from .base import PlatformParser
from .curl_client import CURL_CFFI_AVAILABLE, request_text
from .http_utils import (
    ANDROID_HEADERS,
    COMMON_HEADERS,
    IOS_HEADERS,
    MediaTooLargeError,
    config_int,
    download_m3u8_to_file,
)
from .models import MediaType, PlatformMedia, PlatformParseResult

_CHALLENGE_MARKERS = (
    'id="zh-zse-ck"',
    "static.zhihu.com/zse-ck/",
    '"appname":"zse_ck"',
)


def _get_cookie() -> str:
    return str(base_config.get("ZHIHU_COOKIE", "") or "").strip()


class ZhihuParser(PlatformParser):
    PLATFORM_NAME = "zhihu"
    DISPLAY_NAME = "知乎"
    PRIORITY = 80
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://zhuanlan.zhihu.com/p/123456
        (
            "zhuanlan.zhihu.com",
            re.compile(r"zhuanlan\.zhihu\.com/p/(?P<article_id>\d+)"),
        ),
        # https://www.zhihu.com/question/123/answer/456
        (
            "/answer/",
            re.compile(
                r"www\.zhihu\.com/question/(?P<question_id>\d+)/answer/(?P<answer_id>\d+)"
            ),
        ),
        # https://www.zhihu.com/question/123 (取默认排序首条回答)
        (
            "www.zhihu.com/question/",
            re.compile(
                r"www\.zhihu\.com/question/(?P<question_id2>\d+)(?!\d)(?!/answer)"
            ),
        ),
        # https://www.zhihu.com/pin/123456 (想法)
        ("www.zhihu.com/pin/", re.compile(r"www\.zhihu\.com/pin/(?P<pin_id>\d+)")),
    ]

    def __init__(self):
        self.base_headers = {
            **COMMON_HEADERS,
            "accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "referer": "https://www.zhihu.com/",
            "origin": "https://www.zhihu.com",
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        groups = matched.groupdict()

        if article_id := groups.get("article_id"):
            return await self._parse_article(article_id)

        if answer_id := groups.get("answer_id"):
            return await self._parse_answer(groups["question_id"], answer_id)

        if question_id := groups.get("question_id2"):
            return await self._parse_question(question_id)

        if pin_id := groups.get("pin_id"):
            return await self._parse_pin(pin_id)

        raise UrlParseError(f"无法识别的知乎链接: {url}")

    # ---------------- 请求层(多指纹档位轮询) ----------------

    def _build_headers(
        self, accept: str, user_agent: str | None = None
    ) -> dict[str, str]:
        headers = {**self.base_headers, "accept": accept}
        if user_agent:
            headers["User-Agent"] = user_agent
        if cookie := _get_cookie():
            headers["cookie"] = cookie
        return headers

    @staticmethod
    def _is_challenge(html_text: str, status_code: int) -> bool:
        lowered = html_text.lower()
        return any(marker in lowered for marker in _CHALLENGE_MARKERS) or (
            status_code == 403 and "zse-ck" in lowered
        )

    @staticmethod
    def _is_login_page(final_url: str, html_text: str) -> bool:
        lowered_url = final_url.lower()
        lowered_html = html_text.lower()
        return (
            "/signin" in lowered_url
            or "/signup" in lowered_url
            or "<title>知乎 - 有问题，就会有答案</title>" in lowered_html
        )

    async def _fetch_with_profiles(
        self, url: str, *, want_json: bool = False
    ) -> dict[str, Any]:
        """desktop/ios/mobile 三档 TLS 指纹轮询抓取, 全部失败时给出可操作的提示"""
        accept = (
            "application/json, text/plain, */*"
            if want_json
            else self.base_headers["accept"]
        )
        profiles = (
            ("desktop", "chrome", COMMON_HEADERS["User-Agent"]),
            ("ios", "safari_ios", IOS_HEADERS["User-Agent"]),
            ("mobile", "chrome_android", ANDROID_HEADERS["User-Agent"]),
        )
        saw_challenge = saw_login = False

        for name, impersonate, ua in profiles:
            try:
                resp = await request_text(
                    "GET",
                    url,
                    headers=self._build_headers(accept, ua),
                    impersonate=impersonate,
                    timeout=20,
                )
            except Exception as e:
                logger.debug(f"知乎 {name} 档位请求失败: {e}", "多平台解析")
                continue

            if self._is_challenge(resp.text, resp.status_code):
                saw_challenge = True
                logger.debug(f"知乎 {name} 档位命中反爬挑战页", "多平台解析")
                continue
            if self._is_login_page(resp.final_url, resp.text):
                saw_login = True
                continue
            if resp.status_code >= 400:
                continue

            if want_json:
                text = resp.text.strip()
                if text.startswith("{"):
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        pass
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            node = soup.select_one('script#js-initialData[type="text/json"]')
            if node is None:
                continue
            try:
                payload = json.loads(node.get_text(strip=True))
            except json.JSONDecodeError:
                continue
            if isinstance(payload.get("initialState"), dict):
                return payload

        if saw_challenge or saw_login:
            hint = (
                "当前 Cookie 可能已失效"
                if _get_cookie()
                else (
                    "请配置知乎 Cookie(配置项 ZHIHU_COOKIE)"
                    if CURL_CFFI_AVAILABLE
                    else "建议安装 curl_cffi 并配置知乎 Cookie(ZHIHU_COOKIE)"
                )
            )
            raise ResourceNotFoundError(
                f"知乎抓取被风控拦截，{hint}"
            )
        raise ResourceNotFoundError("知乎页面抓取失败，未获取到有效数据")

    @staticmethod
    def _entities(payload: dict[str, Any]) -> dict[str, Any]:
        return (payload.get("initialState") or {}).get("entities") or {}

    # ---------------- 四类内容 ----------------

    async def _parse_article(self, article_id: str) -> PlatformParseResult:
        url = f"https://zhuanlan.zhihu.com/p/{article_id}"
        payload = await self._fetch_with_profiles(url)
        article = self._entities(payload).get("articles", {}).get(article_id)
        if not isinstance(article, dict):
            raise ResourceNotFoundError("知乎文章数据不存在")

        text, images, video_urls, blocks = self._extract_content(
            str(article.get("content") or ""), url
        )
        return await self._build_result(
            platform_url=url,
            title=str(article.get("title") or "知乎文章"),
            text=text,
            author=article.get("author") or {},
            timestamp=self._safe_int(article.get("created")),
            images=images,
            videos=[(u, None, None) for u in video_urls],
            # 文章仍按文字+图片发送; card_blocks 仅标记其为长文,
            # 使发送器不会把文章正文当成图集摘要塞进卡片
            card_blocks=blocks,
            stats=self._fmt_stats(
                article,
                ("voteupCount", "commentCount", "favlistsCount"),
                ("赞同", "评论", "收藏"),
            ),
        )

    async def _parse_answer(
        self, question_id: str, answer_id: str
    ) -> PlatformParseResult:
        url = f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"
        payload = await self._fetch_with_profiles(url)
        entities = self._entities(payload)
        answer = entities.get("answers", {}).get(answer_id)
        question = entities.get("questions", {}).get(question_id)
        if not isinstance(answer, dict):
            raise ResourceNotFoundError("知乎回答数据不存在")

        question_title = (
            str(question.get("title") or "知乎回答")
            if isinstance(question, dict)
            else "知乎回答"
        )
        text, images, video_urls, blocks = self._extract_content(
            str(answer.get("content") or ""), url
        )
        if answer.get("contentNeedTruncated") or not (text or images):
            # 匿名/风控场景下 SSR 页可能只带 excerpt(纯文本, 无图片), 走 v4 API 补抓
            api_result = await self._fetch_answer_content_blocks(answer_id)
            if api_result:
                text, images, video_urls, blocks = api_result
            elif answer.get("contentNeedTruncated"):
                # 正文被截断且补抓失败, 明确提示而非静默发残缺内容
                notice = "(正文被知乎截断，完整内容请点击链接查看)"
                text = f"{text}\n\n{notice}" if text else notice
                blocks.append(("text", notice))
        if not text and answer.get("excerpt"):
            text = self._clean_text(str(answer.get("excerpt")))
        return await self._build_result(
            platform_url=url,
            title=question_title,
            text=text,
            author=answer.get("author") or {},
            timestamp=self._safe_int(answer.get("createdTime")),
            images=images,
            videos=[(u, None, None) for u in video_urls],
            card_only=True,
            card_blocks=blocks,
            stats=self._fmt_stats(
                answer, ("voteupCount", "commentCount"), ("赞同", "评论")
            ),
        )

    async def _fetch_answer_content_blocks(
        self, answer_id: str
    ) -> tuple[str | None, list[str], list[str], list[tuple[str, str]]] | None:
        """v4 回答 API 补抓完整正文(含图片), 失败或无内容时返回 None"""
        api_url = (
            f"https://www.zhihu.com/api/v4/answers/{answer_id}"
            "?include=content,excerpt,voteup_count,comment_count"
        )
        try:
            payload = await self._fetch_with_profiles(api_url, want_json=True)
        except Exception as e:
            logger.debug(f"知乎回答 API 补抓失败: {e}", "多平台解析")
            return None
        if not isinstance(payload, dict):
            return None
        content = str(payload.get("content") or "")
        if not content.strip():
            return None
        return self._extract_content(content, api_url)

    async def _parse_question(self, question_id: str) -> PlatformParseResult:
        url = f"https://www.zhihu.com/question/{question_id}"
        payload = await self._fetch_with_profiles(url)
        entities = self._entities(payload)
        question = entities.get("questions", {}).get(question_id)
        if not isinstance(question, dict):
            raise ResourceNotFoundError("知乎问题数据不存在")

        # 取默认排序首条回答
        answers_index = (
            (payload.get("initialState") or {})
            .get("question", {})
            .get("answers", {})
            .get(question_id, {})
        )
        ids = answers_index.get("ids") or []
        first_target = (
            ids[0].get("target") if ids and isinstance(ids[0], dict) else None
        )
        answer_id = str(first_target) if first_target else None
        if not answer_id:
            raise ResourceNotFoundError("知乎问题页未找到默认排序的首条回答")

        answer = entities.get("answers", {}).get(answer_id)
        if (
            not isinstance(answer, dict)
            or not answer.get("content")
            or answer.get("contentNeedTruncated")
        ):
            # 问题页的回答正文可能缺失或被截断, 抓取回答页补全
            answer_payload = await self._fetch_with_profiles(
                f"{url}/answer/{answer_id}"
            )
            answer = (
                self._entities(answer_payload).get("answers", {}).get(answer_id)
            )
            if not isinstance(answer, dict):
                raise ResourceNotFoundError("知乎首条回答数据不存在")

        detail_text, detail_images, _, detail_blocks = self._extract_content(
            str(question.get("detail") or ""), url
        )
        answer_text, answer_images, video_urls, answer_blocks = self._extract_content(
            str(answer.get("content") or ""), url
        )
        if not (answer_text or answer_images):
            api_result = await self._fetch_answer_content_blocks(answer_id)
            if api_result:
                answer_text, answer_images, video_urls, answer_blocks = api_result
        blocks = [*detail_blocks]
        if detail_blocks and answer_blocks:
            blocks.append(("text", "——— 回答 ———"))
        blocks.extend(answer_blocks)
        text_parts = [part for part in (detail_text, answer_text) if part]
        return await self._build_result(
            platform_url=url,
            title=str(question.get("title") or "知乎问题"),
            text="\n\n——— 回答 ———\n\n".join(text_parts) or None,
            author=answer.get("author") or {},
            timestamp=self._safe_int(answer.get("createdTime")),
            images=[*detail_images, *answer_images],
            videos=[(u, None, None) for u in video_urls],
            card_only=True,
            card_blocks=blocks,
            stats=self._fmt_stats(
                answer, ("voteupCount", "commentCount"), ("赞同", "评论")
            ),
        )

    async def _parse_pin(self, pin_id: str) -> PlatformParseResult:
        url = f"https://www.zhihu.com/pin/{pin_id}"
        api_url = (
            f"https://www.zhihu.com/api/v4/pins/{pin_id}"
            "?include=content,content_html,created_time,updated_time,author,origin_pin"
        )
        payload = await self._fetch_with_profiles(api_url, want_json=True)
        if not isinstance(payload, dict) or not (
            payload.get("content_html") or payload.get("content")
        ):
            raise ResourceNotFoundError("知乎想法数据不存在")

        body_html = str(payload.get("content_html") or "")
        text, images, video_urls, _blocks = self._extract_content(body_html, url)
        content = payload.get("content")
        if not text and isinstance(content, str):
            text = self._clean_text(content)
        # 想法的视频在 content 块列表中(content_html 不含视频标签), 单独提取
        videos: list[tuple[str, str | None, float | None]] = [
            (u, None, None) for u in video_urls
        ]
        known = {v[0] for v in videos}
        for video in self._extract_pin_videos(payload):
            if video[0] not in known:
                videos.append(video)
        return await self._build_result(
            platform_url=url,
            title=(text or "知乎想法")[:64],
            text=text,
            author=payload.get("author") or {},
            timestamp=self._safe_int(payload.get("created_time")),
            images=images,
            videos=videos,
        )

    _VIDEO_QUALITY_RANK: ClassVar[dict[str, int]] = {"ld": 0, "sd": 1, "hd": 2}

    def _extract_pin_videos(
        self, payload: dict[str, Any]
    ) -> list[tuple[str, str | None, float | None]]:
        """从想法 content 块列表提取视频 (url, 封面, 时长秒)

        video 块的 playlist 为清晰度列表(ld/sd/hd), 取最高清晰度的直链。
        """
        videos: list[tuple[str, str | None, float | None]] = []
        seen: set[str] = set()
        for block in payload.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "video":
                continue
            playlist = [
                item
                for item in block.get("playlist") or []
                if isinstance(item, dict) and item.get("url")
            ]
            if not playlist:
                continue
            best = max(
                playlist,
                key=lambda item: (
                    self._VIDEO_QUALITY_RANK.get(str(item.get("quality")), 0),
                    self._safe_int(item.get("width")) or 0,
                ),
            )
            video_url = self._normalize_url(str(best.get("url")))
            if not video_url or video_url in seen:
                continue
            seen.add(video_url)
            thumbnail = block.get("thumbnail")
            cover = (
                self._normalize_url(thumbnail) if isinstance(thumbnail, str) else None
            )
            duration = float(self._safe_int(block.get("duration")) or 0)
            videos.append((video_url, cover or None, duration or None))
        return videos

    # ---------------- 正文抽取(原文保序) ----------------

    _SKIP_TAGS: ClassVar[set[str]] = {
        "script", "style", "noscript", "svg", "audio", "iframe", "video", "source",
    }
    _BLOCK_TAGS: ClassVar[set[str]] = {
        "p", "div", "section", "article", "blockquote", "figure", "figcaption",
        "ul", "ol", "li", "table", "pre", "tr",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def _extract_content(
        self, html_text: str, page_url: str
    ) -> tuple[str | None, list[str], list[str], list[tuple[str, str]]]:
        """按原文顺序抽取 (纯文本, 图片列表, 视频直链列表, 有序内容块)"""
        if not html_text or not html_text.strip():
            return None, [], [], []

        segments: list[tuple[str, str]] = []
        soup = BeautifulSoup(html_text, "html.parser")
        self._walk(soup, segments, page_url)

        text_parts: list[str] = []
        images: list[str] = []
        blocks: list[tuple[str, str]] = []
        buffer: list[str] = []

        def flush():
            if buffer:
                joined = "\n\n".join(part for part in buffer if part.strip())
                if joined.strip():
                    text_parts.append(joined.strip())
                    blocks.append(("text", joined.strip()))
                buffer.clear()

        for kind, value in segments:
            if kind == "text":
                if value.strip():
                    buffer.append(value.strip())
            else:
                flush()
                if value not in images:
                    images.append(value)
                    blocks.append(("image", value))
        flush()

        videos = self._extract_video_urls(soup, page_url)
        return ("\n\n".join(text_parts) or None), images, videos, blocks

    def _walk(
        self,
        node: Tag | NavigableString,
        segments: list[tuple[str, str]],
        page_url: str,
    ):
        """递归遍历 DOM, 产出保序的 ("text"|"image", 值) 段"""
        if isinstance(node, NavigableString):
            text = self._clean_text(str(node))
            if text:
                segments.append(("text", text))
            return
        if not isinstance(node, Tag):
            return

        name = (node.name or "").lower()
        if not name or name in self._SKIP_TAGS:
            return
        if name == "img":
            if img_url := self._extract_image_url(node, page_url):
                segments.append(("image", img_url))
            return
        if name == "br":
            if segments and segments[-1][0] == "text":
                segments[-1] = ("text", segments[-1][1] + "\n")
            return
        if name == "hr":
            segments.append(("text", "---"))
            return
        if name in ("ul", "ol"):
            items = [
                self._clean_text(li.get_text(" ", strip=True))
                for li in node.find_all("li", recursive=False)
            ]
            ordered = name == "ol"
            lines = [
                f"{i}. {t}" if ordered else f"- {t}"
                for i, t in enumerate(items, 1)
                if t
            ]
            if lines:
                segments.append(("text", "\n".join(lines)))
            return
        if name == "blockquote":
            text = self._clean_text(node.get_text("\n", strip=True))
            if text:
                segments.append(("text", "> " + text.replace("\n", "\n> ")))
            return

        has_block_child = any(
            isinstance(child, Tag)
            and (child.name or "").lower() in self._BLOCK_TAGS
            for child in node.children
        )
        # 含图片的块(figure/div 包裹 img 的懒加载结构)必须递归, 否则图片会被
        # 整体拍平成文字而丢失
        if (
            name in self._BLOCK_TAGS
            and not has_block_child
            and node.find("img") is None
        ):
            text = self._clean_text(node.get_text("\n", strip=True))
            if text:
                segments.append(("text", text))
            return

        for child in node.children:
            self._walk(child, segments, page_url)

    def _extract_image_url(self, tag: Tag, page_url: str) -> str | None:
        for key in ("data-original", "data-actualsrc", "data-src", "src", "srcset"):
            raw = tag.get(key)
            if not raw or not isinstance(raw, str):
                continue
            candidate = raw.split()[0] if raw.strip() else ""
            if candidate:
                return self._normalize_url(candidate, page_url)
        return None

    def _extract_video_urls(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[str]:
        """提取正文内嵌视频直链"""
        videos: list[str] = []
        seen: set[str] = set()
        for tag in soup.find_all(["video", "source"]):
            if not isinstance(tag, Tag):
                continue
            for key in ("src", "data-src"):
                raw = tag.get(key)
                if not isinstance(raw, str) or not raw.strip():
                    continue
                video_url = self._normalize_url(raw.split()[0], page_url)
                if not video_url or video_url in seen:
                    continue
                lowered = video_url.lower()
                if not (
                    re.search(r"\.(mp4|m4v|mov|webm|m3u8)(?:$|[?#])", lowered)
                    or "video.zhihu.com" in lowered
                ):
                    continue
                seen.add(video_url)
                videos.append(video_url)
        return videos

    # ---------------- 结果组装 ----------------

    async def _build_result(
        self,
        *,
        platform_url: str,
        title: str,
        text: str | None,
        author: dict[str, Any],
        timestamp: int | None,
        images: list[str],
        videos: list[tuple[str, str | None, float | None]],
        stats: str | None = None,
        card_only: bool = False,
        card_blocks: list[tuple[str, str]] | None = None,
    ) -> PlatformParseResult:
        contents: list[PlatformMedia] = [
            PlatformMedia(
                type=MediaType.IMAGE, url=img, headers=self.base_headers
            )
            for img in images
        ]

        # m3u8 视频需 ffmpeg 落盘, 在此异步完成; 失败跳过该视频
        for video_url, cover_url, duration in videos:
            if ".m3u8" in video_url.lower():
                try:
                    max_mb = config_int("MULTI_PLATFORM_MAX_VIDEO_SIZE_MB", 100)
                    path = await download_m3u8_to_file(
                        video_url,
                        PLUGIN_TEMP_DIR,
                        headers=self.base_headers,
                        max_size_mb=max_mb,
                    )
                    contents.append(
                        PlatformMedia(
                            type=MediaType.VIDEO,
                            url=video_url,
                            local_path=str(path),
                        )
                    )
                except (MediaTooLargeError, DownloadError) as e:
                    logger.warning(f"知乎 m3u8 视频下载失败跳过: {e}", "多平台解析")
            else:
                contents.append(
                    PlatformMedia(
                        type=MediaType.VIDEO,
                        url=video_url,
                        cover_url=cover_url,
                        duration=duration,
                        headers=self.base_headers,
                    )
                )

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=title.strip() or None,
            text=text,
            author_name=str(author.get("name") or "").strip() or None,
            author_avatar=str(
                author.get("avatarUrl") or author.get("avatar_url") or ""
            )
            or None,
            timestamp=timestamp,
            url=platform_url,
            contents=contents,
            extra_text=stats,
            card_only=card_only,
            card_blocks=card_blocks or [],
        )

    @staticmethod
    def _normalize_url(url: str, page_url: str | None = None) -> str:
        import html as html_lib

        value = html_lib.unescape(str(url or "")).strip().strip("\"'")
        value = value.replace("\\u002F", "/").replace("\\/", "/")
        value = value.replace("&amp;", "&").rstrip(".,);")
        if not value or value.startswith(("data:", "blob:")):
            return ""
        if value.startswith("//"):
            value = f"https:{value}"
        elif page_url and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
            value = urljoin(page_url, value)
        return value if value.startswith(("http://", "https://")) else ""

    @staticmethod
    def _clean_text(text: str) -> str:
        import html as html_lib

        value = html_lib.unescape(text or "")
        value = value.replace("\xa0", " ").replace("\u3000", " ")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t\f\v]+", " ", value)
        value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _fmt_stats(
        data: dict[str, Any], keys: tuple[str, ...], labels: tuple[str, ...]
    ) -> str | None:
        parts = []
        for key, label in zip(keys, labels):
            value = data.get(key)
            if isinstance(value, int | float) and value > 0:
                parts.append(f"{label} {format_number(int(value))}")
        return " · ".join(parts) or None
