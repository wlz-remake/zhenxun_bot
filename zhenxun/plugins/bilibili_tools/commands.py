import asyncio
import time
from typing import cast

import httpx
import nonebot
from arclet.alconna import Arparma
from bilibili_api import login_v2
from bilibili_api.utils.network import get_session
from nonebot.adapters import Bot, Event, Message
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import (
    Alconna,
    AlconnaMatcher,
    AlconnaMatches,
    Args,
    MultiVar,
    Option,
    Query,
    Subcommand,
    on_alconna,
)
from nonebot_plugin_session import EventSession
from nonebot_plugin_waiter import prompt_until

from zhenxun import ui
from zhenxun.builtin_plugins.superuser.plugin_config_manager import pconf_cmd
from zhenxun.models.group_console import GroupConsole
from zhenxun.services.log import logger
from zhenxun.ui.models import LayoutData, NotebookData, UserInfoBlock
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.rules import admin_check

from .config import (
    AVATAR_CACHE_DIR,
    MODULE_NAME,
    check_and_refresh_credential,
    clear_credential,
    get_credential,
    save_credential_to_file,
)
from .data_source import (
    BiliSub,
    BiliSubTarget,
    SubCheckResult,
    _checking_subs,
    add_target_to_sub,
    add_up_sub,
    check_and_send_update,
    get_sub_status,
)
from .services.cover_service import CoverService
from .services.download_service import DownloadTask, download_manager
from .services.network_service import ParserService
from .services.queue_service import parse_queue
from .sub_utils import get_cached_avatar, get_user_card
from .utils.exceptions import BilibiliBaseException
from .utils.url_parser import extract_bilibili_url_from_event


async def get_target_ids(session: EventSession, gids: Query[list[int]]) -> list[str]:
    """根据命令参数或会话上下文获取目标ID列表"""
    if gids.available and gids.result:
        return [f"group_{gid}" for gid in gids.result]

    # 根据 session 上下文确定目标 ID；id2 存在视为群聊，否则取 id1 视为私聊
    if session.id2:
        return [f"group_{session.id2}"]
    if session.id1:
        return [f"private_{session.id1}"]
    # id1 与 id2 均为空，无法确定会话目标，避免生成 "private_None" 这类无效 ID
    logger.warning("无法从会话上下文确定目标 ID：id1 和 id2 均为空")
    return []


# ============================================================
# 指令定义：统一前缀 bili
# ============================================================

# 公共指令（所有人可用）：下载、封面
bili_public_cmd = Alconna(
    "bili",
    Subcommand("下载", Args["link?", str]),
    Subcommand("封面"),
)

# 管理指令（需要群管权限）：订阅管理、被动解析开关
bili_admin_cmd = Alconna(
    "bili",
    Subcommand(
        "sub",
        Subcommand(
            "add",
            Args["ids", MultiVar(str)],
            Option("-g|--group", Args["gids", MultiVar(int)]),
        ),
        Subcommand(
            "del",
            Args["db_ids", MultiVar(int)],
            Option("-g|--group", Args["gids", MultiVar(int)]),
        ),
        Subcommand(
            "config",
            Args["params", MultiVar(str)],
            Option("-g|--group", Args["gids", MultiVar(int)]),
        ),
        Subcommand("list", Option("-g|--group", Args["gids", MultiVar(int)])),
        Subcommand(
            "clear", Option("--all"), Option("-g|--group", Args["gids", MultiVar(int)])
        ),
        Subcommand("checkall"),
        Subcommand("forcepush", Args["db_ids", MultiVar(int)]),
    ),
    Subcommand("开启群被动解析"),
    Subcommand("关闭群被动解析"),
)

# 超级用户指令：账号登录、状态、登出
bili_su_cmd = Alconna(
    "bili",
    Subcommand("login"),
    Subcommand("status"),
    Subcommand("logout"),
)

# use_cmd_start=False: 当 alconna_use_command_start=True 时，AlconnaRule.__init__
# 会调用 command_manager.update(command)，这会导致 command_manager 内部的
# __commands[namespace][name] 指针被更新为最后注册的命令。后续 on_alconna 的冲突
# 解决逻辑 (conflict_resolver="default") 会通过 get_command 获取 exist 命令，
# 但此时 exist 已指向最后一个 update 过的命令（其 formatter 已被分离），
# 从而在 exist.formatter.remove(command) 时抛出 KeyError。
# 由于三个命令共享同一 root "bili"，必须禁用 use_cmd_start 以避免此问题。
bili_public_matcher = on_alconna(
    bili_public_cmd,
    priority=5,
    block=True,
    use_cmd_start=False,
)

