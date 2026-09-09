"""解密快照缓存 —— 把加密库解密到本地快照目录，并按 mtime/size 增量刷新。

快照布局（镜像 db_storage 内的相对路径）：
    <snapshot_root>/<account>/<relative_path>.db
    <snapshot_root>/<account>/_snapshot_meta.json

只解密监控所需的库：session / sessiondata / contact / micromsg /
message_N / msg / message（其余 FTS、收藏、朋友圈等一律跳过）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from .decryptor import WeChatDatabaseDecryptor

LOG = logging.getLogger("wechat_data.snapshot")

_META_FILE = "_snapshot_meta.json"
_MESSAGE_DB_RE = re.compile(r"^(message(?:_\d+)?|msg\d*|micromsg)\.db$",
                            re.IGNORECASE)
_SESSION_DB_RE = re.compile(r"^(session|sessiondata)\.db$", re.IGNORECASE)
_CONTACT_DB_RE = re.compile(r"^contact\.db$", re.IGNORECASE)


def is_monitored_db(name: str) -> bool:
    """判断一个库文件是否在监控采集范围内。"""
    lowered = str(name or "").strip().lower()
    if not lowered.endswith(".db"):
        return False
    if lowered.startswith("biz_") or "fts" in lowered:
        return False
    return bool(_MESSAGE_DB_RE.fullmatch(lowered)
                or _SESSION_DB_RE.fullmatch(lowered)
                or _CONTACT_DB_RE.fullmatch(lowered))


def scan_monitored_databases(db_storage: Path) -> List[Path]:
    """递归扫描 db_storage 下需要解密的库文件（按相对路径排序）。"""
    found: List[Path] = []
    for root, _dirs, files in os.walk(db_storage):
        for name in files:
            if is_monitored_db(name):
                found.append(Path(root) / name)
    found.sort(key=lambda p: str(p).lower())
    return found


def _load_meta(account_dir: Path) -> Dict[str, Any]:
    meta_path = account_dir / _META_FILE
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_meta(account_dir: Path, meta: Dict[str, Any]) -> None:
    meta_path = account_dir / _META_FILE
    tmp_path = meta_path.with_suffix(".json.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(meta_path)
    except OSError as exc:
        LOG.warning("快照 meta 写入失败 %s: %s", meta_path, exc)


def _source_fingerprint(db_path: Path) -> Dict[str, Any]:
    try:
        st = db_path.stat()
        return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    except OSError:
        return {}


def sync_snapshot(db_storage: Path, account: str, key_hex: str,
                  snapshot_root: Path) -> Dict[str, Any]:
    """把账号 db_storage 下的监控库解密/刷新到快照目录。

    返回 {account, snapshot_dir, total, updated, failed}。
    """
    db_storage = Path(db_storage)
    account_dir = Path(snapshot_root) / str(account)
    account_dir.mkdir(parents=True, exist_ok=True)
    meta = _load_meta(account_dir)
    decryptor = WeChatDatabaseDecryptor(key_hex)

    sources = scan_monitored_databases(db_storage)
    updated: List[str] = []
    failed: List[str] = []
    seen_relative: set[str] = set()

    for source in sources:
        relative = source.relative_to(db_storage)
        seen_relative.add(str(relative).lower())
        fingerprint = _source_fingerprint(source)
        cached = meta.get(str(relative))
        if cached == fingerprint and (account_dir / relative).is_file():
            continue                       # 未变化：直接复用快照
        output = account_dir / relative
        result = decryptor.decrypt_database(source, output)
        if result.get("success"):
            meta[str(relative)] = fingerprint
            updated.append(str(relative))
            LOG.info("快照已刷新: %s (%s, %d/%d 页)", relative,
                     result.get("key_mode"), result.get("successful_pages"),
                     result.get("total_pages"))
        else:
            failed.append(f"{relative}: {result.get('error')}")
            LOG.warning("快照解密失败 %s: %s", relative, result.get("error"))

    # 清理源库已消失的陈旧快照
    stale_removed = False
    for stale in list(meta):
        if stale.lower() not in seen_relative:
            stale_path = account_dir / stale
            try:
                if stale_path.is_file():
                    stale_path.unlink()
            except OSError:
                pass
            meta.pop(stale, None)
            stale_removed = True

    # 运行期每轮都会调用（D1 周期刷新）：仅在指纹有变化时落盘，
    # 避免每 2s 一次无意义的 tmp+fsync+replace
    if updated or stale_removed:
        _save_meta(account_dir, meta)
    return {
        "account": account,
        "snapshot_dir": str(account_dir),
        "total": len(sources),
        "updated": updated,
        "failed": failed,
    }


def snapshot_is_ready(snapshot_root: Path, account: str) -> bool:
    """判断该账号快照是否至少包含一个可用的会话库。"""
    account_dir = Path(snapshot_root) / str(account)
    if not account_dir.is_dir():
        return False
    for path in account_dir.rglob("*.db"):
        if _SESSION_DB_RE.fullmatch(path.name.lower()):
            return True
    return False
