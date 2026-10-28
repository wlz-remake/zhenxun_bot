import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from bilibili_api import video
from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot_plugin_alconna import AlconnaMatcher

from zhenxun.services.log import logger

from ..config import (
    MAX_CONCURRENT_DOWNLOADS,
    PLUGIN_TEMP_DIR,
    base_config,
    get_credential,
)
from ..model import VideoInfo
from ..utils.common import extract_video_page_num
from ..utils.exceptions import BilibiliBaseException, DownloadError, MediaProcessError
from ..utils.file_utils import merge_media_files
from ..utils.message import send_video_with_retry
from .cache_service import VIDEO_CACHE_DIR, CacheService
from .network_service import download_bilibili_file


@dataclass
class DownloadTask:
    """封装一个下载任务所需的所有信息"""

    bot: Bot
    event: Event
    info_model: Any
    is_manual: bool


class DownloadManager:
    """下载管理器，负责队列、并发控制和任务执行"""

    def __init__(self):
        self._initialized = False
        self.active_tasks: set[asyncio.Task] = set()
        self.semaphore: asyncio.Semaphore | None = None
        # per-video 锁字典：用于避免并发请求下载同一视频时的 TOCTOU 竞态条件
        # （两个请求同时缓存未命中后各自下载，导致重复下载和文件冲突）
        self._video_locks: dict[str, asyncio.Lock] = {}

    def _get_video_lock(self, video_id: str, page_num: int) -> asyncio.Lock:
        """获取指定视频的并发控制锁

        同一 video_id + page_num 的下载请求共享同一把锁，
        避免并发下载同一视频导致的临时文件冲突和重复下载。
        """
        lock_key = f"{video_id}_{page_num}"
        if lock_key not in self._video_locks:
            self._video_locks[lock_key] = asyncio.Lock()
        return self._video_locks[lock_key]

    @asynccontextmanager
    async def _acquire_video_lock(self, video_id: str, page_num: int):
        """获取视频锁的上下文管理器，退出后自动清理空闲锁以避免内存泄漏。

        同一 video_id + page_num 的下载请求共享同一把锁，避免并发下载同一视频
        导致的临时文件冲突和重复下载。锁释放后若无其他协程在等待，则从字典中
        移除该锁，防止长期运行后 _video_locks 字典无限增长。
        """
        lock = self._get_video_lock(video_id, page_num)
        try:
            async with lock:
                yield
        finally:
            # 锁已释放。检查是否仍有协程在等待该锁；若无则从字典中移除，
            # 避免长期运行后 _video_locks 字典无限增长。
            # _waiters 是 CPython asyncio.Lock 的内部属性，存放正在等待该锁的 Future 列表。
            # 在单线程事件循环中，此检查与 pop 之间无 await，不存在并发竞态。
            waiters = getattr(lock, "_waiters", None)
            if not waiters:
                lock_key = f"{video_id}_{page_num}"
                self._video_locks.pop(lock_key, None)
                logger.debug(f"已清理空闲视频锁: {lock_key}")

    def initialize(self):
        """初始化下载管理器，启动工作者协程"""
        if self._initialized:
            return
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._initialized = True
        logger.info(f"下载管理器已初始化，最大并发数: {MAX_CONCURRENT_DOWNLOADS}")

    async def add_task(
        self, task: DownloadTask, matcher: AlconnaMatcher | None = None
    ) -> asyncio.Task:
        """为下载任务创建后台任务，并进行并发控制

        Returns:
            创建的后台下载任务。调用方可以选择 await 该任务以等待下载完成，
            从而将下载与发送视频纳入外层锁（如 parse_queue）的保护范围，
            实现"解析→发送预览图→下载→发送视频"的原子性。
            若不等待，则保持原有的"fire and forget"语义。
        """
        if matcher and self.semaphore and self.semaphore.locked():
            await matcher.send(
                f'"{task.info_model.title}" 已加入下载任务，正在等待空闲下载位...'
            )

        new_task = asyncio.create_task(self._task_wrapper(task))
        self.active_tasks.add(new_task)
        new_task.add_done_callback(self.active_tasks.discard)
        return new_task

    async def _task_wrapper(self, task: DownloadTask):
        """
        包装单个下载任务的完整生命周期，包括并发控制、消息通知和异常处理。
        """
        try:
            logger.info(f"任务: {task.info_model.title}, 等待信号量...")
            assert self.semaphore is not None
            async with self.semaphore:
                logger.info(f"信号量已获取，开始处理任务: {task.info_model.title}")
                if task.is_manual:
                    try:
                        await task.bot.send(
                            task.event, f"▶️ 开始下载: {task.info_model.title}"
                        )
                    except Exception as send_err:
                        logger.warning(f"发送'开始下载'消息失败: {send_err}")
                await self._execute_download(
                    task.bot, task.event, task.info_model, task.is_manual
                )
        except Exception as e:
            logger.error(f"下载任务 '{task.info_model.title}' 执行失败", e=e)
            if task.is_manual:
                try:
                    error_message = getattr(e, "message", str(e))
                    await task.bot.send(
                        task.event,
                        f'❌ 下载"{task.info_model.title}"失败: {error_message}',
                    )
                except Exception as send_err:
                    logger.error(f"发送下载失败消息也失败了: {send_err}")

    @staticmethod
    def _select_appropriate_quality(
        video_streams: list[dict[str, Any]],
        audio_streams: list[dict[str, Any]],
        initial_quality_id: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """选择合适的视频和音频流（不再基于文件大小限制降低清晰度）"""
        quality_preference = [120, 116, 112, 80, 74, 64, 32, 16]

        if not video_streams:
            raise DownloadError("没有可用的视频流列表。")

        best_audio = (
            max(audio_streams, key=lambda s: s.get("bandwidth", 0))
            if audio_streams
            else None
        )

        available_streams = sorted(
            [s for s in video_streams if s.get("id") in quality_preference],
            key=lambda s: quality_preference.index(s.get("id")),  # type: ignore
        )
        if initial_quality_id:
            available_streams = [
                s for s in available_streams if s.get("id", 0) <= initial_quality_id
            ]

        if not available_streams:
            available_streams = sorted(
                video_streams, key=lambda s: s.get("id", 0), reverse=True
            )

        selected_video = available_streams[0]
        logger.debug(f"选择视频流质量: {selected_video.get('id')}")

        return selected_video, best_audio

    @staticmethod
    def _check_duration(
        duration_seconds: float,
        max_duration_minutes: int,
        user_id: str,
        is_manual: bool,
    ) -> None:
        """检查视频时长是否超过限制"""
        if max_duration_minutes <= 0:
            return

        superusers = get_driver().config.superusers
        if is_manual and user_id in superusers:
            logger.debug(f"用户 {user_id} 是超级用户且为手动下载，不受时长限制")
            return

        duration_minutes = round(duration_seconds / 60, 1)
        if duration_seconds > max_duration_minutes * 60:
            error_msg = (
                f"视频时长 {duration_minutes}分钟 超过限制 {max_duration_minutes}分钟"
            )
            logger.warning(error_msg)
            raise DownloadError(error_msg)

    async def _execute_download(
        self,
        bot: Bot,
        event: Event,
        info_model: Any,
        is_manual: bool,
    ) -> None:
        """根据模型类型分发到具体的下载方法"""
        if isinstance(info_model, VideoInfo):
            await self._download_video(bot, event, info_model, is_manual)
        else:
            raise BilibiliBaseException(
                f"不支持下载此类型的内容: {type(info_model).__name__}"
            )

    async def _download_video(
        self,
        bot: Bot,
        event: Event,
        video_info: VideoInfo,
        is_manual: bool,
    ) -> None:
        """执行普通视频的下载、合并和发送"""
        video_id = video_info.bvid or f"av{video_info.aid}"
        page_num = extract_video_page_num(video_info.parsed_url)

        logger.info(
            f"开始处理视频: {video_info.title} (ID: {video_id}, P{page_num + 1})"
        )

        if cached_file := await CacheService.get_video_cache(video_id, page_num):
            logger.info(f"使用缓存视频: {cached_file.name}")
            await send_video_with_retry(bot, event, cached_file)
            return

        # 使用 per-video 锁避免并发下载同一视频导致的 TOCTOU 竞态条件：
        # 两个请求同时缓存未命中后各自下载，会写入相同的临时/输出文件路径，
        # 导致文件损坏或重复下载浪费带宽。锁内重新检查缓存（双重检查锁定），
        # 让等待方直接复用先到者下载并缓存好的结果。
        async with self._acquire_video_lock(video_id, page_num):
            # 双重检查：获取锁后可能已有其他请求完成下载并写入缓存
            if cached_file := await CacheService.get_video_cache(video_id, page_num):
                logger.info(f"等待锁后缓存已命中，复用已下载视频: {cached_file.name}")
                await send_video_with_retry(bot, event, cached_file)
                return

            max_duration_key = (
                "MANUAL_DOWNLOAD_MAX_DURATION"
                if is_manual
                else "AUTO_DOWNLOAD_MAX_DURATION"
            )
            max_duration = base_config.get(max_duration_key, 0)
            self._check_duration(
                video_info.duration, max_duration, event.get_user_id(), is_manual
            )

            v = video.Video(
                bvid=video_info.bvid, aid=video_info.aid, credential=get_credential()
            )
            download_url_data = await v.get_download_url(page_index=page_num)

            if not (dash_info := download_url_data.get("dash")):
                raise DownloadError("无法获取DASH信息", context={"video_id": video_id})

            if not (video_streams := dash_info.get("video", [])):
                raise DownloadError("没有可用的视频流", context={"video_id": video_id})

            audio_streams = dash_info.get("audio", [])

            target_quality = base_config.get("VIDEO_DOWNLOAD_QUALITY", 80)

            selected_video_stream, selected_audio_stream = (
                self._select_appropriate_quality(
                    video_streams,
                    audio_streams,
                    target_quality,
                )
            )

            video_url = selected_video_stream.get("baseUrl")
            audio_url = (
                selected_audio_stream.get("baseUrl") if selected_audio_stream else None
            )

            # 获取备用URL列表
            video_backup_urls = selected_video_stream.get("backupUrl", [])
            audio_backup_urls = (
                selected_audio_stream.get("backupUrl", [])
                if selected_audio_stream
                else []
            )

            if not video_url:
                raise DownloadError(
                    "未能选择有效的视频流URL", context={"video_id": video_id}
                )

            base_filename = f"bili_video_{video_id}_P{page_num + 1}"
            v_stream_path = PLUGIN_TEMP_DIR / f"{base_filename}-video.m4s"
            a_stream_path = (
                PLUGIN_TEMP_DIR / f"{base_filename}-audio.m4s" if audio_url else None
            )

            cache_filename = f"{video_id}_P{page_num + 1}.mp4"
            output_mp4_path = VIDEO_CACHE_DIR / cache_filename
            # 标记输出文件是否为有效缓存，用于 finally 中判断是否需要清理
            output_is_valid = False

            try:
                # 创建包含主URL和备用URL的列表
                video_urls = (
                    [video_url] + video_backup_urls
                    if video_backup_urls
                    else [video_url]
                )
                dl_tasks = [download_bilibili_file(video_urls, v_stream_path)]

                if audio_url and a_stream_path:
                    audio_urls = (
                        [audio_url] + audio_backup_urls
                        if audio_backup_urls
                        else [audio_url]
                    )
                    dl_tasks.append(download_bilibili_file(audio_urls, a_stream_path))

                logger.info(f"开始并行下载 {len(dl_tasks)} 个视频媒体流...")
                results = await asyncio.gather(*dl_tasks, return_exceptions=True)

                if not all(isinstance(r, bool) and r for r in results):
                    raise DownloadError(f"下载视频媒体流失败: {video_id}")

                merge_success = await merge_media_files(
                    v_stream_path, a_stream_path, output_mp4_path
                )
                if not merge_success:
                    raise MediaProcessError("FFmpeg合并或编码失败")

                # 合并成功后标记文件为有效，finally 中不会清理
                output_is_valid = True
                logger.info(f"视频处理成功: {output_mp4_path.name}")
                await CacheService.save_video_to_cache(
                    video_id, page_num, output_mp4_path
                )
                await send_video_with_retry(bot, event, output_mp4_path)
            finally:
                # 清理下载的临时流文件，带重试以应对 Windows 上文件被短暂占用的情况
                for path_to_delete in (v_stream_path, a_stream_path):
                    if not (path_to_delete and path_to_delete.exists()):
                        continue
                    for attempt in range(3):
                        try:
                            path_to_delete.unlink(missing_ok=True)
                            logger.debug(f"成功清理临时文件: {path_to_delete.name}")
                            break
                        except PermissionError:
                            if attempt < 2:
                                await asyncio.sleep(0.1 * (attempt + 1))
                            else:
                                logger.warning(
                                    f"清理临时文件失败 (文件被占用): {path_to_delete.name}"
                                )
                        except Exception as e:
                            logger.warning(f"清理临时文件时发生未知错误: {e}")
                            break
                # 合并失败时清理 ffmpeg 可能生成的部分输出文件，避免残留无效缓存
                if not output_is_valid and output_mp4_path.exists():
                    try:
                        output_mp4_path.unlink(missing_ok=True)
                        logger.debug(
                            f"已清理合并失败的部分输出文件: {output_mp4_path.name}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"清理部分输出文件失败: {output_mp4_path.name}, {e}"
                        )


download_manager = DownloadManager()
