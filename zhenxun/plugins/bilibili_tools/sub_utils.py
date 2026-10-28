import asyncio
import traceback
from pathlib import Path

import aiofiles
import httpx
from bilibili_api import Credential as BilibiliCredential
from bilibili_api import live as bilibili_live_module
from bilibili_api import user as bilibili_user_module
from nonebot_plugin_htmlrender import get_new_page

from zhenxun.services.log import logger

from .config import AVATAR_CACHE_DIR, get_credential
from .utils.headers import get_bilibili_headers

_screenshot_semaphore = asyncio.Semaphore(3)


def build_bilibili_playwright_cookies() -> list[dict]:
    """从当前凭证构建 Playwright 格式的 Cookie 列表

    Returns:
        Cookie 列表，如未登录则返回空列表
    """
    credential = get_credential()
    if not credential:
        return []
    cookies_dict = credential.get_cookies()
    if not cookies_dict:
        return []
    return [
        {"name": k, "value": v, "domain": ".bilibili.com", "path": "/"}
        for k, v in cookies_dict.items()
    ]


_DYNAMIC_TYPE_MAP = {
    "DYNAMIC_TYPE_FORWARD": 1,
    "DYNAMIC_TYPE_DRAW": 2,
    "DYNAMIC_TYPE_WORD": 4,
    "DYNAMIC_TYPE_AV": 8,
    "DYNAMIC_TYPE_ARTICLE": 64,
    "DYNAMIC_TYPE_MUSIC": 256,
    "DYNAMIC_TYPE_LIVE": 2048,
    "DYNAMIC_TYPE_LIVE_RCMD": 4200,
    "DYNAMIC_TYPE_PGC": 4300,
    "DYNAMIC_TYPE_UGC_SEASON": 4300,
    "DYNAMIC_TYPE_COMMON_SQUARE": 4300,
    "DYNAMIC_TYPE_MEDIALIST": 4300,
    "DYNAMIC_TYPE_COURSES_SEASON": 4300,
}


