import asyncio
import time
import traceback
from datetime import datetime
from typing import Any, cast

import httpx
import nonebot
from bilibili_api import select_client
from nonebot import on_message
from nonebot.adapters import Bot, Event
from nonebot.drivers import Driver
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State
from nonebot_plugin_alconna import Image, Segment, Text, UniMessage, UniMsg
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_session import EventSession
from nonebot_plugin_uninfo import Uninfo

from zhenxun.configs.config import Config
from zhenxun.configs.utils import PluginExtraData, RegisterConfig, Task
from zhenxun.services.log import logger
from zhenxun.utils.common_utils import CommonUtils
from zhenxun.utils.depends import GetGroupConfig
from zhenxun.utils.enum import PluginType

from .config import (
    IMAGE_CACHE_DIR,
    MODULE_NAME,
    GroupSettings,
    base_config,
    check_and_refresh_credential,
    load_credential_from_file,
)
from .data_source import BiliSub, check_and_send_update
from .model import ArticleInfo, LiveInfo, UserInfo, VideoInfo
from .services.cache_service import CacheService
from .services.download_service import DownloadTask, download_manager
from .services.network_service import ParserService
from .services.queue_service import parse_queue
from .utils.exceptions import (
    BilibiliBaseException,
    ResourceNotFoundError,
    UnsupportedUrlError,
    UrlParseError,
)
from .utils.message import (
    ImageHelper,
    MessageBuilder,
    get_video_cover_segment,
    render_live_info_to_image,
    render_user_info_to_image,
    render_video_info_to_image,
)
from .platform_parse import PlatformParseService
from .platform_parse.sender import send_platform_parse_result
from .utils.url_parser import extract_bilibili_url_from_message

