"""ServiceAPusher 推送可靠性测试（2026-09-04 实测缺口修复）。

缺口：推送失败单次尝试即静默丢弃，服务 A 短暂离线 = 确认书永久丢失
（水位线已推进，重启 monitor 无法自愈）。修复：退避重试 → 落盘
pending 文件 → 周期性/重启后自动补投，服务 A 按 (group, msg_id) 幂等兜底。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import wechat_monitor
from wechat_monitor import ServiceAPusher


def _payload(**overrides):
    # seq 由 push() 内部自增生成，不在调用参数内
    base = {
        "group": "真英雄", "sender": "wxid_test",
        "msg_id": 110, "create_time": 1788500515, "content": "//100",
        "quoted_content": "##下单 9080\n300476 胜宏科技 限价241.80 买入 50W",
        "quoted_msg_id": 5043338921154128123,
    }
    base.update(overrides)
    return base


class FakeServiceA:
    """可编程假服务 A：记录收到的 body，可控制先失败 N 次 / 判 duplicate。"""

    def __init__(self, fail_first: int = 0, accepted: bool = True):
        self.received: list[dict] = []
        self.fail_first = fail_first
        self.accepted = accepted
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                with outer._lock:
                    outer.received.append(json.loads(body))
                    n = len(outer.received)
                if n <= outer.fail_first:
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = json.dumps(
                    {"accepted": outer.accepted, "reason": "ok"}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _wait_until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """缩短退避间隔与补投周期，避免测试拖时。"""
    monkeypatch.setattr(wechat_monitor, "PUSH_RETRY_DELAYS", (0.05, 0.05, 0.05))
    monkeypatch.setattr(wechat_monitor, "PENDING_RETRY_INTERVAL", 0.2)


class TestServiceAPusherReliability:

    def test_success_delivery_no_pending(self, tmp_path):
        """投递成功：不产生 pending 文件。"""
        srv = FakeServiceA()
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            pusher.push(**_payload())
            assert _wait_until(lambda: len(srv.received) == 1)
            assert srv.received[0]["msg_id"] == 110
            assert srv.received[0]["quoted_content"].startswith("##下单 9080")
            assert not pending.exists()
        finally:
            pusher.close()
            srv.stop()

    def test_failure_retries_then_persists(self, tmp_path):
        """服务 A 持续失败：退避重试后落盘待补投，不丢消息。"""
        srv = FakeServiceA(fail_first=10**9)  # 永远失败
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            pusher.push(**_payload(msg_id=999))
            assert _wait_until(lambda: pending.exists())
            lines = [json.loads(x) for x in
                     pending.read_text(encoding="utf-8").splitlines() if x]
            assert len(lines) == 1
            assert lines[0]["msg_id"] == 999
            assert lines[0]["content"] == "//100"
        finally:
            pusher.close()
            srv.stop()

    def test_retry_succeeds_before_pending(self, tmp_path):
        """先失败后恢复：退避重试内成功，不落盘。"""
        srv = FakeServiceA(fail_first=2)  # 前 2 次失败，第 3 次成功
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            pusher.push(**_payload())
            assert _wait_until(lambda: len(srv.received) >= 3)
            assert not pending.exists()
        finally:
            pusher.close()
            srv.stop()

    def test_pending_redelivered_on_restart(self, tmp_path):
        """重启恢复：pending 文件重新入队，服务 A 恢复后自动送达并清空文件。"""
        # 第一步：服务 A 持续 500 → 退避重试耗尽 → 落盘 pending
        # （不用死端口：本机 TUN 环境下 connect 拒绝可达秒级，时序不确定）
        dead = FakeServiceA(fail_first=10**9)
        pending = tmp_path / "pending_push.jsonl"
        pusher1 = ServiceAPusher(dead.url, pending_path=pending)
        try:
            pusher1.push(**_payload(msg_id=777))
            assert _wait_until(lambda: pending.exists())
        finally:
            pusher1.close()
            dead.stop()

        # 第二步：服务 A 上线，新 pusher 加载 pending 补投
        srv = FakeServiceA()
        pusher2 = ServiceAPusher(srv.url, pending_path=pending)
        try:
            assert _wait_until(lambda: len(srv.received) == 1)
            assert srv.received[0]["msg_id"] == 777
            assert _wait_until(lambda: not pending.exists())
        finally:
            pusher2.close()
            srv.stop()

    def test_pending_retry_while_running(self, tmp_path):
        """运行中服务 A 恢复：周期性补投自动追赶，无需重启 monitor。"""
        srv = FakeServiceA(fail_first=10**9)
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            pusher.push(**_payload(msg_id=888))
            assert _wait_until(lambda: pending.exists())
            # 服务 A 恢复（同一 server 对象改计数器即可放行）
            srv.fail_first = 0
            assert _wait_until(lambda: len(srv.received) >= 1)
            assert _wait_until(lambda: not pending.exists())
        finally:
            pusher.close()
            srv.stop()

    def test_close_drains_unsent_queue_to_pending(self, tmp_path):
        """close 时排空队列：未发送的消息落盘待补投，不随停机丢失。"""
        srv = FakeServiceA(fail_first=10**9)
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        pusher.push(**_payload(msg_id=555))
        pusher.close()  # 重试间隔被 fixture 缩短，close 前可能已失败落盘
        try:
            lines = [json.loads(x) for x in
                     pending.read_text(encoding="utf-8").splitlines() if x]
            assert any(rec["msg_id"] == 555 for rec in lines)
        finally:
            srv.stop()

    def test_duplicate_response_is_success(self, tmp_path):
        """accepted=false（duplicate）：消息已在服务 A，按成功处理不落盘。"""
        srv = FakeServiceA(accepted=False)
        pending = tmp_path / "pending_push.jsonl"
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            pusher.push(**_payload())
            assert _wait_until(lambda: len(srv.received) == 1)
            time.sleep(0.5)  # 留出潜在重试窗口
            assert len(srv.received) == 1  # 无重试
            assert not pending.exists()
        finally:
            pusher.close()
            srv.stop()

    def test_pending_file_corrupt_line_skipped(self, tmp_path):
        """pending 文件损坏行跳过，正常行仍补投。"""
        pending = tmp_path / "pending_push.jsonl"
        good = _payload(msg_id=321)
        pending.write_text(
            "{not json}\n" + json.dumps(good, ensure_ascii=False) + "\n",
            encoding="utf-8")
        srv = FakeServiceA()
        pusher = ServiceAPusher(srv.url, pending_path=pending)
        try:
            assert _wait_until(lambda: len(srv.received) == 1)
            assert srv.received[0]["msg_id"] == 321
        finally:
            pusher.close()
            srv.stop()


def test_api_client_default_timeout_hardened():
    """ApiClient 默认读超时 30s：/api/chat/messages 后端固有延迟 5-8s，
    10s 在负载峰值下余量不足（2026-09-07 生产 poll error 根因）。"""
    from wechat_monitor import ApiClient, Config

    client = ApiClient(Config())
    assert client.timeout == 30.0