def _reconstruct_card(
    dynamic_type: str, module_dynamic: dict | None, module_author: dict | None
) -> dict:
    """从新版动态模块重构旧版card字段，以兼容现有代码"""
    card: dict = {}
    module_dynamic = module_dynamic or {}
    module_author = module_author or {}

    desc_text = ""
    desc_obj = module_dynamic.get("desc")
    if isinstance(desc_obj, dict):
        desc_text = desc_obj.get("text", "")

    # 提取描述中的所有跳转链接URL（用于广告过滤检测）
    # B站新版动态的 desc.text 为纯文本，外部推广链接的URL仅在 rich_text_nodes.jump_url 中，
    # 若不单独提取，广告过滤将无法检测到文本里隐藏的带货链接
    desc_jump_urls = []
    if isinstance(desc_obj, dict):
        for node in desc_obj.get("rich_text_nodes") or []:
            if isinstance(node, dict):
                jump_url = node.get("jump_url", "")
                if jump_url:
                    desc_jump_urls.append(jump_url)

    # 提取附加商品卡片信息（带货广告的推广链接常在此处，且此时 desc 可能为空）
    # additional.type == "ADDITIONAL_TYPE_GOODS" 表示“UP主的推荐”商品卡片，
    # 其 items[].jump_url 通常指向淘宝客等外部电商推广链接
    additional_goods = None
    additional_obj = module_dynamic.get("additional")
    if isinstance(additional_obj, dict):
        goods = additional_obj.get("goods")
        if isinstance(goods, dict):
            additional_goods = {
                "type": additional_obj.get("type", ""),
                "head_text": goods.get("head_text", ""),
                "items": [],
            }
            for g_item in goods.get("items") or []:
                if isinstance(g_item, dict):
                    additional_goods["items"].append(
                        {
                            "name": g_item.get("name", ""),
                            "price": g_item.get("price", ""),
                            "jump_url": g_item.get("jump_url", ""),
                        }
                    )

    major = module_dynamic.get("major") or module_dynamic.get("module_dynamic_major")
    major_type = major.get("type", "") if major else ""

    # 提取图片：新版API中图片可能在 major.draw.items[].src 或 major.opus.pics[].url
    pictures = []
    if major:
        # draw 类型图片
        draw = major.get("draw") or {}
        for draw_item in draw.get("items") or []:
            if isinstance(draw_item, dict):
                src = draw_item.get("src", "")
                if src:
                    pictures.append(
                        {
                            "img_src": src,
                            "img_width": draw_item.get("width", 0),
                            "img_height": draw_item.get("height", 0),
                        }
                    )
        # opus 类型图片（图文动态）
        opus = major.get("opus") or {}
        for pic in opus.get("pics") or []:
            if isinstance(pic, dict):
                url = pic.get("url", "")
                if url:
                    pictures.append(
                        {
                            "img_src": url,
                            "img_width": pic.get("width", 0),
                            "img_height": pic.get("height", 0),
                        }
                    )

    if dynamic_type == "DYNAMIC_TYPE_DRAW" or major_type == "MAJOR_TYPE_DRAW":
        card["item"] = {"description": desc_text}
        if pictures:
            card["item"]["pictures"] = pictures
        card["user"] = {
            "name": module_author.get("name", ""),
        }

    elif dynamic_type == "DYNAMIC_TYPE_WORD":
        card["item"] = {"content": desc_text}
        card["user"] = {
            "name": module_author.get("name", ""),
        }

    elif dynamic_type == "DYNAMIC_TYPE_AV" or major_type == "MAJOR_TYPE_ARCHIVE":
        archive = (major.get("archive") or {}) if major else {}
        card["aid"] = archive.get("aid", "")
        card["bvid"] = archive.get("bvid", "")
        card["title"] = archive.get("title", "")
        archive_desc = archive.get("desc", "")
        if isinstance(archive_desc, dict):
            card["desc"] = archive_desc.get("text", "")
        else:
            card["desc"] = str(archive_desc) if archive_desc else ""
        archive_cover = archive.get("cover", "")
        if isinstance(archive_cover, dict):
            card["pic"] = archive_cover.get("url", "")
        else:
            card["pic"] = str(archive_cover) if archive_cover else ""
        card["owner"] = {"name": module_author.get("name", "")}
        if desc_text:
            card["item"] = {"description": desc_text}

    elif dynamic_type == "DYNAMIC_TYPE_ARTICLE" or major_type == "MAJOR_TYPE_ARTICLE":
        article = (major.get("article") or {}) if major else {}
        card["id"] = article.get("id", "")
        card["title"] = article.get("title", "")
        card["summary"] = article.get("summary", "")
        card["author"] = {"name": module_author.get("name", "")}

    elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
        card["item"] = {"content": desc_text}
        card["user"] = {"name": module_author.get("name", "")}

    elif dynamic_type == "DYNAMIC_TYPE_LIVE_RCMD" or major_type == "MAJOR_TYPE_LIVE":
        live = (major.get("live") or {}) if major else {}
        card["live_play_info"] = live
        card["item"] = {"description": desc_text}
        card["user"] = {"name": module_author.get("name", "")}

    else:
        if desc_text:
            card["item"] = {"description": desc_text, "content": desc_text}
        # 对于未明确匹配的动态类型，如果提取到了图片也一并放入 item.pictures
        if pictures and "item" not in card:
            card["item"] = {"pictures": pictures}
        elif pictures and "item" in card:
            card["item"]["pictures"] = pictures
        card["user"] = {"name": module_author.get("name", "")}

    # 保存描述跳转链接与商品卡片信息，供广告过滤检测使用
    # 使用下划线前缀的独立字段，不污染 item.description 等下游展示字段
    if desc_jump_urls:
        card["_desc_jump_urls"] = desc_jump_urls
    if additional_goods:
        card["_additional_goods"] = additional_goods

    return card


