import asyncio
from typing import Any, NoReturn, cast

import httpx
from bilibili_api import article
from bilibili_api import exceptions as BiliExceptions
from bilibili_api import live, user, video

from zhenxun.services.log import logger
from zhenxun.utils.decorator.retry import Retry

from ..config import get_credential
from ..model import ArticleInfo, LiveInfo, Owner, Stat, UserInfo, UserStat, VideoInfo
from ..utils.common import extract_video_page_num
from ..utils.exceptions import (
    BilibiliRequestError,
    BilibiliResponseError,
    RateLimitError,
    ResourceForbiddenError,
    ResourceNotFoundError,
    UrlParseError,
)

RETRYABLE_EXCEPTIONS = (
    BiliExceptions.NetworkException,
    httpx.RequestError,
    asyncio.TimeoutError,
)


def _raise_for_response_code(
    e: Exception,
    context: dict[str, Any],
    log_label: str,
    not_found_codes: list[int] | None = None,
) -> NoReturn:
    """将 B站 ResponseCodeException 统一映射为业务异常

    Args:
        e: bilibili-api 抛出的 ResponseCodeException
        context: 异常上下文字典（会被修改，加入 code 字段）
        log_label: 日志中显示的资源标识（如 "视频 vid" 或 "直播间 room_id"）
        not_found_codes: 视为"资源不存在"的响应码列表，默认为 [-404]
    """
    code = getattr(e, "code", 0)
    msg = getattr(e, "msg", str(e))
    context["code"] = code
    logger.error(f"B站API错误 ({log_label}): 代码 {code}, 消息: {msg}", "B站解析")

    if code == -403:
        raise ResourceForbiddenError(
            f"{log_label} 访问被禁止: {msg}", cause=e, context=context
        )
    elif code in (not_found_codes or [-404]):
        raise ResourceNotFoundError(
            f"{log_label} 资源不存在或不可见: {msg}", cause=e, context=context
        )
    elif code == -412:
        raise RateLimitError(
            f"请求频率过高 ({log_label}): {msg}", retry_after=60, context=context
        )
    else:
        raise BilibiliResponseError(
            f"B站API错误 ({log_label}): 代码 {code}, 消息: {msg}",
            cause=e,
            context=context,
        )


def _raise_for_network_error(
    e: Exception,
    context: dict[str, Any],
    log_label: str,
) -> NoReturn:
    """将 B站 NetworkException 统一映射为 BilibiliRequestError"""
    logger.error(f"获取{log_label}信息网络错误: {e}", "B站解析")
    raise BilibiliRequestError(
        f"获取{log_label}信息网络错误: {e}", cause=e, context=context
    )


def _raise_for_unexpected_error(
    e: Exception,
    context: dict[str, Any],
    log_label: str,
) -> NoReturn:
    """将未知异常统一映射为 BilibiliResponseError"""
    logger.error(f"获取{log_label}信息失败: {e}", "B站解析")
    raise BilibiliResponseError(
        f"获取{log_label}信息意外错误: {e}", cause=e, context=context
    )


