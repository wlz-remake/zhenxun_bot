"""多平台解析统一数据模型

参考 AstrBot 万能解析插件 (astrbot_plugin_parser) 的 ParseResult/MediaContent 抽象,
简化为扁平结构以适配 bilibili_tools 现有的消息构建/发送流程。
"""

from enum import Enum

from pydantic import BaseModel, Field


class MediaType(str, Enum):
    """媒体内容类型"""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"


class PlatformMedia(BaseModel):
    """单个媒体内容(图片/视频/音频)"""

    type: MediaType
    """媒体类型"""
    url: str
    """媒体下载直链"""
    cover_url: str | None = None
    """视频封面(媒体卡片渲染使用)"""
    duration: float = 0
    """时长(秒), 0 表示未知"""
    headers: dict[str, str] = Field(default_factory=dict)
    """下载该媒体所需的请求头(如 Referer 防盗链), 为空时使用通用 UA"""
    local_path: str | None = None
    """本地已落盘文件路径(如需特殊下载流程的平台), 存在时跳过下载"""


class PlatformParseResult(BaseModel):
    """平台无关的统一解析结果

    与 B 站专用的 VideoInfo/LiveInfo 等模型解耦,
    多平台解析器只需产出该模型, 即可由统一的 sender 完成消息构建与发送。
    """

    platform: str
    """平台标识, 如 douyin/weibo/xhs"""
    platform_display: str
    """平台显示名, 如 抖音/微博/小红书"""
    title: str | None = None
    """标题"""
    text: str | None = None
    """正文/描述"""
    author_name: str | None = None
    """作者名"""
    author_avatar: str | None = None
    """作者头像(媒体卡片渲染使用)"""
    timestamp: int | None = None
    """发布时间戳(秒), 未知为 None"""
    url: str | None = None
    """内容来源页链接"""
    contents: list[PlatformMedia] = Field(default_factory=list)
    """媒体内容列表"""
    extra_text: str | None = None
    """附加说明文本(统计信息等)"""
    card_only: bool = False
    """为 True 时发送器只发送渲染卡片(知乎回答等长文场景), 不再发送文字与图片"""
    card_blocks: list[tuple[str, str]] = Field(default_factory=list)
    """有序内容块 [("text", 文本) | ("image", 图片URL)], 供长文卡片按原文顺序渲染"""

    @property
    def images(self) -> list[PlatformMedia]:
        return [c for c in self.contents if c.type == MediaType.IMAGE]

    @property
    def videos(self) -> list[PlatformMedia]:
        return [c for c in self.contents if c.type == MediaType.VIDEO]

    @property
    def audios(self) -> list[PlatformMedia]:
        return [c for c in self.contents if c.type == MediaType.AUDIO]