bili_admin_matcher = on_alconna(
    bili_admin_cmd,
    priority=5,
    block=True,
    rule=admin_check(MODULE_NAME, "GROUP_BILIBILI_SUB_LEVEL"),
    use_cmd_start=False,
)

bili_su_matcher = on_alconna(
    bili_su_cmd,
    priority=5,
    block=True,
    permission=SUPERUSER,
    use_cmd_start=False,
)

# 登录二维码有效期（秒），handle_login 与 check_login_status 共用此常量
LOGIN_TIMEOUT = 30

# 每个登录会话包含: (QrCodeLogin 实例, 开始时间, 后台轮询任务, 状态检查锁)
# 状态检查锁用于序列化 handle_status 与 check_login_status 对 check_state() 的并发调用，
# 避免两者同时获取到 DONE 状态后重复保存凭证、重复发送成功消息。
login_sessions: dict[
    str, tuple[login_v2.QrCodeLogin, float, asyncio.Task, asyncio.Lock]
] = {}


# ============================================================
# 公共指令处理：下载、封面
# ============================================================


@bili_public_matcher.assign("下载")
async def handle_bili_download(
    matcher: AlconnaMatcher,
    bot: Bot,
    event: Event,
    link: Query[str] = Query("下载.link", ""),
):
    logger.info("处理 bili 下载 命令")
    target_url = link.result if link.available else ""
    if not target_url:
        target_url = await extract_bilibili_url_from_event(bot, event)

    if not target_url:
        await matcher.finish(
            "未找到有效的B站链接或ID，请检查输入或回复包含B站链接的消息。"
        )

    await matcher.send("正在解析链接...")

    async with parse_queue:
        try:
            parsed_content = await ParserService.parse(target_url)

            task = DownloadTask(
                bot=bot,
                event=event,
                info_model=parsed_content,
                is_manual=True,
            )
            await download_manager.add_task(task, matcher)

        except BilibiliBaseException as e:
            logger.error(f"下载任务创建失败 (已处理异常): {e}", e=e)
            await matcher.finish(f"任务创建失败: {e.message}")
        except Exception as e:
            logger.error(f"下载任务创建失败 (未处理异常): {e}", e=e)
            await matcher.finish("任务创建时发生意外错误，请检查日志。")


@bili_public_matcher.assign("封面")
async def handle_bili_cover(matcher: AlconnaMatcher, bot: Bot, event: Event):
    logger.info("处理 bili 封面 命令")

    bilibili_url = await extract_bilibili_url_from_event(bot, event)

    if not bilibili_url:
        await matcher.finish("请引用包含B站链接的消息后使用此命令。")

    await matcher.send("正在获取封面，请稍候...")

    async with parse_queue:
        try:
            cover_message = await CoverService.get_cover_message(bilibili_url)
            await cover_message.send()
            logger.info(f"成功发送封面 for {bilibili_url}")
        except BilibiliBaseException as e:
            logger.warning(f"获取封面失败 for {bilibili_url}: {e.message}")
            await matcher.send(f"获取封面失败: {e.message}")
        except Exception as e:
            logger.error(f"处理bili封面命令时发生错误: {e}", e=e)
            await matcher.send("获取封面时发生错误，请稍后重试。")


# ============================================================
# 订阅管理指令处理（群管权限）
# ============================================================


