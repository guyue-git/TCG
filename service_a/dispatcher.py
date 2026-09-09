"""分发器：按序接收 → 幂等去重 → 分类 → 线程池派发.

并发模型：
* ThreadPoolExecutor（可配置 worker 数）并行处理；
* 同一对对手方的消息经 per-counterparty 锁串行（保序、并防跨群同
  对手方的确认书序号竞态），不同对手方并行；
* 幂等键 = (group, msg_id)：微信消息 id 全局唯一，不随 monitor 进程
  重启归零（进程内 seq 会被误判 duplicate 导致漏单，2026-09-04 排查修复）；
  msg_id 缺失时退回 (group, seq) 兜底；
  check-then-remember 在锁内原子完成，并发提交不双派发；
* unsupported 类别只记日志，不入线程池。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .classifier import classify
from .handler import HandlerContext
from .handlers import HANDLERS
from .inbox import InboxStore
from .message import IncomingMessage

LOG = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, ctx: HandlerContext, inbox: InboxStore,
                 workers: int = 4,
                 seen_keys: set[tuple[str, int]] | None = None):
        self.ctx = ctx
        self.inbox = inbox
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="svc-a")
        # 幂等键来源可注入：服务重启后用 inbox 落盘记录重建
        self._seen: set[tuple[str, int]] = seen_keys if seen_keys is not None else set()
        self._seen_lock = threading.Lock()
        self._group_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    # ---- 对外入口 ----
    def submit(self, message: IncomingMessage) -> tuple[bool, str]:
        """接收一条消息：原子去重 → 落盘 → 分类 → 派发。

        返回 (accepted, reason)；accepted=False 时不进入处理流程。
        落盘/分类抛出的任何异常会回滚幂等占位并向上传播（HTTP 层转 500，
        monitor 走退避重试补投）——毒消息不得"占位后静默吞"。
        """
        if not self._claim(message):
            return False, "duplicate"
        try:
            self.inbox.append(message)
            counterparty_id = self.ctx.group_map.get(message.group)
            category = classify(message.content, message.quoted_content,
                                counterparty_id)
        except Exception:
            with self._seen_lock:
                self._seen.discard(_idem_key(message))
            raise
        handler = HANDLERS.get(category)
        if handler is None:
            LOG.info("[%s] 类别 %r 无处理器（seq=%s），仅记录",
                     message.group, category, message.seq)
            return True, f"logged:{category}"
        self._pool.submit(self._run_handler, handler, message, category)
        return True, f"dispatched:{category}"

    def replay(self) -> int:
        """重放 inbox 全部历史（幂等保护下补处理）。返回派发条数。"""
        dispatched = 0
        for message in self.inbox.load_all():
            accepted, reason = self.submit(message)
            if accepted and reason.startswith("dispatched"):
                dispatched += 1
        return dispatched

    def seen_from_inbox(self) -> "Dispatcher":
        """用 inbox 已落盘的幂等键预填去重集合（服务重启恢复用）。"""
        for message in self.inbox.load_all():
            with self._seen_lock:
                self._seen.add(_idem_key(message))
        return self

    def shutdown(self) -> None:
        """等待队列排空后关闭线程池。"""
        self._pool.shutdown(wait=True)

    # ---- 内部 ----
    def _claim(self, message: IncomingMessage) -> bool:
        """原子占位：首次见到返回 True 并记住；重复返回 False。

        必须与去重判定在同一把锁内完成，否则 ThreadingHTTPServer 的
        并发提交会对同一消息双双通过检查（check-then-act 竞态）。
        """
        key = _idem_key(message)
        with self._seen_lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def _run_handler(self, handler, message: IncomingMessage,
                     category: str) -> None:
        lock = self._dispatch_lock(message)
        with lock:
            try:
                output = handler(message, self.ctx)
            except FileExistsError as exc:
                LOG.error("[%s] 产出文件冲突，需人工介入: %s",
                          message.group, exc)
            except Exception:  # noqa: BLE001 — 单条失败不拖垮线程池
                LOG.exception("[%s] 处理器 %s 执行失败 (seq=%s)",
                              message.group, category, message.seq)

    def _dispatch_lock(self, message: IncomingMessage) -> threading.Lock:
        """锁粒度 = 对手方（序号按 (对手方, 结构, 日期) 分配）.

        确认书序号扫描 output/ 取 max+1：两个群映射到同一对手方时，
        群级锁无法阻止并发同号 → 后者 FileExistsError 丢确认书。
        以对手方为锁键（未映射群退回群名），同对手方跨群串行，
        是原"同群串行"的超集，保序语义不变；跨对手方仍并行。
        """
        key = self.ctx.group_map.get(message.group) or message.group
        with self._locks_lock:
            if key not in self._group_locks:
                self._group_locks[key] = threading.Lock()
            return self._group_locks[key]


def _idem_key(message: IncomingMessage) -> tuple[str, int]:
    """幂等键：(group, msg_id)。

    msg_id 缺失（monitor 兜底场景）时退回 (group, seq)；同一进程内
    seq 单调唯一，跨进程重启由 msg_id 主键承担。
    """
    if message.msg_id is not None:
        return (message.group, message.msg_id)
    return (message.group, message.seq)
