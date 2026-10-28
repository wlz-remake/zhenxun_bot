"""多平台解析结果统一发送器

发送顺序参考 AstrBot 万能解析插件 core/sender.py 的策略:
- 单一视频/音频且无图文: 先发媒体预览卡片图, 再发媒体文件, 不发文字;
- 其余情况: 先发送 文本+图片, 再下载并发送 视频/音频重媒体。
复用 bilibili_tools 现有的 send_video_with_retry 发送与重试能力。
"""

from datetime import datetime
from pathlib import Path

from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import Audio, Image, Segment, Text, UniMessage

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR
from ..utils.exceptions import DownloadError, ResourceNotFoundError
from ..utils.message import send_video_with_retry
from .card import render_longform_card, render_media_card
from .http_utils import (
    MediaTooLargeError,
    config_bool,
    config_int,
    download_media,
)
from .models import MediaType, PlatformMedia, PlatformParseResult


def _max_images() -> int:
    """图片数量上限(配置 MULTI_PLATFORM_MAX_IMAGE_COUNT, 0=不限制)"""
    return config_int("MULTI_PLATFORM_MAX_IMAGE_COUNT", 10, none_as_zero=True)


def _max_heavy() -> int:
    """视频/音频数量上限(配置 MULTI_PLATFORM_MAX_VIDEO_COUNT, 0=不限制)"""
    return config_int("MULTI_PLATFORM_MAX_VIDEO_COUNT", 0, none_as_zero=True)

_AUDIO_SUFFIX = {
    MediaType.AUDIO: ".mp3",
    MediaType.VIDEO: ".mp4",
    MediaType.IMAGE: ".jpg",
}

_PREFIX = {
    MediaType.AUDIO: "platform_audio_",
    MediaType.VIDEO: "platform_video_",
    MediaType.IMAGE: "platform_img_",
}


def build_result_text(result: PlatformParseResult) -> str:
    """拼装解析结果的文本头部"""
    lines = [f"【{result.platform_display}】"]
    if result.author_name:
        lines.append(f"作者：{result.author_name}")
    if result.title:
        lines.append(f"{result.title}")
    if result.text:
        lines.append(result.text)
    if result.timestamp:
        try:
            time_str = datetime.fromtimestamp(result.timestamp).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines.append(f"时间：{time_str}")
        except (ValueError, OSError, OverflowError):
            pass
    if result.extra_text:
        lines.append(result.extra_text)
    if result.url:
        lines.append(f"链接：{result.url}")
    return "\n".join(lines)


async def _download_media_file(media: PlatformMedia) -> Path:
    """下载媒体到本地缓存目录(已存在 local_path 时直接复用)"""
    if media.local_path:
        return Path(media.local_path)
    max_size_mb = config_int("MULTI_PLATFORM_MAX_VIDEO_SIZE_MB", 100)
    # 图片单独限制上限, 避免超大图占满缓存
    limit = max_size_mb if media.type != MediaType.IMAGE else min(max_size_mb, 20)
    return await download_media(
        media.url,
        PLUGIN_TEMP_DIR,
        headers=media.headers or None,
        max_size_mb=limit,
        default_suffix=_AUDIO_SUFFIX[media.type],
        prefix=_PREFIX[media.type],
    )


async def _build_image_segments(images: list[PlatformMedia]) -> list[Segment]:
    """下载图片并构建消息段列表(数量按配置截断, 失败图片跳过)"""
    max_images = _max_images()
    images_limited = images[:max_images] if max_images > 0 else images
    segments: list[Segment] = []
    for media in images_limited:
        try:
            path = await _download_media_file(media)
            segments.append(Image(path=path))
        except MediaTooLargeError:
            logger.warning(f"图片过大跳过: {media.url}", "多平台解析")
        except DownloadError as e:
            logger.warning(f"图片下载失败跳过: {e}", "多平台解析")
    if max_images > 0 and len(images) > max_images:
        segments.append(Text(f"(图片较多，仅展示前 {max_images} 张)"))
    return segments


