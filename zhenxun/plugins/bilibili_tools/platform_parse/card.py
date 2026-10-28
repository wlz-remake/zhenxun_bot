"""多平台解析媒体卡片渲染

对齐 AstrBot 万能解析插件 core/sender.py + render.py 的 "预览卡片" 策略:
单一视频/音频的解析结果先渲染一张卡片图(头像+作者+时间+平台+标题+封面+正文+统计)
单独发送, 随后直接发送媒体文件, 不再发送文字消息。

渲染基于 nonebot_plugin_htmlrender(html 截图), 该依赖在 NoneBot 未初始化的
环境(如测试脚本)中不可导入, 因此采用函数内懒加载, 失败时由调用方回退文字模式。
"""

import asyncio
import base64
from datetime import datetime
import html as html_lib
import io
from pathlib import Path

import aiofiles
from PIL import Image as PILImage

from zhenxun.services.log import logger

from ..config import PLUGIN_TEMP_DIR
from .http_utils import config_int, download_media, generate_file_name
from .models import MediaType, PlatformMedia, PlatformParseResult

CARD_WIDTH = 800
"""卡片宽度(px), 对齐 AstrBot Renderer.DEFAULT_CARD_WIDTH"""

_AVATAR_SIZE = 160
"""头像渲染尺寸(80 css px * 2 倍缩放)"""

_MAX_COVER_WIDTH = 1500
_MAX_COVER_HEIGHT = 1600
"""封面渲染尺寸上限(750 css px 宽 / 800 css px 高 * 2 倍缩放)"""


async def _download_as_data_uri(
    url: str,
    headers: dict[str, str] | None,
    *,
    avatar: bool = False,
) -> str | None:
    """下载图片素材并转为 data URI(超宽图缩放, 头像裁方), 失败返回 None"""
    try:
        path = await download_media(
            url,
            PLUGIN_TEMP_DIR,
            headers=headers,
            max_size_mb=20,
            default_suffix=".jpg",
            prefix="platform_img_",
        )
        return await asyncio.to_thread(_image_to_data_uri, path, avatar)
    except Exception as e:
        logger.debug(f"卡片素材下载失败, 卡片中将省略该图: {url[:100]} ({e})")
        return None


def _image_to_data_uri(path: Path, avatar: bool) -> str | None:
    """读取本地图片, 归一化为合适尺寸的 JPEG 后编码为 data URI"""
    try:
        with PILImage.open(path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                # 透明区合成白底, 避免转 RGB 后变黑
                img = img.convert("RGBA")
                background = PILImage.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert("RGB")
            if avatar:
                width, height = img.size
                side = min(width, height)
                img = img.crop(
                    (
                        (width - side) // 2,
                        (height - side) // 2,
                        (width + side) // 2,
                        (height + side) // 2,
                    )
                ).resize((_AVATAR_SIZE, _AVATAR_SIZE), PILImage.Resampling.LANCZOS)
            else:
                img.thumbnail(
                    (_MAX_COVER_WIDTH, _MAX_COVER_HEIGHT), PILImage.Resampling.LANCZOS
                )
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=88)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.debug(f"卡片素材读取失败: {path.name} ({e})")
        return None


