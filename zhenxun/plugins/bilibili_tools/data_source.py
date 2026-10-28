import asyncio
import base64
import json
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import cast

import httpx
import nonebot
from bilibili_api import user as bilibili_user_module
from bilibili_api.exceptions import ResponseCodeException
from nonebot.adapters import Bot
from nonebot_plugin_alconna import UniMessage

from zhenxun import ui
from zhenxun.models.group_console import GroupConsole
from zhenxun.services.log import logger
from zhenxun.ui.models import NotebookData
from zhenxun.utils.message import MessageUtils
from zhenxun.utils.platform import PlatformUtils
from zhenxun.utils.utils import ResourceDirManager

from .config import (
    DYNAMIC_PATH,
    MODULE_NAME,
    PUSH_MAX_ATTEMPTS,
    PUSH_RETRY_DELAY,
    SEND_IMAGE_MAX_SIZE_MB,
    base_config,
)
from .filter import is_ad as is_dynamic_ad
from .model import BiliSub, BiliSubTarget
from .sub_utils import (
    get_dynamic_screenshot,
    get_room_info_by_id,
    get_user_card,
    get_user_dynamics,
    get_videos,
)
from .utils.headers import get_bilibili_headers
from .utils.message import ImageHelper

ResourceDirManager.add_temp_dir(DYNAMIC_PATH)

# 正在检查中的订阅 ID 集合，用于避免定时任务与 checkall/forcepush 并发检查同一订阅
# 导致重复推送。同一时间内只允许一个检查任务，晚来的任务直接跳过。
_checking_subs: set[int] = set()

# 单条通知的连续推送失败次数: sub.id -> {通知标识: 失败次数}
# 仅内存记录，重启后清零（最多多重试 PUSH_MAX_ATTEMPTS 次），不影响正确性
_push_fail_counts: dict[int, dict[str, int]] = {}
_TRACKED_KEYS_PER_SUB = 32


def _record_push_failure(sub_id: int, key: str) -> int:
    """记录一次推送失败并返回该通知的累计失败次数"""
    counts = _push_fail_counts.setdefault(sub_id, {})
    counts[key] = counts.get(key, 0) + 1
    if len(counts) > _TRACKED_KEYS_PER_SUB:
        counts.pop(next(iter(counts)))
    return counts[key]


def _clear_push_failure(sub_id: int, key: str) -> None:
    _push_fail_counts.get(sub_id, {}).pop(key, None)


class NotificationType(Enum):
    """通知类型枚举"""

    DYNAMIC = auto()
    VIDEO = auto()
    LIVE = auto()


_notification_type_label = {
    NotificationType.DYNAMIC: "发布了新动态",
    NotificationType.VIDEO: "投稿了新视频",
    NotificationType.LIVE: "开播了",
}


@dataclass
class Notification:
    """通知数据类"""

    content: list
    type: NotificationType
    # 通知对应的内容时间戳与唯一标识（如 dynamic:动态ID、video:BV号）。
    # 推送成功后按条推进订阅进度，使单条失败不再连累同批次已推送成功的部分
    timestamp: int | None = None
    key: str = ""


@dataclass
class SubCheckResult:
    """订阅检查结果，包含通知和待更新的时间戳"""

    notifications: list[Notification]
    # 推送成功后需要更新的动态时间戳
    max_dynamic_timestamp: int | None = None
    # 推送成功后需要更新的视频时间戳
    max_video_timestamp: int | None = None
    # 推送成功后需要更新的直播状态（None 表示无变化）
    # 延迟持久化：仅在通知发送成功后才保存到数据库，避免发送失败导致下次检查无法触发通知
    new_live_status: int | None = None


async def fetch_image_bytes(url: str) -> bytes:
    headers = get_bilibili_headers(referer="https://t.bilibili.com")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.content


async def handle_video_info_error(video_info: dict):
    """处理B站视频信息获取错误并发送通知给超级用户"""
    str_msg = "b站订阅检测失败："
    # 使用 .get() 避免 API 返回数据缺少字段时抛出 KeyError
    error_code = (
        video_info.get("code", "unknown") if isinstance(video_info, dict) else "unknown"
    )
    error_message = (
        video_info.get("message", "未知错误")
        if isinstance(video_info, dict)
        else "返回数据格式异常"
    )
    if error_code == -352:
        str_msg += "风控校验失败，请登录后再尝试。发送'登录b站'"
    elif error_code == -799:
        str_msg += "请求过于频繁，请稍后再试，或适当减小配置文件中的'BATCH_SIZE'以降低并发请求数"
    else:
        str_msg += f"{error_code}，{error_message}"

    bots = nonebot.get_bots()
    for bot_instance in bots.values():
        if bot_instance:
            await PlatformUtils.send_superuser(bot_instance, str_msg)

    return str_msg


