"""服务 A 测试：分类器、收件箱、分发器、HTTP 契约与端到端 PDF 生成."""

from __future__ import annotations

import datetime as dt
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from service_a.classifier import classify
from service_a.config import ServiceConfig
from service_a.dispatcher import Dispatcher
from service_a.handler import HandlerContext
from service_a.inbox import InboxStore, safe_name
from service_a.message import IncomingMessage
from service_a.__main__ import build_dispatcher, make_http_handler

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TS_20260902 = int(dt.datetime(2026, 9, 2, 10, 0, 0).timestamp())


def make_msg(**overrides) -> IncomingMessage:
    base = dict(
        seq=1, group="真英雄", sender="wxid_trader", msg_id=1001,
        create_time=TS_20260902, content="//15.3",
        quoted_content="#下单 9080 300017 网宿科技 限价 买入 200W",
        quoted_msg_id=999)
    base.update(overrides)
    return IncomingMessage(**base)


@pytest.fixture
def ctx(tmp_path) -> HandlerContext:
    return HandlerContext(
        base_dir=tmp_path, group_map={"真英雄": "CP001"},
        edge_path=EDGE_PATH, output_dirname="output")


class TestClassifier:
    def test_order_price_with_quote(self):
        assert classify("//15.3", "#下单 9080 300017 网宿科技 限价 买入 200W") \
            == "cp001_9080_order"

    def test_double_hash_quote_marker(self):
        assert classify("//15.3", "##下单 9080 300017 网宿科技 限价 买入 200W") \
            == "cp001_9080_order"

    def test_90100_order_routed(self):
        assert classify("//58.144", "#下单 90100 688825 长鑫科技 限价 买入 160W") \
            == "cp001_90100_order"

    def test_90100_double_hash_routed(self):
        assert classify("//58.144", "##下单 90100 688825 长鑫科技 限价 买入 160W") \
            == "cp001_90100_order"

    def test_unknown_structure_unsupported(self):
        assert classify("//15.3", "#下单 90999 300017 网宿科技 限价 买入 200W") \
            == "unsupported"

    def test_price_without_quote_unsupported(self):
        assert classify("//15.3", "") == "unsupported"

    def test_margin_unsupported(self):
        assert classify("//到价追保", "#追保 9080 300017 网宿科技 限价 买入 200W") \
            == "unsupported"

    def test_plain_text_unsupported(self):
        assert classify("早上好", "") == "unsupported"


class TestInbox:
    def test_append_and_load_roundtrip(self, tmp_path):
        store = InboxStore(tmp_path / "inbox")
        msg = make_msg(seq=7)
        store.append(msg)
        loaded = store.load_all()
        assert len(loaded) == 1
        assert loaded[0].seq == 7
        assert loaded[0].content == "//15.3"
        assert loaded[0].quoted_msg_id == 999

    def test_safe_name(self):
        assert safe_name('a/b:c*d?"<>|') == "a_b_c_d"


class TestTimestampValidation:
    """create_time 合法化（毒消息防御）：越界/异常一律置 None.

    背景：fromtimestamp 遇离谱值（单位漂移/平台越界）抛异常，曾沿
    submit → inbox._path_for 逃逸；占位已发生 → monitor 重试被判
    duplicate → 消息静默吞。现在反序列化层直接拦截。
    """

    def test_valid_timestamp_kept(self):
        msg = IncomingMessage.from_dict(
            {"seq": 1, "group": "g", "create_time": TS_20260902})
        assert msg.create_time == TS_20260902

    def test_missing_timestamp_none(self):
        msg = IncomingMessage.from_dict({"seq": 1, "group": "g"})
        assert msg.create_time is None

    def test_millis_unit_rejected(self):
        """13 位毫秒级时间戳（单位漂移）→ None（而非 OSError）。"""
        msg = IncomingMessage.from_dict(
            {"seq": 1, "group": "g", "create_time": TS_20260902 * 1000})
        assert msg.create_time is None

    def test_negative_and_huge_rejected(self):
        for bad in (-1, 10**15):
            msg = IncomingMessage.from_dict(
                {"seq": 1, "group": "g", "create_time": bad})
            assert msg.create_time is None, bad

    def test_inbox_path_survives_bad_timestamp(self, tmp_path):
        """直接构造的毒消息（绕过 from_dict）也不得炸 inbox 路径。"""
        store = InboxStore(tmp_path / "inbox")
        poison = make_msg(create_time=10**15)
        store.append(poison)   # 不应抛 OverflowError/OSError
        assert len(store.load_all()) == 1