def _convert_new_dynamics_to_old(data: dict) -> dict:
    """将新版动态API响应(x/polymer/web-dynamic)转换为旧版格式(dynamic_svr)，以兼容现有代码"""
    if not data or "items" not in data:
        return data

    cards = []
    for item in data.get("items", []):
        if not item:
            continue

        dynamic_type_str = item.get("type", "")
        dynamic_type_int = _DYNAMIC_TYPE_MAP.get(dynamic_type_str, 0)

        modules = item.get("modules") or {}
        module_author = modules.get("module_author") or {}
        module_dynamic = modules.get("module_dynamic") or {}

        desc = {
            "timestamp": int(module_author.get("pub_ts") or 0),
            "dynamic_id": item.get("id_str", ""),
            "type": dynamic_type_int,
            "user_profile": {
                "info": {
                    "uid": int(module_author.get("mid") or 0),
                    "uname": module_author.get("name") or "",
                }
            },
        }

        card = _reconstruct_card(dynamic_type_str, module_dynamic, module_author)

        cards.append(
            {
                "desc": desc,
                "card": card,
                "extend_json": {},
            }
        )

    result = {
        "has_more": data.get("has_more", False),
        "cards": cards,
    }

    if "offset" in data:
        result["next_offset"] = data["offset"]

    return result


async def download_and_cache_image(
    cache_dir: Path, identifier: int, url: str, label: str = "图片"
) -> Path | None:
    """下载图片并缓存到指定目录，返回缓存路径

    Args:
        cache_dir: 缓存目录
        identifier: 用于命名缓存文件的唯一标识（如 uid、room_id）
        url: 图片 URL
        label: 日志中显示的标签（如"头像"、"直播封面"）
    """
    if not url or not identifier:
        return None
    cached_path = cache_dir / f"{identifier}.png"
    if cached_path.exists():
        logger.debug(f"{label}缓存命中: ID {identifier}")
        return cached_path

    logger.debug(f"{label}缓存未命中，正在下载: ID {identifier}")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, headers=get_bilibili_headers()
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            async with aiofiles.open(cached_path, "wb") as f:
                await f.write(response.content)
        return cached_path
    except Exception as e:
        logger.error(f"下载{label}失败 ID: {identifier}, URL: {url}", e=e)
    return None


async def get_cached_avatar(uid: int, avatar_url: str) -> Path | None:
    """获取缓存的用户头像路径，如果不存在则下载"""
    return await download_and_cache_image(AVATAR_CACHE_DIR, uid, avatar_url, "头像")


async def get_videos(uid: int, auth: BilibiliCredential | None = None, **kwargs):
    """获取用户投搞视频信息"""
    credential = auth or get_credential()
    user_instance = bilibili_user_module.User(uid=uid, credential=credential)
    return await user_instance.get_videos(**kwargs)


async def get_user_card(
    mid: int, photo: bool = False, auth: BilibiliCredential | None = None, **kwargs
):
    """获取用户卡片信息"""
    credential = auth or get_credential()
    user_instance = bilibili_user_module.User(uid=mid, credential=credential)
    user_info = await user_instance.get_user_info()
    return user_info


# 获取指定用户历史动态
async def get_user_dynamics(
    uid: int,
    offset: int = 0,
    need_top: bool = False,
    auth: BilibiliCredential | None = None,
    **kwargs,
):
    credential = auth or get_credential()
    user_instance = bilibili_user_module.User(uid=uid, credential=credential)
    new_data = await user_instance.get_dynamics_new(
        offset=str(offset) if offset else ""
    )
    return _convert_new_dynamics_to_old(new_data)


async def get_room_info_by_id(
    live_id: int, auth: BilibiliCredential | None = None, **kwargs
):
    """根据房间号获取指定直播间信息"""
    credential = auth or get_credential()
    liveroom_instance = bilibili_live_module.LiveRoom(
        room_display_id=live_id, credential=credential
    )
    return await liveroom_instance.get_room_info()