def _build_card_html(
    *,
    platform: str,
    author_name: str,
    avatar_uri: str | None,
    time_str: str,
    title: str,
    cover_uri: str | None,
    is_video: bool,
    text: str,
    extra_text: str,
) -> str:
    """构建卡片页面 HTML(纯函数, 便于脱离浏览器环境测试)"""
    esc = html_lib.escape

    avatar_html = (
        f'<img class="avatar" src="{avatar_uri}" alt="">'
        if avatar_uri
        else (
            f'<div class="avatar avatar-placeholder">'
            f"{esc(author_name[:1] if author_name else platform[:1])}</div>"
        )
    )
    time_html = f'<div class="time">{esc(time_str)}</div>' if time_str else ""
    title_html = f'<div class="title">{esc(title)}</div>' if title else ""

    cover_html = ""
    if cover_uri:
        badge = (
            '<div class="play-btn"><div class="play-triangle"></div></div>'
            if is_video
            else '<div class="music-btn">♪</div>'
        )
        cover_html = f'<div class="cover">{badge}<img src="{cover_uri}" alt=""></div>'

    text_html = f'<div class="text">{esc(text)}</div>' if text else ""
    extra_html = (
        f'<div class="extra">{esc(extra_text)}</div>' if extra_text else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: {CARD_WIDTH}px;
    background: #ffffff;
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    padding: 25px;
  }}
  .header {{ display: flex; align-items: center; gap: 15px; }}
  .avatar {{
    width: 80px; height: 80px; border-radius: 50%;
    object-fit: cover; flex-shrink: 0;
  }}
  .avatar-placeholder {{
    display: flex; align-items: center; justify-content: center;
    background: #d6dbe4; color: #8a93a3; font-size: 34px;
  }}
  .author-box {{ flex: 1; min-width: 0; }}
  .author {{
    color: #007aff; font-size: 28px; line-height: 1.3;
    word-break: break-all;
  }}
  .time {{ color: #888888; font-size: 22px; margin-top: 5px; }}
  .platform {{
    flex-shrink: 0; background: #333333; color: #ffffff;
    font-size: 20px; padding: 6px 16px; border-radius: 14px;
  }}
  .title {{
    color: #333333; font-size: 30px; line-height: 1.4;
    margin-top: 18px; word-break: break-all;
  }}
  .cover {{
    position: relative; margin-top: 18px;
    width: 750px; border-radius: 8px; overflow: hidden;
  }}
  .cover img {{ display: block; width: 750px; height: auto; }}
  .play-btn {{
    position: absolute; left: 50%; top: 50%;
    width: 128px; height: 128px; margin: -64px 0 0 -64px;
    background: rgba(0, 0, 0, 0.35); border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
  }}
  .play-triangle {{
    width: 0; height: 0; margin-left: 12px;
    border-top: 28px solid transparent;
    border-bottom: 28px solid transparent;
    border-left: 46px solid rgba(255, 255, 255, 0.9);
  }}
  .music-btn {{
    position: absolute; left: 50%; top: 50%;
    width: 128px; height: 128px; margin: -64px 0 0 -64px;
    background: rgba(0, 0, 0, 0.35); border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    color: rgba(255, 255, 255, 0.9); font-size: 64px;
  }}
  .text {{
    color: #333333; font-size: 24px; line-height: 1.6;
    margin-top: 18px; white-space: pre-wrap; word-break: break-all;
  }}
  .extra {{ color: #888888; font-size: 22px; margin-top: 15px; }}
</style>
</head>
<body>
  <div class="header">
    {avatar_html}
    <div class="author-box">
      <div class="author">{esc(author_name)}</div>
      {time_html}
    </div>
    <div class="platform">{esc(platform)}</div>
  </div>
  {title_html}
  {cover_html}
  {text_html}
  {extra_html}
</body>
</html>"""


def _format_time(timestamp: int | None) -> str:
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


async def render_media_card(
    result: PlatformParseResult, media: PlatformMedia
) -> Path | None:
    """渲染单一媒体的预览卡片

    Args:
        result: 解析结果(取作者/标题/正文/统计等元信息)
        media: 唯一的视频/音频媒体(取封面与下载请求头)

    Returns:
        渲染成功的卡片图片路径; 素材全空或渲染环境不可用时返回 None
    """
    try:
        from nonebot_plugin_htmlrender import html_to_pic
    except Exception as e:
        logger.warning(f"htmlrender 不可用, 无法渲染媒体卡片: {e}", "多平台解析")
        return None

    cover_uri = (
        await _download_as_data_uri(media.cover_url, media.headers)
        if media.cover_url
        else None
    )
    avatar_uri = (
        await _download_as_data_uri(result.author_avatar, media.headers, avatar=True)
        if result.author_avatar
        else None
    )
    if not (cover_uri or avatar_uri or result.title or result.text):
        return None

    page_html = _build_card_html(
        platform=result.platform_display,
        author_name=result.author_name or "",
        avatar_uri=avatar_uri,
        time_str=_format_time(result.timestamp),
        title=result.title or "",
        cover_uri=cover_uri,
        is_video=media.type == MediaType.VIDEO,
        text=result.text or "",
        extra_text=result.extra_text or "",
    )
    png = await html_to_pic(
        page_html,
        wait=0,
        device_scale_factor=2,
        # 视口高度取小值: 整页截图高度下限为视口高, 视口过高会在内容
        # 短于视口时在卡片底部留出大片空白
        viewport={"width": CARD_WIDTH, "height": 32},
    )

    PLUGIN_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    card_path = PLUGIN_TEMP_DIR / (
        f"platform_card_{generate_file_name(result.url or result.platform, '.png')}"
    )
    async with aiofiles.open(card_path, "wb") as f:
        await f.write(png)
    return card_path


# ---------------- 长文卡片(知乎回答等) ----------------


def _build_longform_html(
    *,
    platform: str,
    author_name: str,
    avatar_uri: str | None,
    time_str: str,
    title: str,
    blocks: list[tuple[str, str]],
    footer_notes: list[str],
    extra_text: str,
) -> str:
    """构建长文卡片页面 HTML: 头部 + 问题标题 + 按原文顺序的正文/图片 + 统计"""
    esc = html_lib.escape

    avatar_html = (
        f'<img class="avatar" src="{avatar_uri}" alt="">'
        if avatar_uri
        else (
            f'<div class="avatar avatar-placeholder">'
            f"{esc(author_name[:1] if author_name else platform[:1])}</div>"
        )
    )
    time_html = f'<div class="time">{esc(time_str)}</div>' if time_str else ""
    title_html = f'<div class="question">{esc(title)}</div>' if title else ""

    body_parts: list[str] = []
    for kind, value in blocks:
        if kind == "image":
            src = html_lib.escape(value, quote=True)
            body_parts.append(f'<img class="body-img" src="{src}" alt="">')
        else:
            body_parts.append(f"<p>{esc(value)}</p>")
    body_html = "\n".join(body_parts)

    notes = [*footer_notes, *([extra_text] if extra_text else [])]
    footer_html = (
        f'<div class="extra">{esc(" · ".join(notes))}</div>' if notes else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    width: {CARD_WIDTH}px;
    background: #ffffff;
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    padding: 25px;
  }}
  .header {{ display: flex; align-items: center; gap: 15px; }}
  .avatar {{
    width: 80px; height: 80px; border-radius: 50%;
    object-fit: cover; flex-shrink: 0;
  }}
  .avatar-placeholder {{
    display: flex; align-items: center; justify-content: center;
    background: #d6dbe4; color: #8a93a3; font-size: 34px;
  }}
  .author-box {{ flex: 1; min-width: 0; }}
  .author {{
    color: #007aff; font-size: 28px; line-height: 1.3; word-break: break-all;
  }}
  .time {{ color: #888888; font-size: 22px; margin-top: 5px; }}
  .platform {{
    flex-shrink: 0; background: #056de8; color: #ffffff;
    font-size: 20px; padding: 6px 16px; border-radius: 14px;
  }}
  .question {{
    color: #1a1a1a; font-size: 32px; font-weight: 600; line-height: 1.4;
    margin-top: 20px; padding-left: 16px; border-left: 8px solid #056de8;
    word-break: break-all;
  }}
  .body {{ margin-top: 20px; }}
  .body p {{
    color: #333333; font-size: 24px; line-height: 1.7;
    margin-bottom: 18px; white-space: pre-wrap; word-break: break-all;
  }}
  .body-img {{
    display: block; width: 750px; height: auto;
    border-radius: 8px; margin: 6px 0 20px;
  }}
  .extra {{ color: #888888; font-size: 22px; margin-top: 15px; }}
</style>
</head>
<body>
  <div class="header">
    {avatar_html}
    <div class="author-box">
      <div class="author">{esc(author_name)}</div>
      {time_html}
    </div>
    <div class="platform">{esc(platform)}</div>
  </div>
  {title_html}
  <div class="body">
{body_html}
  </div>
  {footer_html}
</body>
</html>"""


async def render_longform_card(result: PlatformParseResult) -> Path | None:
    """渲染长文卡片(知乎回答/问题): 头部 + 问题标题 + 按序正文与图片 + 统计

    图片内嵌卡片(数量上限配置 MULTI_PLATFORM_CARD_MAX_IMAGES, 0=不限制),
    超出上限或加载失败的图片在卡片底部以文字提示。

    Returns:
        卡片图片路径(JPEG); 无可渲染内容或渲染环境不可用时返回 None
    """
    try:
        from nonebot_plugin_htmlrender import html_to_pic
    except Exception as e:
        logger.warning(f"htmlrender 不可用, 无法渲染长文卡片: {e}", "多平台解析")
        return None

    blocks = result.card_blocks or (
        [("text", result.text)] if result.text else []
    )
    if not blocks:
        return None

    avatar_uri = (
        await _download_as_data_uri(result.author_avatar, None, avatar=True)
        if result.author_avatar
        else None
    )

    max_images = config_int("MULTI_PLATFORM_CARD_MAX_IMAGES", 0, none_as_zero=True)
    image_total = sum(1 for kind, _ in blocks if kind == "image")
    # 图片优先用解析器提供的下载请求头(防盗链场景), 拿不到再裸下载
    headers_by_url = {m.url: (m.headers or None) for m in result.images}
    rendered: list[tuple[str, str]] = []
    included = skipped = 0
    for kind, value in blocks:
        if kind != "image":
            rendered.append((kind, value))
            continue
        if max_images > 0 and included >= max_images:
            continue
        uri = await _download_as_data_uri(value, headers_by_url.get(value))
        if uri is None:
            skipped += 1
            continue
        rendered.append(("image", uri))
        included += 1

    if not rendered:
        # 全部图片加载失败且无文字块: 不产出近空白卡片,
        # 返回 None 让发送器回退文字+图片分支(可用平台请求头重试下载)
        return None

    footer_notes: list[str] = []
    if max_images > 0 and (omitted := image_total - included - skipped) > 0:
        footer_notes.append(f"其余 {omitted} 张图片未展示")
    if skipped:
        footer_notes.append(f"{skipped} 张图片加载失败")

    page_html = _build_longform_html(
        platform=result.platform_display,
        author_name=result.author_name or "",
        avatar_uri=avatar_uri,
        time_str=_format_time(result.timestamp),
        title=result.title or "",
        blocks=rendered,
        footer_notes=footer_notes,
        extra_text=result.extra_text or "",
    )
    # 长文卡片含大量照片, 用 JPEG 控制体积(超长 PNG 可能超出 QQ 图片大小限制)
    image_bytes = await html_to_pic(
        page_html,
        wait=0,
        device_scale_factor=2,
        # 同上: 小视口高避免卡片底部空白
        viewport={"width": CARD_WIDTH, "height": 32},
        type="jpeg",
        quality=88,
    )

    PLUGIN_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    card_path = PLUGIN_TEMP_DIR / (
        f"platform_card_{generate_file_name(result.url or result.platform, '.jpg')}"
    )
    async with aiofiles.open(card_path, "wb") as f:
        await f.write(image_bytes)
    return card_path
