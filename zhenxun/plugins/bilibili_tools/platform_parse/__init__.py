"""多平台链接解析子包

参考 AstrBot 万能解析插件 (astrbot_plugin_parser, 其核心移植自 nonebot-plugin-parser)
在 bilibili_tools 内扩展出 B 站之外的多平台内容解析能力:

- 统一的 PlatformParseResult 模型与 sender 发送流程
- 解析入口复用插件现有的 解析锁(parse_queue) / URL去重缓存(CacheService)
- 基础平台无需额外依赖; 知乎/小黑盒建议安装 curl_cffi(TLS 指纹伪装)
- m3u8 视频(HLS)复用插件已有的 ffmpeg 系统依赖处理, 无需 yt-dlp

B 站自身的解析能力(订阅/视频卡片/AI总结/自动下载)不受影响, 仍走原链路。
"""

from .acfun import AcfunParser
from .base import (
    MAX_REDIRECT_HOPS,
    PlatformParser,
    find_parser,
    get_all_parsers,
    get_parser_by_name,
    register_parser,
)
from .douyin import DouyinParser
from .kuaishou import KuaishouParser
from .models import MediaType, PlatformMedia, PlatformParseResult
from .ncm import NcmParser
from .sender import build_result_text, send_platform_parse_result
from .service import PlatformParseService
from .shipinhao import ShipinhaoParser
from .twitter import TwitterParser
from .weibo import WeiboParser
from .xhs import XhsParser
from .xiaoheihe import XiaoheiheParser
from .zhihu import ZhihuParser


def _register_all():
    """注册全部内置平台解析器(按 PRIORITY 升序匹配)"""
    for parser_cls in (
        DouyinParser,
        WeiboParser,
        XhsParser,
        KuaishouParser,
        NcmParser,
        AcfunParser,
        TwitterParser,
        ZhihuParser,
        ShipinhaoParser,
        XiaoheiheParser,
    ):
        register_parser(parser_cls())


_register_all()

__all__ = [
    "MAX_REDIRECT_HOPS",
    "MediaType",
    "PlatformMedia",
    "PlatformParseResult",
    "PlatformParseService",
    "PlatformParser",
    "build_result_text",
    "find_parser",
    "get_all_parsers",
    "get_parser_by_name",
    "send_platform_parse_result",
]