async def _add_subscription(
    uid: int,
    target_id: str,
    uname: str,
    push_config: dict,
    initial_timestamps: dict | None = None,
    **extra_fields,
) -> str:
    """通用的添加订阅和目标关系的内部函数"""
    sub, created = await BiliSub.get_or_create(uid=uid)

    if created:
        sub.push_dynamic = push_config.get("dynamic", False)
        sub.push_video = push_config.get("video", False)
        sub.push_live = push_config.get("live", False)
        if initial_timestamps:
            sub.last_dynamic_timestamp = initial_timestamps.get("dynamic", 0)
            sub.last_video_timestamp = initial_timestamps.get("video", 0)

    sub.uname = uname
    for field, value in extra_fields.items():
        if hasattr(sub, field):
            # 对于 room_id 字段，仅在传入的值不为 None 时才覆盖，
            # 避免 add_up_sub 中 room_id=None 覆盖已有的直播间ID
            if field == "room_id" and value is None:
                logger.debug(
                    f"跳过设置 room_id=None，保留已有值: UID={uid}, 当前room_id={sub.room_id}"
                )
                continue
            setattr(sub, field, value)
    await sub.save()

    _, created_target = await BiliSubTarget.get_or_create(
        subscription=sub, target_id=target_id
    )

    if not created_target:
        return f"ℹ️ 你已经订阅过「{uname}」(UID: {uid}) 了。"

    msg_parts = [
        "🎉 订阅成功！",
        f"UP主：{uname}",
        f"UID：{uid}",
    ]
    if sub.room_id:
        msg_parts.append(f"直播间ID：{sub.room_id}")
    return "\n".join(msg_parts)


async def add_up_sub(uid: int, target_id: str) -> str:
    """添加订阅 UP"""
    try:
        try:
            user_info = await get_user_card(uid)
            if not user_info:
                return f"❌ 未找到ID {uid} 的信息，请检查ID是否正确。"
        except ResponseCodeException:
            return f"❌ API请求失败，请检查ID {uid} 是否正确。"

        authoritative_uid = user_info.get("mid")
        if not authoritative_uid:
            return "❌ 无法从B站API返回的数据中解析出核心UID，订阅失败。"
        uname = user_info.get("name", "未知UP主")

        room_id = None
        if user_info.get("live_room") and user_info["live_room"].get("roomid"):
            room_id = user_info["live_room"]["roomid"]
            logger.info(
                f"从用户信息中直接找到直播间ID: {room_id} (UID: {authoritative_uid})"
            )
        else:
            try:
                user_instance = bilibili_user_module.User(uid=authoritative_uid)
                live_info = await user_instance.get_live_info()
                if live_info and live_info.get("roomid"):
                    room_id = live_info.get("roomid")
                    logger.info(
                        f"通过单独请求找到直播间ID: {room_id} (UID: {authoritative_uid})"
                    )
            except ResponseCodeException:
                logger.debug(
                    f"后备方案：UID {authoritative_uid} 没有关联的直播间或查询失败。"
                )

        default_push_types = base_config.get(
            "DEFAULT_UP_PUSH_TYPES", ["dynamic", "video", "live"]
        )
        push_config = {
            "dynamic": "dynamic" in default_push_types,
            "video": "video" in default_push_types,
            "live": "live" in default_push_types,
        }
        # 新订阅将"最近更新时间戳"设为当前时刻，避免下次检查时把所有历史动态/视频都推送
        current_timestamp = int(datetime.now().timestamp())
        initial_timestamps = {
            "dynamic": current_timestamp,
            "video": current_timestamp,
        }

        return await _add_subscription(
            authoritative_uid,
            target_id,
            uname,
            push_config,
            initial_timestamps,
            room_id=room_id,
        )

    except Exception as e:
        logger.error(f"订阅Up id：{uid} 发生了未预料的错误 {type(e)}：{e}")
        logger.error(traceback.format_exc())
        return "❌ 订阅失败（未知错误）。"


async def add_target_to_sub(uid: int, target_id: str) -> str:
    """为已存在的订阅追加推送目标关系

    适用于同一 UP 主被订阅到多个群的场景：第一次调用 add_up_sub 创建 BiliSub 订阅记录
    （会调用 get_user_card 获取UP主信息），后续目标只需调用本函数追加 target 关系，
    避免对同一 UID 重复调用 get_user_card 造成 API 请求浪费。

    若传入的 UID 尚未建立 BiliSub 订阅记录（例如首次订阅失败后的重试场景），
    本函数会自动回退到 add_up_sub 完整流程。

    Args:
        uid: UP 主 UID
        target_id: 推送目标 ID（如 group_xxx、private_xxx）

    Returns:
        操作结果消息
    """
    sub = await BiliSub.get_or_none(uid=uid)
    if not sub:
        # 订阅记录不存在，回退到完整流程（会调用 get_user_card）
        logger.debug(f"订阅记录不存在，回退到 add_up_sub 完整流程: UID={uid}")
        return await add_up_sub(uid, target_id)

    _, created_target = await BiliSubTarget.get_or_create(
        subscription=sub, target_id=target_id
    )
    if not created_target:
        return f"ℹ️ 你已经订阅过「{sub.uname}」(UID: {uid}) 了。"

    msg_parts = [
        "🎉 订阅成功！",
        f"UP主：{sub.uname}",
        f"UID：{uid}",
    ]
    if sub.room_id:
        msg_parts.append(f"直播间ID：{sub.room_id}")
    return "\n".join(msg_parts)


