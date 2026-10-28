"""网易云音乐解析器

移植自 AstrBot 万能解析插件 core/parsers/ncm.py:
公开接口取歌曲元数据与播放直链, 同时支持 mp3 直链。
"""

import re
from typing import ClassVar

from ..utils.exceptions import ResourceNotFoundError, UrlParseError
from .base import MAX_REDIRECT_HOPS, PlatformParser
from .http_utils import COMMON_HEADERS, get_json, get_redirect_url, new_client
from .models import MediaType, PlatformMedia, PlatformParseResult

_HEADERS = {"Referer": "https://music.163.com/", **COMMON_HEADERS}


class NcmParser(PlatformParser):
    PLATFORM_NAME = "ncm"
    DISPLAY_NAME = "网易云"
    PRIORITY = 50
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://163cn.tv/xxxx (短链)
        ("163cn.tv", re.compile(r"163cn\.tv/(?P<short_key>\w+)")),
        # https://music.163.com/#/song?id=xxx / https://y.music.163.com/m/song?id=xxx
        (
            "music.163.com",
            re.compile(r"(?:y\.)?music\.163\.com/(?:#/|m/)?song\?.*?id=(?P<song_id>\d+)"),
        ),
        # mp3 直链
        (
            "music.126.net",
            re.compile(r"https?://[^/\s]*music\.126\.net/\S+?\.mp3(?:\?\S*)?"),
        ),
    ]

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        if _hop > MAX_REDIRECT_HOPS:
            raise UrlParseError(f"短链重定向次数超过上限({MAX_REDIRECT_HOPS}): {url}")
        # 短链: 302 后重新分发
        if "163cn.tv" in matched.group(0):
            short_url = f"https://163cn.tv/{matched.group('short_key')}"
            async with new_client(follow_redirects=False) as client:
                redirect_url = await get_redirect_url(
                    client, short_url, headers=_HEADERS
                )
            if rematch := self.match(redirect_url):
                return await self.parse(rematch.group(0), rematch, _hop=_hop + 1)
            raise UrlParseError(f"网易云短链重定向后无法识别: {redirect_url}")

        if song_id := matched.groupdict().get("song_id"):
            return await self._parse_song(song_id)

        # mp3 / 私人外链直链: 直接作为音频发送
        direct_url = matched.group(0)
        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title="网易云音乐",
            text="直链音频",
            url=direct_url,
            contents=[
                PlatformMedia(type=MediaType.AUDIO, url=direct_url, headers=_HEADERS)
            ],
        )

    async def _parse_song(self, song_id: str) -> PlatformParseResult:
        detail_url = (
            f"https://music.163.com/api/song/detail/?id={song_id}&ids=[{song_id}]"
        )
        play_url = (
            f"https://music.163.com/api/song/enhance/player/url?ids=[{song_id}]&br=320000"
        )

        async with new_client() as client:
            detail_json = await get_json(client, detail_url, headers=_HEADERS)
            play_json = await get_json(client, play_url, headers=_HEADERS)

        songs = detail_json.get("songs") or []
        if not songs:
            raise ResourceNotFoundError("未找到该歌曲(可能已下架或需要版权)")
        song = songs[0]

        title = song.get("name") or "未知歌曲"
        alias = (song.get("alias") or [""])[0]
        album = song.get("album") or {}
        cover_url = album.get("picUrl")
        if cover_url:
            cover_url = f"{cover_url}?param=640y640"
        artists = song.get("artists") or []
        author_name = " / ".join(ar.get("name", "") for ar in artists) or None
        author_avatar = artists[0].get("img1v1Url") if artists else None
        duration_ms = song.get("duration") or 0

        play_info = (play_json.get("data") or [{}])[0]
        audio_url = play_info.get("url")
        if not audio_url:
            raise ResourceNotFoundError("未获取到播放地址(可能需要VIP或版权受限)")

        contents = [
            PlatformMedia(
                type=MediaType.AUDIO,
                url=audio_url,
                duration=duration_ms / 1000,
                headers=_HEADERS,
            )
        ]
        # 封面作为独立图片内容展示
        if cover_url:
            contents.append(
                PlatformMedia(type=MediaType.IMAGE, url=cover_url, headers=_HEADERS)
            )

        sub_title = f"（{alias}）" if alias else ""
        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=f"{title}{sub_title}",
            text=f"专辑：{album.get('name', '')}" if album.get("name") else None,
            author_name=author_name,
            author_avatar=author_avatar,
            url=f"https://music.163.com/#/song?id={song_id}",
            contents=contents,
        )
