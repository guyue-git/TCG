"""角度 1/2：SQLCipher 加解密 roundtrip 与密钥验证单测。

覆盖：
* sqlcipher_passphrase / raw_enc_key 双模式解密 roundtrip（字节级）；
* 错误密钥必须被 page-1 HMAC 拒绝（key_mismatch）；
* 明文库直通复制；
* 解密产物 SQLite quick_check 通过；
* key_extractor：熵过滤、特征指针扫描、候选验证。
"""

from __future__ import annotations

import hashlib
import os
import struct

import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_data.decryptor import WeChatDatabaseDecryptor, DecryptError
from wechat_data.key_extractor import (is_potential_key, scan_key_stub_pointers,
                                       verify_key_candidate)
from wechat_data.sqlcipher_spec import (PAGE_SIZE, compute_page_hmac,
                                        derive_mac_key,
                                        derive_sqlcipher_enc_key,
                                        verify_key_against_page1)
from wechat_data_fixture import (derive_roundtrip_keys, encrypt_sqlite_db,
                                 make_empty_reserved_db, make_plain_db)


@pytest.fixture()
def plain_db(tmp_path):
    """保留区语义正确的空库（SQLCipher 明文形态）：可做字节级 roundtrip。"""
    db = tmp_path / "plain.db"
    make_empty_reserved_db(db)
    return db


def test_roundtrip_passphrase_mode(plain_db, tmp_path):
    passphrase = os.urandom(32)
    enc_blob = encrypt_sqlite_db(plain_db, passphrase, "sqlcipher_passphrase")
    enc_path = tmp_path / "session.db"
    enc_path.write_bytes(enc_blob)
    out_path = tmp_path / "out.db"

    result = WeChatDatabaseDecryptor(passphrase.hex()).decrypt_database(
        enc_path, out_path)
    assert result["success"], result["error"]
    assert result["key_mode"] == "sqlcipher_passphrase"
    assert result["failed_pages"] == 0
    assert result["hmac_warning_pages"] == 0
    assert out_path.read_bytes() == plain_db.read_bytes()


def test_roundtrip_raw_key_mode(plain_db, tmp_path):
    raw_key = os.urandom(32)
    enc_blob = encrypt_sqlite_db(plain_db, raw_key, "raw_enc_key")
    enc_path = tmp_path / "message_0.db"
    enc_path.write_bytes(enc_blob)
    out_path = tmp_path / "out.db"

    result = WeChatDatabaseDecryptor(raw_key.hex()).decrypt_database(
        enc_path, out_path)
    assert result["success"], result["error"]
    assert result["key_mode"] == "raw_enc_key"
    assert out_path.read_bytes() == plain_db.read_bytes()


def test_wrong_key_rejected(plain_db, tmp_path):
    enc_blob = encrypt_sqlite_db(plain_db, os.urandom(32))
    enc_path = tmp_path / "session.db"
    enc_path.write_bytes(enc_blob)
    wrong = WeChatDatabaseDecryptor(os.urandom(32).hex()).decrypt_database(
        enc_path, tmp_path / "out.db")
    assert not wrong["success"]
    assert wrong["error"] == "key_mismatch"
    assert not (tmp_path / "out.db").exists()


def test_hmac_warning_deduped_across_calls(tmp_path, caplog):
    """P4：快照周期刷新反复解密同一库，WAL 活跃页滑动窗口每轮触发同一批
    页告警。同一（库, 页号）只允许告警一次；hmac_warning_pages 计数不受
    去重影响。（被篡改页会使输出完整性校验失败，但那发生在告警之后，
    与本用例无关——此处只测告警去重这一行为单元。）"""
    import logging
    import sqlite3

    # 多页明文库（单页库没有"非首页"可构造失配）
    plain = tmp_path / "multi.db"
    conn = sqlite3.connect(plain)
    conn.execute("PRAGMA page_size = 4096")
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, val TEXT)")
    conn.executemany("INSERT INTO demo(val) VALUES (?)",
                     [(f"v{i:04d}" + "x" * 100,) for i in range(500)])
    conn.commit()
    conn.close()
    assert plain.stat().st_size > 2 * PAGE_SIZE

    passphrase = os.urandom(32)
    enc_blob = bytearray(encrypt_sqlite_db(plain, passphrase))
    # 翻转第 2 页尾 HMAC 区末字节：stored_hmac 失配但 IV/密文原样
    enc_blob[2 * PAGE_SIZE - 1] ^= 0xFF
    enc_path = tmp_path / "message_0.db"
    enc_path.write_bytes(bytes(enc_blob))
    decryptor = WeChatDatabaseDecryptor(passphrase.hex())

    with caplog.at_level(logging.WARNING, logger="wechat_data.decryptor"):
        r1 = decryptor.decrypt_database(enc_path, tmp_path / "out1.db")
        r2 = decryptor.decrypt_database(enc_path, tmp_path / "out2.db")
    assert r1["hmac_warning_pages"] == 1
    assert r2["hmac_warning_pages"] == 1
    warns = [r for r in caplog.records if "HMAC" in r.getMessage()]
    assert len(warns) == 1               # 第二轮同页不再告警


