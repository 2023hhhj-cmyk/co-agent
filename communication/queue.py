"""
客服消息队列 - 基于 Redis List

功能：
- 消息排队（LPUSH/BRPOP）
- 队列长度查询（LLEN）
- 批量消费（Worker）
- 排队通知（发送 → 撤回）

设计：
- 客户消息进入队列后，回调立即返回，不阻塞
- Worker 按批次取出消息，并发处理
- 处理完成后撤回排队提示消息
"""

import asyncio
import json
import logging
import time
from typing import Any, Callable, Optional

import redis.asyncio as redis

from models.customer import QueuedMessage

logger = logging.getLogger(__name__)


class CSMessageQueue:
    """客服消息队列 - Redis 实现"""

    QUEUE_KEY = "cs:message_queue"
    NOTIFY_KEY_PREFIX = "cs:notify_msg:"

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def enqueue(self, message: QueuedMessage) -> int:
        """
        将消息放入队列

        Args:
            message: 队列消息

        Returns:
            放入前的队列长度（前面有多少人在等待）
        """
        # 先获取当前队列长度（用于排队提示）
        queue_len = await self.redis.llen(self.QUEUE_KEY)

        # 放入队列
        await self.redis.lpush(self.QUEUE_KEY, json.dumps(message.to_dict()))

        logger.info(f"消息入队: customer={message.customer_id}, queue_len={queue_len}")
        return queue_len

    async def dequeue(self, count: int = 5) -> list[QueuedMessage]:
        """
        从队列取出一批消息

        Args:
            count: 取出数量

        Returns:
            消息列表
        """
        messages = []
        for _ in range(count):
            data = await self.redis.rpop(self.QUEUE_KEY)
            if data is None:
                break
            try:
                msg_dict = json.loads(data)
                messages.append(QueuedMessage.from_dict(msg_dict))
            except Exception as e:
                logger.error(f"解析队列消息失败: {e}")

        return messages

    async def get_queue_length(self) -> int:
        """获取当前队列长度"""
        return await self.redis.llen(self.QUEUE_KEY)

    async def save_notify_msg_id(self, customer_id: str, msg_id: str, expire: int = 600) -> None:
        """
        保存排队提示消息的 ID（用于后续撤回）

        Args:
            customer_id: 客户 ID
            msg_id: 排队提示消息 ID
            expire: 过期时间（秒），默认 10 分钟
        """
        key = f"{self.NOTIFY_KEY_PREFIX}{customer_id}"
        await self.redis.set(key, msg_id, ex=expire)

    async def get_and_clear_notify_msg_id(self, customer_id: str) -> Optional[str]:
        """
        获取并清除排队提示消息 ID

        Args:
            customer_id: 客户 ID

        Returns:
            消息 ID，或 None
        """
        key = f"{self.NOTIFY_KEY_PREFIX}{customer_id}"
        msg_id = await self.redis.get(key)
        if msg_id:
            await self.redis.delete(key)
        return msg_id


class QueueWorker:
    """
    队列消费者 - 定时批量取出并处理

    每次取出 batch_size 条消息，asyncio.gather 并发处理，
    处理完后等待 interval 秒再取下一批。
    """

    def __init__(
        self,
        queue: CSMessageQueue,
        handler: Callable,
        batch_size: int = 5,
        interval: float = 1.0,
    ):
        """
        Args:
            queue: 消息队列
            handler: 消息处理函数，签名 async (QueuedMessage) -> None
            batch_size: 每批处理数量
            interval: 批次间隔（秒）
        """
        self.queue = queue
        self.handler = handler
        self.batch_size = batch_size
        self.interval = interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """启动 Worker"""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"QueueWorker 已启动: batch_size={self.batch_size}, interval={self.interval}s")

    async def stop(self) -> None:
        """停止 Worker"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("QueueWorker 已停止")

    async def _run(self) -> None:
        """主循环"""
        while self._running:
            try:
                # 取出一批消息
                messages = await self.queue.dequeue(self.batch_size)

                if not messages:
                    # 队列为空，等待
                    await asyncio.sleep(self.interval)
                    continue

                logger.info(f"QueueWorker 取出 {len(messages)} 条消息")

                # 并发处理
                tasks = [self._safe_handle(msg) for msg in messages]
                await asyncio.gather(*tasks)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"QueueWorker 异常: {e}", exc_info=True)

            # 批次间隔
            await asyncio.sleep(self.interval)

    async def _safe_handle(self, message: QueuedMessage) -> None:
        """安全处理单条消息（捕获异常不影响其他消息）"""
        try:
            await self.handler(message)
        except Exception as e:
            logger.error(f"处理消息失败: customer={message.customer_id}, error={e}", exc_info=True)