@bili_admin_matcher.assign("sub.list")
async def handle_list(
    session: EventSession, gids: Query[list[int]] = Query("sub.list.group.gids", [])
):
    target_ids = await get_target_ids(session, gids)
    if not target_ids:
        await MessageUtils.build_message("未能确定操作目标，请检查指令。").finish()

    targets = await BiliSubTarget.filter(target_id__in=target_ids).prefetch_related(
        "subscription"
    )
    # 多群场景下可能多个群订阅了同一个UP主，按 sub.id 去重避免重复展示
    seen_sub_ids: set[int] = set()
    subs = [
        t.subscription
        for t in targets
        if t.subscription.id not in seen_sub_ids
        and not seen_sub_ids.add(t.subscription.id)
    ]

    if not subs:
        if len(target_ids) == 1:
            msg = "该群目前没有任何订阅..." if session.id2 else "您目前没有任何订阅..."
        else:
            msg = f"指定的 {len(target_ids)} 个目标群组目前没有任何订阅..."
        await MessageUtils.build_message(msg).finish()

    notebook = NotebookData(elements=[])
    if len(target_ids) == 1:
        notebook.head("B站订阅列表", level=1)
    else:
        notebook.head(f"B站订阅列表 ({len(target_ids)} 个目标群组)", level=1)
    notebook.text("使用 `bili sub del <ID>` 或 `bili sub config <ID> ...` 来管理订阅。")

    for sub in sorted(subs, key=lambda s: s.id):
        # 优先检查本地头像缓存，命中则无需调用API获取face_url
        # 典型场景：订阅列表已查看过一次后，头像已缓存到本地
        cached_avatar = AVATAR_CACHE_DIR / f"{sub.uid}.png"
        if cached_avatar.exists():
            avatar_path = cached_avatar
            logger.debug(f"头像缓存命中，跳过API调用: UID {sub.uid}")
        else:
            # 缓存未命中，需要调用API获取头像URL并下载
            face_url = ""
            try:
                card_info = await get_user_card(sub.uid)
                if card_info:
                    face_url = card_info.get("face", "")
            except Exception as e:
                logger.warning(f"获取UID {sub.uid} 的用户信息失败: {e}")
            avatar_path = await get_cached_avatar(sub.uid, face_url)
        avatar_uri = avatar_path.absolute().as_uri() if avatar_path else ""

        subtitle = f"UID: {sub.uid} | 房间号: {sub.room_id or '无'}"

        user_block = UserInfoBlock(
            name=f"[{sub.id}] {sub.uname or '未知名称'}",
            avatar_url=avatar_uri,
            subtitle=subtitle,
        )
        notebook.add_component(user_block)

        status_layout = LayoutData.row(gap="8px", align_items="center")

        dynamic_text = "@ 动态" if sub.at_all_dynamic else "动态"
        dynamic_color = "success" if sub.push_dynamic else "info"
        status_layout.add_item(ui.badge(dynamic_text, color_scheme=dynamic_color))

        video_text = "@ 视频" if sub.at_all_video else "视频"
        video_color = "success" if sub.push_video else "info"
        status_layout.add_item(ui.badge(video_text, color_scheme=video_color))

        live_text = "@ 直播" if sub.at_all_live else "直播"
        live_color = "success" if sub.push_live else "info"
        status_layout.add_item(ui.badge(live_text, color_scheme=live_color))

        notebook.add_component(status_layout.build())
        # 创建一个自定义的分隔线组件并添加
        custom_divider = ui.divider(color="#fce4ec", thickness="1px", margin="25px 0")
        notebook.add_component(custom_divider)

    img_bytes = await ui.render(notebook, use_cache=False)
    await MessageUtils.build_message(img_bytes).finish()


@bili_admin_matcher.assign("sub.add")
async def handle_add(
    session: EventSession,
    ids: Query[list[str]] = Query("sub.add.ids", []),
    gids: Query[list[int]] = Query("sub.add.group.gids", []),
    matcher: Matcher = Matcher(),
):
    target_ids = await get_target_ids(session, gids)
    if not target_ids:
        await MessageUtils.build_message("未能确定操作目标，请检查指令。").finish()

    if not ids.available:
        await MessageUtils.build_message("请提供至少一个UP主UID。").finish()

    results = []
    # 记录已通过 add_up_sub 完成首次订阅处理的 UID，避免同一 UID 被多群订阅时
    # 重复调用 get_user_card。同一 UID 仅首次走完整流程，后续目标仅追加 target 关系。
    processed_uids: set[int] = set()
    for target_id in target_ids:
        group_str = f" [目标: {target_id.replace('group_', '')}]"
        for bilibili_id_str in ids.result:
            bilibili_id_str = bilibili_id_str.strip()

            if not bilibili_id_str.isdigit():
                result = f"❌ 无效的ID：{bilibili_id_str}（仅支持纯数字的UP主UID）。"
            else:
                bilibili_id = int(bilibili_id_str)
                if bilibili_id in processed_uids:
                    # 该 UID 已建立 BiliSub 订阅记录，仅追加 target 关系，
                    # 不再调用 get_user_card，避免重复 API 请求
                    logger.debug(
                        f"UID {bilibili_id} 已存在订阅记录，仅追加目标: {target_id}"
                    )
                    result = await add_target_to_sub(bilibili_id, target_id)
                else:
                    # 首次订阅该 UID，走完整流程（调用 get_user_card）
                    result = await add_up_sub(bilibili_id, target_id)
                    # 无论成功或失败都标记为已处理：成功则后续目标可走 add_target_to_sub；
                    # 失败则 add_target_to_sub 内部会回退到 add_up_sub 进行重试
                    processed_uids.add(bilibili_id)
            results.append(f"{result}{group_str}")

    await MessageUtils.build_message("\n---\n".join(results)).finish()


