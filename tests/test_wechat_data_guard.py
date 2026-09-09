"""角度 3/4：检测器与账号防护（绑定链 / 账号切换 / 显式账号校验）。

用临时目录伪造 xwechat_files 结构（global_config MMKV/AES-CFB、
key_info.db 登录痕迹、加密库），全链路不依赖真实微信。
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_data import detector
from wechat_data.account_guard import AccountGuard, AccountGuardError
from wechat_data_fixture import (encrypt_sqlite_db, make_empty_reserved_db,
                                 make_plain_db)


def _write_global_config(root: Path, wxid: str, nickname: str = "") -> None:
    """构造 all_users/config/global_config（与 detector.parse_global_config 对齐）。"""
    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                out.append(byte | 0x80)
            else:
                out.append(byte)
                return bytes(out)

    payload = bytearray()
    for key, text in (("mmkv_key_user_name", wxid),
                      ("mmkv_key_nick_name", nickname or wxid)):
        raw = text.encode("utf-8")              # MMKV varint 按「字节」计数
        payload += key.encode("utf-8")
        payload += varint(len(raw) + 1)
        payload += varint(len(raw))
        payload += raw
    key = b"xwechat_crypt_key"[:16]
    cipher = Cipher(algorithms.AES(key), modes.CFB(b"\x00" * 16),
                    backend=default_backend())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(bytes(payload)) + encryptor.finalize()
    config_dir = root / "all_users" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "global_config").write_bytes(b"\x00\x00\x00\x00" + encrypted)


def _make_account(root: Path, wxid_dir: str, key_material: bytes) -> Path:
    """构造一个账号目录：db_storage/session/session.db（已加密，保留区语义）。"""
    account_dir = root / wxid_dir
    session_dir = account_dir / "db_storage" / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    plain = account_dir / "_plain_tmp.db"
    make_empty_reserved_db(plain)
    encrypted = encrypt_sqlite_db(plain, key_material)
    plain.unlink()
    (session_dir / "session.db").write_bytes(encrypted)
    return account_dir


def _touch_login(root: Path, wxid_dir: str, mtime: float) -> None:
    login_dir = root / "all_users" / "login" / wxid_dir
    login_dir.mkdir(parents=True, exist_ok=True)
    key_info = login_dir / "key_info.db"
    key_info.write_bytes(b"fake")
    os.utime(key_info, (mtime, mtime))


@pytest.fixture(autouse=True)
def _no_real_wechat(monkeypatch):
    """测试机可能真实运行着微信：统一注入「无微信进程 + 无 Hook」，
    防止夹具触发真实进程内存扫描或重启微信（慢且非确定性）。"""
    from wechat_data import detector as _detector
    from wechat_data import key_extractor as _ke
    monkeypatch.setattr(_detector, "find_running_weixin", lambda: [])
    monkeypatch.setattr(_ke, "wx_key_available", lambda: False)
    monkeypatch.setattr(_ke, "extract_db_key_via_hook", lambda *a, **k: None)
    monkeypatch.setattr(_ke, "extract_db_key_via_relaunch", lambda *a, **k: None)


@pytest.fixture()
def fake_extractor(fake_wechat, monkeypatch):
    """密钥提取桩：伪造运行中的微信进程，并对探针库逐一验证 A/B 密钥，
    命中即返回（模拟真实「内存提取 -> HMAC 验证」链路）。"""
    _root, key_a, key_b = fake_wechat
    from pathlib import Path as _Path
    from wechat_data import detector as _detector
    from wechat_data import key_extractor as _ke

    def fake_extract(pid, probe_db, **kwargs):
        with _Path(probe_db).open("rb") as fh:
            page1 = fh.read(4096)
        for key in (key_a, key_b):
            if _ke.verify_key_candidate(key, page1):
                return key.hex()
        return None

    monkeypatch.setattr(_detector, "find_running_weixin",
                        lambda: [(4321, "Weixin.exe")])
    monkeypatch.setattr(
        "wechat_data.account_guard.key_extractor.extract_db_key_from_process",
        fake_extract)


@pytest.fixture()
def fake_wechat(tmp_path):
    """两个账号 A/B：B 的 key_info 更新（=当前登录），各有独立密钥。"""
    root = tmp_path / "xwechat_files"
    root.mkdir()
    key_a, key_b = os.urandom(32), os.urandom(32)
    _make_account(root, "wxid_aaa_1e7a", key_a)
    _make_account(root, "wxid_bbb_9f3c", key_b)
    _touch_login(root, "wxid_aaa_1e7a", time.time() - 3600)
    _touch_login(root, "wxid_bbb_9f3c", time.time())
    _write_global_config(root, "wxid_bbb_9f3c", "小明")
    return root, key_a, key_b


# ---- detector ----

def test_discover_data_roots_finds_fake_root(fake_wechat):
    root, _key_a, _key_b = fake_wechat
    roots = detector.discover_data_roots(str(root))
    # 本机可能真实存在其他 xwechat_files（如 Documents 下），只要求包含目标
    assert root in [Path(r) for r in roots]


def test_list_account_dirs_skips_internal(fake_wechat):
    root, _ka, _kb = fake_wechat
    accounts = [p.name for p in detector.list_account_dirs(root)]
    assert accounts == ["wxid_aaa_1e7a", "wxid_bbb_9f3c"]


def test_parse_global_config(fake_wechat):
    root, _ka, _kb = fake_wechat
    parsed = detector.parse_global_config(root)
    assert parsed == {"wxid": "wxid_bbb_9f3c", "nickname": "小明"}  # parse_global_config 返回原始值


def test_detect_current_account_prefers_key_info_mtime(fake_wechat):
    root, _ka, _kb = fake_wechat
    info = detector.detect_current_account(root)
    assert info["account"] == "wxid_bbb"
    assert info["source"] == "key_info_mtime"


def test_detect_current_account_falls_back_to_global_config(tmp_path):
    root = tmp_path / "xwechat_files"
    root.mkdir()
    _make_account(root, "wxid_only_1e7a", os.urandom(32))
    _write_global_config(root, "wxid_only_1e7a")
    info = detector.detect_current_account(root)
    assert info["account"] == "wxid_only"  # 账号身份已规范化为裸 wxid
    assert info["source"] == "global_config"


# ---- account_guard ----

def _make_guard(root: Path, keys_file: Path, **kwargs) -> AccountGuard:
    return AccountGuard(explicit_data_root=str(root),
                        snapshot_root=root.parent / "snapshots",
                        key_store_path=keys_file, **kwargs)


def test_guard_matches_suffixed_data_dir(tmp_path, monkeypatch):
    """登录目录为裸 wxid、数据目录带安装后缀（真实微信 4.x 形态）。
    2026-09-08 打包冒烟实测暴露：精确匹配失败导致绑定失败。"""
    from pathlib import Path as _Path
    from wechat_data import detector as _detector
    from wechat_data import key_extractor as _ke

    root = tmp_path / "xwechat_files"
    root.mkdir(exist_ok=True)
    key = os.urandom(32)
    _make_account(root, "wxid_only_1e7a", key)
    _touch_login(root, "wxid_only", time.time())
    _write_global_config(root, "wxid_only")

    def fake_extract(pid, probe_db, **kwargs):
        with _Path(probe_db).open("rb") as fh:
            page1 = fh.read(4096)
        return key.hex() if _ke.verify_key_candidate(key, page1) else None

    monkeypatch.setattr(_detector, "find_running_weixin",
                        lambda: [(4321, "Weixin.exe")])
    monkeypatch.setattr(
        "wechat_data.account_guard.key_extractor.extract_db_key_from_process",
        fake_extract)

    guard = _make_guard(root, tmp_path / "keys.json")
    bind = guard.bind()
    assert bind["account"] == "wxid_only"             # 账号 = 裸 wxid
    assert bind["wxid_dir_name"] == "wxid_only_1e7a"  # 数据 = 带后缀目录


def test_guard_bind_uses_current_account_and_caches_key(
        fake_wechat, tmp_path, fake_extractor):
    root, key_a, key_b = fake_wechat
    keys_file = tmp_path / "keys.json"
    guard = _make_guard(root, keys_file)
    bind = guard.bind()
    assert bind["account"] == "wxid_bbb"
    assert bind["key_source"] == "memory_extracted" or \
        bind["key_source"] == "cached_verified"
    # 密钥已按账号缓存，且与 B 的密钥一致
    import json
    store = json.loads(keys_file.read_text(encoding="utf-8"))
    assert store["wxid_bbb"]["db_key"] == key_b.hex()
    # 快照产出了解密后的会话库（snapshot_dir 已含账号目录）
    assert (Path(bind["snapshot_dir"]) / "session" / "session.db").is_file()


def test_guard_rebind_on_account_switch(fake_wechat, tmp_path, fake_extractor):
    root, key_a, key_b = fake_wechat
    keys_file = tmp_path / "keys.json"
    guard = _make_guard(root, keys_file)
    first = guard.bind()
    assert first["account"] == "wxid_bbb"

    # 模拟用户切换账号：A 重新登录（key_info 最新）
    _touch_login(root, "wxid_aaa_1e7a", time.time() + 10)
    account = guard.ensure_current()
    assert account == "wxid_aaa"
    bind = guard.bind_info
    assert bind["account"] == "wxid_aaa"
    # A 的密钥与 B 的不同（绑定链逐账号独立验证）
    assert bind["key_hex"] == key_a.hex()


def test_guard_no_rebind_when_account_stable(fake_wechat, tmp_path,
                                             fake_extractor):
    root, _ka, _kb = fake_wechat
    guard = _make_guard(root, tmp_path / "keys.json")
    guard.bind()
    assert guard.ensure_current() == "wxid_bbb"
    assert guard.ensure_current() == "wxid_bbb"


def test_guard_rejects_explicit_account_mismatch(fake_wechat, tmp_path):
    root, _ka, _kb = fake_wechat
    guard = AccountGuard(explicit_data_root=str(root),
                         explicit_account="wxid_zzz",
                         snapshot_root=tmp_path / "snapshots",
                         key_store_path=tmp_path / "keys.json")
    with pytest.raises(AccountGuardError) as exc:
        guard.bind()
    assert "不一致" in str(exc.value)


def test_guard_uses_cached_key_without_wechat_process(fake_wechat, tmp_path):
    """缓存密钥验证通过时不要求微信进程运行（离线重启场景）。"""
    import json
    root, _ka, key_b = fake_wechat
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(
        {"wxid_bbb": {"db_key": key_b.hex()}}, ensure_ascii=False))
    guard = _make_guard(root, keys_file)
    bind = guard.bind()
    assert bind["key_source"] == "cached_verified"
    assert bind["weixin_pids"] == []      # 全程未要求微信进程


def test_guard_cached_key_of_other_account_not_trusted(fake_wechat, tmp_path):
    """A 账号的缓存密钥绝不能解 B 的库——必须重新提取。"""
    import json
    root, key_a, _kb = fake_wechat
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(
        {"wxid_bbb": {"db_key": key_a.hex()}}))   # 故意放 A 的密钥
    guard = _make_guard(root, keys_file)
    # 微信未运行 -> 内存提取不可用 -> 应显式失败而非错配继续
    with pytest.raises(AccountGuardError) as exc:
        guard.bind()
    assert "密钥" in str(exc.value)


# ---- D1：运行期快照周期刷新（2026-09-09）----

def test_guard_refreshes_snapshot_while_running(fake_wechat, tmp_path):
    """账号未变时 ensure_current 也必须刷新快照：微信 WAL 模式下新消息
    checkpoint 进主库后源库指纹变化，运行期不刷新将永远读到启动时刻的
    静态数据（本次实测导致监控群消息全部漏采）。"""
    import json
    root, _ka, key_b = fake_wechat
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(
        {"wxid_bbb": {"db_key": key_b.hex()}}))
    guard = _make_guard(root, keys_file)
    guard.bind()
    snap_db = Path(guard.bind_info["snapshot_dir"]) / "session" / "session.db"
    assert snap_db.is_file()

    # 模拟微信 checkpoint 后源库更新：内容不同的 session.db 覆盖源。
    # （用明文库走解密器「已是 SQLite 直接复制」分支——加密 roundtrip 对
    # 明文库有保留区语义要求，与被测的刷新逻辑无关。）
    new_plain = root / "_new_plain_tmp.db"
    make_plain_db(new_plain)                      # demo 表 + 50 行（与旧空库不同）
    src = (root / "wxid_bbb_9f3c" / "db_storage" / "session" / "session.db")
    src.write_bytes(new_plain.read_bytes())
    new_plain.unlink()

    account = guard.ensure_current()
    assert account == "wxid_bbb"                  # 账号未切换，但快照已刷新
    conn = sqlite3.connect(snap_db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "demo" in tables


def test_guard_refresh_failure_keeps_polling(fake_wechat, tmp_path, monkeypatch):
    """快照周期刷新异常时降级为 warning，不阻断轮询（沿用旧快照）。"""
    import json
    root, _ka, key_b = fake_wechat
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(
        {"wxid_bbb": {"db_key": key_b.hex()}}))
    guard = _make_guard(root, keys_file)
    guard.bind()

    from wechat_data import snapshot as _snapshot_mod
    def boom(*a, **k):
        raise OSError("disk busy")
    monkeypatch.setattr(_snapshot_mod, "sync_snapshot", boom)
    assert guard.ensure_current() == "wxid_bbb"   # 不抛异常