async def get_sub_status(sub: BiliSub, force_push: bool = False) -> SubCheckResult:
    """获取订阅状态"""
    start_time = time.time()
    all_notifications: list[Notification] = []
    max_dynamic_timestamp: int | None = None
    max_video_timestamp: int | None = None
    new_live_status: int | None = None

    try:
        if sub.push_dynamic or sub.push_video:
            up_result = await _get_up_status(sub, force_push)
            if up_result.notifications:
                all_notifications.extend(up_result.notifications)
            if up_result.max_dynamic_timestamp is not None:
                max_dynamic_timestamp = up_result.max_dynamic_timestamp
            if up_result.max_video_timestamp is not None:
                max_video_timestamp = up_result.max_video_timestamp

        if sub.push_live and sub.room_id:
            live_result = await _get_live_status(sub)
            if live_result.notifications:
                all_notifications.extend(live_result.notifications)
            if live_result.new_live_status is not None:
                new_live_status = live_result.new_live_status

    except ResponseCodeException as msg:
        error_code = getattr(msg, "code", "unknown")
        error_message = getattr(msg, "msg", str(msg))
        logger.error(
            f"订阅状态检查失败: UID={sub.uid}, 错误码={error_code}, 错误信息={error_message}"
        )
        return SubCheckResult(notifications=[])
    except Exception as e:
        logger.error(
            f"订阅状态检查发生未预期异常: UID={sub.uid}, 异常类型={type(e).__name__}, 异常信息={e}"
        )
        logger.debug(f"异常详细信息:\n{traceback.format_exc()}")
        return SubCheckResult(notifications=[])

    duration = time.time() - start_time
    if all_notifications:
        logger.info(
            f"订阅状态检查完成: UID={sub.uid}, 检测到 {len(all_notifications)} 个更新, 耗时={duration:.2f}秒"
        )
    else:
        logger.debug(
            f"订阅状态检查完成: UID={sub.uid}, 未检测到更新, 耗时={duration:.2f}秒"
        )

    return SubCheckResult(
        notifications=all_notifications,
        max_dynamic_timestamp=max_dynamic_timestamp,
        max_video_timestamp=max_video_timestamp,
        new_live_status=new_live_status,
    )


async def _get_live_status(sub: BiliSub) -> SubCheckResult:
    """获取直播订阅状态

    注意：直播状态变更时不立即保存到数据库，而是通过 SubCheckResult.new_live_status
    传递给调用方，待通知发送成功后再持久化，避免发送失败导致下次检查无法触发通知。
    """
    start_time = time.time()
    if not sub.room_id:
        return SubCheckResult(notifications=[])

    try:
        logger.debug(f"获取直播间信息: 房间ID={sub.room_id}")
        live_info_raw = await get_room_info_by_id(sub.room_id)
        if not live_info_raw or not live_info_raw.get("room_info"):
            logger.error(
                f"直播间信息获取失败或结构异常: 房间ID={sub.room_id}, 返回数据={live_info_raw}"
            )
            return SubCheckResult(notifications=[])

        live_info = live_info_raw["room_info"]
        logger.debug(f"成功获取直播间信息: 房间ID={sub.room_id}, 数据结构完整")
    except Exception as e:
        logger.error(
            f"获取直播间信息异常: 房间ID={sub.room_id}, 异常类型={type(e).__name__}, 异常信息={e}"
        )
        logger.debug(f"异常详细信息:\n{traceback.format_exc()}")
        return SubCheckResult(notifications=[])

    title = live_info["title"]
    room_id = live_info["room_id"]
    live_status = live_info["live_status"]
    cover = live_info.get("cover")
    logger.debug(
        f"直播间信息: 房间ID={sub.room_id}, 实际房间ID={room_id}, 标题={title}, 直播状态={live_status}"
    )

    old_live_status = sub.live_status
    logger.debug(
        f"订阅信息: 房间ID={sub.room_id}, 主播名={sub.uname}, 当前状态={old_live_status}, API状态={live_status}"
    )

    notifications = []
    # 延迟持久化：仅更新内存中的 live_status，不立即保存到数据库。
    # new_live_status 用于在通知发送成功后由调用方统一持久化，
    # 避免发送失败时状态已更新导致下次检查无法触发通知。
    new_live_status: int | None = None
    if old_live_status != live_status:
        logger.info(
            f"直播状态变化: 房间ID={sub.room_id}, 主播={sub.uname}, 旧状态={old_live_status}, 新状态={live_status}"
        )
        sub.live_status = live_status
        new_live_status = live_status
        logger.debug(
            f"已更新内存中的直播状态（待推送成功后持久化）: 房间ID={sub.room_id}, 新状态={live_status}"
        )
    else:
        logger.debug(f"直播状态未变化: 房间ID={sub.room_id}, 状态={live_status}")

    # old_live_status 可能为 None（model.py 中 live_status 字段允许为 null），
    # 将 None 视为"未直播"状态，确保首次检查时若主播正在直播也能触发开播通知
    if (
        (old_live_status is None or old_live_status in [0, 2])
        and live_status == 1
        and cover
    ):
        logger.info(f"检测到开播: 房间ID={sub.room_id}, 主播={sub.uname}, 标题={title}")

        notebook = NotebookData(elements=[])
        notebook.image(cover)
        notebook.head(f"{sub.uname} 开播啦！🎉", level=2)
        notebook.text(f"**标题：** {title}")
        notebook.text(
            f"**直播间：** [https://live.bilibili.com/{room_id}](https://live.bilibili.com/{room_id})"
        )

        img_bytes = await ui.render(notebook, use_cache=False)
        notifications.append(
            Notification(
                content=[img_bytes, f"直播间链接: https://live.bilibili.com/{room_id}"],
                type=NotificationType.LIVE,
                key=f"live:{room_id}:{live_status}",
            )
        )

    duration = time.time() - start_time
    if notifications:
        logger.info(
            f"直播状态检查完成: 房间ID={sub.room_id}, 检测到开播, 耗时={duration:.2f}秒"
        )
    else:
        logger.debug(
            f"直播状态检查完成: 房间ID={sub.room_id}, 未检测到开播, 耗时={duration:.2f}秒"
        )

    return SubCheckResult(
        notifications=notifications,
        new_live_status=new_live_status,
    )