@bili_admin_matcher.assign("sub.del")
async def handle_del(
    session: EventSession,
    db_ids: Query[list[int]] = Query("sub.del.db_ids"),
    gids: Query[list[int]] = Query("sub.del.group.gids", []),
):
    target_ids = await get_target_ids(session, gids)
    if not target_ids:
        await MessageUtils.build_message("未能确定操作目标，请检查指令。").finish()

    if not db_ids.available:
        await MessageUtils.build_message(
            "请提供至少一个要删除的订阅ID (通过 `bili sub list` 查看)。"
        ).finish()

    total_deleted_count = 0
    fail_list = []
    for db_id in db_ids.result:
        deleted_for_id = await BiliSubTarget.filter(
            subscription_id=db_id, target_id__in=target_ids
        ).delete()
        if deleted_for_id > 0:
            total_deleted_count += deleted_for_id
        else:
            fail_list.append(str(db_id))

    await BiliSubTarget.clean_orphaned_subs()

    msg = f"成功从 {len(target_ids)} 个目标中删除了 {total_deleted_count} 个订阅关系。"
    if fail_list:
        msg += f"\n未能删除对ID {', '.join(fail_list)} 的订阅关系 (可能ID错误或不属于目标群组)。"

    await MessageUtils.build_message(msg).finish()


@bili_admin_matcher.assign("sub.config")
async def handle_config(
    session: EventSession,
    params: Query[list[str]] = Query("sub.config.params"),
    gids: Query[list[int]] = Query("sub.config.group.gids", []),
):
    target_ids = await get_target_ids(session, gids)
    if not target_ids:
        await MessageUtils.build_message("未能确定操作目标，请检查指令。").finish()

    if not params.available:
        await MessageUtils.build_message(
            "用法错误: `bili sub config <ID...> [+|-][类型...]`"
        ).finish()

    param_list = params.result
    db_ids = [int(p) for p in param_list if p.isdigit()]
    settings = [p for p in param_list if not p.isdigit()]

    if not db_ids or not settings:
        await MessageUtils.build_message(
            "用法错误: `bili sub config <ID...> [+|-][类型...]`"
        ).finish()

    owned_subs: list[int] = cast(
        list[int],
        await BiliSubTarget.filter(
            target_id__in=target_ids, subscription_id__in=db_ids
        ).values_list("subscription_id", flat=True),
    )
    valid_ids = set(owned_subs)
    invalid_ids = set(db_ids) - valid_ids

    if not valid_ids:
        await MessageUtils.build_message(
            f"你没有权限配置ID为 {', '.join(map(str, invalid_ids))} 的订阅。"
        ).finish()

    updates = {}
    for setting in settings:
        if not setting or setting[0] not in "+-":
            continue

        value = setting.startswith("+")

        if setting.startswith(("+at:", "-at:")):
            key = setting[4:]
            at_mapping = {
                "dynamic": "at_all_dynamic",
                "video": "at_all_video",
                "live": "at_all_live",
            }
            if key in at_mapping:
                updates[at_mapping[key]] = value
            elif key == "all":
                for field in at_mapping.values():
                    updates[field] = value
            continue

        key = setting[1:]

        # 推送开关配置项统一只支持英文关键词
        if key == "dynamic":
            updates["push_dynamic"] = value
        elif key == "video":
            updates["push_video"] = value
        elif key == "live":
            updates["push_live"] = value
        elif key == "all":
            updates.update(
                {"push_dynamic": value, "push_video": value, "push_live": value}
            )

    if not updates:
        await MessageUtils.build_message(
            "未提供有效的配置项（如: +live, -video, +at:live）。"
        ).finish()

    await BiliSub.filter(id__in=list(valid_ids)).update(**updates)

    msg = f"已为订阅ID {', '.join(map(str, valid_ids))} 更新了推送设置。"
    if invalid_ids:
        msg += f"\n无法配置ID: {', '.join(map(str, invalid_ids))} (权限不足或ID错误)。"

    await MessageUtils.build_message(msg).finish()


