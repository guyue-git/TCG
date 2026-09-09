"""角度 5：快照读取器（会话/消息/引用消息/分页/联系人映射）单测。

用合成快照目录（明文库）验证 reader 的 schema 防御式发现与
CANDIDATE_KEYS 字段对齐，全部为微信 4.x 惯例命名。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_data.reader import MessageSnapshotReader
from wechat_data_fixture import build_chat_message_table, insert_message

GROUP = "12345678@chatroom"
FRIEND = "wxid_friend"


def _snapshot(tmp_path: Path) -> Path:
    """构造合成快照：session.db + contact.db + message_0.db。"""
    snap = tmp_path / "snapshot" / "wxid_test"
    (snap / "session").mkdir(parents=True)
    (snap / "contact").mkdir(parents=True)
    (snap / "message").mkdir(parents=True)

    conn = sqlite3.connect(snap / "session" / "session.db")
    conn.execute("CREATE TABLE SessionTable (localId INTEGER PRIMARY KEY, "
                 "StrUsrName TEXT, NickName TEXT, lastTimestamp INTEGER)")
    conn.executemany(
        "INSERT INTO SessionTable (StrUsrName, NickName, lastTimestamp) "
        "VALUES (?,?,?)",
        [(GROUP, "真英雄", 1700000100), (FRIEND, "好友", 1700000200)])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(snap / "contact" / "contact.db")
    conn.execute("CREATE TABLE Contact (UserName TEXT PRIMARY KEY, "
                 "NickName TEXT, Remark TEXT)")
    conn.executemany("INSERT INTO Contact VALUES (?,?,?)",
                     [(GROUP, "真英雄官方名", ""), (FRIEND, "好友", "备注")])
    conn.commit()
    conn.close()

    conn = sqlite3.connect(snap / "message" / "message_0.db")
    table = build_chat_message_table(conn, GROUP)
    insert_message(conn, table, 1, content="早上好", create_time=1700000000,
                   sender_wxid="wxid_member1")
    insert_message(conn, table, 2, content="//11.73", create_time=1700000050,
                   sender_wxid="wxid_member2")
    # type 49 引用消息：引用 localId=1 的消息
    quote_xml = (
        '<msg><appmsg appid="" sdkver="0"><title>11.73</title><type>57</type>'
        "<refermsg><type>1</type><svrid>100001</svrid><fromusr>wxid_test</fromusr>"
        "<chatusr>wxid_member1</chatusr><displayname>张三</displayname>"
        "<content>早上好</content></refermsg></appmsg></msg>")
    insert_message(conn, table, 3, content=quote_xml, msg_type=49,
                   create_time=1700000060, sender_wxid="wxid_member2")
    # 好友私聊消息（另一个 per-chat 表）
    table2 = build_chat_message_table(conn, FRIEND)
    insert_message(conn, table2, 1, content="hi", create_time=1700000000,
                   sender_wxid=FRIEND)
    conn.commit()
    conn.close()
    return snap


def test_list_sessions_fields(tmp_path):
    reader = MessageSnapshotReader(_snapshot(tmp_path))
    sessions = reader.list_sessions()
    by_name = {s["username"]: s for s in sessions}
    assert set(by_name) == {GROUP, FRIEND}
    assert by_name[GROUP]["name"] == "真英雄"      # 会话表昵称优先
    assert by_name[GROUP]["isGroup"] is True
    assert by_name[FRIEND]["isGroup"] is False


def test_list_messages_desc_and_offset(tmp_path):
    reader = MessageSnapshotReader(_snapshot(tmp_path))
    page1 = reader.list_messages(GROUP, limit=2, offset=0)
    assert [m["localId"] for m in page1] == [3, 2]      # newest-first
    page2 = reader.list_messages(GROUP, limit=2, offset=2)
    assert [m["localId"] for m in page2] == [1]


def test_message_field_normalization(tmp_path):
    reader = MessageSnapshotReader(_snapshot(tmp_path))
    msg = reader.list_messages(GROUP, limit=1, offset=1)[0]   # localId=2
    assert msg["localId"] == 2
    assert msg["message_content"] == "//11.73"
    assert msg["senderUsername"] == "wxid_member2"
    assert msg["senderDisplayName"] == "wxid_member2"          # 联系人表中无此 wxid
    assert msg["create_time"] == 1700000050
    assert msg["isSent"] is False
    assert msg["isGroup"] is True


def test_quote_message_parsed(tmp_path):
    reader = MessageSnapshotReader(_snapshot(tmp_path))
    msg = reader.list_messages(GROUP, limit=1, offset=0)[0]   # localId=3
    assert msg["quoteServerId"] == 100001
    assert msg["quoteUsername"] == "张三"
    assert msg["quoteContent"] == "早上好"


def test_sender_resolved_via_name2id_and_contact(tmp_path):
    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "contact" / "contact.db")
    conn.execute("INSERT INTO Contact VALUES ('wxid_member1','李四','')")
    conn.commit()
    conn.close()
    reader = MessageSnapshotReader(snap)
    msg = reader.list_messages(GROUP, limit=1, offset=2)[0]   # localId=1
    assert msg["senderUsername"] == "wxid_member1"
    assert msg["senderDisplayName"] == "李四"


def test_private_chat_isolated_per_table(tmp_path):
    reader = MessageSnapshotReader(_snapshot(tmp_path))
    msgs = reader.list_messages(FRIEND, limit=10)
    assert [m["localId"] for m in msgs] == [1]
    assert msgs[0]["isGroup"] is False
    assert msgs[0]["message_content"] == "hi"


def test_missing_snapshot_dir_returns_empty(tmp_path):
    reader = MessageSnapshotReader(tmp_path / "nonexistent")
    assert reader.list_sessions() == []
    assert reader.list_messages("x@chatroom") == []


# ---- D3：主内容列 zstd 压缩（2026-09-09 实测微信 4.x 行为）----

ZSTD_GROUP = "99999@chatroom"


def test_zstd_compressed_main_content(tmp_path):
    """整条消息被 zstd 压缩后直接存入 message_content（实测「//88」即此形态），
    reader 须按 magic 识别解压，否则前缀匹配静默失败。"""
    import zstandard

    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "message" / "message_0.db")
    table = build_chat_message_table(conn, ZSTD_GROUP)
    compressed = zstandard.ZstdCompressor().compress("//88".encode("utf-8"))
    insert_message(conn, table, 1, content=compressed, create_time=1700000100,
                   sender_wxid="wxid_member1")
    conn.commit()
    conn.close()

    reader = MessageSnapshotReader(snap)
    msg = reader.list_messages(ZSTD_GROUP, limit=1)[0]
    assert msg["message_content"] == "//88"


def test_plain_bytes_main_content_decoded(tmp_path):
    """主内容列为非压缩 bytes（BLOB 形态的普通文本）时按 UTF-8 解码。"""
    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "message" / "message_0.db")
    table = build_chat_message_table(conn, ZSTD_GROUP)
    insert_message(conn, table, 1, content="//66".encode("utf-8"),
                   create_time=1700000100, sender_wxid="wxid_member1")
    conn.commit()
    conn.close()

    reader = MessageSnapshotReader(snap)
    msg = reader.list_messages(ZSTD_GROUP, limit=1)[0]
    assert msg["message_content"] == "//66"


# ---- E1：contact.db 兜底解析群会话（2026-09-09 目标机事故）----
# 会话表按活跃度维护 + LIMIT 截断，不活跃目标群可能不在会话库可见集合
# （实测 001记录 全程零采集）。reader 必须从通讯录补全群聊会话。

INACTIVE_GROUP = "77777@chatroom"


def test_contact_fallback_adds_inactive_group(tmp_path):
    """会话库不含不活跃群时，从 contact.db 通讯录兜底补全群会话。"""
    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "contact" / "contact.db")
    conn.execute("INSERT INTO Contact VALUES (?,?,?)",
                 (INACTIVE_GROUP, "查卡拉", ""))
    conn.commit()
    conn.close()

    reader = MessageSnapshotReader(snap)
    by_name = {s["username"]: s for s in reader.list_sessions()}
    assert by_name[INACTIVE_GROUP]["name"] == "查卡拉"
    assert by_name[INACTIVE_GROUP]["isGroup"] is True


def test_contact_fallback_skips_private_chats(tmp_path):
    """通讯录兜底只补群聊（@chatroom），好友不注入会话列表。"""
    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "contact" / "contact.db")
    conn.execute("INSERT INTO Contact VALUES (?,?,?)",
                 ("wxid_newfriend", "新好友", ""))
    conn.commit()
    conn.close()

    reader = MessageSnapshotReader(snap)
    users = {s["username"] for s in reader.list_sessions()}
    assert "wxid_newfriend" not in users


def test_contact_fallback_remark_used_without_nick(tmp_path):
    """通讯录群条目无 NickName 时回落 Remark 作为群名。"""
    snap = _snapshot(tmp_path)
    conn = sqlite3.connect(snap / "contact" / "contact.db")
    conn.execute("INSERT INTO Contact VALUES (?,?,?)",
                 (INACTIVE_GROUP, "", "003记录"))
    conn.commit()
    conn.close()

    reader = MessageSnapshotReader(snap)
    by_name = {s["username"]: s for s in reader.list_sessions()}
    assert by_name[INACTIVE_GROUP]["name"] == "003记录"