async def _get_up_status(sub: BiliSub, force_push: bool = False) -> SubCheckResult:
    start_time = time.time()

    try:
        video_info_raw = await get_videos(sub.uid)

        # 类型检查：确保返回的是字典类型，否则走错误处理
        if not isinstance(video_info_raw, dict):
            logger.error(
                f"视频信息格式错误: UID={sub.uid}, 返回类型={type(video_info_raw)}"
            )
            await handle_video_info_error(
                {"code": -1, "message": "获取视频信息时返回了非字典类型"}
            )
            return SubCheckResult(notifications=[])

        # 错误码检查：在用户名提取之前检查API返回的错误码，
        # 避免API返回错误时因提取用户名失败（如 KeyError）被 except 捕获而提前返回，
        # 导致 handle_video_info_error 未被调用、错误告警丢失
        if "code" in video_info_raw and video_info_raw.get("code", 0) != 0:
            logger.error(
                f"视频API返回错误: UID={sub.uid}, 错误码={video_info_raw.get('code')}, 错误信息={video_info_raw.get('message', '未知错误')}"
            )
            await handle_video_info_error(video_info_raw)
            return SubCheckResult(notifications=[])

        uname = None
        vlist = None
        if "data" in video_info_raw and video_info_raw["data"].get("list", {}).get(
            "vlist"
        ):
            vlist = video_info_raw["data"]["list"]["vlist"]
            uname = vlist[0].get("author")

        if not uname:
            logger.debug(f"视频信息中未找到用户名，回退到 get_user_card: UID={sub.uid}")
            user_info = await get_user_card(sub.uid)
            if not user_info:
                logger.warning(f"UP主信息获取失败: UID={sub.uid}")
                return SubCheckResult(notifications=[])
            # 使用 .get() 避免 user_info 缺少 name 字段时抛出 KeyError
            uname = user_info.get("name", "未知UP主")
        else:
            logger.debug(
                f"成功从视频信息中获取UP主用户名: UID={sub.uid}, 用户名={uname}"
            )

    except Exception as e:
        logger.error(
            f"获取视频列表异常: UID={sub.uid}, 异常类型={type(e).__name__}, 异常信息={e}"
        )
        logger.debug(f"异常详细信息:\n{traceback.format_exc()}")
        return SubCheckResult(notifications=[])

    logger.debug(f"解析视频数据结构: UID={sub.uid}")
    if "list" in video_info_raw and "page" in video_info_raw:
        video_info_data = video_info_raw
        logger.debug(f"使用直接返回的视频数据结构: UID={sub.uid}")
    elif "data" in video_info_raw and isinstance(video_info_raw["data"], dict):
        video_info_data = video_info_raw["data"]
        logger.debug(f"使用data字段中的视频数据结构: UID={sub.uid}")
    else:
        logger.error(
            f"视频数据结构不符合预期: UID={sub.uid}, 数据结构={list(video_info_raw.keys())}"
        )
        await handle_video_info_error(video_info_raw.get("data", video_info_raw))
        return SubCheckResult(notifications=[])

    notifications: list[Notification] = []

    if sub.uname != uname:
        logger.info(
            f"UP主用户名变更: UID={sub.uid}, 旧名称={sub.uname}, 新名称={uname}"
        )
        sub.uname = uname
        await sub.save(update_fields=["uname"])
        logger.debug(f"已更新UP主用户名: UID={sub.uid}, 新名称={uname}")

    # 处理动态
    max_dynamic_timestamp = sub.last_dynamic_timestamp
    if sub.push_dynamic:
        try:
            # get_all_new_dynamics 同时返回原始动态数据，复用给 is_ad 避免N+1 API调用
            new_dynamics, dynamics_data = await get_all_new_dynamics(sub, force_push)
            for dynamic in new_dynamics:
                if not dynamic["img"]:
                    continue

                dynamic_time = datetime.fromtimestamp(dynamic["timestamp"])
                dynamic_time_str = dynamic_time.strftime("%Y-%m-%d %H:%M:%S")

                is_ad = False
                if base_config.get("ENABLE_AD_FILTER") and not force_push:
                    logger.info(
                        f"[广告过滤] 启用广告过滤检查: UID={sub.uid}, 用户名={sub.uname}, 动态ID={dynamic['id']}"
                    )
                    try:
                        # 传入预获取的 dynamics_data，避免 is_ad 内部重复调用 get_user_dynamics
                        is_ad = await is_dynamic_ad(
                            sub.uid, dynamic["id"], dynamics_data
                        )
                        if is_ad:
                            logger.warning(
                                f"[广告过滤] 动态被过滤拦截: UID={sub.uid}, 用户名={sub.uname}, 动态ID={dynamic['id']}"
                            )
                            # 更新时间戳，避免重复检测
                            if (
                                max_dynamic_timestamp is None
                                or dynamic["timestamp"] > max_dynamic_timestamp
                            ):
                                max_dynamic_timestamp = dynamic["timestamp"]
                            continue
                    except Exception as e:
                        logger.error(
                            f"[广告过滤] 过滤检查异常: UID={sub.uid}, 动态ID={dynamic['id']}, 错误={e}"
                        )

                logger.info(
                    f"准备推送新动态: UID={sub.uid}, 动态ID={dynamic['id']}, 发布时间={dynamic_time_str}"
                )

                # 创建动态通知
                notebook = NotebookData(elements=[])
                notebook.head(f"{uname} 发布了动态！📢", level=2)
                base64_str = base64.b64encode(dynamic["img"]).decode()
                notebook.image(f"data:image/png;base64,{base64_str}")

                msg_list_content = []
                img_bytes = await ui.render(notebook, frameless=True)
                msg_list_content.append(img_bytes)

                # 添加动态图片
                if dynamic["images"] and base_config.get("ENABLE_DYNAMIC_IMAGE", False):
                    logger.info(
                        f"动态中包含 {len(dynamic['images'])} 张图片，将一并发送"
                    )
                    for img_url in dynamic["images"]:
                        try:
                            img_data = await fetch_image_bytes(img_url)
                            msg_list_content.append(img_data)
                        except Exception as e:
                            logger.warning(
                                f"下载动态中的图片失败: {img_url}, 错误: {e}"
                            )

                msg_list_content.append(
                    f"\n查看详情: https://t.bilibili.com/{dynamic['id']}"
                )

                notifications.append(
                    Notification(
                        content=msg_list_content,
                        type=NotificationType.DYNAMIC,
                        timestamp=dynamic["timestamp"],
                        key=f"dynamic:{dynamic['id']}",
                    )
                )

                # 更新最大时间戳
                if (
                    max_dynamic_timestamp is None
                    or dynamic["timestamp"] > max_dynamic_timestamp
                ):
                    max_dynamic_timestamp = dynamic["timestamp"]

        except ResponseCodeException as msg:
            logger.error(
                f"动态获取失败: UID={sub.uid}, 错误码={getattr(msg, 'code', 'unknown')}, 错误信息={getattr(msg, 'msg', str(msg))}"
            )

    # 处理视频
    max_video_timestamp = sub.last_video_timestamp
    new_videos = []
    if sub.push_video and video_info_data.get("list", {}).get("vlist"):
        vlist = video_info_data["list"]["vlist"]
        videos_to_process = vlist
        if force_push or not sub.last_video_timestamp:
            videos_to_process = [vlist[0]] if vlist else []
        # 遍历视频，找出符合条件的
        for video in videos_to_process:
            video_created = video.get("created", 0)
            if (
                not force_push
                and sub.last_video_timestamp is not None
                and video_created <= sub.last_video_timestamp
            ):
                continue

            new_videos.append(video)

            # 更新最大时间戳
            if max_video_timestamp is None or video_created > max_video_timestamp:
                max_video_timestamp = video_created

        # 按时间戳从旧到新排序视频（force_push 模式下不需要）
        if not force_push:
            new_videos.sort(key=lambda x: x.get("created", 0))

        for video in new_videos:
            video_title = video.get("title", "未知标题")
            video_bvid = video.get("bvid", "未知BV号")
            video_created = video.get("created", 0)
            video_time_str = (
                datetime.fromtimestamp(video_created).strftime("%Y-%m-%d %H:%M:%S")
                if video_created
                else "未知时间"
            )

            logger.info(
                f"准备推送新视频: UID={sub.uid}, 标题={video_title}, 发布时间={video_time_str}"
            )

            # 创建视频通知
            notebook = NotebookData(elements=[])
            notebook.head(f"{uname} 投稿了新视频啦！🎉", level=2)
            notebook.image(video["pic"])
            notebook.text(f"**标题：** {video_title}")
            notebook.text(f"**Bvid：** {video_bvid}")

            msg_list_content = []
            img_bytes = await ui.render(notebook, frameless=True)
            msg_list_content.append(img_bytes)
            msg_list_content.append(
                f"\n视频链接: https://www.bilibili.com/video/{video_bvid}"
            )

            notifications.append(
                Notification(
                    content=msg_list_content,
                    type=NotificationType.VIDEO,
                    timestamp=video_created,
                    key=f"video:{video_bvid}",
                )
            )

    # 统一更新时间戳（仅记录，不保存，等推送成功后再保存）
    if not force_push:
        if (
            max_dynamic_timestamp is not None
            and max_dynamic_timestamp != sub.last_dynamic_timestamp
        ):
            logger.debug(
                f"待更新动态时间戳: UID={sub.uid}, 新时间={max_dynamic_timestamp}"
            )

        if (
            max_video_timestamp is not None
            and max_video_timestamp != sub.last_video_timestamp
        ):
            logger.debug(
                f"待更新视频时间戳: UID={sub.uid}, 新时间={max_video_timestamp}"
            )

    duration = time.time() - start_time
    if notifications:
        logger.info(
            f"UP主状态检查完成: UID={sub.uid}, 检测到 {len(notifications)} 个更新, 耗时={duration:.2f}秒"
        )
    else:
        logger.debug(
            f"UP主状态检查完成: UID={sub.uid}, 未检测到更新, 耗时={duration:.2f}秒"
        )

    return SubCheckResult(
        notifications=notifications,
        max_dynamic_timestamp=max_dynamic_timestamp if not force_push else None,
        max_video_timestamp=max_video_timestamp if not force_push else None,
    )