@bili_admin_matcher.assign("sub.clear")
async def handle_clear(
    bot: Bot,
    event: Event,
    matcher: Matcher,
    session: EventSession,
    arp: Arparma = AlconnaMatches(),
):
    use_g = arp.query("sub.clear.group") is not None
    use_all = arp.query("sub.clear.all") is not None
    if use_g or use_all:
        if not await SUPERUSER(bot, event):
            await MessageUtils.build_message(
                "❌ 只有超级用户才能使用 --all 或 -g 参数。"
            ).finish()

    target_ids: list[str] = []
    description = ""
    if use_all:
        target_ids = [
            str(x)
            for x in await BiliSubTarget.all()
            .distinct()
            .values_list("target_id", flat=True)
        ]
        description = f"所有 {len(target_ids)} 个目标"
    else:
        gids_tuple = arp.query("sub.clear.group.gids")
        if gids_tuple:
            target_ids = [f"group_{gid}" for gid in gids_tuple]
            description = f"{len(target_ids)} 个指定目标"
        else:
            # 根据 session 上下文确定目标 ID；id2 存在视为群聊，否则取 id1 视为私聊
            if session.id2:
                target_ids = [f"group_{session.id2}"]
            elif session.id1:
                target_ids = [f"private_{session.id1}"]
            else:
                # id1 与 id2 均为空，无法确定会话目标，避免生成 "private_None"
                logger.warning("无法从会话上下文确定目标 ID：id1 和 id2 均为空")
                target_ids = []
            description = "当前会话"

    if not target_ids:
        await MessageUtils.build_message("未能确定操作目标，请检查指令。").finish()

    subs_to_delete_count = await BiliSubTarget.filter(target_id__in=target_ids).count()

    if subs_to_delete_count == 0:
        await MessageUtils.build_message("当前没有任何订阅可供清空。").finish()

    confirm_msg = f"⚠️ 你确定要清空「{description}」的 {subs_to_delete_count} 个订阅吗？\n请在30秒内回复【确认/是/yes】以继续，回复【否/取消/no】或其它内容将取消操作。"

    def check_confirm(msg: Message):
        reply_text = msg.extract_plain_text().strip().lower()
        return reply_text in ["确认", "是", "yes", "否", "取消", "no"]

    confirmed = await prompt_until(confirm_msg, check_confirm, timeout=30)

    if confirmed:
        reply_text = confirmed.extract_plain_text().strip().lower()
        if reply_text in ["确认", "是", "yes"]:
            deleted_count = await BiliSubTarget.filter(
                target_id__in=target_ids
            ).delete()
            await BiliSubTarget.clean_orphaned_subs()
            msg = f"✅ 已成功清空「{description}」的 {deleted_count} 个订阅。"
            await MessageUtils.build_message(msg).finish()
        else:
            await MessageUtils.build_message("ℹ️ 操作已取消。").finish()
    else:
        await MessageUtils.build_message("⌛ 操作超时，已自动取消。").finish()


@bili_admin_matcher.assign("sub.checkall")
async def handle_check_all(bot: Bot, event: Event, matcher: Matcher):
    # checkall 为超级用户指令，在管理 matcher 中额外校验超级用户权限
    if not await SUPERUSER(bot, event):
        await MessageUtils.build_message("❌ 仅超级用户可用此指令。").finish()

    await matcher.send("开始主动检查所有B站订阅，请稍候...")

    bots = nonebot.get_bots()
    if not bots:
        await MessageUtils.build_message(
            "没有任何机器人实例在线，无法执行检查。"
        ).finish()

    bot_instance: Bot = next(iter(bots.values()))

    await check_and_refresh_credential()

    all_subs = await BiliSub.all()
    if not all_subs:
        await MessageUtils.build_message("数据库中没有任何订阅。").finish()

    _checkall_semaphore = asyncio.Semaphore(5)

    async def _check_sub_and_send(sub: BiliSub) -> int:
        """检查单个订阅并发送更新，复用 data_source.check_and_send_update 避免逻辑重复。"""
        async with _checkall_semaphore:
            # check_and_send_update 内部已处理超时、异常、时间戳更新等逻辑，
            # 此处仅需通过信号量限制并发数
            return await check_and_send_update(sub, bot_instance, force_push=False)

    tasks = [_check_sub_and_send(sub) for sub in all_subs]
    results = await asyncio.gather(*tasks)
    update_count = sum(results)

    await MessageUtils.build_message(
        f"✅ 主动检查完成！\n共检查 {len(all_subs)} 个订阅，发现了 {update_count} 个更新并已推送。"
    ).finish()