__plugin_meta__ = PluginMetadata(
    name="B站工具",
    description="B站订阅通知与内容解析（视频、直播、专栏/动态），并支持抖音/微博/小红书/快手/A站/网易云/推特/知乎/视频号/小黑盒等多平台链接解析",
    usage="""
## B站工具
一个功能强大且易于使用的B站插件，统一指令前缀 `bili`，支持订阅通知与内容解析。

### 📖 订阅管理 (需要群管或更高权限，固定每时1分检查订阅更新)

*   **`bili sub list`**
    查看当前会话（群聊或私聊）的所有订阅。

*   **`bili sub add <内容...>`**
    为当前会话添加一个或多个订阅。
    - `<内容>`: **UP主UID**。
    - **示例**:
        - `bili sub add 732482333` (订阅UP主)

*   **`bili sub del <ID...>`**
    从当前会话删除一个或多个订阅。ID通过 `bili sub list` 查看。

*   **`bili sub config <ID...> <设置...>`**
    为当前会话中的指定订阅ID批量配置推送选项。
    - **推送类型**: `+dynamic` / `-dynamic`、`+video` / `-video`、`+live` / `-live`、`+all` / `-all`
    - **艾特全体**: `+at:dynamic` / `-at:dynamic`、`+at:video` / `-at:video`、`+at:live` / `-at:live`、`+at:all` / `-at:all`
    - **示例**: `bili sub config 3 4 +live -dynamic +at:live`

*   **`bili sub clear`**
    **[危险]** 清空当前会话的所有订阅，操作前会请求确认。

### 🛠️ 超级用户指令

*   **跨群管理与清空**
    - 在 `add`, `del`, `list`, `config` 命令后附加 `-g, --group <群号...>` 参数，可以跨群管理订阅。
    - `bili sub clear -g <群号...>`: 清空**指定群组**的订阅。
    - `bili sub clear --all`: **[高危]** 清空**所有**目标（所有群和私聊）的订阅。

*   **账号与全局管理**
    - `bili login`: 通过扫描二维码登录B站账号，以获取和保存凭证。
    - `bili status`: 检查当前B站账号凭证的有效状态。
    - `bili logout`: 清除已保存的B站凭证，退出登录。
    - `bili sub checkall`: 立即对所有已订阅的项目进行一次更新检查。
    - `bili sub forcepush <ID...>`: 强制推送指定ID订阅的最新内容，无论之前是否已推送。

### 🎬 内容解析

*   **被动解析**
    自动监听消息中的链接，并发送解析结果。
    - B站: 视频(av/BV)、直播、专栏(cv)、动态、用户空间。
    - 多平台: 抖音、微博、小红书、快手、AcFun(A站)、网易云音乐、Twitter/X、
      知乎(文章/回答/问题/想法)、微信视频号、小黑盒(帖子/游戏页)。
    - 开关控制: `bili 开启群被动解析` / `bili 关闭群被动解析` (群管权限)。
    - 多平台解析总开关: 配置项 `ENABLE_MULTI_PLATFORM_PARSE`。
    - 发送数量控制: 配置项 `MULTI_PLATFORM_MAX_IMAGE_COUNT`(图片, 默认10)、
      `MULTI_PLATFORM_MAX_VIDEO_COUNT`(视频/音频, 默认0不限制), 均设为0不限制。
    - 单一视频/音频先发媒体预览卡片图再发媒体(不发文字):
      配置项 `MULTI_PLATFORM_RENDER_MEDIA_CARD`(默认开启, 关闭回退纯文字)。
    - 图文图集(小红书等)摘要卡片与图片合为一条消息发送(不发文字),
      知乎回答/问题只发长文卡片:
      同样由 `MULTI_PLATFORM_RENDER_MEDIA_CARD` 控制;
      `MULTI_PLATFORM_CARD_MAX_IMAGES` 控制卡片内嵌图片上限(默认0不限制)。
    - 知乎建议配置 `ZHIHU_COOKIE` 绕过反爬; 视频号短链需配置
      `SHIPINHAO_YUANBAO_COOKIE`(腾讯元宝Cookie); 知乎/小黑盒建议安装
      curl_cffi 库(TLS指纹伪装, `pip install curl_cffi`)。

*   **手动视频下载**
    - 命令: `bili 下载 [链接/ID]`
    - 用于下载 B 站视频，支持视频链接、av/BV号、或引用包含视频链接的消息。

*   **获取封面**
    - 命令: `bili 封面`
    - 获取 B 站视频的原始封面图片，需引用包含链接的消息。

*   **自动下载控制 (需要管理员权限)**
    - `bili 自动下载 on`: 为当前群聊开启视频自动下载。
    - `bili 自动下载 off`: 为当前群聊关闭视频自动下载。
    - 超级用户可追加 `-g <群号...>` 或 `-t <标签>` 对指定群组进行批量操作。

### ⚙ 配置文件
*   下面的功能需要在配置文件或WebUI中修改（休眠、广告过滤、缓存、下载画质等）。
""".strip(),
    extra=PluginExtraData(
        author="weLizo",
        version="1.47",
        plugin_type=PluginType.DEPENDANT,
        menu_type="订阅通知",
        configs=[
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_AT_ALL",
                value=False,
                help="是否开启B站订阅@全体功能总开关",
                default_value=False,
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_SLEEP_MODE",
                value=True,
                help="是否开启B站订阅定时休眠",
                default_value=True,
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="SLEEP_TIME_RANGE",
                value="23:00-07:00",
                help="休眠时间段 (格式 HH:MM-HH:MM)，例如 '23:00-07:00'",
                default_value="23:00-07:00",
                type=str,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_AD_FILTER",
                value=True,
                help="是否开启广告过滤",
                default_value=True,
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="DEFAULT_UP_PUSH_TYPES",
                value=["dynamic", "video", "live"],
                help="UP主类型订阅默认推送的内容 (可选: dynamic, video, live)",
                default_value=["dynamic", "video", "live"],
                type=list[str],
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="BATCH_SIZE",
                value=10,
                help="每次检查的订阅批次大小",
                default_value=10,
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_DYNAMIC_IMAGE",
                value=False,
                help="是否推送/解析动态中的图片（转发动态除外）",
                default_value=False,
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="GROUP_BILIBILI_SUB_LEVEL",
                value="4",
                default_value="4",
                help="群内bilibili订阅需要管理的权限",
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="CACHE_TTL",
                value=0,
                default_value=0,
                help="被动解析冷却时间（分钟），同一会话中的同一链接在此时间内不再次解析，设为0关闭冷却",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_MINIAPP_PARSE",
                value=True,
                default_value=True,
                help="是否在被动解析中解析QQ小程序/JSON卡片中的链接(含B站与多平台链接)",
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="RENDER_AS_IMAGE",
                value=True,
                default_value=True,
                help="是否将被动解析结果渲染成图片发送",
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="CACHE_CLEAN_INTERVAL_HOURS",
                value=24,
                default_value=24,
                help="缓存清理间隔（小时），控制所有类型缓存的清理间隔",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="AUTO_DOWNLOAD_MAX_DURATION",
                value=10,
                default_value=10,
                help="自动下载最大时长(分钟), 超过此值不下载，设为0关闭时长限制",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MANUAL_DOWNLOAD_MAX_DURATION",
                value=20,
                default_value=20,
                help="手动下载最大时长(分钟), 超过此值不下载，设为0关闭时长限制，超级用户不受影响",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="CACHE_EXPIRY_DAYS",
                value=7,
                default_value=7,
                help="缓存过期时间(天)",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MAX_VIDEO_CACHE_SIZE_MB",
                value=1024,
                default_value=1024,
                help="视频缓存最大大小(MB), 超过此大小自动清理最旧的缓存",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="VIDEO_DOWNLOAD_QUALITY",
                value=64,
                default_value=64,
                help="视频下载质量(16=360P, 32=480P, 64=720P, 80=1080P)",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ENABLE_MULTI_PLATFORM_PARSE",
                value=True,
                default_value=True,
                help="是否启用多平台链接解析(抖音/微博/小红书/快手/A站/网易云/推特/知乎/视频号/小黑盒)",
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MULTI_PLATFORM_MAX_VIDEO_SIZE_MB",
                value=100,
                default_value=100,
                help="多平台解析的媒体下载大小上限(MB), 超过后仅发送文本信息, 设为0不限制",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MULTI_PLATFORM_MAX_IMAGE_COUNT",
                value=10,
                default_value=10,
                help="单条解析结果最多发送的图片数量, 超出部分不再发送并附提示, 设为0不限制",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MULTI_PLATFORM_MAX_VIDEO_COUNT",
                value=0,
                default_value=0,
                help="单条解析结果最多发送的视频/音频数量, 超出部分不再发送并附提示, 设为0不限制(默认)",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MULTI_PLATFORM_RENDER_MEDIA_CARD",
                value=True,
                default_value=True,
                help="媒体卡片总开关: 单一视频/音频先发预览卡片再发媒体、图集(小红书等)卡片与图片合发、知乎回答/问题只发长文卡片; 关闭后全部回退纯文字+媒体",
                type=bool,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="MULTI_PLATFORM_CARD_MAX_IMAGES",
                value=0,
                default_value=0,
                help="长文卡片(知乎回答/问题)最多内嵌的图片数量, 超出部分在卡片底部提示, 设为0不限制(默认)",
                type=int,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="ZHIHU_COOKIE",
                value="",
                default_value="",
                help="知乎 Cookie(登录 zhihu.com 后复制请求头 Cookie 字符串), 用于绕过反爬拦截, 强烈建议配置",
                type=str,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="SHIPINHAO_YUANBAO_COOKIE",
                value="",
                default_value="",
                help="腾讯元宝 Cookie(登录 yuanbao.tencent.com 后复制), 视频号分享短链解析必需, 浏览器长链无需配置",
                type=str,
            ),
            RegisterConfig(
                module=MODULE_NAME,
                key="XIAOHEIHE_COOKIE",
                value="",
                default_value="",
                help="小黑盒 Cookie(可选), 触发风控验证码时配置可提高解析成功率",
                type=str,
            ),
        ],
        tasks=[Task(module=MODULE_NAME, name="b站解析")],
        group_config_model=GroupSettings,
    ).dict(),
)


