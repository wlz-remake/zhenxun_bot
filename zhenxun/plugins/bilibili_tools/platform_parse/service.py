"""多平台解析服务: URL 提取 + 解析器分发"""

import re

from nonebot_plugin_alconna.uniseg import Hyper

from zhenxun.services.log import logger

from ..utils.exceptions import BilibiliBaseException

# 与 bilibili 提取器保持一致的小程序排除列表(系统卡片等无解析价值)
_EXCLUDED_APPS = [
    "com.tencent.qun.invite",
    "com.tencent.qqav.groupvideo",
    "com.tencent.mobileqq.reading",
    "com.tencent.weather",
]

_GENERIC_URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+")

_ESCAPE_SLASH_PATTERN = re.compile(r"\\+/")


def extract_platform_candidate_urls(hyper_raw: str) -> list[str]:
    r"""从 Hyper(小程序/JSON卡片) 原始数据中提取候选链接

    卡片 JSON 中的 URL 通常带有 \/ 转义(甚至多重转义), 需先还原再匹配。
    """
    if not hyper_raw:
        return []
    if any(app_name in hyper_raw for app_name in _EXCLUDED_APPS):
        return []

    candidates: list[str] = []
    # qqdocurl 字段常被二次编码, 优先还原
    if match := re.search(r'"qqdocurl"\s*:\s*"([^"]+)"', hyper_raw):
        decoded = match.group(1).replace("\\", "")
        candidates.append(decoded)

    normalized = _ESCAPE_SLASH_PATTERN.sub("/", hyper_raw)
    candidates.extend(
        url.rstrip("\\") for url in _GENERIC_URL_PATTERN.findall(normalized)
    )
    return candidates


class PlatformParseService:
    """多平台解析统一入口"""

    @staticmethod
    def find_from_message(message, check_hyper: bool = True):
        """从消息(UniMsg)中查找可解析的多平台链接

        B 站链接由专用提取器优先处理, 此处仅匹配其余平台。

        Returns:
            (解析器实例, 正则 Match) 或 None
        """
        from .base import find_parser

        if check_hyper:
            for seg in message:
                if isinstance(seg, Hyper) and seg.raw:
                    for url in extract_platform_candidate_urls(seg.raw):
                        # 排除已被 B 站提取器处理的域名, 避免重复解析
                        if "bilibili.com" in url or "b23.tv" in url:
                            continue
                        if found := find_parser(url):
                            logger.debug(
                                f"从小程序/卡片中识别到 "
                                f"{found[0].DISPLAY_NAME} 链接: {url}",
                                "多平台解析",
                            )
                            return found

        plain_text = message.extract_plain_text().strip()
        if plain_text and (found := find_parser(plain_text)):
            logger.debug(
                f"从消息文本中识别到 {found[0].DISPLAY_NAME} 链接: {found[1].group(0)}",
                "多平台解析",
            )
            return found

        return None

    @staticmethod
    async def parse(parser, matched: re.Match[str]):
        """执行平台解析

        插件自有异常(含平台解析器主动抛出的资源不存在/下载失败等)原样上抛,
        其余底层异常(如 httpx 网络错误)统一包装为 UrlParseError 便于上层处理。
        """
        from ..utils.exceptions import UrlParseError

        try:
            return await parser.parse(matched.group(0), matched)
        except BilibiliBaseException:
            raise
        except Exception as e:
            raise UrlParseError(
                f"{parser.DISPLAY_NAME} 解析失败: {type(e).__name__}: {e}",
                cause=e,
                context={"matched": matched.group(0)},
            ) from e
