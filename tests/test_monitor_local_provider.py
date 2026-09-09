"""角度 6：monitor + LocalProvider 端到端集成冒烟（含账号切换隔离）。

覆盖：
* local 模式首轮基线 -> 新消息采集 -> 重复轮询幂等；
* JSONL 落盘按 <账号>_<群名> 前缀隔离；
* 登录账号切换（B -> A）后：重绑、目标群解析、水位线互不污染；
* 引用消息与主消息一并落盘。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import wechat_monitor
from wechat_monitor import Config, Monitor
from wechat_data.provider import LocalProvider
from wechat_data_fixture import build_chat_message_table, insert_message

GROUP = "88888@chatroom"
GROUP_NAME = "真英雄"
ACCOUNT_A = "wxid_aaa_1e7a"
ACCOUNT_B = "wxid_bbb_9f3c"


class FakeGuard:
    """绑定结果可编程的 AccountGuard 替身（绕开真实进程/密钥）。"""

    def __init__(self, snapshots: dict[str, str]):
        self.snapshots = snapshots          # account -> snapshot dir
        self.current = next(iter(snapshots))
        self._counter = 0

    def bind(self):
        return self._bind_info()

    def ensure_current(self):
        return self.current

    def _bind_info(self):
        self._counter += 1
        return {
            "account": self.current,
            "wxid_dir_name": self.current,
            "key_hex": "00" * 32,
            "key_source": "fixture",
            "snapshot_dir": self.snapshots[self.current],
            "snapshot_total": 1,
            "bind_cost_seconds": 0.0,
        }


def _build_snapshot(root: Path, account: str, msgs: list[dict]) -> Path:
    snap = root / account
    (snap / "session").mkdir(parents=True)
    (snap / "message").mkdir(parents=True)

    conn = sqlite3.connect(snap / "session" / "session.db")
    conn.execute("CREATE TABLE SessionTable (localId INTEGER PRIMARY KEY, "
                 "StrUsrName TEXT, NickName TEXT, lastTimestamp INTEGER)")
    conn.execute("INSERT INTO SessionTable (StrUsrName, NickName, "
                 "lastTimestamp) VALUES (?,?,?)", (GROUP, GROUP_NAME, 1))
    conn.commit()
    conn.close()

    conn = sqlite3.connect(snap / "message" / "message_0.db")
    table = build_chat_message_table(conn, GROUP)
    for m in msgs:
        insert_message(conn, table, m["local_id"], content=m["content"],
                       create_time=m["time"], sender_wxid=m.get("sender", ""),
                       msg_type=m.get("type", 1))
    conn.commit()
    conn.close()
    return snap


def _build_monitor(tmp_path: Path, snapshots: dict[str, str]) -> tuple:
    cfg = Config(target_groups=[GROUP_NAME], output_dir=str(tmp_path / "logs"),
                 state_file=str(tmp_path / "state.json"), poll_interval=0.01,
                 start_mode="resume", prefix="//", quote_marker="#")
    provider = LocalProvider(FakeGuard(snapshots))
    monitor = Monitor(provider, cfg, next(iter(snapshots)))
    return monitor, cfg


def _read_records(logs: Path) -> list[dict]:
    records = []
    for path in sorted(logs.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_local_provider_end_to_end(tmp_path):
    snap = _build_snapshot(tmp_path / "snapshots", ACCOUNT_A, [
        {"local_id": 1, "content": "开盘", "time": 1700000000,
         "sender": "wxid_m1"},
        {"local_id": 2, "content": "//11.73", "time": 1700000050,
         "sender": "wxid_m1"},
    ])
    monitor, cfg = _build_monitor(tmp_path, {ACCOUNT_A: str(snap)})

    # 第 1 轮：基线（frontier=2，历史不回写）
    assert monitor.poll_once() == 0
    assert monitor._store_key(GROUP) in monitor.state

    # 新消息到达（localId=4：// 触发消息 + 引用历史；type 49 正文取 title）
    conn = sqlite3.connect(snap / "message" / "message_0.db")
    table = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")][0]
    quote_xml = ('<msg><appmsg><title>//11.74</title><type>57</type>'
                 "<refermsg><svrid>100001</svrid>"
                 "<displayname>张三</displayname><content>开盘</content>"
                 "</refermsg></appmsg></msg>")
    insert_message(conn, table, 4, content=quote_xml, msg_type=49,
                   create_time=1700000070, sender_wxid="wxid_m2")
    conn.commit()
    conn.close()

    # 第 2 轮：采集 1 条主消息 + 1 条引用
    assert monitor.poll_once() == 2
    records = _read_records(monitor.output_dir)
    assert len(records) == 2
    direct = [r for r in records if r.get("save_reason") == "direct"]
    quoted = [r for r in records if r.get("save_reason") == "quoted"]
    assert len(direct) == 1 and len(quoted) == 1
    assert direct[0]["content"] == "//11.74"
    assert direct[0]["group"] == GROUP_NAME
    assert quoted[0]["content"] == "#开盘"
    assert quoted[0]["quoted_from"] == 4
    # local 模式：文件名带账号前缀
    files = list(monitor.output_dir.glob("*.jsonl"))
    assert files and files[0].name.startswith(ACCOUNT_A)

    # 第 3 轮：无新消息，幂等
    assert monitor.poll_once() == 0
    assert len(_read_records(monitor.output_dir)) == 2


def test_account_switch_isolates_watermarks(tmp_path):
    snap_a = _build_snapshot(tmp_path / "snapshots", ACCOUNT_A, [
        {"local_id": 1, "content": "//9.1 A 群消息", "time": 1700000000,
         "sender": "wxid_m1"},
    ])
    snap_b = _build_snapshot(tmp_path / "snapshots", ACCOUNT_B, [
        {"local_id": 7, "content": "//8.8 B 群消息", "time": 1700000100,
         "sender": "wxid_m1"},
    ])
    monitor, _cfg = _build_monitor(
        tmp_path, {ACCOUNT_B: str(snap_b), ACCOUNT_A: str(snap_a)})

    # 以 B 启动：首轮基线，不写
    assert monitor.poll_once() == 0
    assert f"{ACCOUNT_B}|{GROUP}" in monitor.state

    # 模拟切换回 A（本地Id 空间完全不同：A 的 1 < B 的 7）
    monitor.client._guard.current = ACCOUNT_A
    assert monitor.poll_once() == 0            # A 首轮也是基线
    assert f"{ACCOUNT_A}|{GROUP}" in monitor.state

    # A 出现新消息（localId=1 之后没有更大值 -> 基线即最新，写 0）
    # 再插入一条新消息验证 A 的水位线独立推进
    conn = sqlite3.connect(snap_a / "message" / "message_0.db")
    table = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")][0]
    insert_message(conn, table, 2, content="//9.2 A 新消息",
                   create_time=1700000050, sender_wxid="wxid_m1")
    conn.commit()
    conn.close()
    assert monitor.poll_once() == 1
    records = _read_records(monitor.output_dir)
    contents = [r["content"] for r in records]
    assert "//9.2 A 新消息" in contents
    # B 的消息绝不能混入 A 的采集结果
    assert "//8.8 B 群消息" not in contents
    # 水位线两套并存
    assert f"{ACCOUNT_A}|{GROUP}" in monitor.state
    assert f"{ACCOUNT_B}|{GROUP}" in monitor.state


def test_pre_poll_updates_monitor_account(tmp_path):
    snap = _build_snapshot(tmp_path / "snapshots", ACCOUNT_B, [])
    monitor, _cfg = _build_monitor(tmp_path, {ACCOUNT_B: str(snap)})
    monitor.account = "stale_value"
    monitor.client._guard.current = ACCOUNT_B
    monitor.poll_once()
    assert monitor.account == ACCOUNT_B


# ---- E2 修复：首触基线补采启动后到达的目标消息（2026-09-09 目标机事故）----

def test_first_contact_rescues_post_start_target(tmp_path):
    """群不活跃→新消息唤醒会话→首次解析恰逢首触基线：启动后到达的
    目标消息必须被补采，而不是被水位直接标记为已处理（实测丢第一条）。"""
    now = int(time.time())
    snap = _build_snapshot(tmp_path / "snapshots", ACCOUNT_A, [
        {"local_id": 1, "content": "开盘", "time": 1700000000,
         "sender": "wxid_m1"},
        {"local_id": 2, "content": "//11.99", "time": now,
         "sender": "wxid_m2"},
    ])
    monitor, _cfg = _build_monitor(tmp_path, {ACCOUNT_A: str(snap)})

    # 首轮：基线 + 衡采启动后的 // 消息；启动前的历史积压仍不回采
    assert monitor.poll_once() == 1
    records = _read_records(monitor.output_dir)
    assert [r["content"] for r in records] == ["//11.99"]
    assert monitor._store_key(GROUP) in monitor.state

    # 幂等：第二轮不重复补采
    assert monitor.poll_once() == 0
    assert len(_read_records(monitor.output_dir)) == 1


def test_first_contact_does_not_backfill_old_backlog(tmp_path):
    """启动前的历史目标消息不被首触补采（避免换机后旧积压刷单）。"""
    snap = _build_snapshot(tmp_path / "snapshots", ACCOUNT_A, [
        {"local_id": 1, "content": "//11.73 旧消息", "time": 1700000050,
         "sender": "wxid_m1"},
    ])
    monitor, _cfg = _build_monitor(tmp_path, {ACCOUNT_A: str(snap)})
    assert monitor.poll_once() == 0
    assert _read_records(monitor.output_dir) == []


# ---- E1 修复：部分目标群解析不到时逐群限频 WARNING ----

def test_missing_group_warns_rate_limited(tmp_path, caplog):
    """一个群解析成功、另一个解析不到时，缺失群必须有 WARNING 痕迹
    （此前只在全部失败时打一条 DEBUG，单群失败零痕迹），且限频不刷屏。"""
    snap = _build_snapshot(tmp_path / "snapshots", ACCOUNT_A, [])
    cfg = Config(target_groups=["真英雄", "003记录"],
                 output_dir=str(tmp_path / "logs"),
                 state_file=str(tmp_path / "state.json"), poll_interval=0.01,
                 start_mode="resume", prefix="//", quote_marker="#")
    provider = LocalProvider(FakeGuard({ACCOUNT_A: str(snap)}))
    monitor = Monitor(provider, cfg, ACCOUNT_A)

    with caplog.at_level(logging.WARNING, logger="monitor_hashtag"):
        monitor.poll_once()
        monitor.poll_once()

    warns = [r.getMessage() for r in caplog.records
             if "未在会话列表解析到" in r.getMessage()]
    assert any("003记录" in m for m in warns)          # 缺失群有告警
    assert not any("真英雄" in m for m in warns)        # 已解析群不误报
    assert len(warns) == 1                              # 第二轮限频，不重复