async def get_dynamic_screenshot(dynamic_id: str) -> bytes | None:
    url = f"https://t.bilibili.com/{dynamic_id}"
    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            async with _screenshot_semaphore:
                async with get_new_page(
                    viewport={"width": 2000, "height": 1000},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    device_scale_factor=3,
                ) as page:
                    try:
                        cookies = build_bilibili_playwright_cookies()
                        if cookies:
                            await page.context.add_cookies(cookies)  # type: ignore
                    except Exception as e:
                        logger.warning(f"获取 cookies 失败: {e}")
                    # 使用 domcontentloaded 替代 networkidle，避免因B站页面持久连接导致无限等待
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    if page.url == "https://www.bilibili.com/404":
                        logger.warning(f"动态 {dynamic_id} 不存在")
                        return None
                    # 等待动态卡片元素加载完成，确保截图内容完整
                    await page.wait_for_selector(".card", timeout=10000)
                    # 等待图片等资源加载
                    await asyncio.sleep(1)
                    card = await page.query_selector(".card")
                    if not card:
                        raise RuntimeError("未找到动态卡片元素 .card")
                    clip = await card.bounding_box()
                    if not clip:
                        raise RuntimeError("无法获取动态卡片 .card 的边界框")
                    bar = await page.query_selector(".bili-tabs__header")
                    if not bar:
                        raise RuntimeError("未找到标签栏元素 .bili-tabs__header")
                    bar_bound = await bar.bounding_box()
                    if not bar_bound:
                        raise RuntimeError("无法获取标签栏 .bili-tabs__header 的边界框")
                    # 计算截图高度：标签栏顶部 - 卡片顶部
                    # 若页面布局异常导致结果为非正数，回退到使用卡片自身高度
                    clip_height = bar_bound["y"] - clip["y"]
                    if clip_height <= 0:
                        logger.warning(
                            f"动态 {dynamic_id} 截图高度计算异常 (bar_y={bar_bound['y']}, clip_y={clip['y']})，"
                            f"回退到卡片自身高度: {clip.get('height', 0)}"
                        )
                    else:
                        clip["height"] = clip_height
                    # 同时传入 clip 和 full_page=True 是有意为之的组合用法，不是 bug：
                    # - full_page=True：将截图底图设为整个可滚动文档（而非仅当前视口），
                    #   确保高度超过视口(1000px)的动态卡片能被完整渲染。
                    # - clip：在全页文档坐标系上做二次裁剪，只保留从卡片顶部到标签栏
                    #   顶部之间的区域（已通过上方 clip_height 计算排除标签栏及以下内容）。
                    # 若仅用 clip 而不带 full_page，当卡片超出视口时下方会被截断；
                    # 若仅用 full_page 而不带 clip，会截取到不需要的标签栏等页面元素。
                    # 参考 Playwright driver 的 screenshotter.js: screenshotPage() 中，
                    # full_page 分支会先取整页尺寸，再通过 trimClipToSize(clip, documentRect)
                    # 将 clip 限制在全页范围内，最终输出整页坐标系上的 clip 区域。
                    return await page.screenshot(clip=clip, full_page=True)
        except Exception as e:
            error_name = type(e).__name__
            # 对浏览器相关错误进行重试（如 TargetClosedError、net::ERR_ABORTED 等）
            is_browser_error = error_name in ("TargetClosedError",) or any(
                keyword in str(e)
                for keyword in [
                    "Target page",
                    "ERR_ABORTED",
                    "net::ERR",
                    "frame was detached",
                ]
            )
            if is_browser_error and attempt < max_attempts - 1:
                wait_time = 2 * (attempt + 1)
                logger.warning(
                    f"获取动态截图遇到浏览器错误，将在 {wait_time} 秒后重试 "
                    f"(第 {attempt + 1}/{max_attempts} 次): 动态ID={dynamic_id}, "
                    f"错误: {error_name}: {e}"
                )
                await asyncio.sleep(wait_time)
                continue
            logger.warning(
                f"Error in get_dynamic_screenshot({url}): {traceback.format_exc()}"
            )
            break
    return None
