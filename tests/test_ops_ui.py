"""ops_ui 单元测试：进程管控 / PDF 扫描 / 告警聚合（不弹真实窗口）.

关键场景：
* ProcessHandle：优雅停止（SIGBREAK handler 子进程 exit 42）、强杀兜底、
  stdin 启动模式注入
* ProcessManager：启动次序（service -> /health -> monitor）、pid 文件、
  接管、重复启动拒绝
* PdfScanner：稳定判定（连续 2 次大小不变才入表）、消失出表、排序
* AlertCenter：日志关键字告警、60s 去重、确认置灰、上限截断
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ops_ui.alerts import AlertCenter
from ops_ui.pdf_scanner import PdfScanner, parse_pdf_name
import ops_ui.process_manager as pm_mod
from ops_ui.process_manager import (
    MONITOR_NAME,
    SERVICE_NAME,
    ProcessHandle,
    ProcessManager,
    ProcessSpec,
    ProcessStartError,
    is_pid_alive,
)

PYTHON = sys.executable

# ---------- 测试用子进程脚本 ----------

CHILD_GRACEFUL = """
import signal, sys, time
def handle(signum, frame):
    print("GOT-SIGBREAK", flush=True)
    sys.exit(42)
signal.signal(signal.SIGBREAK, handle)
for line in sys.stdin:               # 读到启动模式行（或 EOF 前阻塞）
    print("STDIN=" + line.strip(), flush=True)
    break
print("READY", flush=True)
while True:
    time.sleep(0.2)
"""

CHILD_PLAIN = """
import time
print("READY", flush=True)
while True:
    time.sleep(0.2)
"""

CHILD_HEALTH_SERVER = """
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port = int(sys.argv[1])
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""

CHILD_UTF8_ECHO = """
import sys, time
# 模拟真实入口的代码级强制 UTF-8（frozen exe 不理会 PYTHONIOENCODING）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print("READY-中文确认书生成", flush=True)
time.sleep(10)
"""


def _write_child(tmp_path: Path, name: str, code: str) -> list[str]:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return [PYTHON, str(path)]


def _wait_ready(sink: queue.Queue, timeout: float = 10.0,
                handle: ProcessHandle | None = None) -> list[str]:
    """等子进程输出 READY；返回已消费的日志行（供后续断言）。"""
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            _, line = sink.get(timeout=0.2)
        except queue.Empty:
            continue
        seen.append(line)
        if "READY" in line:
            return seen
    rc = handle.returncode() if handle is not None else "?"
    raise AssertionError(f"子进程未就绪 rc={rc}, 已见日志: {seen}")


# ---------- PDF 解析 ----------


class TestPdfParse:
    def test_parse_standard_name(self):
        parsed = parse_pdf_name("TradeConfirm(ITC90100)_CP001_20260903_001.pdf")
        assert parsed == {
            "template": "ITC90100", "counterparty": "CP001",
            "trade_date": "20260903", "serial": "001"}

    def test_parse_cp003(self):
        parsed = parse_pdf_name("TradeConfirm(ITC9080)_CP003_20260904_002.pdf")
        assert parsed is not None
        assert parsed["counterparty"] == "CP003"
        assert parsed["serial"] == "002"

    @pytest.mark.parametrize("name", [
        "manual_notes.pdf", "TradeConfirm.pdf",
        "TradeConfirm(X)_CP1_2026_1.pdf",
        "TradeConfirm(ITC90100)_CP001_20260903_001.txt",
    ])
    def test_invalid_names_return_none(self, name):
        assert parse_pdf_name(name) is None


# ---------- PDF 扫描 ----------