driver: Driver = nonebot.get_driver()


# 游标分页：记录上次处理的最后一条订阅 ID，避免 offset 分页在订阅增删时产生漂移
_current_sub_id = 0
_subs_lock = asyncio.Lock()

# 保存后台任务引用，防止被 GC 回收导致任务被静默取消
_credential_refresh_task: asyncio.Task | None = None


async def _initialize_services():
    """初始化所有服务：缓存、凭证、下载管理器"""
    global _credential_refresh_task
    await CacheService.initialize()
    await load_credential_from_file()
    select_client("httpx")
    download_manager.initialize()

    _credential_refresh_task = asyncio.create_task(check_and_refresh_credential())


@driver.on_startup
async def _startup():
    # 清理旧插件 (bilibili_sub / parse_bilibili) 遗留的被动技能记录，
    # 避免同名任务 "b站解析" 重复导致内置开关命令异常
    try:
        from zhenxun.models.task_info import TaskInfo
        from zhenxun.services.cache.runtime_cache import TaskInfoMemoryCache

        deleted = await TaskInfo.filter(
            module__in=["parse_bilibili", "bilibili_sub"]
        ).delete()
        if deleted:
            logger.info(f"已清理 {deleted} 条旧B站插件的被动技能记录")
            await TaskInfoMemoryCache.refresh()
    except Exception as e:
        logger.warning(f"清理旧B站插件被动技能记录失败: {e}")

    await _initialize_services()