async def get_all_new_dynamics(
    sub: BiliSub, force_push: bool = False
) -> tuple[list[dict], dict | None]:
    """获取用户所有新动态

    Returns:
        元组 (new_dynamics, dynamic_info):
            - new_dynamics: 新动态列表
            - dynamic_info: 原始动态数据（包含 cards 字段），可供调用方复用避免重复API请求
    """
    start_time = time.time()
    uid = sub.uid
    new_dynamics = []

    try:
        dynamic_info = await get_user_dynamics(uid)
    except json.JSONDecodeError as e:
        logger.error(
            f"获取用户动态时返回了非JSON内容 (可能被风控): UID={uid}, 异常信息={e}"
        )
        return new_dynamics, None
    except ResponseCodeException as e:
        logger.error(f"获取用户动态API错误: UID={uid}, Code={e.code}, Message={e.msg}")
        return new_dynamics, None
    except Exception as e:
        logger.error(
            f"获取用户动态异常: UID={uid}, 异常类型={type(e).__name__}, 异常信息={e}"
        )
        logger.debug(f"异常详细信息:\n{traceback.format_exc()}")
        return new_dynamics, None

    if not dynamic_info or not dynamic_info.get("cards") or not dynamic_info["cards"]:
        if dynamic_info:
            logger.warning(
                f"获取到的动态数据中没有cards字段或为空: UID={uid}, 数据={dynamic_info.keys()}"
            )
        return new_dynamics, None

    # 遍历动态，找出符合条件的
    cards_to_process = dynamic_info["cards"]
    if force_push or not sub.last_dynamic_timestamp:
        cards_to_process = [cards_to_process[0]] if cards_to_process else []

    for card in cards_to_process:
        try:
            dynamic_upload_time = card["desc"]["timestamp"]
            dynamic_id = card["desc"]["dynamic_id"]

            if (
                not force_push
                and sub.last_dynamic_timestamp is not None
                and dynamic_upload_time <= sub.last_dynamic_timestamp
            ):
                continue  # 跳过旧动态

            dynamic_time_str = datetime.fromtimestamp(dynamic_upload_time).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            logger.debug(
                f"找到新动态: UID={uid}, 动态ID={dynamic_id}, 发布时间={dynamic_time_str}"
            )

            # 提取动态中的图片URL
            dynamic_images = []
            try:
                card_str = card.get("card", "{}")
                if isinstance(card_str, dict):
                    card_data = card_str
                else:
                    card_data = json.loads(card_str)

                if "item" in card_data:
                    item = card_data["item"]
                    if "pictures" in item:
                        for pic in item["pictures"]:
                            if "img_src" in pic:
                                dynamic_images.append(pic["img_src"])
                    elif "pic" in item:
                        dynamic_images.append(item["pic"])
            except Exception as e:
                logger.warning(f"解析动态图片时出错: {e}")

            # 获取动态截图
            dynamic_img = None
            try:
                # 多条动态连续截图时增加延迟，避免浏览器资源耗尽或触发风控
                if new_dynamics:
                    await asyncio.sleep(2)
                dynamic_img = await get_dynamic_screenshot(dynamic_id)
                if dynamic_img:
                    logger.debug(
                        f"成功获取动态截图: UID={uid}, 动态ID={dynamic_id}, 图片大小={len(dynamic_img)}字节"
                    )
            except Exception as e:
                logger.error(
                    f"获取动态截图异常: UID={uid}, 动态ID={dynamic_id}, 异常类型={type(e).__name__}, 异常信息={e}"
                )

            new_dynamics.append(
                {
                    "img": dynamic_img,
                    "timestamp": dynamic_upload_time,
                    "id": dynamic_id,
                    "images": dynamic_images if dynamic_images else None,
                }
            )
        except Exception as e:
            logger.error(f"处理动态时出错: UID={uid}, 错误={e}")
            continue

    # 按时间戳从旧到新排序（force_push 模式下不需要）
    if not force_push:
        new_dynamics.sort(key=lambda x: x["timestamp"])

    duration = time.time() - start_time
    if new_dynamics:
        logger.info(
            f"获取用户动态完成: UID={uid}, 找到 {len(new_dynamics)} 条新动态, 耗时={duration:.2f}秒"
        )
    else:
        logger.debug(
            f"获取用户动态完成: UID={uid}, 未找到新动态, 耗时={duration:.2f}秒"
        )

    return new_dynamics, dynamic_info