class TestPdfScanner:
    def _make(self, tmp_path: Path, name: str, size: int = 100) -> Path:
        target = tmp_path / name
        target.write_bytes(b"x" * size)
        return target

    def test_stable_after_two_rounds(self, tmp_path):
        scanner = PdfScanner(tmp_path)
        self._make(tmp_path, "TradeConfirm(ITC90100)_CP001_20260903_001.pdf")
        assert scanner.scan() == []            # 第 1 轮：未稳定
        entries = scanner.scan()               # 第 2 轮：稳定入表
        assert len(entries) == 1
        entry = entries[0]
        assert entry.counterparty == "CP001"
        assert entry.template == "ITC90100"
        assert entry.serial == "001"
        assert entry.size == 100

    def test_growing_file_not_listed(self, tmp_path):
        scanner = PdfScanner(tmp_path)
        target = self._make(tmp_path, "TradeConfirm(ITC90100)_CP001_20260903_001.pdf")
        scanner.scan()
        target.write_bytes(b"x" * 200)         # 大小变化 -> 重新计数
        assert scanner.scan() == []
        assert len(scanner.scan()) == 1        # 再连续两轮一致才入表

    def test_disappeared_file_removed(self, tmp_path):
        scanner = PdfScanner(tmp_path)
        target = self._make(tmp_path, "TradeConfirm(ITC90100)_CP001_20260903_001.pdf")
        scanner.scan()
        scanner.scan()
        assert len(scanner.scan()) == 1
        target.unlink()
        assert scanner.scan() == []

    def test_sorted_mtime_desc(self, tmp_path):
        scanner = PdfScanner(tmp_path)
        old = self._make(tmp_path, "TradeConfirm(ITC90100)_CP001_20260903_001.pdf")
        new = self._make(tmp_path, "TradeConfirm(ITC9080)_CP003_20260904_002.pdf")
        past = time.time() - 3600
        os.utime(old, (past, past))
        scanner.scan()
        entries = scanner.scan()
        assert [e.file_name for e in entries] == [new.name, old.name]

    def test_nonstandard_name_fills_dashes(self, tmp_path):
        scanner = PdfScanner(tmp_path)
        # glob 只匹配 TradeConfirm(*)_*_*.pdf；构造一个合 glob 但不合正则的
        weird = tmp_path / "TradeConfirm(x)_y_z.pdf"
        weird.write_bytes(b"x" * 50)
        scanner.scan()
        entries = scanner.scan()
        assert len(entries) == 1
        assert entries[0].counterparty == "-"


# ---------- 告警 ----------


class TestAlertCenter:
    def test_edge_failure_pattern(self):
        center = AlertCenter()
        alert = center.feed_log_line(
            "service", "2026-09-04 10:00:00 ERROR Edge 未产出 PDF（exit=1）")
        assert alert is not None
        assert alert.level == "error"
        assert alert.title == "PDF 生成失败"

    def test_retry_warning_pattern(self):
        center = AlertCenter()
        alert = center.feed_log_line(
            "service", "2026-09-04 10:00:00 WARNING Edge 渲染第 1 次尝试失败: x")
        assert alert is not None
        assert alert.level == "warn"

    def test_plain_error_matches_fallback(self):
        center = AlertCenter()
        alert = center.feed_log_line("monitor", "2026-09-04 ERROR something bad")
        assert alert is not None
        assert alert.title == "运行日志 ERROR"

    def test_duplicate_suppressed_within_window(self):
        center = AlertCenter()
        first = center.feed_log_line("service", "ERROR Edge 未产出 PDF a")
        second = center.feed_log_line("service", "ERROR Edge 未产出 PDF b")
        assert first is not None and second is None

    def test_acknowledge(self):
        center = AlertCenter()
        alert = center.add("error", "进程异常退出", "monitor rc=1")
        assert center.has_unacknowledged()
        center.acknowledge(alert)
        assert not center.has_unacknowledged()
        assert center.recent()[0].acknowledged

    def test_max_alerts_truncation(self):
        center = AlertCenter(max_alerts=5)
        for i in range(10):
            center.add("warn", f"告警{i}", f"detail {i}")
        recent = center.recent(limit=50)
        assert len(recent) == 5
        assert recent[0].title == "告警9"   # 最新在前