@driver.on_shutdown
async def _shutdown():
    from bilibili_api.utils.network import get_session

    # 关闭前刷新URL缓存，避免防抖机制导致的数据丢失
    try:
        await CacheService.flush_url_cache()
    except Exception as e:
        logger.warning(f"关闭前刷新URL缓存失败: {e}")

    # 关闭前刷新视频缓存索引，避免防抖机制导致的索引数据丢失
    try:
        await CacheService.flush_video_cache_index()
    except Exception as e:
        logger.warning(f"关闭前刷新视频缓存索引失败: {e}")

    session = cast(httpx.AsyncClient, get_session())
    if session and not session.is_closed:
        await session.aclose()


def should_run():
    """判断当前时间是否在运行时间段内"""
    time_range_str = Config.get_config(MODULE_NAME, "SLEEP_TIME_RANGE", "23:00-07:00")
    now = datetime.now().time()

    try:
        parts = time_range_str.split("-")
        if len(parts) != 2:
            raise ValueError("时间范围格式错误")

        start_time = datetime.strptime(parts[0].strip(), "%H:%M").time()
        end_time = datetime.strptime(parts[1].strip(), "%H:%M").time()

        if start_time > end_time:
            return end_time <= now < start_time
        else:
            return not (start_time <= now < end_time)

    except (ValueError, IndexError) as e:
        logger.error(f"解析休眠时间配置 '{time_range_str}' 失败: {e}，将默认允许运行。")
        return True