@bili_admin_matcher.assign("sub.forcepush")
async def handle_force_push(
    bot: Bot,
    event: Event,
    session: EventSession,
    matcher: Matcher,
    db_ids: Query[list[int]] = Query("sub.forcepush.db_ids", []),
):
    # forcepush 为超级用户指令，在管理 matcher 中额外校验超级用户权限
    if not await SUPERUSER(bot, event):
        await MessageUtils.build_message("❌ 仅超级用户可用此指令。").finish()

    if not db_ids.available or not db_ids.result:
        await MessageUtils.build_message(
            "请提供至少一个要推送的订阅ID (通过 `bili sub list` 查看)。"
        ).finish()

    bots = nonebot.get_bots()
    if not bots:
        await MessageUtils.build_message(
            "没有任何机器人实例在线，无法执行推送。"
        ).finish()

    results = []
    for db_id in db_ids.result:
        sub = await BiliSub.get_or_none(id=db_id)
        if not sub:
            results.append(f"❌ 未找到ID为 {db_id} 的订阅。")
            continue

        # 并发去重：避免与定时任务/checkall 同时检查同一订阅导致重复推送
        # 与 check_and_send_update 共用 _checking_subs 集合，晚来的任务直接跳过
        if sub.id in _checking_subs:
            logger.info(
                f"强制推送跳过（已有其他任务正在检查）: UID={sub.uid}, 名称={sub.uname}, 订阅ID={db_id}"
            )
            results.append(f"ℹ️ [{db_id}] {sub.uname} 正在被其他任务检查，已跳过。")
            continue

        _checking_subs.add(sub.id)
        try:
            await matcher.send(
                f"正在为 [{db_id}] {sub.uname} 获取最新内容并强制推送..."
            )

            try:
                result: SubCheckResult = await asyncio.wait_for(
                    get_sub_status(sub, force_push=True), timeout=120
                )
                if result.notifications:
                    for notification in result.notifications:
                        await MessageUtils.build_message(
                            notification.content
                        ).send()  # 强制推送仅发送到当前会话
                    results.append(f"✅ 已成功推送 [{db_id}] {sub.uname} 的最新内容。")
                else:
                    results.append(
                        f"ℹ️ 未能为 [{db_id}] {sub.uname} 获取到可推送的最新内容。"
                    )

                if result.new_live_status is not None:
                    await sub.save(update_fields=["live_status"])
                    logger.debug(
                        f"force_push 已持久化直播状态: UID={sub.uid}, 订阅ID={db_id}, 新状态={result.new_live_status}"
                    )
            except asyncio.TimeoutError:
                results.append(f"❌ 为 [{db_id}] {sub.uname} 获取内容超时。")
            except Exception as e:
                logger.error(f"强制推送时发生错误: ID={db_id}", e=e)
                results.append(f"❌ 为 [{db_id}] {sub.uname} 推送时发生内部错误: {e}")
        finally:
            # 无论正常结束还是异常退出，都从检查集合中移除，允许后续任务再次检查
            _checking_subs.discard(sub.id)

    await MessageUtils.build_message("\n---\n".join(results)).finish()


# ============================================================
# 被动解析开关指令处理（群管权限）
# ============================================================


@bili_admin_matcher.assign("开启群被动解析")
async def handle_open_parse(session: EventSession, matcher: Matcher):
    """开启当前群组的B站被动解析"""
    group_id = str(session.id2) if session.id2 else None
    if not group_id:
        await MessageUtils.build_message("该指令仅在群聊中可用。").finish()

    # 超级用户禁用的被动技能，普通管理员无法开启
    if await GroupConsole.is_superuser_block_task(group_id, MODULE_NAME):
        await MessageUtils.build_message(
            "❌ 该被动技能已被超级用户禁用，无法开启。"
        ).finish()

    await GroupConsole.set_unblock_task(group_id, MODULE_NAME)
    logger.info(f"已开启群 {group_id} 的B站被动解析")
    await MessageUtils.build_message("✅ 已开启当前群组的B站被动解析。").finish()


@bili_admin_matcher.assign("关闭群被动解析")
async def handle_close_parse(session: EventSession, matcher: Matcher):
    """关闭当前群组的B站被动解析"""
    group_id = str(session.id2) if session.id2 else None
    if not group_id:
        await MessageUtils.build_message("该指令仅在群聊中可用。").finish()

    await GroupConsole.set_block_task(group_id, MODULE_NAME)
    logger.info(f"已关闭群 {group_id} 的B站被动解析")
    await MessageUtils.build_message("✅ 已关闭当前群组的B站被动解析。").finish()


# ============================================================
# 账号登录/状态/登出指令处理（超级用户）
# ============================================================


