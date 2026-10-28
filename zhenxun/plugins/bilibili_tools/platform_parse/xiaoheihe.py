"""小黑盒解析器

移植自 AstrBot 万能解析插件 core/parsers/xiaoheihe.py:
- 匿名设备指纹: POST deviceprofile/v4 (硬编码加密块换取 deviceId → x_xhh_tokenid)
- 自定义 hkey 签名: 时间戳+路径+nonce 经字符表交织/MD5/AES MixColumns 变体生成
- BBS 帖子: link/tree 接口 → 正文 JSON 块按原文顺序抽取图文
- 游戏页: __NUXT_DATA__ devalue 反序列化 → 游戏信息/截图/预告片

m3u8 视频经 ffmpeg 落盘后发送(替代参考插件的 yt-dlp 方案)。
"""

import hashlib
import html
import json
import random
import re
import time
from typing import Any, ClassVar

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR, base_config
from ..utils.exceptions import DownloadError, ResourceNotFoundError, UrlParseError
from .base import PlatformParser
from .curl_client import request_text
from .http_utils import (
    COMMON_HEADERS,
    MediaTooLargeError,
    config_int,
    download_m3u8_to_file,
)
from .models import MediaType, PlatformMedia, PlatformParseResult

# deviceprofile 接口的硬编码加密块(来源: 参考插件, 与官网前端逻辑对应)
_V4_EP = (
    "V1ZCERzVgMWrKv+VcTl5QmS9JuPWLOQ8A0mACeTyYXtTbiguOrHhwaqnagZ6zdAgF"
    "4WpAYBvUH3EDnPRlNWut4CTDU1tCa80BSnvTMC9X1j9Kh6IMlGmzPIqpBzzx9r7Nt"
    "9XtUhv2WiQ2BgPnUwOFe7gN9r8Yj3184qxn1btJL8="
)
_V4_DATA = (
    "abbbe96a1579aa6fe4fa84e875851b7d7a843a14c5c9573c771d9c1443c9b3a"
    "d7603a8d9d67dbc9bd001bf42702ac82e4a6979323ff305eecd74b9620ee140"
    "0c135f840b35d9402ec3e3a93fcb3d0d3d6b3e740f5176b72225b6fb8a0d483"
    "cab753aa71062dc9b59bc8de950628f23607301c6cd94e75f680b86485a11ac"
    "36eba1413e9f14b274eadff30114dfb1cedadc4bd08ef83c5b2d048970d07d3"
    "943afef809b44e3b9fee602c91e274fee1523a8beee7e7cec85680b279d616d"
    "da15e98b1b0aa718276bcdb05d4ac3e44e72da220e0ea798ad7452aec01d0db"
    "c31ad6bf147eab7f7e539d35fe5149110aae5c7069a67eba4aae638505819f8"
    "9e2a58bc3b5001c8a5045334121ef04a8e442d7dbb7776bd6013674d2c0028a"
    "f131bf6bde47b90dce5c8b9463c9f83d0e7264145c2f6f259d70c4d63a4996b"
    "b7c0074e8a59fa298ad144ec139cb29bc94074fbe2f4a88400d85c003793e2b"
    "e2077184c3ba2e792926fce25f24d3a764a7c2667446173c74aa704d0d517f2"
    "10926aaef05376230b43c3a676dad6ff1c9603553d66eadfb492445eac44745"
    "acc620b325560d4941c10e05f3099a17a553fd763a1b7d6ef29f512e436bdfa"
    "9fa7c5a70b6a5f91bbcb21946fc2ce92db0c92930008b0fc82e90c3c73f9265"
    "2ca388f77b262a918cf59160fa88e481138ee7fe9a9b51d7949a74d22d1dab4"
    "e865c12325bfb5b9e748526afb6d8a05c543fd6dc72e81b06a4ebbf8149fca5"
    "37a19330da2011eec0229e2302babe239397aa1c2292ab3807cf0aa129d078a"
    "a9da010003eac5bb2c06435fbbe9bee7543290c1224745bb485d78f42ee4e82"
    "afb27a38befc60a688fb2514795064926bf205357bd46b7c14dd15aea2cab48"
    "5c993f0df5a20811d0a7b3bfb1fcb0737c8305675e9bdac396ef8cffb0b6bc4"
    "700c3d881c1945329b721b9080bed46b18105b7c9fea4f8276f0fcd09fe99ec"
    "52fa50b11e12a19eb9d091ecde701ab2879e2d7727386b28bbde8d62832e1ad"
    "822ea57b383cdd3767e8ee64e201bf00fe9cc8428ece3262550764fea47c69e"
    "e4339de98767f034d8852993fdefa315d9dcda71a74b665804706d4f9a8c139"
    "3670c2220e4ceac833620e0dc8175eb7a77b8b37c1a9d9940c67d44c8bc6b5f"
    "9e46273e2f5149d3d3148e8f7a02c4a4c3c998924b7d0e93528952034adc20d"
    "c342404a8606f0c07cb2b98c4a5434e69b69282daf952f586b9eed4b4f1ef0c"
    "fe5c6d156d14fb5057c8c32a355d07e2f56737d1ccfad573d42c840bbe8b750"
    "388211f2c0c5d6a1e34e7741389a742dff58bb0b9f339707a349a09519ca78d"
    "5e4f1baaf2598ab9001c15824494eecc17735e69a193e5437cbe44c6f156a0b"
    "b8df4fed5edefd4f56f4ef0b4d8cc40fe623836da3c5e662005825c9d344074"
    "be2306d6241c163fe92a6ce40ff60538d7464f5a06b6bb9ca1e6f18491ca3c7"
    "d6c00e299cbb1ca1c525a981fc6c6f2bb05f709101099b8bd0d2c2a628d94c6"
    "1aa97fdd58c9f357359fbd5be9e8f0f534f4481fb780d58e3e599e01fdd5a7f"
    "c5fb7e01b76fd58b2f264947d2149fefa57577ef326e264fc827939329031d9"
    "01be7579ecf5fccdab11c615c1a053f198297c0723faf8b17ea3335d49df2bf"
    "dd17271c2b64745b1f412d87297edd4404a4ae5312debf73b66afcc3d884b93"
    "8de41b6ee87265ce624897f3557ebe2d97e6fb17f1dc6a893e48dfa16ef2bff"
    "d8f3e06f0a1fcf44c7f2efa372e0ff61344c93f4a2a66538fcc134cd0bf94d5"
    "4c969cda4392af70608cbab6cfa340b674ba3a59385c0ed9bb236ff6ed10e1e"
    "5a9d4b6529c075dc1ac23cfdae18ab1651a5ee747322e51e3cc6035ca929789"
    "00924e661a2694a47873569baa95fd821711dc53a1e0299ed707e337b570591"
    "a3f61a5e39f8a75771da1613e8236c9b1b94cb5617fdaf2424d68a7fbd83ebf"
    "356fc87e8a805bee5bbd20a55a70881394d7624b1dcf5a135f1cf40b842eca3"
    "3d46b72447e0a2e85adf6c26efa6cc73b63573840f7b6229fb03ab45a8b639b"
    "5a66bbd6f63d10e59db49d7a9c9af3e3aeb79b7b756e24d5002917e7e788018"
    "4f80fcc605a1ba825c779e6083fd7fb0920bbcee021ec8e35427391b871b149"
    "c306c2dbda602044cd53ec424dd70cfd1c14a23c9964c039258cff4b75112f8"
    "15d9717433c1989ec398cd2acd67c89be82a409e0ef8f3e9ea8ec8b51b5ea5a"
    "005b5e735978d9a2987a76d62a2af230e30dc6327f7c0d153add27c7e8a320e"
    "4df6c05ab91fe0b9f6f9e13c50f39454066776503eb2ec84b74b4b2d5228627"
    "d81c938f7201610c9b703e4fd283a94835b7387db2880443a050d3eb0859aa1"
    "efd0f9bb7613b6b918ec2f7b5bb3e7722105b595e7973a93e3de8153a0f8e5b"
    "fd1aa6cefc6285fea85e8381ddcce98b31dda33db2a3c80ac04df14b872c805"
    "15373f231c3653fb2db799b32e83e59fb0f5763febca3d291b49bf83dd7ebd6"
    "1229300b65d44964d9e679f6061a0b2ea1bcd9f5af9bf710047237d87d13394"
    "ea8b4627c6997589d0b58379d025b076460eab88d6615ee92b0aa6c47f721f9"
    "7e0b5bbe721f06544d0a1bb81402697f2d72ad32c791dab45064b4d18460602"
    "9494b268feaebb268e7f92352dc3482f857c14885aabbad98a43e5f8fa5d77d"
    "61dc22f23080b9e6403c76f5fb862d7520ab85ae7c1d0e339729f664e7d668f"
    "4b9d1301acabb62fda5940db236ea9d2ca896cbb6a13eda6120fa5881453cb4"
    "490438460c00db4cd4bdf5df993d3a8d5726c756015eed542e0a4b910570f39"
    "7211c3f84f6a0d038e82270f94543e8da1e8d0cffd8f4f561daaf6003ad1fad"
    "fdd89c50f057a79225d8647aead74b33216e328c4204686b4ae93ce5f7ee25e"
    "1c83fe2cb72c67589aa4865d278ff7a112d09c16707de8acd61b49b901a3266"
    "e8ef55f1351fdc3013154635e51e649cbf31fc9b32f6956800834ca73e0b75b"
    "2b54d7125257eb6c24ebff52b741109be6da99bb6e0ffab85c3c219550ec3fc"
    "b12e2e4d0234627b061193c290baa1be73241be70925c08d33e6efdd44eca9a"
    "5160bdc5b47bd1f9d3f2cbf38848cf1aaa2a4827f86e43e06246b3bf94cb0b9"
    "f050c89533a3be9ffecefebd1a92e04197f18d7fadc0bfc8664de18425d5c03"
    "59b58049267934756f513bd68ea427b38f15213f42cce05cd59f5ea502967ec"
    "6a096daaa5e5d2a373227f2fe4514e27dfa012d708f7e94a286452972b5fab4"
    "581ecee3df40bad802cbb50b1a5d9dd3323a5f7c61ab893b16782a0ba64fd42"
    "10c30ac00f9d21b9124e5e5b323f43badf56761e1eea5c86ff61f19ce1485f4"
    "2cf6cadd751bbfb2ef87229eee5068ef6e209f123d29a571a374974ceac2e77"
    "f143faba60fc5d16f88d801fa01d879420b5d1393ad5b2bc913e3b0ba7155a6"
    "7648196573126273cccc79f2eac32ab68d72cc0f7170feca9c9726af9d65962"
    "663d5281372386ec88bd2fa82316f687535ecd39f00658523708ca4785529f5"
    "93baf100597ed00c15ae8ff87baa295871680b4096ac03a550f0f015297198b"
    "1a93f38cfefbeceabc099c1026664d77f616b4f069cf8bf53d2684b9a4d933c"
    "3c65a3aef21559527bfc6586e0247efa244a0a355b43751bc09be8012699468"
    "a8c332d60b11bb4881bf56b92ead10e059ac40f83a4d6725cacbc1bb307c839"
    "c4edc8b5484b9e2935842e867e739223f2eaaaff04d9701cfa49e3f80be4f2d"
    "1b7e8eb76fd7f33dfa79831f75ee65a75b7c7fff98254818f1ab77bca856656"
    "4d48e0012733dd426bf841f27f960394b1bacb8a3e36b96c41d751584cd580f"
    "ef1b6a8bf990487268348f682a27549ecbb9674b14f2fc97f203f3468f248ec"
    "3cf5171aa5e8a8d31a9a433c4f7644736aaf6695b28771fe66b4736e3afb322"
    "11ad534b05641600d2cdc79a251fc4c4e5540df9a40aaad329fedd49a429b20"
    "70e1345a4146c297ee2a03f056675054e83207d17de21242032c30398259440"
    "84e60cbd70eb4c469859824cd7d04340de0d19e614a0826a63c63e15c3372b1"
    "7515d4b6951ff6c612f65c3e6538fd0515bcb4814bb641fca5a45c7dae9"
)