def test_plaintext_db_copied_through(plain_db, tmp_path):
    out_path = tmp_path / "out.db"
    result = WeChatDatabaseDecryptor(os.urandom(32).hex()).decrypt_database(
        plain_db, out_path)
    assert result["success"] and result["copied_as_sqlite"]
    assert out_path.read_bytes() == plain_db.read_bytes()


def test_verify_key_against_page1_roles(plain_db, tmp_path):
    passphrase = os.urandom(32)
    enc_blob = encrypt_sqlite_db(plain_db, passphrase)
    enc_path = tmp_path / "session.db"
    enc_path.write_bytes(enc_blob)
    assert verify_key_against_page1(passphrase.hex(), enc_path) == \
        "sqlcipher_passphrase"
    assert verify_key_against_page1(os.urandom(32).hex(), enc_path) == ""
    assert verify_key_against_page1("zz", enc_path) == ""
    # 明文库：无需密钥，返回空串
    assert verify_key_against_page1(passphrase.hex(), plain_db) == ""


def test_decryptor_rejects_bad_key_format():
    with pytest.raises(DecryptError):
        WeChatDatabaseDecryptor("short")


def test_derive_chain_matches_independent_implementation(plain_db, tmp_path):
    """sqlcipher_spec 派生链与夹具独立实现逐字节一致（防移植走样）。"""
    salt = os.urandom(16)
    material = os.urandom(32)
    enc_key_ref, mac_key_ref = derive_roundtrip_keys(material, salt)
    enc_key = derive_sqlcipher_enc_key(material, salt)
    mac_key = derive_mac_key(enc_key, salt)
    assert enc_key == enc_key_ref
    assert mac_key == mac_key_ref

    # HMAC 结构性质：页号参与运算（页 1 与页 2 不同）；载荷翻转即失效
    page = bytearray(PAGE_SIZE)
    page[0:16] = salt
    page[16:4032] = os.urandom(4016)
    page[4032:4096] = os.urandom(64)
    h1 = compute_page_hmac(mac_key, bytes(page), 1)
    h2 = compute_page_hmac(mac_key, bytes(page), 2)
    assert h1 != h2
    tampered = bytearray(page)
    tampered[100] ^= 0xFF
    assert compute_page_hmac(mac_key, bytes(tampered), 1) != h1


# ---- key_extractor ----

def test_is_potential_key_filters():
    assert is_potential_key(os.urandom(32))
    assert not is_potential_key(b"\x00" * 32)                 # 全零
    assert not is_potential_key(b"a" * 32)                    # 单字节重复
    assert not is_potential_key(b"Hello, World! This is a PK"[:32])  # 可打印文本
    assert not is_potential_key(b"\x01" * 31)                 # 长度不足


def _make_stub_blob(pointer: int) -> bytes:
    """内存布局：指针低 6 字节（通配）+ 26 字节固定尾巴。

    尾巴 = 指针高 2 字节（00 00，地址 < 2^48）+ 8 字节零 + u64(0x20) + u64(0x2f)。
    """
    tail = bytes.fromhex(
        "0000000000000000000020000000000000002f00000000000000")
    assert len(tail) == 26
    return struct.pack("<Q", pointer)[:6] + tail


def test_scan_key_stub_pointers():
    pointer = 0x0000_1234_5678_9ABC
    blob = _make_stub_blob(pointer)
    assert scan_key_stub_pointers(blob) == [pointer]
    # 多个命中 + 干扰数据
    blob2 = blob + b"\xff" * 64 + blob
    assert scan_key_stub_pointers(blob2) == [pointer, pointer]
    assert scan_key_stub_pointers(b"\x00" * 4096) == []
    # 高 2 字节非零（>= 2^48）不构成合法特征前缀 -> 读出的指针错误，仍会
    # 返回候选（后续由 HMAC 验证淘汰），这里只验证扫描本身不崩
    assert isinstance(scan_key_stub_pointers(b"\xab" * 6 + bytes.fromhex(
        "0000000000000000000020000000000000002f00000000000000")), list)


def test_verify_key_candidate_against_synthetic_page1(tmp_path):
    db = tmp_path / "p.db"
    make_empty_reserved_db(db)
    passphrase = os.urandom(32)
    blob = encrypt_sqlite_db(db, passphrase)
    page1 = blob[:PAGE_SIZE]
    assert verify_key_candidate(passphrase, page1) == passphrase.hex()
    assert verify_key_candidate(os.urandom(32), page1) is None
    assert verify_key_candidate(b"\x00" * 32, page1) is None