@scheduler.scheduled_job(
    "cron",
    minute=1,
    max_instances=1,
    misfire_grace_time=40,
)
async def check_subscriptions():
    """定时任务：检查订阅并发送消息"""
    global _current_sub_id
    start_time = time.time()
    logger.debug(
        f"B站订阅检查任务开始执行 - 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    if base_config.get("ENABLE_SLEEP_MODE") and not should_run():
        logger.debug(
            f"B站订阅检查任务处于休眠时间段，跳过执行 - 当前时间: {datetime.now().strftime('%H:%M:%S')}"
        )
        return

    bots = nonebot.get_bots()
    if not bots:
        logger.warning("B站订阅检查任务未找到可用的机器人实例")
        return

    bot_instance = next(iter(bots.values()))

    try:
        await check_and_refresh_credential()

        batch_size = base_config.get("BATCH_SIZE", 10)
        batch_to_check = []

        async with _subs_lock:
            total_subs = await BiliSub.all().count()

            if total_subs == 0:
                logger.debug("B站订阅检查：数据库中没有订阅，跳过本次检查。")
                return

            # 使用游标分页 (id > _current_sub_id)
            prev_sub_id = _current_sub_id
            batch_to_check = (
                await BiliSub.all()
                .filter(id__gt=_current_sub_id)
                .order_by("id")
                .limit(batch_size)
            )

            if not batch_to_check and prev_sub_id > 0:
                logger.info(f"游标 id>{prev_sub_id} 已到末尾，重置游标并重新获取本批")
                _current_sub_id = 0
                batch_to_check = (
                    await BiliSub.all()
                    .filter(id__gt=0)
                    .order_by("id")
                    .limit(batch_size)
                )

            if batch_to_check:
                _current_sub_id = batch_to_check[-1].id

            # 若本批数量不足 batch_size，说明已遍历到末尾，重置游标供下一轮从头开始
            if len(batch_to_check) < batch_size:
                _current_sub_id = 0

            logger.info(
                f"B站订阅检查任务: "
                f"本次检查游标 id>{prev_sub_id}, 获取 {len(batch_to_check)} 条 (共 {total_subs} 个), "
                f"下一游标: {_current_sub_id}"
            )

        if not batch_to_check:
            logger.info("B站订阅检查任务：当前批次为空，可能是由于游标回绕。")
            return

        tasks = [check_and_send_update(sub, bot_instance) for sub in batch_to_check]
        await asyncio.gather(*tasks)

    except Exception as e:
        logger.error(
            f"B站订阅检查任务批次处理异常: 错误类型={type(e).__name__}, 错误信息={e}"
        )
        logger.debug(f"B站订阅检查任务批次处理异常详细信息:\n{traceback.format_exc()}")

    total_duration = time.time() - start_time
    logger.debug(f"B站订阅检查任务执行完成 - 总耗时: {total_duration:.2f}秒")


# ============================================================
# 被动解析
# ============================================================


async def _handle_auto_download(
    bot: Bot, event: Event, video_info: VideoInfo
) -> asyncio.Task:
    """处理自动下载请求，返回下载任务供调用方等待其完成

    返回的后台下载任务由调用方决定是否 await：
    被动解析场景下会 await 以将下载纳入 parse_queue 锁的保护范围，
    实现"解析→发送预览图→下载→发送视频"的原子性。
    """
    session_id = event.get_session_id()
    logger.info(f"为会话 {session_id} 触发自动下载: {video_info.title}")

    task = DownloadTask(bot=bot, event=event, info_model=video_info, is_manual=False)
    return await download_manager.add_task(task)


async def _rule(uninfo: Uninfo, message: UniMsg, state: T_State) -> bool:
    plain_text = message.extract_plain_text().strip()
    if await CommonUtils.task_is_block(uninfo, MODULE_NAME):
        return False

    # 跳过 bili 主动指令，避免被动解析与指令冲突
    if plain_text.startswith("bili "):
        logger.debug("消息被识别为 bili 主动指令，被动解析跳过", "B站解析")
        return False

    check_hyper = base_config.get("ENABLE_MINIAPP_PARSE", True)
    if not check_hyper:
        logger.debug("小程序/卡片解析已禁用", "B站解析")

    url = extract_bilibili_url_from_message(message, check_hyper=check_hyper)

    if url:
        logger.debug(f"从消息中提取到B站URL: {url}", "B站解析")
        # 将提取到的 URL 存入 state，避免 handler 中重复提取
        state["_bili_url"] = url
        return True

    # 多平台链接解析（B站优先，其余平台兜底）
    if base_config.get("ENABLE_MULTI_PLATFORM_PARSE", True):
        platform_found = PlatformParseService.find_from_message(
            message, check_hyper=check_hyper
        )
        if platform_found:
            parser, matched = platform_found
            logger.debug(
                f"从消息中识别到 {parser.DISPLAY_NAME} 链接: {matched.group(0)}",
                "多平台解析",
            )
            # 解析器与 Match 一并存入 state, 供 handler 直接分发
            state["_platform_parse"] = (parser, matched)
            return True

    logger.debug("消息不符合被动解析规则", "B站解析")
    return False


async def _create_rendered_message(
    info_model: Any,
    render_func: Any,
    builder_func: Any,
    render_enabled: bool,
) -> UniMsg | None:
    """通用的消息构建函数，封装了渲染为图片或回退到文本的逻辑"""
    link_url = (
        getattr(info_model, "room_url", None)
        or getattr(info_model, "parsed_url", None)
        or getattr(info_model, "url", None)
    )

    if render_enabled:
        type_name = type(info_model).__name__
        logger.debug(
            f"渲染 {type_name} 消息: {getattr(info_model, 'title', getattr(info_model, 'name', ''))}",
            "B站解析",
        )
        try:
            image_bytes = await render_func(info_model)
            if image_bytes:
                segments: list[Segment] = [Image(raw=image_bytes)]
                if link_url:
                    segments.append(Text(f"链接：{link_url}"))
                return UniMessage(segments)
            else:
                logger.warning(f"{type_name} 渲染函数返回空，尝试原始消息", "B站解析")
                return await builder_func(info_model)
        except Exception as render_err:
            logger.error("渲染失败，将使用原始消息", "B站解析", e=render_err)
            return await builder_func(info_model)
    else:
        logger.debug(f"构建文本消息: {type(info_model).__name__}", "B站解析")
        return await builder_func(info_model)


async def _build_article_message(
    article_info: ArticleInfo, render_enabled: bool
) -> UniMsg | None:
    logger.debug(
        f"构建文章/动态消息: {article_info.type} {article_info.id}, 渲染模式: {render_enabled}",
        "B站解析",
    )
    article_message = await MessageBuilder.build_article_message(
        article_info, render_enabled=render_enabled
    )

    if not article_message:
        return None

    msg_list_content = list(article_message)

    dynamic_images = []
    # 如果动态中包含图片，且功能开关已开启，则添加到消息列表中
    if (
        article_info.type == "opus"
        and article_info.images
        and base_config.get("ENABLE_DYNAMIC_IMAGE", False)
    ):
        logger.info(f"动态中包含 {len(article_info.images)} 张图片，将一并发送")

        for idx, img_url in enumerate(article_info.images, 1):
            try:
                # 下载图片
                file_name = f"bili_dynamic_img_{article_info.id}_{idx}.jpg"
                img_path = IMAGE_CACHE_DIR / file_name
                if await ImageHelper.download_image(img_url, img_path):
                    dynamic_images.append(Image(path=img_path))
                    logger.debug(f"成功添加动态中的图片: {img_url}")
            except Exception as e:
                logger.warning(f"下载动态中的图片失败: {img_url}, 错误: {e}")

    if article_info.url:
        image_segment: Image | None = None
        for seg in article_message:
            if isinstance(seg, Image):
                image_segment = seg
                break

        if image_segment:
            return UniMessage(
                [image_segment] + dynamic_images + [Text(f"链接：{article_info.url}")]
            )

    return UniMessage(msg_list_content + dynamic_images)


# 构建视频消息
async def _build_video_message(
    video_info: VideoInfo, render_enabled: bool
) -> UniMsg | None:
    link_url = video_info.parsed_url

    if not render_enabled:
        # 非渲染模式下，build_video_message 本身就会单独发送封面图片
        logger.debug("构建视频文本消息（含单独封面）", "B站解析")
        return await MessageBuilder.build_video_message(video_info)

    segments: list[Segment] = []

    # 1. 单独发送视频封面（从解析图片中拆出）
    cover_segment = await get_video_cover_segment(video_info)
    if cover_segment:
        segments.append(cover_segment)
        logger.debug("已将视频封面作为单独图片加入消息", "B站解析")

    # 2. 渲染不含封面的解析图片
    try:
        image_bytes = await render_video_info_to_image(video_info)
        if image_bytes:
            segments.append(Image(raw=image_bytes))
        else:
            logger.warning("VideoInfo 渲染返回空，尝试原始消息", "B站解析")
            return await MessageBuilder.build_video_message(video_info)
    except Exception as render_err:
        logger.error("视频渲染失败，将使用原始消息", "B站解析", e=render_err)
        return await MessageBuilder.build_video_message(video_info)

    if link_url:
        segments.append(Text(f"链接：{link_url}"))

    return UniMessage(segments) if segments else None


async def _build_message_for_content(
    content: Any, render_enabled: bool
) -> UniMsg | None:
    """根据解析内容的类型，分发到相应的消息构建函数"""
    if isinstance(content, ArticleInfo):
        return await _build_article_message(content, render_enabled)

    # 视频特殊处理：封面单独发送（改动4）
    if isinstance(content, VideoInfo):
        return await _build_video_message(content, render_enabled)

    build_mapping = {
        LiveInfo: (render_live_info_to_image, MessageBuilder.build_live_message),
        UserInfo: (render_user_info_to_image, MessageBuilder.build_user_message),
    }

    for content_type, (render_func, builder_func) in build_mapping.items():
        if isinstance(content, content_type):
            return await _create_rendered_message(
                content, render_func, builder_func, render_enabled
            )

    logger.warning(f"内容类型 {type(content).__name__} 没有匹配的消息构建器。")
    return None


_matcher = on_message(priority=50, block=False, rule=_rule)


async def _handle_platform_parse(
    bot: Bot,
    event: Event,
    session: EventSession,
    platform_target: tuple,
) -> None:
    """处理多平台链接解析（B站以外的平台）

    与 B 站被动解析共用 解析锁(parse_queue) 与 URL 去重缓存(CacheService)。
    """
    parser, matched = platform_target
    cache_key = matched.group(0)

    async with parse_queue:
        # 在获取解析锁后重新检查缓存，防止并发请求重复解析同一链接
        if not await CacheService.should_parse_url(cache_key, session):
            logger.debug(f"多平台解析：链接在缓存中且TTL未过期，跳过: {cache_key}")
            return

        try:
            logger.info(
                f"多平台解析：开始解析 {parser.DISPLAY_NAME} 链接: {cache_key}",
                session=session,
            )
            result = await PlatformParseService.parse(parser, matched)
            await send_platform_parse_result(bot, event, result)
            await CacheService.add_url_to_cache(cache_key, session)
            logger.info(
                f"多平台解析：成功解析并发送 {parser.DISPLAY_NAME}: {cache_key}",
                session=session,
            )
        except ResourceNotFoundError as e:
            # 资源不存在时回滚缓存，便于下次消息重新尝试
            await CacheService.remove_url_from_cache(cache_key, session)
            logger.info(
                f"多平台解析：{parser.DISPLAY_NAME} 资源不存在: {cache_key}, "
                f"原因: {e.message}",
                session=session,
            )
        except (UrlParseError, UnsupportedUrlError) as e:
            await CacheService.remove_url_from_cache(cache_key, session)
            logger.warning(
                f"多平台解析：{parser.DISPLAY_NAME} 解析失败: {cache_key}, "
                f"原因: {e.message}",
                session=session,
            )
        except BilibiliBaseException as e:
            await CacheService.remove_url_from_cache(cache_key, session)
            logger.error(
                f"多平台解析：{parser.DISPLAY_NAME} 处理错误: {cache_key}, "
                f"类型: {type(e).__name__}, 原因: {e.message}",
                session=session,
            )
        except Exception as e:
            await CacheService.remove_url_from_cache(cache_key, session)
            logger.error(
                f"多平台解析：处理链接时发生意外错误: {cache_key}",
                session=session,
                e=e,
            )
            logger.error(traceback.format_exc())


@_matcher.handle()
async def _(
    bot: Bot,
    event: Event,
    session: EventSession,
    message: UniMsg,
    state: T_State,
    group_config: GroupSettings = GetGroupConfig(GroupSettings),
):
    # 直接从 rule 阶段存入 state 的 URL 获取，避免重复调用 extract_bilibili_url_from_message
    target_url = state.get("_bili_url")
    platform_target = state.get("_platform_parse")

    if not target_url:
        # 兜底：若 state 中无 URL（理论上不会发生），再尝试提取一次
        check_hyper = base_config.get("ENABLE_MINIAPP_PARSE", True)
        target_url = extract_bilibili_url_from_message(message, check_hyper=check_hyper)

    if not target_url:
        if not platform_target:
            logger.debug("被动解析：在消息中未找到有效的解析目标，退出处理。")  # type: ignore
            return
        # 多平台链接（B站以外）解析
        await _handle_platform_parse(bot, event, session, platform_target)
        return

    async with parse_queue:
        # 在获取解析锁后重新检查缓存，防止并发请求重复解析同一URL
        if not await CacheService.should_parse_url(target_url, session):
            logger.debug(f"被动解析：URL在缓存中且TTL未过期，跳过: {target_url}")
            return

        try:
            logger.info(f"被动解析：开始解析URL: {target_url}", session=session)
            parsed_content = await ParserService.parse(target_url)

            if not parsed_content:
                return

            render_enabled = base_config.get("RENDER_AS_IMAGE", True)
            final_message = await _build_message_for_content(
                parsed_content, render_enabled
            )

            if final_message:
                await final_message.send()  # type: ignore
                await CacheService.add_url_to_cache(target_url, session)
                logger.info(f"被动解析：成功解析并发送: {target_url}", session=session)

                if (
                    isinstance(parsed_content, VideoInfo)
                    and group_config.auto_download_enabled
                ):
                    # 在 parse_queue 锁内等待下载任务完成，确保
                    # "解析→发送预览图→下载→发送视频" 作为原子操作执行，
                    # 避免后续解析请求在当前请求视频发送前就抢占发送预览图。
                    # _task_wrapper 内部已捕获所有异常（自动下载仅记录日志），
                    # 此处额外 try 防御：下载失败不应回滚已成功解析的缓存。
                    download_task = await _handle_auto_download(
                        bot, event, parsed_content
                    )
                    try:
                        await download_task
                    except Exception as download_err:
                        logger.warning(
                            f"被动解析触发的自动下载任务异常: {target_url}",
                            session=session,
                            e=download_err,
                        )
            else:
                logger.info(
                    f"被动解析：最终消息为空或未构建 (URL: {target_url})",
                    session=session,
                )
                await CacheService.add_url_to_cache(target_url, session)

        except ResourceNotFoundError as e:
            # 资源不存在时回滚缓存，使下次消息可重新尝试解析
            # （资源可能因下架/权限等原因临时返回 -404，回滚便于后续重试）
            await CacheService.remove_url_from_cache(target_url, session)
            logger.info(
                f"被动解析：资源不存在: {target_url}, 错误: {e.message}",
                session=session,
            )
        except (UrlParseError, UnsupportedUrlError) as e:
            await CacheService.remove_url_from_cache(target_url, session)
            logger.warning(
                f"被动解析：URL解析失败: {target_url}. 原因: {e.message}",
                session=session,
            )
        except BilibiliBaseException as e:
            # API/网络/限流等错误通常为瞬时性，回滚缓存以便下次消息时重试
            await CacheService.remove_url_from_cache(target_url, session)
            logger.error(
                f"被动解析：API或处理错误: {target_url}. 类型: {type(e).__name__}, 原因: {e.message}",
                session=session,
            )
        except Exception as e:
            # 未知异常同样回滚，避免因瞬时故障导致URL在TTL内无法重试
            await CacheService.remove_url_from_cache(target_url, session)
            logger.error(
                f"被动解析：处理URL时发生意外错误: {target_url}", session=session, e=e
            )
            logger.error(traceback.format_exc())


from . import commands  # noqa: F401