class RecordingHandler:
    """记录调用顺序的假处理器（测试分发器行为）。"""

    def __init__(self):
        self.calls: list[int] = []
        self.lock = threading.Lock()

    def handle(self, message: IncomingMessage, ctx: HandlerContext):
        with self.lock:
            self.calls.append(message.seq)


@pytest.fixture
def dispatcher(tmp_path, monkeypatch) -> Dispatcher:
    """使用假处理器的分发器（不渲染 PDF）。"""
    monkeypatch.setattr("service_a.handlers.HANDLERS",
                        {"cp001_9080_order": RecordingHandler().handle})
    monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                        {"cp001_9080_order": RecordingHandler().handle})
    ctx = HandlerContext(base_dir=tmp_path, group_map={"真英雄": "CP001"},
                         edge_path=EDGE_PATH)
    return Dispatcher(ctx, InboxStore(tmp_path / "inbox"), workers=2)


class TestDispatcher:
    def test_submit_dispatches_and_persists(self, dispatcher, monkeypatch):
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        accepted, reason = dispatcher.submit(make_msg())
        assert accepted and reason == "dispatched:cp001_9080_order"
        dispatcher.shutdown()
        assert recorder.calls == [1]
        assert len(dispatcher.inbox.load_all()) == 1

    def test_duplicate_seq_rejected(self, dispatcher):
        first, _ = dispatcher.submit(make_msg(seq=5))
        second, reason = dispatcher.submit(make_msg(seq=5))
        dispatcher.shutdown()
        assert first is True
        assert second is False and reason == "duplicate"
        # 落盘只一次
        assert len(dispatcher.inbox.load_all()) == 1

    def test_idempotency_key_is_group_msgid(self, dispatcher):
        """幂等键 = (group, msg_id)：同 msg_id 不同 seq 判重；同 seq 不同
        msg_id（monitor 重启 seq 归零后的真实新消息）不判重。"""
        a, _ = dispatcher.submit(make_msg(seq=1, msg_id=5001))
        b, reason = dispatcher.submit(make_msg(seq=99, msg_id=5001))  # 同 msg_id
        c, reason2 = dispatcher.submit(make_msg(seq=1, msg_id=5002))  # seq 撞但 msg_id 新
        dispatcher.shutdown()
        assert (a, b, c) == (True, False, True)
        assert reason == "duplicate"
        assert len(dispatcher.inbox.load_all()) == 2

    def test_concurrent_submit_single_dispatch(self, tmp_path, monkeypatch):
        """ThreadingHTTPServer 并发提交同一消息：原子 claim 下只派发一次。"""
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        ctx = HandlerContext(base_dir=tmp_path, group_map={"真英雄": "CP001"},
                             edge_path=EDGE_PATH)
        d = Dispatcher(ctx, InboxStore(tmp_path / "inbox"), workers=4)
        results: list[tuple[bool, str]] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(d.submit(make_msg(seq=7, msg_id=7001)))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        d.shutdown()
        assert sum(1 for ok, _ in results if ok) == 1
        assert len(d.inbox.load_all()) == 1

    def test_unsupported_logged_not_dispatched(self, dispatcher, monkeypatch):
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        accepted, reason = dispatcher.submit(
            make_msg(content="早上好", quoted_content=""))
        dispatcher.shutdown()
        assert accepted and reason == "logged:unsupported"
        assert recorder.calls == []

    def test_handler_exception_does_not_kill_pool(self, dispatcher, monkeypatch):
        def bad_handler(message, ctx):
            raise RuntimeError("boom")

        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": bad_handler})
        accepted, _ = dispatcher.submit(make_msg())
        dispatcher.shutdown()
        assert accepted  # 异常被吞，仅记日志

    def test_submit_rollback_on_inbox_failure(self, tmp_path, monkeypatch):
        """落盘异常必须回滚幂等占位：monitor 重试时重新受理，不静默吞."""
        from service_a.inbox import InboxStore as _Store
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        ctx = HandlerContext(base_dir=tmp_path, group_map={"真英雄": "CP001"},
                             edge_path=EDGE_PATH)
        d = Dispatcher(ctx, _Store(tmp_path / "inbox"), workers=1)
        real_append = d.inbox.append
        attempts = {"n": 0}

        def flaky_append(message):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("disk full")
            real_append(message)

        monkeypatch.setattr(d.inbox, "append", flaky_append)
        with pytest.raises(OSError):
            d.submit(make_msg())
        # 占位已回滚：重试（append 恢复）可重新受理
        accepted, reason = d.submit(make_msg())
        d.shutdown()
        assert accepted is True
        assert recorder.calls == [1]

    def test_dispatch_lock_is_per_counterparty(self, tmp_path):
        """锁粒度 = 对手方：跨群同对手方共用锁（防序号竞态）；未映射群按群名."""
        ctx = HandlerContext(base_dir=tmp_path,
                             group_map={"群A": "CP001", "群B": "CP001",
                                        "群C": "CP002"},
                             edge_path=EDGE_PATH)
        d = Dispatcher(ctx, InboxStore(tmp_path / "inbox"), workers=2)
        la = d._dispatch_lock(make_msg(group="群A"))
        lb = d._dispatch_lock(make_msg(group="群B"))
        lc = d._dispatch_lock(make_msg(group="群C"))
        unmapped = d._dispatch_lock(make_msg(group="陌生群"))
        d.shutdown()
        assert la is lb            # 同对手方（跨群）→ 同锁
        assert la is not lc        # 不同对手方 → 并行
        assert unmapped is not la  # 未映射群按群名独立

    def test_replay_respects_idempotency(self, tmp_path, monkeypatch):
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        ctx = HandlerContext(base_dir=tmp_path, group_map={"真英雄": "CP001"},
                             edge_path=EDGE_PATH)
        inbox = InboxStore(tmp_path / "inbox")
        # 预先落两条 inbox（msg_id 必须不同：幂等键 = (group, msg_id)）
        store = InboxStore(tmp_path / "inbox")
        store.append(make_msg(seq=1, msg_id=1001))
        store.append(make_msg(seq=2, msg_id=1002))
        d1 = Dispatcher(ctx, inbox, workers=1)
        d1.replay()
        d1.shutdown()
        assert sorted(recorder.calls) == [1, 2]
        # 重放第二次：从 inbox 恢复幂等集合后全部跳过
        recorder2 = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder2.handle})
        d2 = Dispatcher(ctx, inbox, workers=1).seen_from_inbox()
        n = d2.replay()
        d2.shutdown()
        assert n == 0