@bili_su_matcher.assign("login")
async def handle_login(bot: Bot, event: Event, matcher: Matcher):
    user_id = event.get_user_id()
    if not user_id:
        await MessageUtils.build_message("无法获取用户ID，无法开始登录。").finish()

    if user_id in login_sessions:
        _, start_time, old_task, _ = login_sessions[user_id]
        elapsed_time = time.time() - start_time

        if elapsed_time > LOGIN_TIMEOUT:
            # 取消旧的后台轮询任务，避免它误清除新会话
            old_task.cancel()
            del login_sessions[user_id]
            await MessageUtils.build_message(
                f"您上一个登录会话已超时（超过{LOGIN_TIMEOUT}秒），已自动取消。\n现在为您创建新的登录会话..."
            ).send()
        else:
            remaining_time = int(LOGIN_TIMEOUT - elapsed_time)
            await MessageUtils.build_message(
                f"您已有一个登录会话正在进行中，请在 {remaining_time} 秒内完成或等待超时后重试。"
            ).finish()

    try:
        login_handler = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        # 先生成二维码，成功后再创建后台轮询任务并注册会话，
        # 避免 generate_qrcode 失败时后台任务已启动导致的竞态条件
        await login_handler.generate_qrcode()

        task = asyncio.create_task(check_login_status(matcher, user_id))
        # 为每个登录会话创建独立的状态检查锁，序列化 check_state() 调用
        login_sessions[user_id] = (login_handler, time.time(), task, asyncio.Lock())

        qr_picture_obj = login_handler.get_qrcode_picture()
        if not qr_picture_obj or not qr_picture_obj.content:
            # 获取二维码图像失败，取消后台轮询任务并清理会话，避免任务泄漏
            task.cancel()
            login_sessions.pop(user_id, None)
            await MessageUtils.build_message("获取二维码图像失败，请重试。").finish()

        msg_parts = ["请使用B站APP扫描二维码登录：", qr_picture_obj.content]
        await MessageUtils.build_message(msg_parts).send()
    except FinishedException:
        # finish() 抛出的 FinishedException 不应被下面的异常处理捕获，
        # 否则会导致发送错误的错误信息并误清理登录会话
        raise
    except Exception as e:
        # 取消可能已创建的后台轮询任务，避免任务泄漏
        if user_id in login_sessions:
            _, _, task, _ = login_sessions[user_id]
            if not task.done():
                task.cancel()
                logger.debug(f"已取消用户 {user_id} 的后台登录轮询任务")
        login_sessions.pop(user_id, None)
        await MessageUtils.build_message(f"生成登录二维码失败: {e}").finish()


async def check_login_status(matcher: Matcher, user_id: str):
    """后台轮询检查二维码登录状态，并在成功或失败时通知用户"""
    if user_id not in login_sessions:
        return

    login_handler, start_time, _, state_lock = login_sessions[user_id]
    scan_message_sent = False

    logger.info(f"开始为用户 {user_id} 自动检查登录状态...")

    try:
        while time.time() - start_time < LOGIN_TIMEOUT:
            try:
                # 使用锁序列化 check_state() 调用，避免与 handle_status 并发检查
                # 导致重复保存凭证或重复发送成功消息
                async with state_lock:
                    # 再次检查会话是否仍存在（handle_status 可能已处理并移除）
                    if user_id not in login_sessions:
                        break
                    status = await login_handler.check_state()

                    if status == login_v2.QrCodeLoginEvents.DONE:
                        credential = login_handler.get_credential()

                        # 尝试从 httpx 会话中获取 buvid3，补全凭证
                        try:
                            session = cast(httpx.AsyncClient, get_session())
                            if buvid3_cookie := session.cookies.get("buvid3"):
                                logger.info(f"成功获取到 buvid3: {buvid3_cookie}")
                                credential.buvid3 = buvid3_cookie
                        except Exception as e:
                            logger.warning(f"尝试获取 buvid3 时出错: {e}")

                        await save_credential_to_file(credential)
                        dedeuserid = getattr(credential, "dedeuserid", "未知")
                        await matcher.send(
                            f"🎉 登录成功！账号UID {dedeuserid} 的凭证已保存。"
                        )
                        break
                    elif status == login_v2.QrCodeLoginEvents.TIMEOUT:
                        await matcher.send(
                            "二维码已过期，请重新发送 `bili login` 获取。"
                        )
                        break
                    elif (
                        status == login_v2.QrCodeLoginEvents.SCAN
                        and not scan_message_sent
                    ):
                        await matcher.send("已扫码，请在手机上确认登录...")
                        scan_message_sent = True

                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"检查用户 {user_id} 登录状态时出错", e=e)
                # matcher.send 本身也可能失败（如用户已拉黑机器人），
                # 需捕获以确保后续会话清理逻辑能正常执行
                try:
                    await matcher.send("检查登录状态时发生错误，流程已终止。")
                except Exception as send_err:
                    logger.warning(f"发送登录错误通知失败: {send_err}")
                break
    finally:
        # 无论循环正常结束还是异常退出，都确保清理登录会话，
        # 避免因 matcher.send 失败导致会话残留
        # 仅当 login_sessions 中记录的仍是本次任务的 login_handler 时才移除，
        # 避免旧的后台任务误清除用户重新登录后创建的新会话
        current = login_sessions.get(user_id)
        if current is not None and current[0] is login_handler:
            login_sessions.pop(user_id, None)


