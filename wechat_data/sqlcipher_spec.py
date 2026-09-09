"""SQLCipher 4 / WCDB 加密规格 —— 参数派生、页 HMAC、页解密、密钥验证。

移植自 WeChatDataAnalysis src/wechat_decrypt_tool/wechat_decrypt.py，
只保留核心链路（诊断分支已简化），参数与原实现逐字节一致：

* PBKDF2-HMAC-SHA512，256000 轮（passphrase -> enc_key）
* mac_key = PBKDF2-HMAC-SHA512(enc_key, salt^0x3a, 2 轮)
* AES-256-CBC，页大小 4096，页尾 reserve = IV(16) + HMAC-SHA512(64)
* 页 HMAC 覆盖：加密载荷 + IV，并追加小端 4 字节页号
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import struct
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SQLITE_HEADER = b"SQLite format 3\x00"
PAGE_SIZE = 4096
KEY_SIZE = 32
SALT_SIZE = 16
IV_SIZE = 16
HMAC_SIZE = 64
RESERVE_SIZE = IV_SIZE + HMAC_SIZE          # 80
PBKDF2_ROUNDS = 256000
MAC_PBKDF2_ROUNDS = 2


def derive_sqlcipher_enc_key(key_material: bytes, salt: bytes) -> bytes:
    """passphrase/原始密钥材料 -> AES enc_key（256000 轮）。"""
    return hashlib.pbkdf2_hmac(
        "sha512", key_material, salt, PBKDF2_ROUNDS, dklen=KEY_SIZE)


def derive_mac_key(enc_key: bytes, salt: bytes) -> bytes:
    """enc_key -> 页 HMAC key（salt 逐字节异或 0x3A 后 2 轮派生）。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    return hashlib.pbkdf2_hmac(
        "sha512", enc_key, mac_salt, MAC_PBKDF2_ROUNDS, dklen=KEY_SIZE)


def compute_page_hmac(mac_key: bytes, page: bytes, page_num: int) -> bytes:
    """计算一页的 HMAC-SHA512（页号小端 4 字节追加）。"""
    offset = SALT_SIZE if page_num == 1 else 0
    data_end = PAGE_SIZE - RESERVE_SIZE + IV_SIZE
    mac = hmac.new(mac_key, digestmod=hashlib.sha512)
    mac.update(page[offset:data_end])
    mac.update(struct.pack("<I", page_num))
    return mac.digest()


def decrypt_page(enc_key: bytes, page: bytes, page_num: int) -> bytes:
    """解密单页并重排为标准 SQLite 页（页尾 reserve 补零）。"""
    iv = page[PAGE_SIZE - RESERVE_SIZE:PAGE_SIZE - RESERVE_SIZE + IV_SIZE]
    offset = SALT_SIZE if page_num == 1 else 0
    encrypted_payload = page[offset:PAGE_SIZE - RESERVE_SIZE]

    cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv),
                    backend=default_backend())
    decryptor = cipher.decryptor()
    plain_body = decryptor.update(encrypted_payload) + decryptor.finalize()

    if page_num == 1:
        return SQLITE_HEADER + plain_body + (b"\x00" * RESERVE_SIZE)
    return plain_body + (b"\x00" * RESERVE_SIZE)


def resolve_page1_key_material(
        key_material: bytes, page1: bytes) -> Optional[Tuple[bytes, bytes, str]]:
    """判定密钥材料是 raw enc_key 还是 SQLCipher passphrase（page-1 HMAC）。

    返回 (enc_key, mac_key, mode)；不匹配返回 None。
    """
    if len(page1) < PAGE_SIZE:
        return None

    salt = page1[:SALT_SIZE]
    stored_hmac = page1[PAGE_SIZE - HMAC_SIZE:PAGE_SIZE]
    candidates = [
        ("raw_enc_key", key_material,
         derive_mac_key(key_material, salt)),
    ]
    derived_key = derive_sqlcipher_enc_key(key_material, salt)
    candidates.append(
        ("sqlcipher_passphrase", derived_key, derive_mac_key(derived_key, salt)))

    for mode, enc_key, mac_key in candidates:
        if hmac.compare_digest(stored_hmac, compute_page_hmac(mac_key, page1, 1)):
            return enc_key, mac_key, mode
    return None


def read_page1(db_path: str | Path) -> bytes:
    """读取库文件第一页；文件过小或不可读返回空串。"""
    try:
        with Path(db_path).open("rb") as fh:
            return fh.read(PAGE_SIZE)
    except OSError:
        return b""


def verify_key_against_page1(key_hex: str, db_path: str | Path) -> str:
    """用 page-1 HMAC 校验密钥与库文件是否匹配。

    返回 key_mode（raw_enc_key / sqlcipher_passphrase / "" 表示已解密明文库），
    校验失败返回空串。不抛异常。
    """
    page1 = read_page1(db_path)
    if not page1:
        return ""
    if page1.startswith(SQLITE_HEADER):
        return ""          # 明文库无需密钥（也不能证明密钥正确）
    try:
        key_material = bytes.fromhex(str(key_hex or "").strip())
    except ValueError:
        return ""
    if len(key_material) != KEY_SIZE:
        return ""
    resolved = resolve_page1_key_material(key_material, page1)
    return resolved[2] if resolved is not None else ""


def sqlite_quick_check(db_path: str | Path) -> Tuple[bool, str]:
    """对解密产物做 SQLite quick_check 完整性校验。返回 (ok, detail)。"""
    path = str(db_path)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            detail = str(row[0]) if row else ""
        finally:
            conn.close()
        return detail.lower() == "ok", detail
    except sqlite3.Error as exc:
        return False, f"{type(exc).__name__}: {exc}"


def build_diagnostic(page_num: int, reason: str) -> Dict[str, Any]:
    """构造最小页诊断信息（不包含任何业务明文）。"""
    return {"page": int(page_num), "reason": str(reason)}