async def _send_heavy_media(
    bot: Bot, event: Event, result: PlatformParseResult, heavy: list[PlatformMedia]
):
    """下载并发送视频/音频重媒体(数量按配置截断)"""
    max_heavy = _max_heavy()
    heavy_limited = heavy[:max_heavy] if max_heavy > 0 else heavy
    for media in heavy_limited:
        try:
            path = await _download_media_file(media)
        except MediaTooLargeError as e:
            logger.warning(f"媒体过大跳过发送: {e}", "多平台解析")
            await UniMessage(
                Text(f"⚠️ {e.message}，仅提供链接：{result.url or media.url}")
            ).send(target=event, bot=bot)
            continue
        except DownloadError as e:
            logger.warning(f"媒体下载失败: {e}", "多平台解析")
            await UniMessage(Text("⚠️ 媒体下载失败，可点击链接查看原内容")).send(
                target=event, bot=bot
            )
            continue

        if media.type == MediaType.VIDEO:
            try:
                await send_video_with_retry(bot, event, path)
            except DownloadError as e:
                logger.warning(f"视频发送失败: {e}", "多平台解析")
                await UniMessage(Text("⚠️ 视频发送失败，可点击链接查看原内容")).send(
                    target=event, bot=bot
                )
        else:
            try:
                await UniMessage(Audio(path=str(path))).send(target=event, bot=bot)
            except Exception as e:
                logger.warning(f"音频发送失败: {e}", "多平台解析")
                await UniMessage(Text("⚠️ 音频发送失败，可点击链接查看原内容")).send(
                    target=event, bot=bot
                )

    if max_heavy > 0 and len(heavy) > max_heavy:
        await UniMessage(
            Text(f"(媒体较多，仅发送前 {max_heavy} 个视频/音频)")
        ).send(target=event, bot=bot)


async def send_platform_parse_result(
    bot: Bot, event: Event, result: PlatformParseResult
):
    """发送多平台解析结果

    长文(知乎回答/问题): 只发送长文卡片(及内嵌视频);
    图集(小红书图文等): 摘要卡片与图片合为一条消息;
    单一视频/音频: 媒体预览卡片图先行, 随后发送媒体(不发送文字);
    其余情况: 文本+图片先行, 视频音频随后。

    Raises:
        解析内容为空或文本消息发送失败时向上抛出异常, 由调用方统一回滚缓存。
    """
    header_text = build_result_text(result)
    heavy = [*result.videos, *result.audios]
    images = result.images

    if not (result.text or result.title or images or heavy):
        # 与整体设计保持一致: 空结果以异常上抛, 由调用方回滚缓存并记录日志
        # (header_text 恒含平台名前缀, 不能作为内容判空依据)
        raise ResourceNotFoundError(
            f"{result.platform_display} 解析结果为空, 未找到可发送的内容"
        )

    # 1) 长文卡片模式(知乎回答/问题): 只发卡片(及内嵌视频), 不发文字与图片
    if result.card_only and config_bool("MULTI_PLATFORM_RENDER_MEDIA_CARD", True):
        card_path: Path | None = None
        try:
            card_path = await render_longform_card(result)
        except Exception as e:
            logger.warning(
                f"长文卡片渲染失败, 回退文字模式: {type(e).__name__}: {e}", "多平台解析"
            )
        if card_path:
            await UniMessage(Image(path=card_path)).send(target=event, bot=bot)
            return await _send_heavy_media(bot, event, result, heavy)
        # 渲染失败继续走下方文字+图片分支

    # 2) 图集模式(小红书图文等): 摘要卡片与图片合为一条消息发送, 不发文字
    #    (card_only/card_blocks 的长文结果不适用, 正文仍走原文字分支)
    if (
        images
        and not heavy
        and not (result.card_only or result.card_blocks)
        and config_bool("MULTI_PLATFORM_RENDER_MEDIA_CARD", True)
    ):
        card_path = None
        try:
            card_path = await render_longform_card(result)
        except Exception as e:
            logger.warning(
                f"图集卡片渲染失败, 回退文字模式: {type(e).__name__}: {e}", "多平台解析"
            )
        if card_path:
            segments: list[Segment] = [
                Image(path=card_path),
                *(await _build_image_segments(images)),
            ]
            await UniMessage(segments).send(target=event, bot=bot)
            return
        # 渲染失败继续走下方文字+图片分支

    # 3) 单一视频/音频: 先发媒体预览卡片, 不发文字(渲染失败回退文字模式)
    if (
        len(heavy) == 1
        and not images
        and config_bool("MULTI_PLATFORM_RENDER_MEDIA_CARD", True)
    ):
        card_path = None
        try:
            card_path = await render_media_card(result, heavy[0])
        except Exception as e:
            logger.warning(
                f"媒体卡片渲染失败, 回退文字模式: {type(e).__name__}: {e}", "多平台解析"
            )
        if card_path:
            await UniMessage(Image(path=card_path)).send(target=event, bot=bot)
        else:
            await UniMessage(Text(header_text)).send(target=event, bot=bot)
        return await _send_heavy_media(bot, event, result, heavy)

    # 4) 文本 + 图片
    segments: list[Segment] = [
        Text(header_text),
        *(await _build_image_segments(images)),
    ]

    await UniMessage(segments).send(target=event, bot=bot)

    # 5) 视频 / 音频
    await _send_heavy_media(bot, event, result, heavy)
