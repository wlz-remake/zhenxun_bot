"""多平台解析器抽象基类与注册表

借鉴 AstrBot 万能解析插件 BaseParser 的 "关键词 + 正则" 双重匹配路由:
- KEY_PATTERNS 中的关键词用于快速过滤(为空时跳过关键词检查)
- 正则命中后由 parse() 完成该平台的具体解析
"""

from abc import ABC, abstractmethod
import re
from typing import ClassVar

from zhenxun.services.log import logger

from .models import PlatformParseResult

MAX_REDIRECT_HOPS = 5
"""短链重定向解析的最大跳数, 防止平台返回循环重定向导致无限递归"""


class PlatformParser(ABC):
    """平台解析器基类

    子类必须定义 PLATFORM_NAME/DISPLAY_NAME/KEY_PATTERNS 并实现 parse()。
    解析器实例为无状态单例( DouyinParser 的 ttwid 缓存为模块级, 线程安全够用),
    由 registry 统一创建与检索。
    """

    PLATFORM_NAME: ClassVar[str] = ""
    """平台标识, 如 douyin"""
    DISPLAY_NAME: ClassVar[str] = ""
    """平台显示名, 如 抖音"""
    PRIORITY: ClassVar[int] = 100
    """解析优先级, 数值越小越先匹配"""
    KEY_PATTERNS: ClassVar[list[tuple[str, re.Pattern[str]]]] = []
    """(关键词, 正则) 列表, 关键词为空字符串时仅用正则匹配"""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.PLATFORM_NAME:
            raise ValueError(f"解析器 {cls.__name__} 未定义 PLATFORM_NAME")
        if not cls.KEY_PATTERNS:
            raise ValueError(f"解析器 {cls.__name__} 未定义 KEY_PATTERNS")

    @classmethod
    def match(cls, text: str) -> re.Match[str] | None:
        """检查文本是否命中该平台的任一 (关键词, 正则) 模式"""
        for keyword, pattern in cls.KEY_PATTERNS:
            if keyword and keyword not in text:
                continue
            if matched := pattern.search(text):
                return matched
        return None

    @abstractmethod
    async def parse(
        self, url: str, matched: re.Match[str], _hop: int = 0
    ) -> PlatformParseResult:
        """解析命中的链接, 返回统一解析结果

        Args:
            url: 命中的链接文本(可能无协议前缀, 由解析器自行补全)
            matched: 规则匹配阶段的正则 Match 对象(可提取命名分组)
            _hop: 短链重定向跳数(内部使用), 超过 MAX_REDIRECT_HOPS 视为循环
        """
        raise NotImplementedError


_registry: list[PlatformParser] = []


def register_parser(parser: PlatformParser):
    """注册解析器实例(按 PRIORITY 升序排列)"""
    if not any(isinstance(p, type(parser)) for p in _registry):
        _registry.append(parser)
        _registry.sort(key=lambda p: p.PRIORITY)
        logger.debug(
            f"注册多平台解析器: {parser.DISPLAY_NAME}({parser.PLATFORM_NAME})",
            "多平台解析",
        )


def get_all_parsers() -> list[PlatformParser]:
    """获取全部已注册解析器"""
    return list(_registry)


def get_parser_by_name(platform_name: str) -> PlatformParser | None:
    """按平台标识获取解析器"""
    for parser in _registry:
        if parser.PLATFORM_NAME == platform_name:
            return parser
    return None


def find_parser(text: str) -> tuple[PlatformParser, re.Match[str]] | None:
    """遍历注册表, 返回第一个命中文本的解析器及其 Match"""
    for parser in _registry:
        if matched := parser.match(text):
            return parser, matched
    return None
