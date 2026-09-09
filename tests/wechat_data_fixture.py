"""wechat_data 测试公共夹具：构造 SQLCipher 格式样本库与合成快照。

加密方向实现与 sqlcipher_spec 解密方向互为镜像，用于 roundtrip 验证：
    页 1:  salt(16) + AES(plain[16:4016]) + iv(16) + HMAC-SHA512(mac, enc+iv, 页号)
    页 n:  AES(plain[0:4016]) + iv(16) + HMAC(...)
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import os
import sqlite3
import struct
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PAGE_SIZE = 4096
KEY_SIZE = 32
RESERVE = 80


def derive_roundtrip_keys(key_material: bytes, salt: bytes) -> tuple[bytes, bytes]:
    """与 sqlcipher_spec 相同的派生链：enc_key / mac_key。"""
    enc_key = hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, dklen=32)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    return enc_key, mac_key


def encrypt_sqlite_db(plain_path: Path, key_material: bytes,
                      mode: str = "sqlcipher_passphrase") -> bytes:
    """把明文 SQLite 文件加密为 SQLCipher 布局字节串。"""
    plain = plain_path.read_bytes()
    pad = (-len(plain)) % PAGE_SIZE
    plain += b"\x00" * pad
    salt = os.urandom(16)
    if mode == "raw_enc_key":
        enc_key = key_material
    else:
        enc_key = hashlib.pbkdf2_hmac("sha512", key_material, salt, 256000, dklen=32)
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)

    out = bytearray()
    total_pages = len(plain) // PAGE_SIZE
    for page_num in range(1, total_pages + 1):
        page = plain[(page_num - 1) * PAGE_SIZE: page_num * PAGE_SIZE]
        offset = 16 if page_num == 1 else 0
        body = page[offset:PAGE_SIZE - RESERVE]
        iv = os.urandom(16)
        cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv),
                        backend=default_backend())
        encryptor = cipher.encryptor()
        encrypted = encryptor.update(body) + encryptor.finalize()
        mac = hmac_mod.new(mac_key, encrypted + iv, hashlib.sha512)
        mac.update(struct.pack("<I", page_num))
        if page_num == 1:
            out += salt
        out += encrypted + iv + mac.digest()
    return bytes(out)


def make_plain_db(path: Path) -> None:
    """构造带几行数据的小型明文库（page1 尾部保持零）。"""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, val TEXT)")
        conn.executemany("INSERT INTO demo(val) VALUES (?)",
                         [(f"v{i}",) for i in range(50)])
        conn.commit()
    finally:
        conn.close()


def make_empty_reserved_db(path: Path) -> None:
    """构造单页「保留区语义正确」的空 SQLite 库（模拟 SQLCipher 明文形态）。

    SQLCipher 库的 header 保留字节（byte 20）恒为 80，页尾 80 字节不属于
    有效载荷——这正是 SQLCipher 加密布局「页 1 = salt(16)+密文(4000)+IV+HMAC」
    不丢数据的先决条件。普通 sqlite3 建库无法设置保留区，故手工构造：
    header 各字段有效、schema 为空（quick_check=ok）、页尾 80 字节为零。
    """
    import struct as _struct
    page = bytearray(PAGE_SIZE)
    page[0:16] = b"SQLite format 3\x00"
    page[16:18] = _struct.pack(">H", 4096)     # page size
    page[18] = 1                               # write version (legacy)
    page[19] = 1                               # read version
    page[20] = 80                              # reserved space（关键）
    page[21], page[22], page[23] = 64, 32, 32  # payload fractions
    page[24:28] = _struct.pack(">I", 1)        # file change counter
    page[28:32] = _struct.pack(">I", 1)        # db size in pages
    page[40:44] = _struct.pack(">I", 1)        # schema cookie
    page[44:48] = _struct.pack(">I", 4)        # schema format
    page[56:60] = _struct.pack(">I", 1)        # text encoding (utf-8)
    page[92:96] = _struct.pack(">I", 1)        # version-valid-for
    page[96:100] = _struct.pack(">I", 3045000)  # sqlite version
    # 页 1 同时是 sqlite_master 的叶子表 btree 根页（空 schema，0 个单元格）
    page[100] = 0x0D                            # leaf table btree
    page[101:103] = b"\x00\x00"                 # first freeblock
    page[103:105] = b"\x00\x00"                 # cell count = 0
    page[105:107] = _struct.pack(">H", 4016)    # cell content area（= usable）
    page[107] = 0                               # fragmented free bytes
    path.write_bytes(bytes(page))


def build_chat_message_table(conn: sqlite3.Connection, username: str) -> str:
    """按微信 4.x 惯例建 per-chat 表（ChatMsg_<md5>）+ Name2ID。"""
    md5 = hashlib.md5(username.encode("utf-8")).hexdigest()
    table = f"ChatMsg_{md5}"
    conn.execute(
        f'CREATE TABLE "{table}" ('
        "localId INTEGER PRIMARY KEY, MsgSvrID INTEGER, Type INTEGER, "
        "IsSender INTEGER, CreateTime INTEGER, SenderTalkerId INTEGER, "
        "StrContent TEXT, CompressContent BLOB)")
    if not _table_exists(conn, "Name2ID"):
        conn.execute("CREATE TABLE Name2ID (usrName TEXT, id INTEGER PRIMARY KEY)")
    return table


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return row is not None


def insert_message(conn: sqlite3.Connection, table: str, local_id: int, *,
                   content: str = "", compress: bytes = b"", msg_type: int = 1,
                   is_sender: int = 0, create_time: int = 1700000000,
                   sender_talker_id: int = 0, sender_wxid: str = "") -> None:
    if sender_wxid:
        row = conn.execute("SELECT id FROM Name2ID WHERE usrName=?",
                           (sender_wxid,)).fetchone()
        if row is None:
            cur = conn.execute("INSERT INTO Name2ID(usrName) VALUES (?)",
                               (sender_wxid,))
            sender_talker_id = cur.lastrowid
        else:
            sender_talker_id = int(row[0])
    conn.execute(
        f'INSERT INTO "{table}" (localId, MsgSvrID, Type, IsSender, CreateTime, '
        "SenderTalkerId, StrContent, CompressContent) VALUES (?,?,?,?,?,?,?,?)",
        (local_id, 100000 + local_id, msg_type, is_sender, create_time,
         sender_talker_id, content, compress))
