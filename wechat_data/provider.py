"""本地数据源 Provider —— 对 wechat_monitor 暴露与远程 API 等价的接口。

接口契约（与 wechat_monitor.ApiClient 对齐）：
    health() / wait_health() / discover_account() / list_sessions(account)
    / list_messages(account, username, limit, offset, order, source)
额外约定：
    pre_poll() -> str  每轮轮询前巡检登录实例；返回当前账号（可能因账号
                       切换而变化），Monitor 据此刷新 self.account。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .account_guard import AccountGuard, AccountGuardError
from .reader import MessageSnapshotReader

LOG = logging.getLogger("wechat_data.provider")

KIND = "local"


class LocalProvider:
    """直连本机微信数据（解密快照），不依赖 WeChatDataAnalysis。"""

    kind = KIND

    def __init__(self, guard: AccountGuard):
        self._guard = guard
        self._reader: Optional[MessageSnapshotReader] = None
        self._reader_account: str = ""
        self._bound: Optional[Dict[str, Any]] = None

    @classmethod
    def try_create(cls, *, explicit_data_root: str = "",
                   explicit_account: str = "",
                   snapshot_root: Optional[Path] = None) -> "LocalProvider":
        return cls(AccountGuard(
            explicit_data_root=explicit_data_root,
            explicit_account=explicit_account,
            snapshot_root=snapshot_root))

    # ---- 生命周期 ----

    def health(self) -> bool:
        try:
            self._ensure_bound()
            return True
        except AccountGuardError as exc:
            LOG.debug("local provider not ready: %s", exc)
            return False

    def wait_health(self, retries: int = 30, delay: float = 2.0) -> None:
        last_error: Optional[str] = None
        for i in range(retries):
            try:
                self._ensure_bound()
                LOG.info("本地微信数据绑定就绪（账号 %s，耗时 %.1fs）",
                         self._bound["account"], self._bound.get("bind_cost_seconds", 0))
                return
            except AccountGuardError as exc:
                last_error = str(exc)
                LOG.info("本地微信数据未就绪（%d/%d）: %s", i + 1, retries, exc)
                time.sleep(delay)
        raise AccountGuardError(last_error or "本地微信数据绑定失败")

    def _ensure_bound(self) -> Dict[str, Any]:
        if self._bound is None:
            self._bound = self._guard.bind()
        return self._bound

    def pre_poll(self) -> str:
        """巡检登录实例；账号切换时失效绑定与 reader 缓存并重绑。"""
        account = self._guard.ensure_current()
        if account != (self._bound or {}).get("account"):
            # 绑定发生变化：清空本地缓存，确保后续读取新账号的快照
            self._bound = None
            self._reader = None
            self._ensure_bound()
        elif self._reader is not None and account != self._reader_account:
            self._reader = None
        return account

    def describe(self) -> Dict[str, Any]:
        info = dict(self._bound or {})
        info.pop("key_hex", None)               # 绝不外泄密钥
        return info

    # ---- 数据接口 ----

    def _get_reader(self) -> MessageSnapshotReader:
        bind = self._ensure_bound()
        if self._reader is None or self._reader_account != bind["account"]:
            self._reader = MessageSnapshotReader(bind["snapshot_dir"])
            self._reader_account = str(bind["account"])
        return self._reader

    def discover_account(self) -> str:
        try:
            return str(self._ensure_bound()["account"])
        except AccountGuardError:
            return ""

    def list_sessions(self, account: str, **_kwargs: Any) -> List[Dict[str, Any]]:
        return self._get_reader().list_sessions()

    def list_messages(self, account: str, username: str, limit: int,
                      offset: int = 0, order: str = "desc",
                      source: str = "auto") -> List[Dict[str, Any]]:
        return self._get_reader().list_messages(
            username, limit=limit, offset=offset, order=order)