def shrink_oversized_images(content: list) -> list:
    """把消息中超过体积上限的图片压缩后返回新的消息列表"""
    max_bytes = SEND_IMAGE_MAX_SIZE_MB * 1024 * 1024
    return [
        ImageHelper.shrink_for_send(msg, max_bytes) if isinstance(msg, bytes) else msg
        for msg in content
    ]


def is_media_upload_failure(error: Exception | None) -> bool:
    """判断异常是否为图片等富媒体上传失败

    适配器（如 NapCat）会把 Highway 分片上传失败包装成 retcode=1200 的
    ActionFailed，这类失败才值得压缩图片后重试。
    """
    if error is None:
        return False
    if getattr(error, "retcode", None) == 1200:
        return True
    error_text = str(error).lower()
    return any(
        keyword in error_text
        for keyword in ("retcode=1200", "httpupload", "too large", "rich media")
    )


async def _send_with_fallback(
    bot: Bot,
    msg_list: list,
    fallback_msg_list: list,
    *,
    user_id: str | None,
    group_id: str | None,
    sub: BiliSub,
) -> None:
    """向单个目标发送订阅消息，失败时按失败原因逐级降级重试

    先按原图发送；只有失败原因是富媒体上传（多为图片过大）时才压缩图片重试，
    最后降级为纯文字（含链接）。所有尝试都失败才抛出异常，由调用方按
    "未推送成功"处理。
    """
    last_error: Exception | None = None
    attempt = 0

    async def _try(payload: list) -> bool:
        nonlocal last_error, attempt
        attempt += 1
        try:
            await PlatformUtils.send_message(
                bot,
                user_id=user_id,
                group_id=group_id,
                message=MessageUtils.build_message(payload),
            )
        except Exception as e:
            last_error = e
            logger.warning(
                f"B站订阅推送第{attempt}次尝试失败: UID={sub.uid}, "
                f"错误类型={type(e).__name__}, 错误信息={e}"
            )
            return False
        if attempt > 1:
            logger.info(f"B站订阅推送降级后发送成功: UID={sub.uid}, 第{attempt}次尝试")
        return True

    if await _try(msg_list):
        return

    if is_media_upload_failure(last_error):
        compressed_list = shrink_oversized_images(msg_list)
        if compressed_list != msg_list:
            await asyncio.sleep(PUSH_RETRY_DELAY)
            if await _try(compressed_list):
                return

    if fallback_msg_list:
        await asyncio.sleep(PUSH_RETRY_DELAY)
        if await _try(fallback_msg_list):
            return

    if last_error:
        raise last_error