class BilibiliApiService:
    """B站API服务"""

    @staticmethod
    def _create_video_instance(vid: str) -> video.Video:
        """创建Video实例"""
        if vid.lower().startswith("av"):
            try:
                aid = int(vid[2:])
                return video.Video(aid=aid, credential=get_credential())
            except ValueError:
                raise UrlParseError(f"无效的av号: {vid}")
        elif vid.upper().startswith("BV"):
            return video.Video(bvid=vid, credential=get_credential())
        else:
            raise UrlParseError(f"无效的视频ID格式: {vid}")

    @staticmethod
    def _map_video_info_to_model(info: dict[str, Any], parsed_url: str) -> VideoInfo:
        """将API返回的视频信息映射到VideoInfo模型"""
        owner = Owner(
            mid=info["owner"]["mid"],
            name=info["owner"]["name"],
            face=info["owner"]["face"],
        )

        stat = Stat(
            aid=info["stat"]["aid"],
            view=info["stat"]["view"],
            danmaku=info["stat"]["danmaku"],
            reply=info["stat"]["reply"],
            favorite=info["stat"]["favorite"],
            coin=info["stat"]["coin"],
            share=info["stat"]["share"],
            now_rank=info["stat"]["now_rank"],
            his_rank=info["stat"]["his_rank"],
            like=info["stat"]["like"],
            dislike=info["stat"]["dislike"],
        )

        video_model = VideoInfo(
            bvid=info["bvid"],
            aid=info["aid"],
            videos=info["videos"],
            tid=info["tid"],
            tname=info["tname"],
            copyright=info["copyright"],
            pic=info["pic"],
            title=info["title"],
            pubdate=info["pubdate"],
            ctime=info["ctime"],
            desc=info["desc"],
            state=info["state"],
            duration=info["duration"],
            rights=info["rights"],
            owner=owner,
            stat=stat,
            dynamic=info["dynamic"],
            cid=info["cid"],
            dimension=info["dimension"],
            parsed_url=parsed_url,
        )

        if "pages" in info:
            video_model.pages = info["pages"]
        if "short_link_v2" in info:
            video_model.short_link_v2 = info["short_link_v2"]
        if "first_frame" in info:
            video_model.first_frame = info["first_frame"]

        return video_model

    @staticmethod
    def _map_live_info_to_model(info: dict[str, Any], parsed_url: str) -> LiveInfo:
        """将API返回的直播间信息映射到LiveInfo模型"""
        room_info = info["room_info"]
        anchor_info = info.get("anchor_info", {}).get("base_info", {})

        live_model = LiveInfo(
            room_id=room_info["room_id"],
            short_id=room_info["short_id"],
            uid=room_info["uid"],
            title=room_info["title"],
            cover=room_info["cover"],
            live_status=room_info["live_status"],
            live_start_time=room_info.get("live_start_time", 0),
            area_id=room_info["area_id"],
            area_name=room_info["area_name"],
            parent_area_id=room_info["parent_area_id"],
            parent_area_name=room_info["parent_area_name"],
            description=room_info.get("description", ""),
            parsed_url=parsed_url,
        )

        if anchor_info:
            live_model.uname = anchor_info.get("uname", "")
            live_model.face = anchor_info.get("face", "")

        live_model.room_url = f"https://live.bilibili.com/{room_info['room_id']}"
        live_model.space_url = f"https://space.bilibili.com/{room_info['uid']}"

        if "keyframe" in room_info:
            live_model.keyframe_url = room_info["keyframe"]

        return live_model

    @staticmethod
    @Retry.api(exception=RETRYABLE_EXCEPTIONS)  # type: ignore
    async def get_video_info(vid: str, parsed_url: str) -> VideoInfo:
        """获取视频信息"""
        logger.debug(f"获取视频信息: {vid}, URL: {parsed_url}", "B站解析")

        try:
            v = BilibiliApiService._create_video_instance(vid)

            logger.debug(f"使用bilibili-api获取视频信息: {vid}", "B站解析")
            info = await v.get_info()

            if not info or "bvid" not in info:
                logger.warning(f"视频未找到: {vid}", "B站解析")
                raise ResourceNotFoundError(f"视频未找到: {vid}")

            logger.debug(f"创建VideoInfo模型: {vid}", "B站解析")
            video_model = BilibiliApiService._map_video_info_to_model(info, parsed_url)

            page_index = extract_video_page_num(parsed_url)

            try:
                logger.debug(
                    f"尝试获取视频 {vid} (P{page_index + 1}) 的AI总结", "B站解析"
                )
                ai_conclusion = await v.get_ai_conclusion(page_index=page_index)
                if (
                    ai_conclusion
                    and (model_result := ai_conclusion.get("model_result"))
                    and (summary := model_result.get("summary"))
                ):
                    video_model.ai_summary = summary
                    logger.debug(f"成功获取AI总结 for {vid}", "B站解析")
            except Exception as e:
                logger.debug(
                    f"获取AI总结失败 for {vid} (可能无总结或需要更高权限): {e}",
                    "B站解析",
                )

            try:
                logger.debug(f"尝试获取视频 {vid} 的在线人数", "B站解析")
                online_data = await v.get_online()
                logger.debug(f"在线人数数据: {online_data}", "B站解析")
                if online_data and (total := online_data.get("total")):
                    video_model.online_count = str(total)
                elif online_data and (count := online_data.get("count")):
                    video_model.online_count = str(count)
            except Exception as e:
                logger.debug(f"获取在线人数失败 for {vid}: {e}", "B站解析")

            logger.debug(f"视频信息获取成功: {video_model.title}", "B站解析")
            return video_model
        except BiliExceptions.ArgsException as e:
            logger.error(f"Bilibili-api参数错误 ({vid}): {e}", "B站解析")
            raise UrlParseError(
                f"视频ID格式错误: {vid}",
                cause=e,
                context={"vid": vid, "url": parsed_url},
            )
        except ResourceNotFoundError:
            raise ResourceNotFoundError(
                f"视频未找到: {vid}", context={"vid": vid, "url": parsed_url}
            )
        except BiliExceptions.ResponseCodeException as e:
            _raise_for_response_code(
                e,
                {"vid": vid, "url": parsed_url},
                f"视频 {vid}",
                not_found_codes=[-404, 62002],
            )

        except BiliExceptions.NetworkException as e:
            _raise_for_network_error(e, {"vid": vid, "url": parsed_url}, f"视频 {vid}")

        except Exception as e:
            _raise_for_unexpected_error(
                e, {"vid": vid, "url": parsed_url}, f"视频 {vid}"
            )

    @staticmethod
    @Retry.api(exception=RETRYABLE_EXCEPTIONS)  # type: ignore
    async def get_live_info(room_id: int, parsed_url: str) -> LiveInfo:
        """获取直播间信息"""
        logger.debug(f"获取直播间信息: {room_id}", "B站解析")

        try:
            room = live.LiveRoom(room_display_id=room_id, credential=get_credential())

            logger.debug(f"使用bilibili-api获取直播间信息: {room_id}", "B站解析")
            info = await room.get_room_info()

            if not info or "room_info" not in info:
                logger.warning(f"直播间未找到或响应无效: {room_id}", "B站解析")
                raise ResourceNotFoundError(f"直播间信息未找到或响应无效: {room_id}")

            logger.debug(f"创建LiveInfo模型: {room_id}", "B站解析")
            live_model = BilibiliApiService._map_live_info_to_model(info, parsed_url)

            logger.debug(f"直播间信息获取成功: {live_model.title}", "B站解析")
            return live_model

        except ResourceNotFoundError:
            raise ResourceNotFoundError(
                f"直播间未找到: {room_id}",
                context={"room_id": room_id, "url": parsed_url},
            )
        except BiliExceptions.ResponseCodeException as e:
            _raise_for_response_code(
                e,
                {"room_id": room_id, "url": parsed_url},
                f"直播间 {room_id}",
            )

        except BiliExceptions.NetworkException as e:
            _raise_for_network_error(
                e, {"room_id": room_id, "url": parsed_url}, f"直播间 {room_id}"
            )

        except Exception as e:
            _raise_for_unexpected_error(
                e, {"room_id": room_id, "url": parsed_url}, f"直播间 {room_id}"
            )

    @staticmethod
    @Retry.api(exception=RETRYABLE_EXCEPTIONS)  # type: ignore
    async def get_article_info(cv_id: str, parsed_url: str) -> ArticleInfo:
        """获取专栏文章信息"""
        logger.debug(f"获取专栏信息: {cv_id}", "B站解析")
        context = {"cv_id": cv_id, "url": parsed_url}

        try:
            article_id_int: int
            if cv_id.lower().startswith("cv"):
                try:
                    article_id_int = int(cv_id[2:])
                except ValueError:
                    raise UrlParseError(f"无效的专栏ID格式: {cv_id}", context=context)
            else:
                raise UrlParseError(f"专栏ID必须以 'cv' 开头: {cv_id}", context=context)

            art = article.Article(cvid=article_id_int, credential=get_credential())

            logger.debug(f"获取专栏基础信息: cv{article_id_int}", "B站解析")
            info_data = await art.get_info()
            if not info_data:
                raise ResourceNotFoundError(
                    f"获取专栏基础信息失败或为空: {cv_id}", context=context
                )

            title = info_data.get("title", "")
            author_name = info_data.get("author_name", "")

            logger.debug(f"获取专栏 Markdown 内容: cv{article_id_int}", "B站解析")
            await art.fetch_content()
            markdown_content = art.markdown()

            article_info = ArticleInfo(
                id=cv_id,
                type="article",
                url=parsed_url,
                title=title,
                author=author_name,
                markdown_content=markdown_content,
            )
            logger.debug(f"专栏信息获取成功: {title}", "B站解析")
            return article_info

        except BiliExceptions.ResponseCodeException as e:
            context["code"] = e.code  # type: ignore
            logger.error(
                f"B站API错误 (专栏 {cv_id}): 代码 {e.code}, 消息: {e.msg}",
                "B站解析",
            )
            if e.code == -404 or "获取信息失败" in str(e):
                raise ResourceNotFoundError(
                    f"专栏未找到: {cv_id}", cause=e, context=context
                )
            elif e.code == -412:
                logger.warning(
                    f"专栏 {cv_id} 请求被限流 (-412)，retry_after=60s: {e.msg}",
                    "B站解析",
                )
                raise RateLimitError(
                    f"请求频率过高 (专栏 {cv_id}): {e.msg}",
                    retry_after=60,
                    cause=e,
                    context=context,
                )
            else:
                raise BilibiliResponseError(
                    f"B站API错误: {e.msg}", cause=e, context=context
                )
        except BiliExceptions.NetworkException as e:
            _raise_for_network_error(e, context, f"专栏 {cv_id}")
        except Exception as e:
            _raise_for_unexpected_error(e, context, f"专栏 {cv_id}")

    @staticmethod
    @Retry.api(exception=RETRYABLE_EXCEPTIONS)  # type: ignore
    async def get_user_info(uid: int, parsed_url: str) -> UserInfo:
        """获取用户空间信息"""
        logger.debug(f"获取用户信息: {uid}", "B站解析")
        context = {"uid": uid, "url": parsed_url}

        try:
            user_instance = user.User(uid=uid, credential=get_credential())

            logger.debug(f"并发获取用户核心信息、关系信息、UP主统计: {uid}", "B站解析")
            results = await asyncio.gather(
                user_instance.get_user_info(),
                user_instance.get_relation_info(),
                user_instance.get_up_stat(),
                return_exceptions=True,
            )

            user_info_data: dict[str, Any] | None = None
            relation_info_data: dict[str, Any] | None = None
            up_stat_data: dict[str, Any] | None = None
            errors = []

            if isinstance(results[0], Exception):
                errors.append(f"get_user_info 失败: {results[0]}")
                logger.error("获取 user_info 失败", e=results[0])
            else:
                user_info_data = cast(dict[str, Any], results[0])

            if isinstance(results[1], Exception):
                errors.append(f"get_relation_info 失败: {results[1]}")
                logger.error("获取 relation_info 失败", e=results[1])
            else:
                relation_info_data = cast(dict[str, Any], results[1])

            if isinstance(results[2], Exception):
                errors.append(f"get_up_stat 失败: {results[2]}")
                logger.error("获取 up_stat 失败", e=results[2])
            else:
                up_stat_data = cast(dict[str, Any], results[2])

            if not user_info_data:
                error_msg = ", ".join(errors)
                if any(
                    getattr(e, "code", None) == -404
                    for e in results
                    if isinstance(e, Exception)
                ):
                    raise ResourceNotFoundError(
                        f"用户未找到: {uid}",
                        cause=results[0] if errors else None,  # type: ignore
                        context=context,
                    )
                raise BilibiliResponseError(
                    f"获取用户核心信息失败: {error_msg}",
                    cause=results[0] if errors else None,  # type: ignore
                    context=context,
                )

            stat = UserStat(
                following=relation_info_data.get("following", 0)
                if relation_info_data
                else 0,
                follower=relation_info_data.get("follower", 0)
                if relation_info_data
                else 0,
                archive_view=(up_stat_data.get("archive", {}) or {}).get("view", 0)
                if up_stat_data
                else 0,
                article_view=(up_stat_data.get("article", {}) or {}).get("view", 0)
                if up_stat_data
                else 0,
                likes=up_stat_data.get("likes", 0) if up_stat_data else 0,
            )

            live_room_info = user_info_data.get("live_room", {}) or {}
            raw_live_url = live_room_info.get("url", "")
            if raw_live_url:
                if raw_live_url.startswith("//"):
                    live_room_url = "https:" + raw_live_url
                elif raw_live_url.startswith(("http://", "https://")):
                    live_room_url = raw_live_url
                else:
                    # 兜底：无协议前缀时补上 https://
                    live_room_url = "https://" + raw_live_url
            else:
                live_room_url = ""
            user_model = UserInfo(
                mid=user_info_data.get("mid", uid),
                name=user_info_data.get("name", ""),
                face=user_info_data.get("face", ""),
                sign=user_info_data.get("sign", ""),
                level=user_info_data.get("level", 0),
                sex=user_info_data.get("sex", "保密"),
                birthday=user_info_data.get("birthday", ""),
                top_photo=user_info_data.get("top_photo", ""),
                live_room_status=live_room_info.get("liveStatus", 0),
                live_room_url=live_room_url,
                live_room_title=live_room_info.get("title", ""),
                stat=stat,
                parsed_url=parsed_url,
            )

            logger.debug(f"用户信息获取成功: {user_model.name}", "B站解析")
            if errors:
                logger.warning(f"获取用户 {uid} 部分信息失败: {', '.join(errors)}")

            return user_model

        except ResourceNotFoundError:
            raise
        except ResourceForbiddenError:
            raise
        except BilibiliResponseError:
            raise
        except BilibiliRequestError:
            raise
        except Exception as e:
            logger.error(f"获取用户信息失败 ({uid}): {e}", "B站解析")
            raise BilibiliResponseError(
                f"获取用户信息意外错误 ({uid}): {e}",
                cause=e,
                context={"uid": uid, "url": parsed_url},
            )