# 匿名设备指纹缓存(token, device_id), 避免每次解析重新注册
_device_ctx: dict[str, str] | None = None

_HEADERS = {
    **COMMON_HEADERS,
    "accept": "application/json, text/plain, */*",
    "referer": "https://www.xiaoheihe.cn/",
    "origin": "https://www.xiaoheihe.cn",
}


def _get_cookie() -> str:

    return str(base_config.get("XIAOHEIHE_COOKIE", "") or "").strip()


def _build_headers(accept: str | None = None) -> dict[str, str]:
    headers = {**_HEADERS}
    if accept:
        headers["accept"] = accept
    if cookie := _get_cookie():
        headers["cookie"] = cookie
    return headers


class XiaoheiheParser(PlatformParser):
    PLATFORM_NAME = "xiaoheihe"
    DISPLAY_NAME = "小黑盒"
    PRIORITY = 84
    CHAR_TABLE: ClassVar[str] = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = [
        # https://www.xiaoheihe.cn/app/bbs/link/xxxx (BBS 帖子分享)
        (
            "xiaoheihe.cn/app/bbs/link",
            re.compile(r"xiaoheihe\.cn/app/bbs/link/(?P<link_id>[0-9a-z]+)"),
        ),
        # https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...link_id=xxx
        (
            "api.xiaoheihe.cn/v3/bbs",
            re.compile(
                r"api\.xiaoheihe\.cn/v3/bbs/app/api/web/share\?"
                r"[A-Za-z0-9._%&+=/#@?;-]*link_id=(?P<link_id2>[0-9a-z]+)"
            ),
        ),
        # https://api.xiaoheihe.cn/game/share_game_detail?...appid=..&game_type=..
        (
            "api.xiaoheihe.cn/game/share_game_detail",
            re.compile(
                r"api\.xiaoheihe\.cn/game/share_game_detail\?"
                r"[A-Za-z0-9._%&+=/#@?;-]*appid=(?P<appid>[0-9a-z]+)"
                r"[A-Za-z0-9._%&+=/#@?;-]*game_type=(?P<game_type>[a-z]+)"
            ),
        ),
        # https://www.xiaoheihe.cn/app/topic/game/pc/123456 (游戏页)
        (
            "xiaoheihe.cn/app/topic/game",
            re.compile(
                r"xiaoheihe\.cn/app/topic/game/(?P<game_type2>[a-z]+)/(?P<appid2>[0-9a-z]+)"
            ),
        ),
    ]

    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        groups = matched.groupdict()

        if link_id := (groups.get("link_id") or groups.get("link_id2")):
            return await self._parse_bbs(link_id)

        appid = groups.get("appid") or groups.get("appid2")
        game_type = groups.get("game_type") or groups.get("game_type2") or "pc"
        if appid:
            return await self._parse_game(appid, game_type)

        raise UrlParseError(f"无法识别的小黑盒链接: {url}")

    # ---------------- BBS 帖子 ----------------

    async def _parse_bbs(self, link_id: str) -> PlatformParseResult:
        request_ctx = await self._get_device_ctx()
        payload = await self._fetch_link_tree(link_id, request_ctx)
        link = payload.get("link")
        if not isinstance(link, dict):
            raise ResourceNotFoundError("小黑盒 link/tree 缺少 link 节点")

        title = self._clean_text(str(link.get("title") or "")) or None
        author_name, avatar = self._build_author(link)
        body_text, image_urls = self._parse_body_segments(link)
        web_url = f"https://www.xiaoheihe.cn/app/bbs/link/{link_id}"

        contents = [
            PlatformMedia(type=MediaType.IMAGE, url=img, headers=_HEADERS)
            for img in image_urls
        ]
        stats_parts = []

        # 帖子内嵌视频
        if link.get("has_video"):
            if video_url := str(link.get("video_url") or "").strip():
                media = await self._build_video_media(video_url)
                if media:
                    contents.append(media)
                else:
                    stats_parts.append("视频下载失败已跳过")

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=title,
            text=body_text,
            author_name=author_name,
            author_avatar=avatar,
            url=web_url,
            contents=contents,
            extra_text="，".join(stats_parts) or None,
        )

    async def _fetch_link_tree(
        self, link_id: str, request_ctx: dict[str, str]
    ) -> dict[str, Any]:
        sig = self._sign_path("/bbs/app/link/tree")
        params = {
            "os_type": "web",
            "app": "heybox",
            "client_type": "web",
            "version": "999.0.4",
            "web_version": "2.5",
            "x_client_type": "web",
            "x_app": "heybox_website",
            "heybox_id": "",
            "x_os_type": "Windows",
            "device_info": "Chrome",
            "device_id": request_ctx.get("device_id", ""),
            "link_id": link_id,
            "owner_only": "1",
            **sig,
        }
        # 注意: 不能用 cookies= 参数(会整体覆盖 Cookie 头, 丢弃用户配置的 Cookie),
        # 需把匿名 tokenid 合并进 Cookie 头一起发送
        user_cookie = _get_cookie()
        tokenid = request_ctx["x_xhh_tokenid"]
        if user_cookie and "x_xhh_tokenid=" in user_cookie:
            merged_cookie = user_cookie
        elif user_cookie:
            merged_cookie = f"{user_cookie}; x_xhh_tokenid={tokenid}"
        else:
            merged_cookie = f"x_xhh_tokenid={tokenid}"
        headers = {**_build_headers(), "cookie": merged_cookie}
        payload = await self._request_json(
            "GET",
            "https://api.xiaoheihe.cn/bbs/app/link/tree",
            params=params,
            headers=headers,
        )
        status = payload.get("status")
        if status != "ok":
            hint = ""
            if status == "show_captcha":
                hint = "(触发风控验证，建议配置 XIAOHEIHE_COOKIE 或稍后重试)"
                # 失效缓存的匿名指纹, 下次解析重新注册设备
                global _device_ctx
                _device_ctx = None
            raise ResourceNotFoundError(
                f"小黑盒 link/tree 请求失败: {status}{hint}"
            )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ResourceNotFoundError("小黑盒 link/tree 结果为空")
        return result

    def _build_author(self, link: dict[str, Any]) -> tuple[str | None, str | None]:
        user = link.get("user")
        if not isinstance(user, dict):
            return None, None
        name = (
            self._clean_text(str(user.get("username") or user.get("nickname") or ""))
            or None
        )
        avatar = str(user.get("avatar") or "") or None
        return name, avatar

    def _parse_body_segments(
        self, link: dict[str, Any]
    ) -> tuple[str | None, list[str]]:
        """解析帖子正文, 返回 (合并文本, 去重后的图片列表)"""
        raw_text = link.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            return None, []

        try:
            blocks = json.loads(raw_text)
        except json.JSONDecodeError:
            return self._clean_text(raw_text) or None, []

        if not isinstance(blocks, list):
            return self._clean_text(raw_text) or None, []

        text_parts: list[str] = []
        images: list[str] = []
        seen: set[str] = set()

        for block in blocks:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "") == "img":
                img = self._normalize_image_url(str(block.get("url") or ""))
                key = self._image_dedup_key(img)
                if img and key and key not in seen:
                    seen.add(key)
                    images.append(img)
                continue
            html_block = str(block.get("text") or "")
            if not html_block:
                continue
            # HTML 块按内联 <img> 的位置拆为有序文/图段
            for part in re.split(r"(<img\b[^>]*>)", html_block, flags=re.I):
                if not part:
                    continue
                if re.fullmatch(r"<img\b[^>]*>", part, flags=re.I):
                    m = re.search(
                        r'data-original="([^"]+)"|src="([^"]+)"', part, re.I
                    )
                    if m:
                        img = self._normalize_image_url(m.group(1) or m.group(2) or "")
                        key = self._image_dedup_key(img)
                        if img and key and key not in seen:
                            seen.add(key)
                            images.append(img)
                else:
                    text = self._html_block_to_text(part)
                    if text:
                        text_parts.append(text)

        return ("\n\n".join(text_parts) or None), images

    @staticmethod
    def _normalize_image_url(url: str) -> str:
        if not url:
            return ""
        url = html.unescape(url)
        # /bbs/ 为普通帖子图, /live/ 为实况照片(live photo)的静态帧
        if not url.startswith("http") or not (
            "/bbs/" in url or "/live/" in url
        ):
            return ""
        # 去掉七牛 imageMogr2 压缩参数, 取平台留存的原图
        if "imageMogr2" in url:
            url = url.split("?", 1)[0]
        return url

    @staticmethod
    def _image_dedup_key(url: str) -> str:
        if not url:
            return ""
        return url.split("?", 1)[0].replace(
            "imgheybox1.max-c.com", "imgheybox.max-c.com"
        )

    @staticmethod
    def _clean_text(text: str) -> str:
        text = html.unescape(text.replace("\xa0", " "))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _html_block_to_text(self, html_block: str) -> str:
        fragment = html.unescape(html_block)
        fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"</p>\s*<p[^>]*>", "\n", fragment, flags=re.I)
        fragment = re.sub(r"<img\b[^>]*>", "", fragment, flags=re.I)
        fragment = re.sub(r"<[^>]+>", "", fragment)
        lines = [self._clean_text(line) for line in fragment.splitlines()]
        return "\n\n".join(line for line in lines if line).strip()

    # ---------------- 游戏页 ----------------
    # 注: 小黑盒官网已迁移为纯前端 SPA(无 __NUXT_DATA__ 可抓),
    # 参考插件的页面抓取方案已失效, 此处改为 双API 组合:
    #   小黑盒 game_introduction(简介/厂商/发行时间)
    #   + Steam appdetails(名称/截图/预告/价格)

    async def _parse_game(self, appid: str, game_type: str) -> PlatformParseResult:
        appid = appid.strip()
        if not appid:
            raise ResourceNotFoundError("无效的小黑盒游戏 appid")

        web_url = (
            f"https://www.xiaoheihe.cn/app/topic/game/"
            f"{game_type.strip().lower() or 'pc'}/{appid}"
        )
        intro = await self._fetch_game_intro(appid)
        steam = await self._fetch_steam_appdetails(appid)
        if not intro and not steam:
            raise ResourceNotFoundError(
                "小黑盒游戏数据获取失败(简介与 Steam 接口均未返回数据)"
            )

        title = self._build_game_title(steam, appid)
        desc = self._build_game_desc(intro, steam)
        image_urls = self._extract_game_images(steam)
        video_urls = self._extract_game_videos(steam)
        if not desc and not image_urls and not video_urls:
            raise ResourceNotFoundError(f"未找到该游戏(appid: {appid})的可用数据")

        contents = [
            PlatformMedia(type=MediaType.IMAGE, url=img, headers=_HEADERS)
            for img in image_urls
        ]
        stats_parts = []

        for video_url in video_urls[:1]:
            media = await self._build_video_media(video_url)
            if media:
                contents.append(media)
            else:
                stats_parts.append("预告片下载失败已跳过")

        return PlatformParseResult(
            platform=self.PLATFORM_NAME,
            platform_display=self.DISPLAY_NAME,
            title=title,
            text=desc,
            url=web_url,
            contents=contents,
            extra_text="，".join(stats_parts) or None,
        )

    async def _fetch_game_intro(self, appid: str) -> dict[str, Any]:
        """小黑盒游戏简介接口(无需签名)"""
        try:
            payload = await self._request_json(
                "GET",
                "https://api.xiaoheihe.cn/game/game_introduction/",
                params={"steam_appid": appid, "return_json": 1},
            )
        except Exception as e:
            logger.debug(f"小黑盒游戏简介接口失败: {e}", "多平台解析")
            return {}
        if payload.get("status") != "ok":
            return {}
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    async def _fetch_steam_appdetails(self, appid: str) -> dict[str, Any]:
        """Steam 公开 appdetails 接口(中文名/截图/预告/价格), 失败返回空"""
        try:
            resp = await request_text(
                "GET",
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "l": "schinese"},
                headers={"accept": "application/json"},
                impersonate="chrome",
                timeout=15,
            )
        except Exception as e:
            logger.debug(f"Steam appdetails 请求失败: {e}", "多平台解析")
            return {}
        if resp.status_code != 200:
            return {}
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            return {}
        app_data = (data.get(appid) or {}).get("data")
        return app_data if isinstance(app_data, dict) else {}

    @staticmethod
    def _build_game_title(steam: dict[str, Any], appid: str) -> str:
        name = str(steam.get("name") or "").strip()
        return name or f"小黑盒游戏(appid: {appid})"

    def _build_game_desc(
        self, intro: dict[str, Any], steam: dict[str, Any]
    ) -> str | None:
        lines: list[str] = []

        about = self._strip_tags(
            str(intro.get("about_the_game") or steam.get("about_the_game") or "")
        )
        if about:
            lines.append(about[:1500])

        genres = steam.get("genres")
        if isinstance(genres, list):
            type_names = [
                str(g.get("description") or "").strip()
                for g in genres
                if isinstance(g, dict)
            ]
            type_names = [t for t in type_names if t]
            if type_names:
                lines.append(f"类型：{' / '.join(type_names)}")

        release_date = ""
        intro_release = intro.get("release_date")
        if isinstance(intro_release, dict):
            release_date = self._format_release_date(
                str(intro_release.get("date") or "")
            )
        elif isinstance(intro_release, str) and intro_release:
            release_date = self._format_release_date(intro_release)
        if not release_date and isinstance(steam.get("release_date"), dict):
            release_date = self._format_release_date(
                str((steam["release_date"] or {}).get("date") or "")
            )
        if release_date:
            lines.append(f"发布时间：{release_date}")

        developer = self._extract_company_text(intro.get("developers")) or ", ".join(
            steam.get("developers") or []
        )
        if developer:
            lines.append(f"开发商：{developer}")
        publisher = self._extract_company_text(intro.get("publishers")) or ", ".join(
            steam.get("publishers") or []
        )
        if publisher:
            lines.append(f"发行商：{publisher}")

        if isinstance(steam.get("price_overview"), dict):
            price_data = steam["price_overview"]
            final = str(price_data.get("final_formatted") or "").strip()
            initial = str(price_data.get("initial_formatted") or "").strip()
            if final:
                price_line = f"价格：{final}"
                if initial and initial != final:
                    price_line += f"(原价 {initial})"
                lines.append(price_line)

        return "\n\n".join(line for line in lines if line).strip() or None

    @staticmethod
    def _extract_game_images(steam: dict[str, Any]) -> list[str]:
        images: list[str] = []
        seen: set[str] = set()
        screenshots = steam.get("screenshots")
        if isinstance(screenshots, list):
            for item in screenshots:
                if isinstance(item, dict):
                    url = str(item.get("path_full") or "").strip()
                    if url and url not in seen:
                        seen.add(url)
                        images.append(url)
        if not images and steam.get("header_image"):
            header = str(steam.get("header_image") or "").strip()
            if header:
                images.append(header)
        return images

    @staticmethod
    def _extract_game_videos(steam: dict[str, Any]) -> list[str]:
        movies = steam.get("movies")
        if not isinstance(movies, list):
            return []
        for item in movies:
            if not isinstance(item, dict):
                continue
            mp4 = item.get("mp4") or {}
            if isinstance(mp4, dict):
                url = str(mp4.get("480") or mp4.get("max") or "").strip()
                if url:
                    return [url]
            webm = item.get("webm") or {}
            if isinstance(webm, dict):
                url = str(webm.get("480") or webm.get("max") or "").strip()
                if url:
                    return [url]
        return []

    @staticmethod
    def _extract_company_text(items: Any) -> str:
        if not isinstance(items, list):
            return ""
        return ",".join(
            item["value"]
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("value"), str)
            and item["value"]
        )

    @staticmethod
    def _strip_tags(text: str) -> str:
        if not text:
            return ""
        value = re.sub(r"(?is)<script[^>]*>.*?</script>", "", text)
        value = re.sub(r"(?is)<style[^>]*>.*?</style>", "", value)
        value = re.sub(r"(?is)<img[^>]*>", "", value)
        value = re.sub(r"(?i)</p\s*>", "\n\n", value)
        value = re.sub(r"(?i)</div\s*>", "\n", value)
        value = re.sub(r"(?i)<li[^>]*>", "\n・", value)
        value = re.sub(r"(?i)</(ul|ol)\s*>", "\n", value)
        value = re.sub(r"(?i)</h[1-6]\s*>", "\n", value)
        value = re.sub(r"(?i)<br\s*/?>", "\n", value)
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value)
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"\n{3,}", "\n\n", value).strip()
        return value

    @staticmethod
    def _format_release_date(text: str) -> str:
        if not text:
            return ""
        value = html.unescape(text).strip()
        value = re.sub(r"\s+", "", value)
        if matched := re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$", value):
            return f"{matched.group(1)}.{int(matched.group(2))}.{int(matched.group(3))}"
        if matched := re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", value):
            return f"{matched.group(1)}.{int(matched.group(2))}.{int(matched.group(3))}"
        if matched := re.match(r"^(\d{4})年(\d{1,2})月$", value):
            return f"{matched.group(1)}.{int(matched.group(2))}"
        return text.strip()

    # ---------------- 设备指纹与签名 ----------------

    async def _get_device_ctx(self) -> dict[str, str]:
        global _device_ctx
        if _device_ctx:
            return _device_ctx

        # 优先复用配置 Cookie 中的 tokenid(避免重复匿名注册, 对齐参考实现)
        if cookie := _get_cookie():
            matched = re.search(r"(?:^|;\s*)x_xhh_tokenid=([^;]+)", cookie)
            if matched and matched.group(1).startswith("B"):
                _device_ctx = {
                    "x_xhh_tokenid": matched.group(1),
                    "device_id": "",
                }
                logger.info("小黑盒使用配置 Cookie 中的 x_xhh_tokenid", "多平台解析")
                return _device_ctx

        payload = {
            "appId": "heybox_website",
            "organization": "0yD85BjYvGFAvHaSQ1mc",
            "ep": _V4_EP,
            "data": _V4_DATA,
            "os": "web",
            "encode": 5,
            "compress": 2,
        }
        response = await self._request_json(
            "POST",
            "https://fp-it.portal101.cn/deviceprofile/v4",
            json_data=payload,
            headers={"accept": "application/json, text/plain, */*"},
        )
        device_id = str((response.get("detail") or {}).get("deviceId") or "")
        if not device_id:
            raise ResourceNotFoundError(
                f"小黑盒 deviceprofile 未返回 deviceId: {response.get('code')}"
            )
        _device_ctx = {
            "x_xhh_tokenid": f"B{device_id}",
            "device_id": device_id,
        }
        logger.info("小黑盒匿名设备指纹注册成功", "多平台解析")
        return _device_ctx

    def _sign_path(self, path: str) -> dict[str, str | int]:
        now = int(time.time())
        nonce = (
            hashlib.md5((str(now) + str(random.random())).encode())
            .hexdigest()
            .upper()
        )
        hkey = self._ov(path, now + 1, nonce)
        return {"hkey": hkey, "_time": now, "nonce": nonce}

    def _ov(self, path: str, ts: int, nonce: str) -> str:
        path = "/" + "/".join(p for p in path.split("/") if p) + "/"
        interleaved = self._interleave(
            [self._av(str(ts), -2), self._sv(path), self._sv(nonce)]
        )[:20]
        md5_hex = hashlib.md5(interleaved.encode()).hexdigest()
        prefix = self._av(md5_hex[:5], -4)
        suffix = str(
            sum(self._mix_columns([ord(c) for c in md5_hex[-6:]])) % 100
        ).zfill(2)
        return prefix + suffix

    def _av(self, text: str, cut: int) -> str:
        table = self.CHAR_TABLE[:cut]
        return "".join(table[ord(c) % len(table)] for c in text)

    def _sv(self, text: str) -> str:
        return "".join(self.CHAR_TABLE[ord(c) % len(self.CHAR_TABLE)] for c in text)

    @staticmethod
    def _interleave(parts: list[str]) -> str:
        result: list[str] = []
        max_len = max(len(part) for part in parts)
        for i in range(max_len):
            for part in parts:
                if i < len(part):
                    result.append(part[i])
        return "".join(result)

    @staticmethod
    def _xtime(value: int) -> int:
        return ((value << 1) ^ 27) & 0xFF if value & 128 else value << 1

    @classmethod
    def _mul3(cls, value: int) -> int:
        return cls._xtime(value) ^ value

    @classmethod
    def _mul6(cls, value: int) -> int:
        return cls._mul3(cls._xtime(value))

    @classmethod
    def _mul12(cls, value: int) -> int:
        return cls._mul6(cls._mul3(cls._xtime(value)))

    @classmethod
    def _mul14(cls, value: int) -> int:
        return cls._mul12(value) ^ cls._mul6(value) ^ cls._mul3(value)

    @classmethod
    def _mix_columns(cls, col: list[int]) -> list[int]:
        values = list(col)
        while len(values) < 4:
            values.append(0)
        mixed = [
            cls._mul14(values[0]) ^ cls._mul12(values[1])
            ^ cls._mul6(values[2]) ^ cls._mul3(values[3]),
            cls._mul3(values[0]) ^ cls._mul14(values[1])
            ^ cls._mul12(values[2]) ^ cls._mul6(values[3]),
            cls._mul6(values[0]) ^ cls._mul3(values[1])
            ^ cls._mul14(values[2]) ^ cls._mul12(values[3]),
            cls._mul12(values[0]) ^ cls._mul6(values[1])
            ^ cls._mul3(values[2]) ^ cls._mul14(values[3]),
        ]
        if len(values) > 4:
            mixed.extend(values[4:])
        return mixed

    # ---------------- 请求与媒体 ----------------

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        merged = headers or _build_headers()
        resp = await request_text(
            method,
            url,
            headers=merged,
            params=params,
            json_data=json_data,
            cookies=cookies,
            timeout=20,
            allow_redirects=(method.upper() == "GET"),
        )
        if resp.status_code >= 400:
            raise ResourceNotFoundError(
                f"小黑盒接口请求失败(HTTP {resp.status_code}): {url}"
            )
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise UrlParseError(f"小黑盒接口返回非 JSON: {url}") from e
        return data if isinstance(data, dict) else {}

    async def _build_video_media(self, video_url: str) -> PlatformMedia | None:
        """m3u8 经 ffmpeg 落盘, 直链走统一下载; 失败返回 None 由调用方降级"""
        max_mb = config_int("MULTI_PLATFORM_MAX_VIDEO_SIZE_MB", 100)
        try:
            if ".m3u8" in video_url.lower():
                path = await download_m3u8_to_file(
                    video_url,
                    PLUGIN_TEMP_DIR,
                    headers=_HEADERS,
                    max_size_mb=max_mb,
                )
                return PlatformMedia(
                    type=MediaType.VIDEO, url=video_url, local_path=str(path)
                )
            return PlatformMedia(
                type=MediaType.VIDEO, url=video_url, headers=_HEADERS
            )
        except (MediaTooLargeError, DownloadError) as e:
            logger.warning(f"小黑盒视频下载失败: {e}", "多平台解析")
            return None
        except Exception as e:
            logger.warning(f"小黑盒视频处理异常: {type(e).__name__}: {e}", "多平台解析")
            return None