@bili_su_matcher.assign("status")
async def handle_status(bot: Bot, event: Event, matcher: Matcher):
    user_id = event.get_user_id()
    if not user_id:
        await MessageUtils.build_message("无法获取用户ID，无法检查状态。").send()
        return

    # 优先检查是否有正在进行的登录会话
    if user_id in login_sessions:
        login_session, _, old_task, state_lock = login_sessions[user_id]
        try:
            # 使用锁序列化 check_state() 调用，避免与后台 check_login_status 并发检查
            # 导致重复保存凭证或重复发送成功消息
            async with state_lock:
                # 再次检查会话是否仍存在（后台任务可能已处理并移除）
                if user_id not in login_sessions:
                    await MessageUtils.build_message(
                        "登录会话已结束，请使用 `bili login` 重新登录。"
                    ).send()
                    return
                status = await login_session.check_state()
                if status == login_v2.QrCodeLoginEvents.DONE:
                    credential = login_session.get_credential()
                    await save_credential_to_file(credential)
                    # 删除会话前先取消后台轮询任务，避免它重复发送"登录成功"消息
                    old_task.cancel()
                    # 使用 pop 而非 del，防止后台任务在 await 期间已移除该条目导致 KeyError
                    login_sessions.pop(user_id, None)
                    dedeuserid = getattr(credential, "dedeuserid", "未知")
                    await MessageUtils.build_message(
                        f"🎉 登录成功！账号UID {dedeuserid} 的凭证已保存。"
                    ).send()
                    return
                elif status == login_v2.QrCodeLoginEvents.TIMEOUT:
                    # 删除会话前先取消后台轮询任务，避免它继续轮询已过期的会话
                    old_task.cancel()
                    # 使用 pop 而非 del，防止后台任务在 await 期间已移除该条目导致 KeyError
                    login_sessions.pop(user_id, None)
                    await MessageUtils.build_message(
                        "二维码已过期，请重新发送 `bili login` 获取新的二维码。"
                    ).send()
                    return
                elif status == login_v2.QrCodeLoginEvents.SCAN:
                    await MessageUtils.build_message(
                        "已扫码，请在手机上确认登录..."
                    ).send()
                    return
                else:
                    await MessageUtils.build_message("等待扫码中...").send()
                    return
        except Exception as e:
            # 异常时也需取消后台任务
            old_task.cancel()
            login_sessions.pop(user_id, None)
            await MessageUtils.build_message(f"检查登录状态失败: {e}").send()
            return

    credential = get_credential()
    if not credential:
        await MessageUtils.build_message(
            "当前未登录B站账号。\n请使用 `bili login` 扫码登录。"
        ).send()
        return

    status_lines = ["B站登录凭证状态："]
    try:
        is_valid = await credential.check_valid()
        if is_valid:
            uid = getattr(credential, "dedeuserid", "未知")
            status_lines.append(f"✅ 凭证有效，当前登录账号UID: {uid}")
            need_refresh = await credential.check_refresh()
            if need_refresh:
                status_lines.append("⚠️ 凭证即将过期，将在下次定时检查时自动刷新。")
        else:
            status_lines.append("❌ 凭证已失效，请使用 `bili login` 重新登录。")
    except Exception as e:
        logger.error("检查凭证有效性时出错", e=e)
        status_lines.append(f"❓ 凭证状态检查失败: {e}")

    await MessageUtils.build_message("\n".join(status_lines)).send()


@bili_su_matcher.assign("logout")
async def handle_logout(matcher: Matcher):
    try:
        credential = get_credential()
        if not credential:
            await MessageUtils.build_message("当前没有已登录的账号。").send()
            return

        uid = getattr(credential, "dedeuserid", "未知")
        await clear_credential()
        await MessageUtils.build_message(f"账号 {uid} 已退出登录").send()

    except Exception as e:
        await MessageUtils.build_message(f"退出登录失败: {e}").finish()


# ============================================================
# 自动下载与配置快捷指令（基于 pconf）
# ============================================================

pconf_cmd.shortcut(
    r"bili 自动下载 on\s*(?P<targets>.*)",
    command="pconf",
    arguments=[
        "set",
        "auto_download_enabled=True",
        "-p",
        MODULE_NAME,
        "{targets}",
    ],
    prefix=True,
)
pconf_cmd.shortcut(
    r"bili 自动下载 off\s*(?P<targets>.*)",
    command="pconf",
    arguments=[
        "set",
        "auto_download_enabled=False",
        "-p",
        MODULE_NAME,
        "{targets}",
    ],
    prefix=True,
)
pconf_cmd.shortcut(
    r"bili 配置\s*(?P<targets>.*)",
    command="pconf",
    arguments=[
        "list",
        "-p",
        MODULE_NAME,
        "{targets}",
    ],
    prefix=True,
)