async def send_sub_msg(
    notification: Notification, sub: BiliSub, bot
) -> tuple[int, int]:
    """推送订阅信息到所有目标

    Returns:
        元组 (success_count, error_count):
            - success_count: 成功推送的目标数量
            - error_count: 推送失败的目标数量
    """

    start_time = time.time()
    logger.debug(f"B站订阅推送开始: UID={sub.uid}, 名称={sub.uname}")

    base_msg_list = list(notification.content)

    temp_group: set[str] = set()
    if not base_msg_list:
        logger.warning(f"B站订阅推送收到空消息列表: UID={sub.uid}")
        return 0, 0

    # 图片发送失败时的纯文字兜底：至少把链接推给用户，避免整条通知被判为未推送
    text_only_list = [msg for msg in base_msg_list if isinstance(msg, str)]
    if text_only_list:
        text_only_list = [
            f"📢 {sub.uname} {_notification_type_label[notification.type]}！",
            *text_only_list,
        ]

    sub_targets: list[str] = cast(
        list[str],
        await BiliSubTarget.filter(subscription_id=sub.id).values_list(
            "target_id", flat=True
        ),
    )
    logger.debug(f"B站订阅推送目标用户数量: {len(sub_targets)}, UID={sub.uid}")

    success_count = 0
    error_count = 0

    for target_id in sub_targets:
        # 标记本次推送是否包含 @全体 段，用于异常日志中提示适配器兼容性问题
        has_at_all = False
        try:
            if target_id.startswith("group_"):
                group_id = target_id.replace("group_", "")
                if group_id in temp_group:
                    continue
                temp_group.add(group_id)

                if await GroupConsole.is_block_plugin(group_id, MODULE_NAME):
                    continue

                at_all_segments: list = []
                try:
                    role_info = await bot.get_group_member_info(
                        group_id=int(group_id),
                        user_id=int(bot.self_id),
                        no_cache=True,
                    )
                    bot_role = role_info["role"]
                    logger.debug(
                        f"B站订阅推送机器人在群 {group_id} 中的角色: {bot_role}, UID={sub.uid}"
                    )

                    if base_config.get("ENABLE_AT_ALL", False) and bot_role in [
                        "owner",
                        "admin",
                    ]:
                        should_at = False
                        if (
                            notification.type == NotificationType.LIVE
                            and sub.at_all_live
                        ):
                            should_at = True
                        elif (
                            notification.type == NotificationType.VIDEO
                            and sub.at_all_video
                        ):
                            should_at = True
                        elif (
                            notification.type == NotificationType.DYNAMIC
                            and sub.at_all_dynamic
                        ):
                            should_at = True

                        if should_at:
                            at_all_segments = list(UniMessage.at_all() + "\n")
                            has_at_all = True
                            logger.debug(
                                f"B站订阅推送将在群 {group_id} 中@全体成员: UID={sub.uid}"
                            )
                except Exception as role_err:
                    logger.warning(
                        f"B站订阅推送获取机器人在群 {group_id} 中的角色失败: {type(role_err).__name__}, {role_err}"
                    )

                target_msg_list = at_all_segments + base_msg_list

                await _send_with_fallback(
                    bot,
                    target_msg_list,
                    at_all_segments + text_only_list,
                    user_id=None,
                    group_id=group_id,
                    sub=sub,
                )
                success_count += 1

            elif target_id.startswith("private_"):
                user_id = target_id.replace("private_", "")
                await _send_with_fallback(
                    bot,
                    base_msg_list,
                    text_only_list,
                    user_id=user_id,
                    group_id=None,
                    sub=sub,
                )
                success_count += 1

        except Exception as e:
            error_count += 1
            at_all_hint = ""
            if has_at_all:
                at_all_hint = "（本次推送包含 @全体 段，当前适配器可能不支持 @全体，请检查适配器兼容性）"
            logger.error(
                f"B站订阅推送发生错误: UID={sub.uid}, 内容={notification.key}, "
                f"错误类型={type(e).__name__}, 错误信息={e}{at_all_hint}"
            )
            logger.debug(f"B站订阅推送错误详细信息:\n{traceback.format_exc()}")

    total_duration = time.time() - start_time
    logger.info(
        f"B站订阅推送完成: UID={sub.uid}, 成功={success_count}, 失败={error_count}, 耗时={total_duration:.2f}秒"
    )

    return success_count, error_count


async def _save_timestamp(sub: BiliSub, field: str, timestamp: int | None) -> None:
    """把进度字段单调推进到 timestamp，仅在更大时写库"""
    if timestamp is None or (getattr(sub, field) or 0) >= timestamp:
        return
    setattr(sub, field, timestamp)
    await sub.save(update_fields=[field])
    logger.debug(f"已推进订阅进度: UID={sub.uid}, 字段={field}, 新值={timestamp}")


