import asyncio

from zhenxun.services.log import logger


class ParseQueueService:
    """解析请求排队服务，确保同一时间只有一个解析进程在运行"""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._waiting_count = 0

    async def __aenter__(self):
        # 跟踪是否已递增等待计数，确保 acquire 被取消时能正确递减
        incremented = False
        if self._lock.locked():
            self._waiting_count += 1
            incremented = True
            logger.info(
                f"解析队列: 当前有解析进程正在运行，请求已加入排队 (等待数: {self._waiting_count})",
                "B站解析",
            )
        try:
            await self._lock.acquire()
        except BaseException:
            # acquire 被取消或抛出异常时，递减等待计数，避免计数器永久漂移
            if incremented:
                self._waiting_count -= 1
                logger.debug(
                    f"解析队列: 等待被取消或失败，已递减等待计数 (剩余等待数: {self._waiting_count})",
                    "B站解析",
                )
            raise
        if self._waiting_count > 0:
            self._waiting_count -= 1
            logger.info(
                f"解析队列: 开始处理排队中的请求 (剩余等待数: {self._waiting_count})",
                "B站解析",
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
        logger.debug("解析队列: 请求处理完成，释放解析锁", "B站解析")
        return False

    @property
    def is_locked(self) -> bool:
        """当前是否有解析进程正在运行"""
        return self._lock.locked()

    @property
    def waiting_count(self) -> int:
        """当前等待处理的请求数量"""
        return self._waiting_count


parse_queue = ParseQueueService()