# ---------- ProcessHandle ----------


class TestProcessHandle:
    def _handle(self, tmp_path: Path, argv: list[str]) -> tuple[
            ProcessHandle, queue.Queue]:
        sink: queue.Queue = queue.Queue()
        return ProcessHandle(ProcessSpec("test", argv, tmp_path), sink), sink

    def test_start_is_running_stop_graceful(self, tmp_path):
        argv = _write_child(tmp_path, "graceful.py", CHILD_GRACEFUL)
        handle, sink = self._handle(tmp_path, argv)
        handle.start(stdin_line="1\n")
        seen = _wait_ready(sink, handle=handle)   # STDIN=1 行先于 READY 输出
        assert handle.is_running()
        assert handle.pid is not None
        # stdin 启动模式已写入并被子进程回显（READY 前的那一行）
        assert any("STDIN=1" in line for line in seen)
        returncode = handle.stop()
        assert returncode == 42          # SIGBREAK handler 优雅退出
        assert not handle.is_running()

    def test_stop_plain_child_force_kills(self, tmp_path):
        argv = _write_child(tmp_path, "plain.py", CHILD_PLAIN)
        handle, sink = self._handle(tmp_path, argv)
        handle.start()
        _wait_ready(sink)
        returncode = handle.stop(graceful_timeout_s=1)
        assert returncode is not None and returncode != 0
        assert not handle.is_running()

    def test_stop_when_not_started_returns_none(self, tmp_path):
        argv = _write_child(tmp_path, "plain.py", CHILD_PLAIN)
        handle, _ = self._handle(tmp_path, argv)
        assert handle.stop() is None

    def test_double_start_rejected(self, tmp_path):
        argv = _write_child(tmp_path, "plain.py", CHILD_PLAIN)
        handle, sink = self._handle(tmp_path, argv)
        handle.start()
        try:
            _wait_ready(sink)
            with pytest.raises(ProcessStartError, match="已在运行"):
                handle.start()
        finally:
            handle.stop(graceful_timeout_s=1)

    def test_pid_file_written_and_cleared(self, tmp_path):
        argv = _write_child(tmp_path, "graceful.py", CHILD_GRACEFUL)
        handle, sink = self._handle(tmp_path, argv)
        handle.start(stdin_line="0\n")
        _wait_ready(sink)
        pid_file = tmp_path / "runtime" / "ui_pids.json"
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        assert data["test"]["pid"] == handle.pid
        handle.stop()
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        assert "test" not in data


# ---------- ProcessManager ----------