async def _persist_notification_progress(
    sub: BiliSub, notification: Notification, persist_timestamp: bool = True
) -> None:
    """持久化单条通知对应的进度（动态/视频时间戳或直播状态）"""
    if notification.type == NotificationType.LIVE:
        # sub.live_status 已在检查阶段更新为最新值，这里只负责写库
        await sub.save(update_fields=["live_status"])
        logger.debug(f"已持久化直播状态: UID={sub.uid}, 状态={sub.live_status}")
        return
    if not persist_timestamp:
        return
    await _save_timestamp(
        sub,
        (
            "last_dynamic_timestamp"
            if notification.type == NotificationType.DYNAMIC
            else "last_video_timestamp"
        ),
        notification.timestamp,
    )


async def check_and_send_update(
    sub: BiliSub, bot: Bot, force_push: bool = False
) -> int:
    """检查单个订阅并发送更新

    Args:
        sub: 订阅记录
        bot: Bot 实例
        force_push: 是否强制推送（为 True 时不更新时间戳）

    Returns:
        本次推送的通知数量
    """
    update_count = 0

    # 并发去重：如果该订阅已在被其他任务检查（如定时任务与 checkall 同时触发），
    # 直接跳过本次检查，避免重复推送
    if sub.id in _checking_subs:
        logger.info(
            f"B站订阅检查任务跳过（已有其他任务正在检查）: UID={sub.uid}, 名称={sub.uname}"
        )
        return 0

    _checking_subs.add(sub.id)
    try:
        logger.info(f"B站订阅检查任务开始检测: UID={sub.uid}, 名称={sub.uname}")

        result: SubCheckResult = await asyncio.wait_for(
            get_sub_status(sub, force_push=force_push), timeout=180
        )

        if result.notifications:
            logger.info(
                f"B站订阅检查任务检测到更新: UID={sub.uid}, 更新数量={len(result.notifications)}"
            )
            # 逐条推进进度：每条通知推送成功后立即写入它对应的进度，不再等整批全部成功。
            # 之前是"任一条失败就整批不保存时间戳"，于是一条持续推送失败的内容（如图片
            # 上传报 ActionFailed）会让进度永远停在原地，同批已推送的动态每次检查都被重推。
            # 现在的取舍是：失败条只在其后没有更新内容被推送成功时才会重试，
            # 宁可漏推一条也不再刷屏，漏推的可用 bili sub forcepush 手动补推。
            failed_types: set[NotificationType] = set()
            live_notified = any(
                notification.type == NotificationType.LIVE
                for notification in result.notifications
            )
            for notification in result.notifications:
                success_count, _ = await send_sub_msg(notification, sub, bot)
                # 仅当通知至少成功推送到一个目标时才计入 update_count，
                # 避免全部目标推送失败时仍被统计为"已推送"导致多计
                if success_count > 0:
                    _clear_push_failure(sub.id, notification.key)
                    update_count += 1
                else:
                    attempts = _record_push_failure(sub.id, notification.key)
                    if attempts < PUSH_MAX_ATTEMPTS:
                        failed_types.add(notification.type)
                        logger.warning(
                            f"通知推送失败，暂不推进进度以便下次检查重试（第{attempts}次）: "
                            f"UID={sub.uid}, 内容={notification.key}"
                        )
                        continue
                    _clear_push_failure(sub.id, notification.key)
                    logger.error(
                        f"通知连续{attempts}次推送失败，跳过该内容以避免刷屏，"
                        f"可用 bili sub forcepush 手动补推: "
                        f"UID={sub.uid}, 名称={sub.uname}, 内容={notification.key}"
                    )
                await _persist_notification_progress(
                    sub, notification, persist_timestamp=not force_push
                )

            if not force_push:
                # 无需推送即可确定的进度（如被广告过滤拦截的动态）也一并推进，
                # 但同类型仍有待重试的通知时保持进度不动，让它下次能被重新获取
                if NotificationType.DYNAMIC not in failed_types:
                    await _save_timestamp(
                        sub, "last_dynamic_timestamp", result.max_dynamic_timestamp
                    )
                if NotificationType.VIDEO not in failed_types:
                    await _save_timestamp(
                        sub, "last_video_timestamp", result.max_video_timestamp
                    )
            if result.new_live_status is not None and not live_notified:
                # 直播状态变化但没有开播通知（如主播下播），直接持久化新状态
                await sub.save(update_fields=["live_status"])
                logger.debug(
                    f"无开播通知但直播状态已变更，已持久化: UID={sub.uid}, "
                    f"新状态={result.new_live_status}"
                )
        else:
            # 没有需要推送的通知时，仍需保存进度。
            # 典型场景：动态被广告过滤拦截后不会生成通知，但进度已更新，
            # 若不保存会导致下次检查时重复获取并过滤同一条广告动态，浪费API调用和截图资源。
            if not force_push:
                await _save_timestamp(
                    sub, "last_dynamic_timestamp", result.max_dynamic_timestamp
                )
                await _save_timestamp(
                    sub, "last_video_timestamp", result.max_video_timestamp
                )
            # 无通知但直播状态可能已变更（如主播下播），一并持久化
            if result.new_live_status is not None:
                await sub.save(update_fields=["live_status"])
                logger.debug(
                    f"无新通知但字段已变更，已更新: UID={sub.uid}, "
                    f"新直播状态={result.new_live_status}"
                )

    except asyncio.TimeoutError:
        logger.error(f"B站订阅检查任务超时: UID={sub.uid}, 名称={sub.uname}")
    except Exception as e:
        logger.error(
            f"B站订阅检查任务异常: UID={sub.uid}, 错误类型={type(e).__name__}, 错误信息={e}"
        )
        logger.debug(f"B站订阅检查任务异常详细信息:\n{traceback.format_exc()}")
    finally:
        # 无论正常结束还是异常退出，都从检查集合中移除，允许后续任务再次检查
        _checking_subs.discard(sub.id)

    return update_count