class TestHttpApi:
    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        cfg = ServiceConfig(inbox_dir=tmp_path / "inbox")
        cfg.resolve(tmp_path)
        dispatcher = build_dispatcher(cfg, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    make_http_handler(dispatcher, ""))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{httpd.server_address[1]}", dispatcher, recorder
        httpd.shutdown()
        dispatcher.shutdown()

    def test_health(self, server):
        url, _, _ = server
        with urllib.request.urlopen(f"{url}/health", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read())["status"] == "ok"

    def test_post_message_accepted(self, server):
        url, _, recorder = server
        req = urllib.request.Request(
            f"{url}/messages",
            data=json.dumps(make_msg().to_dict()).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        assert resp.status == 202 and body["accepted"] is True
        for _ in range(50):
            if recorder.calls:
                break
            threading.Event().wait(0.05)
        assert recorder.calls == [1]

    def test_post_bad_payload_400(self, server):
        url, _, _ = server
        req = urllib.request.Request(
            f"{url}/messages", data=b"not json",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "should raise"
        except urllib.error.HTTPError as exc:
            assert exc.code == 400

    def test_post_oversized_413(self, server):
        """超过 MAX_BODY_BYTES 的请求体直接 413，不读入内存."""
        url, _, _ = server
        from service_a.__main__ import MAX_BODY_BYTES
        req = urllib.request.Request(
            f"{url}/messages",
            data=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "should raise"
        except urllib.error.HTTPError as exc:
            assert exc.code == 413

    def test_auth_token_mismatch_401(self, tmp_path, monkeypatch):
        recorder = RecordingHandler()
        monkeypatch.setattr("service_a.dispatcher.HANDLERS",
                            {"cp001_9080_order": recorder.handle})
        cfg = ServiceConfig(inbox_dir=tmp_path / "inbox")
        cfg.resolve(tmp_path)
        dispatcher = build_dispatcher(cfg, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    make_http_handler(dispatcher, "secret"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            req = urllib.request.Request(
                f"{url}/messages",
                data=json.dumps(make_msg().to_dict()).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False
            except urllib.error.HTTPError as exc:
                assert exc.code == 401
        finally:
            httpd.shutdown()
            dispatcher.shutdown()


def _unused_stub():  # 保留占位：HTTP 层直接复用真实 Dispatcher（HANDLERS 已 patch）
    return None


@pytest.mark.skipif(not Path(EDGE_PATH).exists(), reason="Edge 不可用")
class TestEndToEnd:
    def test_http_to_pdf(self, tmp_path, monkeypatch):
        """POST /messages → 处理器 → output PDF 全链路。"""
        cfg = ServiceConfig(inbox_dir=tmp_path / "inbox",
                            group_map={"真英雄": "CP001"},
                            edge_path=EDGE_PATH)
        cfg.resolve(tmp_path)
        dispatcher = build_dispatcher(cfg, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    make_http_handler(dispatcher, ""))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            req = urllib.request.Request(
                f"{url}/messages",
                data=json.dumps(make_msg().to_dict()).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 202
            dispatcher.shutdown()
            pdfs = list((tmp_path / "output").glob("*.pdf"))
            assert len(pdfs) == 1
            assert pdfs[0].name == \
                "TradeConfirm(ITC9080)_CP001_20260902_001.pdf"
        finally:
            httpd.shutdown()

    def test_http_to_pdf_90100(self, tmp_path):
        """90100 敲出结构：POST → cp001_90100_order → PDF 全链路。"""
        cfg = ServiceConfig(inbox_dir=tmp_path / "inbox",
                            group_map={"真英雄": "CP001"},
                            edge_path=EDGE_PATH)
        cfg.resolve(tmp_path)
        dispatcher = build_dispatcher(cfg, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    make_http_handler(dispatcher, ""))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        msg = make_msg(content="//58.144",
                       quoted_content="#下单 90100 688825 长鑫科技 限价 买入 160W")
        try:
            req = urllib.request.Request(
                f"{url}/messages",
                data=json.dumps(msg.to_dict()).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 202
            dispatcher.shutdown()
            pdfs = list((tmp_path / "output").glob("*.pdf"))
            assert len(pdfs) == 1
            assert pdfs[0].name == \
                "TradeConfirm(ITC90100)_CP001_20260902_001.pdf"
            assert pdfs[0].read_bytes().startswith(b"%PDF")
        finally:
            httpd.shutdown()

    def test_http_to_pdf_cp003(self, tmp_path):
        """CP003 共用模板：POST → cp003_order → PDF 全链路。"""
        cfg = ServiceConfig(inbox_dir=tmp_path / "inbox",
                            group_map={"测试003": "CP003"},
                            edge_path=EDGE_PATH)
        cfg.resolve(tmp_path)
        dispatcher = build_dispatcher(cfg, tmp_path)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                    make_http_handler(dispatcher, ""))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        msg = make_msg(group="测试003", content="//11.73",
                       quoted_content="#下单 9080 002973 侨银股份 限价 买入 100W")
        try:
            req = urllib.request.Request(
                f"{url}/messages",
                data=json.dumps(msg.to_dict()).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                assert resp.status == 202
            dispatcher.shutdown()
            pdfs = list((tmp_path / "output").glob("*.pdf"))
            assert len(pdfs) == 1
            assert pdfs[0].name == \
                "TradeConfirm(ITC9080)_CP003_20260902_001.pdf"
            assert pdfs[0].read_bytes().startswith(b"%PDF")
        finally:
            httpd.shutdown()
