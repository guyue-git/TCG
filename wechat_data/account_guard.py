"""账号一致性防护 —— 「登录微信实例 ↔ 数据」绑定链。

绑定链（四点一致才进入工作态，杜绝数据与登录实例错配）：

    1. 登录账号判定：key_info.db 最近活动时间 / global_config（detector）
    2. 数据目录：该账号 wxid 目录下的 db_storage
    3. 密钥：优先验证缓存密钥（page-1 HMAC）；无效则要求微信进程运行，
       从进程内存提取（key_extractor）并复验
    4. 解密快照：密钥成功解密该账号的库（snapshot.sync_snapshot）

运行中巡检（ensure_current）：每轮轮询前重判当前登录账号；变化即重绑，
水位线按 <账号>|<会话> 隔离，跨账号绝不复用水位。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import detector, key_extractor, snapshot, winproc
from .paths import get_key_store_path, get_snapshot_root
from .sqlcipher_spec import verify_key_against_page1

LOG = logging.getLogger("wechat_data.account_guard")


class AccountGuardError(RuntimeError):
    """绑定失败（附中文可读的修复指引）。"""


def _load_key_store(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_key_store(path: Path, store: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except OSError as exc:
        LOG.warning("密钥缓存写入失败 %s: %s", path, exc)


class AccountGuard:
    """维护「当前登录账号 → 密钥 → 快照」的绑定与巡检。"""

    def __init__(self, *, explicit_data_root: str = "",
                 explicit_account: str = "",
                 snapshot_root: Optional[Path] = None,
                 key_store_path: Optional[Path] = None):
        self._explicit_data_root = str(explicit_data_root or "").strip()
        self._explicit_account = detector.canonical_account_name(
            str(explicit_account or "").strip())
        self._snapshot_root = Path(snapshot_root) if snapshot_root else get_snapshot_root()
        self._key_store_path = key_store_path or get_key_store_path()
        self._lock = threading.RLock()
        self._bind: Optional[Dict[str, Any]] = None

    # ---- 对外接口 ----

    def bind(self) -> Dict[str, Any]:
        """执行完整绑定链；成功返回绑定信息，失败抛 AccountGuardError。"""
        with self._lock:
            self._bind = self._do_bind()
            return dict(self._bind)

    def ensure_current(self) -> str:
        """巡检：登录账号未变则返回当前账号；变化则重绑后返回新账号。

        账号未变时同样刷新解密快照（D1，2026-09-09）：微信 4.x 的消息库
        处于 WAL 模式，新消息要等 checkpoint 落入主库后才对「只拷主库」的
        快照可见——绑定只在启动时发生一次，若运行期不同步快照，monitor
        将永远读到启动时刻的静态数据。sync_snapshot 指纹未变化时仅做
        几次 stat，轮询频率下代价可忽略。
        """
        with self._lock:
            if self._bind is None:
                self.bind()
                return str(self._bind["account"])
            try:
                current = self._detect_current_account_dir()
            except AccountGuardError:
                return str(self._bind["account"])   # 检测暂不可用：沿用旧绑定
            if current and current != str(self._bind["account"]):
                LOG.warning("检测到微信登录账号切换: %s -> %s，重新绑定",
                            self._bind.get("wxid_dir_name"), current)
                self._bind = None
                self.bind()
            else:
                self._refresh_snapshot()
            return str(self._bind["account"])

    def _refresh_snapshot(self) -> None:
        """运行期快照增量刷新；失败降级为 warning，沿用旧快照继续轮询。"""
        bind = self._bind or {}
        db_storage = str(bind.get("db_storage") or "")
        account = str(bind.get("account") or "")
        key_hex = str(bind.get("key_hex") or "")
        if not (db_storage and account and key_hex):
            return                                # 绑定信息不完整：跳过刷新
        try:
            snap = snapshot.sync_snapshot(
                Path(db_storage), account, key_hex, self._snapshot_root)
        except Exception as exc:                 # 刷新失败不阻断轮询
            LOG.warning("快照周期刷新失败（沿用旧快照）: %s", exc)
            return
        if snap["updated"]:
            LOG.info("快照周期刷新: %d 个库更新（%s）", len(snap["updated"]),
                     ", ".join(snap["updated"][:3]))
        if snap["failed"]:
            LOG.warning("快照周期刷新部分失败: %s", "; ".join(snap["failed"][:3]))

    @property
    def bind_info(self) -> Optional[Dict[str, Any]]:
        return dict(self._bind) if self._bind else None

    # ---- 绑定链实现 ----

    def _resolve_roots(self) -> List[Path]:
        roots = detector.discover_data_roots(self._explicit_data_root)
        if not roots:
            raise AccountGuardError(
                "未找到微信数据目录（xwechat_files）。请确认微信已安装并至少"
                "登录过一次；也可在 config.ini [data_source] data_root 中显式指定。")
        return roots

    def _detect_current_account_dir(self) -> str:
        """返回当前登录账号的 wxid 目录名（仅目录名，非完整绑定）。"""
        last_error: Optional[str] = None
        for root in self._resolve_roots():
            info = detector.detect_current_account(root)
            wxid = str(info.get("wxid") or "").strip()
            if not wxid:
                last_error = "未能判定当前登录账号"
                continue
            return wxid
        if last_error:
            raise AccountGuardError(last_error)
        return ""

    def _find_account_dir(self, wxid: str) -> Path:
        exact: list[Path] = []
        suffixed: list[Path] = []
        for root in self._resolve_roots():
            for account_dir in detector.list_account_dirs(root):
                if account_dir.name == wxid:
                    exact.append(account_dir)
                elif account_dir.name.startswith(wxid + "_"):
                    # 微信登录目录为裸 wxid，数据目录可能带安装后缀
                    # （wxid_xxx_1e7a），按 db_storage 最近活动取最新安装
                    suffixed.append(account_dir)
        if exact:
            return self._most_recent_storage(exact)
        if suffixed:
            return self._most_recent_storage(suffixed)
        raise AccountGuardError(
            f"当前登录账号 {wxid} 的数据目录不存在（或缺少 db_storage）。"
            "请确认微信已在该机器登录并完成首次数据同步。")

    @staticmethod
    def _most_recent_storage(candidates: list[Path]) -> Path:
        def activity(path: Path) -> float:
            try:
                return (path / "db_storage").stat().st_mtime
            except OSError:
                return 0.0
        return max(candidates, key=activity)

    def _do_bind(self) -> Dict[str, Any]:
        t0 = time.monotonic()
        current_wxid = self._detect_current_account_dir()
        if not current_wxid:
            raise AccountGuardError(
                "未检测到已登录的微信账号。请先登录微信（key_info.db/global_config "
                "均无账号证据）。")
        if self._explicit_account and self._explicit_account != current_wxid:
            raise AccountGuardError(
                f"配置的账号 {self._explicit_account} 与当前登录账号 "
                f"{current_wxid} 不一致——为避免数据错配已拒绝启动。"
                "请切换微信登录或在 config.ini 中修正 account。")

        account_dir = self._find_account_dir(current_wxid)
        db_storage = account_dir / "db_storage"
        probe_dbs = snapshot.scan_monitored_databases(db_storage)
        if not probe_dbs:
            raise AccountGuardError(
                f"账号 {current_wxid} 的 db_storage 下未找到可用的数据库文件。")

        key_hex, key_source = self._resolve_key(current_wxid, db_storage, probe_dbs)

        snap = snapshot.sync_snapshot(db_storage, current_wxid, key_hex,
                                      self._snapshot_root)
        if snap["failed"] and not snapshot.snapshot_is_ready(
                self._snapshot_root, current_wxid):
            raise AccountGuardError(
                f"解密快照失败: {'; '.join(snap['failed'][:3])}")

        weixin_pids = [pid for pid, _name in detector.find_running_weixin()]
        bind_info = {
            "account": current_wxid,
            "wxid_dir_name": account_dir.name,
            "wxid_dir": str(account_dir),
            "db_storage": str(db_storage),
            "key_hex": key_hex,
            "key_source": key_source,
            "weixin_pids": weixin_pids,
            "snapshot_dir": snap["snapshot_dir"],
            "snapshot_updated": snap["updated"],
            "snapshot_total": snap["total"],
            "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "bind_cost_seconds": round(time.monotonic() - t0, 2),
        }
        LOG.info("账号绑定完成: account=%s key_source=%s 快照 %d 个库（本次刷新 %d）耗时 %.1fs",
                 current_wxid, key_source, snap["total"], len(snap["updated"]),
                 bind_info["bind_cost_seconds"])
        return bind_info

    # ---- 密钥解析 ----

    def _resolve_key(self, wxid: str, db_storage: Path,
                     probe_dbs: list[Path]) -> tuple[str, str]:
        # 1) 缓存密钥复验（page-1 HMAC，绝不盲信历史密钥）
        store = _load_key_store(self._key_store_path)
        cached = str((store.get(wxid) or {}).get("db_key") or "").strip()
        if cached and self._validated(cached, probe_dbs):
            LOG.info("使用缓存密钥（page-1 验证通过）")
            return cached, "cached_verified"

        weixin = detector.find_running_weixin()

        # 预载 Weixin.dll 的 internal_db_key 候选（部分版本内存密钥
        # 需与其 XOR 后才是真正的 passphrase）
        dll_xor_keys: list[bytes] = []
        dll_path: Optional[Path] = None
        for pid, _name in (weixin or []):
            image = key_extractor.query_process_image(pid)
            dll_path = key_extractor._locate_weixin_dll(image)
            if dll_path:
                break
        if dll_path:
            dll_xor_keys = key_extractor.scan_dll_xor_keys(dll_path)

        # 2) 被动内存特征扫描（不干扰微信进程）
        if weixin:
            probe_db = self._prefer_probe_db(probe_dbs)
            for pid, name in weixin:
                key_hex = key_extractor.extract_db_key_from_process(
                    pid, probe_db, xor_keys=dll_xor_keys)
                if key_hex and self._validated(key_hex, probe_dbs):
                    self._persist_key(wxid, key_hex)
                    LOG.info("被动扫描提取密钥成功并按账号缓存（pid=%s）", pid)
                    return key_hex, "memory_extracted"

        # 3) wx_key Hook 后备（项目 A 生产路径）：先探测运行中进程，
        #    再走「重启微信等待登录」完整流程（密钥在登录时派生）
        if key_extractor.wx_key_available():
            for pid, _name in (weixin or []):
                key_hex = key_extractor.extract_db_key_via_hook(
                    pid, timeout_seconds=10.0)
                if key_hex and self._validated(key_hex, probe_dbs):
                    self._persist_key(wxid, key_hex)
                    LOG.info("wx_key Hook 提取密钥成功（pid=%s）", pid)
                    return key_hex, "wxkey_hook"
            key_hex = key_extractor.extract_db_key_via_relaunch(
                timeout_seconds=180.0)
            if key_hex and self._validated(key_hex, probe_dbs):
                self._persist_key(wxid, key_hex)
                LOG.info("wx_key 重启流程提取密钥成功")
                return key_hex, "wxkey_relaunch"
            if key_hex:
                raise AccountGuardError(
                    "提取到的密钥与当前账号数据库不匹配——数据与登录实例"
                    "可能错配，已拒绝使用。请重新登录微信后重试。")
        else:
            LOG.warning("wx_key 库未安装，跳过 Hook 后备流程")

        raise AccountGuardError(
            "密钥提取失败。常见原因：①未以管理员身份运行 monitor；"
            "②微信刚启动尚未加载数据库（登录后稍等数秒重试）；"
            "③微信版本更新导致密钥布局变化。")

    @staticmethod
    def _validated(key_hex: str, probe_dbs: list[Path]) -> bool:
        """密钥必须与当前账号的库 page-1 HMAC 匹配（杜绝跨账号密钥）。"""
        return any(verify_key_against_page1(key_hex, p) for p in probe_dbs)

    def _persist_key(self, wxid: str, key_hex: str) -> None:
        store = _load_key_store(self._key_store_path)
        store[wxid] = {"db_key": key_hex,
                       "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        _save_key_store(self._key_store_path, store)

    @staticmethod
    def _prefer_probe_db(probe_dbs: list[Path]) -> Path:
        """探针库优先级：session > micromsg > message_N > 其他（库越小越快）。"""
        def rank(path: Path) -> tuple[int, int]:
            name = path.name.lower()
            if name.startswith("session"):
                return (0, path.stat().st_size if path.exists() else 0)
            if name.startswith("micromsg"):
                return (1, path.stat().st_size if path.exists() else 0)
            if name.startswith(("message", "msg")):
                return (2, path.stat().st_size if path.exists() else 0)
            return (3, path.stat().st_size if path.exists() else 0)
        return sorted(probe_dbs, key=rank)[0]