def _wait_source_ready(sink: queue.Queue, source: str,
                       timeout: float = 10.0) -> None:
    """等指定来源子进程输出 READY（避免停止信号早于子进程注册 handler）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            src, line = sink.get(timeout=0.2)
        except queue.Empty:
            continue
        if src == source and "READY" in line:
            return
    raise AssertionError(f"{source} 未在 {timeout}s 内就绪")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestProcessManager:
    def _make_manager(self, tmp_path: Path,
                      ) -> tuple[ProcessManager, queue.Queue, int]:
        port = _free_port()
        service_argv = _write_child(tmp_path, "fake_service.py",
                                    CHILD_HEALTH_SERVER) + [str(port)]
        monitor_argv = _write_child(tmp_path, "fake_monitor.py",
                                    CHILD_GRACEFUL)
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=port,
                                 service_argv=service_argv,
                                 monitor_argv=monitor_argv)
        return manager, sink, port

    # ---- 解释器解析（2026-09-04：UI 被非 venv 解释器拉起时子进程缺依赖
    #      ImportError 秒退 code=1，故子进程解释器优先取项目 .venv） ----

    def test_resolve_python_prefers_project_venv(self, tmp_path):
        venv_dir = tmp_path / ".venv" / "Scripts"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python.exe").write_text("", encoding="utf-8")
        resolved = pm_mod.resolve_python_exe(tmp_path)
        assert resolved == str(tmp_path / ".venv" / "Scripts" / "python.exe")

    def test_resolve_python_falls_back_to_ui_interpreter(self, tmp_path):
        # 无 .venv 的目录：回退 UI 自身解释器（sys.executable）
        assert pm_mod.resolve_python_exe(tmp_path) == sys.executable

    def test_manager_uses_venv_python_for_children(self, tmp_path):
        """默认 argv 的解释器：有项目 .venv 用之（__init__ 只建 spec 不拉进程）。"""
        venv_dir = tmp_path / ".venv" / "Scripts"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python.exe").write_text("", encoding="utf-8")
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=_free_port())
        venv_python = str(tmp_path / ".venv" / "Scripts" / "python.exe")
        assert manager.handle(SERVICE_NAME)._spec.argv[0] == venv_python
        assert manager.handle(MONITOR_NAME)._spec.argv[0] == venv_python

    def test_manager_default_python_falls_back(self, tmp_path):
        """无 .venv 时默认 argv 回退 UI 自身解释器。"""
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=_free_port())
        assert manager.handle(SERVICE_NAME)._spec.argv[0] == sys.executable
        assert manager.handle(MONITOR_NAME)._spec.argv[0] == sys.executable

    def test_source_argv_uses_absolute_ini(self, tmp_path):
        """源码态默认 argv：ini 必须为绝对路径（cwd 不定时不丢配置）。"""
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=_free_port())
        svc_argv = manager.handle(SERVICE_NAME)._spec.argv
        mon_argv = manager.handle(MONITOR_NAME)._spec.argv
        assert Path(svc_argv[svc_argv.index("-c") + 1]).is_absolute()
        assert Path(mon_argv[mon_argv.index("-c") + 1]).is_absolute()
        assert svc_argv[svc_argv.index("-c") + 1].endswith(
            "service_a_config.ini")
        assert mon_argv[mon_argv.index("-c") + 1].endswith("config.ini")

    def test_frozen_argv_launches_sibling_exes(self, tmp_path, monkeypatch):
        """frozen（方案 B 三入口）态：直接启动同目录 monitor/service_a exe。"""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=_free_port())
        svc_argv = manager.handle(SERVICE_NAME)._spec.argv
        mon_argv = manager.handle(MONITOR_NAME)._spec.argv
        assert Path(svc_argv[0]) == tmp_path / "service_a.exe"
        assert Path(svc_argv[2]) == tmp_path / "service_a_config.ini"
        assert Path(mon_argv[0]) == tmp_path / "monitor.exe"
        assert Path(mon_argv[2]) == tmp_path / "config.ini"

    def test_child_stdout_chinese_not_mojibake(self, tmp_path):
        """子进程 stdout 中文必须按 UTF-8 到达 UI（2026-09-04 乱码修复）。

        管道默认编码为 ANSI 代码页（GBK），父进程按 utf-8 解码 ->
        运行日志乱码且中文告警关键字永远匹配不上；子进程环境注入
        PYTHONIOENCODING/PYTHONUTF8 后两端对齐。
        """
        argv = _write_child(tmp_path, "utf8_child.py", CHILD_UTF8_ECHO)
        sink: queue.Queue = queue.Queue()
        handle = ProcessHandle(
            ProcessSpec(name=SERVICE_NAME, argv=argv, cwd=tmp_path), sink)
        try:
            handle.start()
            deadline = time.monotonic() + 10
            got = ""
            while time.monotonic() < deadline:
                try:
                    _, line = sink.get(timeout=0.2)
                except queue.Empty:
                    continue
                if "中文确认书生成" in line:
                    got = line
                    break
            assert got, "未收到（或乱码导致匹配失败）: 子进程中文输出丢失"
        finally:
            handle.stop()

    def test_child_env_inherits_parent_paths(self, tmp_path):
        """注入 UTF-8 变量时不得丢失父环境（PATH 等），否则子进程崩。"""
        env = ProcessHandle(
            ProcessSpec(name=SERVICE_NAME, argv=[sys.executable],
                        cwd=tmp_path), queue.Queue())._child_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
        assert env.get("PATH") or env.get("Path")  # 系统环境仍在

    def test_start_all_order_and_stop_all(self, tmp_path):
        manager, sink, _ = self._make_manager(tmp_path)
        manager.start_all(monitor_mode="resume")   # 顺序：health 就绪后才起 monitor
        assert manager.is_running(SERVICE_NAME)
        assert manager.is_running(MONITOR_NAME)
        pid_file = tmp_path / "runtime" / "ui_pids.json"
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        assert set(data) == {SERVICE_NAME, MONITOR_NAME}
        # 等子进程完成自身初始化（SIGBREAK handler 已注册）再停止，
        # 否则 CTRL_BREAK 早到会触发默认终止（真实 UI 场景无此竞态：
        # monitor main() 数百 ms 内即完成注册，且强杀兜底业务无损）
        _wait_source_ready(sink, MONITOR_NAME)
        manager.stop_all()                          # monitor 先优雅停，再 service
        assert not manager.is_running(MONITOR_NAME)
        assert manager.returncode(MONITOR_NAME) == 42
        assert not manager.is_running(SERVICE_NAME)
        data = json.loads(pid_file.read_text(encoding="utf-8"))
        assert data == {}

    def test_double_start_rejected(self, tmp_path):
        manager, sink, _ = self._make_manager(tmp_path)
        manager.start_all()
        try:
            _wait_source_ready(sink, MONITOR_NAME)
            with pytest.raises(ProcessStartError):
                manager.start_all()
        finally:
            manager.stop_all()

    def test_start_all_health_timeout_keeps_service(self, tmp_path):
        # 服务 A 起了但不提供 /health（CHILD_PLAIN 是死循环无 http）
        plain_argv = _write_child(tmp_path, "no_health.py", CHILD_PLAIN)
        monitor_argv = _write_child(tmp_path, "fake_monitor.py", CHILD_GRACEFUL)
        sink: queue.Queue = queue.Queue()
        manager = ProcessManager(tmp_path, sink, service_port=_free_port(),
                                 service_argv=plain_argv,
                                 monitor_argv=monitor_argv)
        import ops_ui.process_manager as pm
        original = pm.HEALTH_WAIT_TIMEOUT_S
        pm.HEALTH_WAIT_TIMEOUT_S = 2   # 缩短等待（模块常量在方法内引用）
        try:
            with pytest.raises(ProcessStartError, match="未就绪"):
                manager.start_all()
            assert manager.is_running(SERVICE_NAME)      # 进程保留供排查
            manager.stop_all()
        finally:
            pm.HEALTH_WAIT_TIMEOUT_S = original

    def test_adopt_from_pid_file(self, tmp_path):
        manager, _, _ = self._make_manager(tmp_path)
        # 手工放置一条存活 pid（用当前测试进程自身）
        pid_file = tmp_path / "runtime" / "ui_pids.json"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(json.dumps(
            {SERVICE_NAME: {"pid": os.getpid(), "started_at": time.time()}}),
            encoding="utf-8")
        adopted = manager.adopt_from_pid_file()
        assert SERVICE_NAME in adopted
        assert manager.is_adopted(SERVICE_NAME)
        assert manager.is_running(SERVICE_NAME)
        # 死 pid 应被忽略
        pid_file.write_text(json.dumps(
            {SERVICE_NAME: {"pid": 999999, "started_at": 0}}),
            encoding="utf-8")
        assert manager.adopt_from_pid_file() == {}

    def test_import_main_module_smoke(self):
        import ops_ui.main as main_module   # noqa: F401 布局模块可导入
        assert callable(main_module.main)
