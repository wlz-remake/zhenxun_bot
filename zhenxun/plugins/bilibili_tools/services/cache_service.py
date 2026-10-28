import asyncio
import json
import shutil
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from nonebot_plugin_session import EventSession

from zhenxun.services.log import logger

from ..config import (
    AVATAR_CACHE_DIR,
    IMAGE_CACHE_DIR,
    PLUGIN_CACHE_DIR,
    PLUGIN_TEMP_DIR,
    base_config,
)

VIDEO_CACHE_DIR = PLUGIN_CACHE_DIR / "video_cache"
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_INDEX_FILE = PLUGIN_CACHE_DIR / "cache_index.json"
URL_CACHE_FILE = PLUGIN_CACHE_DIR / "url_cache.json"

_video_cache_lock = asyncio.Lock()
_url_cache_lock = asyncio.Lock()
_clean_lock = asyncio.Lock()

_video_cache_index: dict[str, dict[str, Any]] = {}
_url_context_caches: OrderedDict[str, OrderedDict[str, float]] = OrderedDict()
_URL_CONTEXT_CACHE_CAPACITY = 100
# 上下文（群/用户）数量上限
_MAX_CONTEXT_CACHES = 20

_video_cache_initialized = False
_url_cache_initialized = False


class CacheService:
    """统一缓存管理服务"""

    FILE_EXPIRY = {
        "bili_video_cover_": 1,
        "bili_live_cover_": 1,
        "bili_live_keyframe_": 0.04,
        "bili_article_": 1,
        "bili_opus_": 1,
        "bili_video_": 1,
        # 多平台解析产物(图片/视频/音频/卡片缓存)
        "platform_img_": 1,
        "platform_video_": 1,
        "platform_audio_": 1,
        "platform_card_": 1,
    }

    _CLEAN_INTERVAL = 24 * 3600

    # URL缓存防抖保存相关
    _url_cache_save_task: asyncio.Task | None = None
    _URL_CACHE_SAVE_DEBOUNCE_SECONDS = 5.0

    # 视频缓存索引防抖保存相关
    _video_cache_save_task: asyncio.Task | None = None
    _VIDEO_CACHE_SAVE_DEBOUNCE_SECONDS = 5.0

    @classmethod
    async def initialize(cls):
        """初始化缓存服务"""
        logger.info("正在初始化统一缓存服务...", "B站解析")

        await cls._init_video_cache()
        await cls._init_url_cache()

        clean_interval_hours = base_config.get("CACHE_CLEAN_INTERVAL_HOURS", 24)
        cls._CLEAN_INTERVAL = clean_interval_hours * 3600

        if not PLUGIN_TEMP_DIR.exists():
            PLUGIN_TEMP_DIR.mkdir(parents=True, exist_ok=True)

        cache_expiry_days = base_config.get("CACHE_EXPIRY_DAYS", 7)
        auto_clean_enabled = cache_expiry_days > 0

        if auto_clean_enabled:
            task = asyncio.create_task(cls._auto_clean_task())
            task.set_name("bilibili_cache_cleaner")
            logger.info("缓存自动清理任务已启动", "B站解析")

        logger.info("统一缓存服务初始化完成", "B站解析")

    @classmethod
    async def _init_video_cache(cls):
        """初始化视频文件缓存"""
        global _video_cache_initialized, _video_cache_index

        if _video_cache_initialized:
            return

        async with _video_cache_lock:
            if not VIDEO_CACHE_DIR.exists():
                VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                logger.info(f"已创建视频缓存目录: {VIDEO_CACHE_DIR}", "B站解析")

            if CACHE_INDEX_FILE.exists():
                try:
                    # 使用 asyncio.to_thread 将同步文件读取操作放到线程中执行，避免阻塞事件循环
                    file_content = await asyncio.to_thread(
                        CACHE_INDEX_FILE.read_text, encoding="utf-8"
                    )
                    _video_cache_index = json.loads(file_content)
                    logger.info(
                        f"已加载视频缓存索引，包含 {len(_video_cache_index)} 个条目",
                        "B站解析",
                    )
                except Exception as e:
                    logger.error(f"加载视频缓存索引失败: {e}", "B站解析")
                    _video_cache_index = {}

            invalid_keys = []
            for cache_key, cache_info in _video_cache_index.items():
                file_path = cache_info.get("file_path", "")
                if not file_path or not Path(file_path).exists():
                    invalid_keys.append(cache_key)

            for key in invalid_keys:
                del _video_cache_index[key]

            if invalid_keys:
                logger.info(f"已移除 {len(invalid_keys)} 个无效的缓存条目", "B站解析")
                await cls._save_video_cache_index()

            _video_cache_initialized = True

    @classmethod
    async def _init_url_cache(cls):
        """初始化URL缓存"""
        global _url_cache_initialized, _url_context_caches

        if _url_cache_initialized:
            return

        await cls._load_url_cache_from_disk()
        _url_cache_initialized = True

    @classmethod
    async def get_video_cache(cls, video_id: str, page_num: int = 0) -> Path | None:
        """获取视频缓存文件路径"""
        if not _video_cache_initialized:
            await cls._init_video_cache()

        cache_key = cls._get_video_cache_key(video_id, page_num)

        async with _video_cache_lock:
            cache_info = _video_cache_index.get(cache_key)
            if not cache_info:
                logger.debug(f"视频缓存未命中: {cache_key}", "B站解析")
                return None

            file_path_str = cache_info.get("file_path")
            if not file_path_str:
                logger.warning(f"缓存索引中的文件路径为空: {cache_key}", "B站解析")
                return None

            file_path = Path(file_path_str)
            if not file_path.exists():
                logger.warning(f"缓存文件不存在: {file_path}", "B站解析")
                del _video_cache_index[cache_key]
                await cls._save_video_cache_index()
                return None

            _video_cache_index[cache_key]["last_access_time"] = time.time()
            # 使用防抖机制保存索引，避免每次缓存命中都写磁盘
            cls._schedule_video_cache_save()

            logger.info(f"视频缓存命中: {cache_key} -> {file_path}", "B站解析")
            return file_path

    @classmethod
    async def save_video_to_cache(
        cls, video_id: str, page_num: int, file_path: Path
    ) -> bool:
        """保存视频到缓存"""
        if not _video_cache_initialized:
            await cls._init_video_cache()

        if not file_path.exists():
            logger.warning(f"要缓存的文件不存在: {file_path}", "B站解析")
            return False

        cache_key = cls._get_video_cache_key(video_id, page_num)
        cache_file_name = f"{video_id}_P{page_num + 1}.mp4"
        cache_file_path = VIDEO_CACHE_DIR / cache_file_name

        try:
            if str(file_path) == str(cache_file_path):
                logger.debug(f"文件已在缓存目录中，无需复制: {file_path}", "B站解析")
            else:
                if cache_file_path.exists():
                    cache_file_path.unlink()

                await asyncio.to_thread(shutil.copy2, file_path, cache_file_path)
                logger.debug(
                    f"已复制文件到缓存: {file_path} -> {cache_file_path}", "B站解析"
                )

            async with _video_cache_lock:
                _video_cache_index[cache_key] = {
                    "video_id": video_id,
                    "page_num": page_num,
                    "file_path": str(cache_file_path),
                    "file_size": cache_file_path.stat().st_size,
                    "create_time": time.time(),
                    "last_access_time": time.time(),
                }
                await cls._save_video_cache_index()

            logger.info(
                f"视频已保存到缓存: {cache_key} -> {cache_file_path}", "B站解析"
            )
            return True
        except Exception as e:
            logger.error(f"保存视频到缓存失败: {e}", "B站解析")
            return False

    @classmethod
    async def should_parse_url(cls, url: str, session: EventSession) -> bool:
        """检查URL是否应该被解析（基于缓存TTL）"""
        if not _url_cache_initialized:
            await cls._init_url_cache()

        cache_ttl_minutes = base_config.get("CACHE_TTL", 0)
        if cache_ttl_minutes <= 0:
            return True

        cache_ttl_seconds = cache_ttl_minutes * 60
        context_key = cls._get_context_key(session)
        current_time = time.time()

        context_cache = cls._get_or_create_context_cache(context_key)
        timestamp = context_cache.get(url)

        if timestamp is None:
            logger.debug(
                f"URL '{url}' 在上下文 '{context_key}' 的缓存中未找到", "B站解析"
            )
            if len(context_cache) >= _URL_CONTEXT_CACHE_CAPACITY:
                try:
                    removed_url, _ = context_cache.popitem(last=False)
                    logger.debug(
                        f"上下文 '{context_key}' 缓存已满，移除最旧条目: {removed_url}",
                        "B站解析",
                    )
                except KeyError:
                    pass
            context_cache[url] = current_time
            cls._schedule_url_cache_save()
            return True
        else:
            if current_time - timestamp > cache_ttl_seconds:
                logger.debug(
                    f"URL '{url}' 在上下文 '{context_key}' 的缓存已过期 (TTL={cache_ttl_minutes}分钟)",
                    "B站解析",
                )
                context_cache[url] = current_time
                context_cache.move_to_end(url)
                cls._schedule_url_cache_save()
                return True
            else:
                logger.debug(
                    f"URL '{url}' 在上下文 '{context_key}' 的缓存未过期", "B站解析"
                )
                context_cache.move_to_end(url)
                return False

    @classmethod
    async def add_url_to_cache(cls, url: str, session: EventSession):
        """手动将URL添加到指定会话的缓存"""
        if not _url_cache_initialized:
            await cls._init_url_cache()

        cache_ttl_minutes = base_config.get("CACHE_TTL", 0)
        if cache_ttl_minutes <= 0:
            return

        context_key = cls._get_context_key(session)
        current_time = time.time()

        context_cache = cls._get_or_create_context_cache(context_key)

        if url in context_cache:
            context_cache.move_to_end(url)
        elif len(context_cache) >= _URL_CONTEXT_CACHE_CAPACITY:
            try:
                context_cache.popitem(last=False)
            except KeyError:
                pass
        context_cache[url] = current_time
        logger.debug(
            f"手动添加/更新 URL '{url}' 到上下文 '{context_key}' 的缓存", "B站解析"
        )

        cls._schedule_url_cache_save()

    @classmethod
    async def remove_url_from_cache(cls, url: str, session: EventSession) -> bool:
        """从缓存中移除指定URL

        用于解析失败时回滚缓存：should_parse_url 会在解析开始时将URL写入缓存，
        若解析过程中抛出异常，应调用此方法移除该URL，使其可在下次消息时被重新解析。
        """
        if not _url_cache_initialized:
            await cls._init_url_cache()

        context_key = cls._get_context_key(session)

        context_cache = _url_context_caches.get(context_key)
        if not context_cache:
            return False

        if url in context_cache:
            context_cache.pop(url, None)
            cls._schedule_url_cache_save()
            logger.debug(
                f"已从上下文 '{context_key}' 的缓存中移除 URL '{url}'（解析失败回滚）",
                "B站解析",
            )
            return True
        return False

    @classmethod
    async def clear_url_cache(cls, context_key: str | None = None):
        """清空URL缓存"""
        if not _url_cache_initialized:
            await cls._init_url_cache()

        if context_key:
            if context_key in _url_context_caches:
                _url_context_caches[context_key].clear()
                logger.info(f"已清空上下文 '{context_key}' 的缓存", "B站解析")
            else:
                logger.info(f"尝试清空缓存，但未找到上下文 '{context_key}'", "B站解析")
        else:
            _url_context_caches.clear()
            logger.info("已清空所有上下文的缓存", "B站解析")

        cls._schedule_url_cache_save()

    @classmethod
    async def clean_expired_cache(cls, force: bool = False) -> int:
        """清理过期缓存文件和视频缓存"""
        if not _video_cache_initialized:
            await cls._init_video_cache()

        async with _clean_lock:
            cleaned_videos = await cls._clean_video_cache(force)

            cleaned_files = await cls._clean_temp_files()

            total_cleaned = cleaned_videos + cleaned_files
            if total_cleaned > 0:
                logger.info(f"缓存清理完成，共清理 {total_cleaned} 个文件", "B站解析")

            return total_cleaned

    @classmethod
    async def _clean_video_cache(cls, force: bool = False) -> int:
        """清理过期视频缓存"""
        expiry_days = base_config.get("CACHE_EXPIRY_DAYS", 7)
        max_cache_size_mb = base_config.get("MAX_VIDEO_CACHE_SIZE_MB", 1024)

        # expiry_days <= 0 时跳过过期清理：即使 force=True（定时任务）也不按过期时间删除，
        # 否则 expiry_seconds=0 会导致所有缓存文件被判定为过期而全删。
        # force=True 时仍可执行基于大小的清理（下方 total_size_mb > max_cache_size_mb 分支）。
        if expiry_days <= 0:
            logger.debug("视频缓存过期时间设置为0或负数，跳过过期清理", "B站解析")
            if not force:
                return 0
            expiry_seconds: float | None = None
        else:
            expiry_seconds = expiry_days * 86400

        current_time = time.time()

        async with _video_cache_lock:
            sorted_cache = sorted(
                _video_cache_index.items(),
                key=lambda x: x[1].get("last_access_time", 0),
            )

            total_size = sum(info.get("file_size", 0) for _, info in sorted_cache)
            total_size_mb = total_size / (1024 * 1024)

            logger.debug(
                f"当前视频缓存大小: {total_size_mb:.2f}MB, 限制: {max_cache_size_mb}MB",
                "B站解析",
            )

            to_clean = []

            # 仅当 expiry_seconds 不为 None 时才按过期时间清理
            if expiry_seconds is not None:
                for cache_key, cache_info in sorted_cache:
                    last_access_time = cache_info.get("last_access_time", 0)
                    if current_time - last_access_time > expiry_seconds:
                        to_clean.append((cache_key, cache_info))

            if total_size_mb > max_cache_size_mb:
                size_to_free = total_size - (max_cache_size_mb * 1024 * 1024)
                freed_size = 0

                # 使用 set 存储已标记清理的 cache_key，避免 O(n²) 的列表推导查找
                to_clean_keys = {cache_key for cache_key, _ in to_clean}

                for cache_key, cache_info in sorted_cache:
                    if cache_key not in to_clean_keys:
                        to_clean.append((cache_key, cache_info))
                        freed_size += cache_info.get("file_size", 0)
                        if freed_size >= size_to_free:
                            break

            cleaned_count = 0
            for cache_key, cache_info in to_clean:
                file_path_str = cache_info.get("file_path", "")
                if file_path_str:
                    file_path = Path(file_path_str)
                    if file_path.exists():
                        try:
                            file_path.unlink()
                            logger.debug(f"已删除缓存文件: {file_path}", "B站解析")
                            cleaned_count += 1
                        except Exception as e:
                            logger.warning(
                                f"删除缓存文件失败: {file_path}, 错误: {e}", "B站解析"
                            )

                _video_cache_index.pop(cache_key, None)

            if to_clean:
                await cls._save_video_cache_index()

            if cleaned_count > 0:
                logger.info(
                    f"视频缓存清理完成，共清理 {cleaned_count} 个文件", "B站解析"
                )

            return cleaned_count

    @classmethod
    async def _clean_temp_files(cls) -> int:
        """清理临时文件"""
        current_time = time.time()
        temp_files = []

        if IMAGE_CACHE_DIR.exists():
            for file in IMAGE_CACHE_DIR.glob("*"):
                if file.is_file():
                    temp_files.append(file)
            logger.debug(
                f"扫描图片缓存目录: {IMAGE_CACHE_DIR}，找到 {len(temp_files)} 个文件",
                "B站解析",
            )

        for file in PLUGIN_TEMP_DIR.glob("*"):
            if file.is_file() and file.parent == PLUGIN_TEMP_DIR:
                temp_files.append(file)

        files_by_prefix = {}
        for file in temp_files:
            for prefix in cls.FILE_EXPIRY.keys():
                if file.name.startswith(prefix):
                    if prefix not in files_by_prefix:
                        files_by_prefix[prefix] = []
                    files_by_prefix[prefix].append(file)
                    break

        cleaned_count = 0
        for prefix, files in files_by_prefix.items():
            expiry_days = cls.FILE_EXPIRY.get(prefix, 1)
            expiry_seconds = expiry_days * 86400

            logger.debug(
                f"文件类型 {prefix} 的过期时间为 {expiry_days} 天 ({expiry_seconds} 秒)",
                "B站解析",
            )

            for file in files:
                try:
                    mtime = file.stat().st_mtime
                    file_age_seconds = current_time - mtime
                    file_age_days = file_age_seconds / 86400

                    if file_age_seconds > expiry_seconds:
                        logger.debug(
                            f"文件 {file.name} 已存在 {file_age_days:.2f} 天，超过过期时间 {expiry_days} 天",
                            "B站解析",
                        )
                        file.unlink()
                        cleaned_count += 1
                        logger.debug(f"已清理过期文件: {file.name}", "B站解析")
                except Exception as e:
                    logger.warning(f"清理文件 {file.name} 时出错: {e}", "B站解析")

        if AVATAR_CACHE_DIR.exists():
            avatar_expiry_days = base_config.get("CACHE_EXPIRY_DAYS", 7)
            avatar_expiry_seconds = avatar_expiry_days * 86400
            avatar_files = [f for f in AVATAR_CACHE_DIR.glob("*") if f.is_file()]
            logger.debug(
                f"扫描头像缓存目录: {AVATAR_CACHE_DIR}，找到 {len(avatar_files)} 个文件",
                "B站解析",
            )
            for file in avatar_files:
                try:
                    mtime = file.stat().st_mtime
                    file_age_seconds = current_time - mtime
                    if file_age_seconds > avatar_expiry_seconds:
                        file.unlink()
                        cleaned_count += 1
                        logger.debug(
                            f"已清理过期头像缓存: {file.name} (存在 {file_age_seconds / 86400:.2f} 天)",
                            "B站解析",
                        )
                except Exception as e:
                    logger.warning(f"清理头像缓存 {file.name} 时出错: {e}", "B站解析")

        return cleaned_count

    @classmethod
    async def _auto_clean_task(cls):
        """定时清理任务"""
        logger.info("缓存自动清理任务启动", "B站解析")

        while True:
            try:
                # 每轮循环重新读取清理间隔配置，使运行时修改可即时生效
                clean_interval_hours = base_config.get("CACHE_CLEAN_INTERVAL_HOURS", 24)
                clean_interval_seconds = clean_interval_hours * 3600

                cleaned = await cls.clean_expired_cache(force=True)
                logger.info(
                    f"缓存自动清理完成，清理了 {cleaned} 个文件，下次清理将在 {clean_interval_hours} 小时后进行",
                    "B站解析",
                )

                await asyncio.sleep(clean_interval_seconds)
            except Exception as e:
                logger.error(f"缓存自动清理任务出错: {e}", "B站解析")
                await asyncio.sleep(3600)

    @staticmethod
    def _get_video_cache_key(video_id: str, page_num: int) -> str:
        """生成视频缓存键"""
        return f"{video_id}_{page_num}"

    @staticmethod
    def _get_context_key(session: EventSession) -> str:
        """根据 EventSession 生成唯一的上下文 Key"""
        # 频道场景需要同时具备 id3 (频道ID) 和 id2 (子频道ID)，
        # 避免在 id2 为 None 时生成形如 "guild_xxx_channel_None" 的无效 key
        if session.id3 and session.id2:
            return f"guild_{session.id3}_channel_{session.id2}"
        elif session.id2:
            return f"group_{session.id2}"
        elif session.id1:
            return f"private_{session.id1}"
        else:
            logger.warning(
                "无法从 Session 中获取有效的上下文 ID，将使用全局缓存 Key", "B站解析"
            )
            return "global_fallback_cache"

    @classmethod
    def _get_or_create_context_cache(cls, context_key: str) -> OrderedDict[str, float]:
        """获取或创建指定上下文的 URL 缓存"""
        if context_key in _url_context_caches:
            # 上下文已存在，移动到末尾标记为最近访问
            _url_context_caches.move_to_end(context_key)
            return _url_context_caches[context_key]

        # 上下文数量已达上限，淘汰最久未访问的上下文（头部）
        if len(_url_context_caches) >= _MAX_CONTEXT_CACHES:
            try:
                oldest_key, _ = _url_context_caches.popitem(last=False)
                logger.info(
                    f"上下文数量已达上限 {_MAX_CONTEXT_CACHES}，"
                    f"已淘汰最久未访问的上下文: {oldest_key}",
                    "B站解析",
                )
            except KeyError:
                pass
        _url_context_caches[context_key] = OrderedDict()
        logger.debug(f"为上下文 '{context_key}' 创建了新的缓存", "B站解析")
        return _url_context_caches[context_key]

    @classmethod
    async def _save_video_cache_index(cls):
        """保存视频缓存索引"""
        try:
            # 使用 asyncio.to_thread 将同步文件写入操作放到线程中执行，避免阻塞事件循环
            await asyncio.to_thread(
                CACHE_INDEX_FILE.write_text,
                json.dumps(_video_cache_index, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug(
                f"视频缓存索引已保存: {len(_video_cache_index)} 个条目", "B站解析"
            )
        except Exception as e:
            logger.error(f"保存视频缓存索引失败: {e}", "B站解析")

    @classmethod
    def _schedule_video_cache_save(cls):
        """调度视频缓存索引保存任务（防抖机制）

        多次快速更新缓存时，只会在最后一次更新后的固定延迟后执行一次磁盘写入，
        避免频繁的磁盘IO（与URL缓存防抖机制保持一致）。
        """
        # 如果已有待执行的保存任务，取消它（实现防抖）
        if (
            cls._video_cache_save_task is not None
            and not cls._video_cache_save_task.done()
        ):
            cls._video_cache_save_task.cancel()
        # 创建新的延迟保存任务
        cls._video_cache_save_task = asyncio.create_task(
            cls._debounced_save_video_cache()
        )

    @classmethod
    async def _debounced_save_video_cache(cls):
        """防抖保存视频缓存索引到磁盘

        等待防抖延迟后执行保存，如果在等待期间被取消则直接返回。
        """
        try:
            await asyncio.sleep(cls._VIDEO_CACHE_SAVE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # 被新的保存请求取消，跳过本次保存
            return
        await cls._save_video_cache_index()

    @classmethod
    async def flush_video_cache_index(cls):
        """立即保存视频缓存索引到磁盘（用于关闭前刷新）"""
        if (
            cls._video_cache_save_task is not None
            and not cls._video_cache_save_task.done()
        ):
            cls._video_cache_save_task.cancel()
            cls._video_cache_save_task = None
        await cls._save_video_cache_index()

    @classmethod
    async def _load_url_cache_from_disk(cls):
        """从磁盘加载URL缓存数据"""
        global _url_context_caches

        if not URL_CACHE_FILE.exists():
            logger.info("URL缓存文件不存在，将使用空缓存", "B站解析")
            return

        try:
            async with _url_cache_lock:
                # 使用 asyncio.to_thread 将同步文件读取操作放到线程中执行，避免阻塞事件循环
                file_content = await asyncio.to_thread(
                    URL_CACHE_FILE.read_text, encoding="utf-8"
                )
                cache_data = json.loads(file_content)

                for context_key, urls_data in cache_data.items():
                    if context_key not in _url_context_caches:
                        _url_context_caches[context_key] = OrderedDict()

                    sorted_items = sorted(urls_data.items(), key=lambda x: x[1])

                    if len(sorted_items) > _URL_CONTEXT_CACHE_CAPACITY:
                        sorted_items = sorted_items[-_URL_CONTEXT_CACHE_CAPACITY:]

                    for url, timestamp in sorted_items:
                        _url_context_caches[context_key][url] = timestamp

                logger.info(
                    f"从磁盘加载了 {len(_url_context_caches)} 个上下文的URL缓存数据",
                    "B站解析",
                )
        except Exception as e:
            logger.error(f"从磁盘加载URL缓存失败: {e}", "B站解析")
            _url_context_caches = OrderedDict()

    @classmethod
    async def _save_url_cache_to_disk(cls):
        """将URL缓存数据保存到磁盘"""
        try:
            async with _url_cache_lock:
                # 保存前清理已过期的 URL 条目，避免磁盘存储浪费
                cache_ttl_minutes = base_config.get("CACHE_TTL", 0)
                if cache_ttl_minutes > 0:
                    cache_ttl_seconds = cache_ttl_minutes * 60
                    current_time = time.time()
                    expired_count = 0
                    for context_cache in _url_context_caches.values():
                        expired_urls = [
                            url
                            for url, ts in context_cache.items()
                            if current_time - ts > cache_ttl_seconds
                        ]
                        for url in expired_urls:
                            context_cache.pop(url, None)
                            expired_count += 1
                    if expired_count > 0:
                        logger.debug(
                            f"保存前清理了 {expired_count} 条过期 URL 缓存", "B站解析"
                        )

                cache_data = {}
                for context_key, ordered_dict in _url_context_caches.items():
                    cache_data[context_key] = dict(ordered_dict)

                # 使用 asyncio.to_thread 将同步文件写入操作放到线程中执行，避免阻塞事件循环
                await asyncio.to_thread(
                    URL_CACHE_FILE.write_text,
                    json.dumps(cache_data, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.debug(f"URL缓存数据已保存到磁盘: {URL_CACHE_FILE}", "B站解析")
        except Exception as e:
            logger.error(f"保存URL缓存到磁盘失败: {e}", "B站解析")

    @classmethod
    def _schedule_url_cache_save(cls):
        """调度URL缓存保存任务（防抖机制）

        多次快速更新缓存时，只会在最后一次更新后的固定延迟后执行一次磁盘写入，
        避免频繁的磁盘IO。
        """
        # 如果已有待执行的保存任务，取消它（实现防抖）
        if cls._url_cache_save_task is not None and not cls._url_cache_save_task.done():
            cls._url_cache_save_task.cancel()
        # 创建新的延迟保存任务
        cls._url_cache_save_task = asyncio.create_task(cls._debounced_save_url_cache())

    @classmethod
    async def _debounced_save_url_cache(cls):
        """防抖保存URL缓存到磁盘

        等待防抖延迟后执行保存，如果在等待期间被取消则直接返回。
        """
        try:
            await asyncio.sleep(cls._URL_CACHE_SAVE_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            # 被新的保存请求取消，跳过本次保存
            return
        await cls._save_url_cache_to_disk()

    @classmethod
    async def flush_url_cache(cls):
        """立即保存URL缓存到磁盘（用于关闭前刷新）"""
        if cls._url_cache_save_task is not None and not cls._url_cache_save_task.done():
            cls._url_cache_save_task.cancel()
            cls._url_cache_save_task = None
        await cls._save_url_cache_to_disk()
