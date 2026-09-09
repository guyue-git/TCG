"""运维管理台（ops_ui）进程管控层.

管理两个受管进程（方案 §7.1-7.2）：
* 服务 A：python -m service_a（先启动，/health 就绪后再启动 monitor）
* monitor：python wechat_monitor.py（stdin 管道写入启动模式选择）

边界（用户裁决）：微信登录与 WeChatDataAnalysis 不归本层管理。

关键实测结论（2026-09-04）：
* CREATE_NEW_PROCESS_GROUP 启动的子进程，CTRL_C_EVENT 被系统禁用（handler
  收不到，进程不退）；CTRL_BREAK_EVENT 走 SIGBREAK——monitor 已注册
  SIGBREAK 处理器（极薄适配），可优雅停止并保存断点。
* monitor 每轮 poll 后都 _save_state()，即使强杀兜底，水位最多回退一个
  轮询周期（2s），重复推送由服务 A 幂等键 (group, msg_id) 拦截，业务无损。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)

CREATE_NEW_PROCESS_GROUP = 0x00000200
# frozen 态 UI 是 windowed 应用（无控制台），spawn 控制台子进程会弹黑窗，
# 需 CREATE_NO_WINDOW；源码态父进程有控制台，子进程直接继承，不能加。
# 代价：无控制台父进程的 os.kill(CTRL_BREAK) 会失败，stop() 用
# AttachConsole(子进程) + GenerateConsoleCtrlEvent 兜底（见 _send_ctrl_break）。
CREATE_NO_WINDOW = 0x08000000
CTRL_BREAK_EVENT = 1        # win32 GenerateConsoleCtrlEvent 的类型码

GRACEFUL_STOP_TIMEOUT_S = 10.0
HEALTH_WAIT_TIMEOUT_S = 30.0
HEALTH_PROBE_TIMEOUT_S = 3.0

PID_FILE_NAME = "ui_pids.json"

SERVICE_NAME = "service"
MONITOR_NAME = "monitor"


class ProcessStartError(RuntimeError):
    """启动失败（含健康检查超时）。"""


def resolve_python_exe(base_dir: Path) -> str:
    """解析子进程使用的解释器：优先项目 .venv，回退 UI 自身解释器.

    子进程（服务 A / monitor）依赖装在项目 .venv（jinja2/pymupdf 等），
    而 python_exe 此前直接取 UI 的 sys.executable——若 UI 不是用 .venv
    启动（如系统 Python 拉起），服务 A 会在 import 链上 ImportError
    秒退 code=1（2026-09-04 实测）。固定回退顺序：
    .venv/Scripts/python.exe 存在则用之，否则 sys.executable。
    仅源码态使用；frozen 态走 resolve_child_argv 的 exe 直启分支。
    """
    venv_python = base_dir / ".venv" / "Scripts" / "python.exe"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def resolve_child_argv(base_dir: Path, name: str) -> list[str]:
    """构造子进程默认启动命令（源码态 / PyInstaller frozen 态双支持）.

    frozen（方案 B：三入口 onedir，exe 与 ini 同目录）：
        service_a.exe -c <base_dir>/service_a_config.ini
        monitor.exe   -c <base_dir>/config.ini
    源码态：经 .venv 解释器以模块/脚本方式启动，ini 一律传绝对路径
    （cwd 可能不等于 base_dir，相对路径会读不到配置——审计 #8）。
    """
    service_ini = str((base_dir / "service_a_config.ini").resolve())
    monitor_ini = str((base_dir / "config.ini").resolve())
    if getattr(sys, "frozen", False):
        if name == SERVICE_NAME:
            return [str(base_dir / "service_a.exe"), "-c", service_ini]
        return [str(base_dir / "monitor.exe"), "-c", monitor_ini]
    python_exe = resolve_python_exe(base_dir)
    if name == SERVICE_NAME:
        return [python_exe, "-m", "service_a", "-c", service_ini]
    return [python_exe, "wechat_monitor.py", "-c", monitor_ini]


@dataclass(frozen=True)
class ProcessSpec:
    """单个受管进程的启动描述。"""

    name: str                 # SERVICE_NAME / MONITOR_NAME
    argv: list[str]
    cwd: Path


def is_pid_alive(pid: int) -> bool:
    """Windows 下探测 pid 存活（sig=0 语义：仅检查，不发信号）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def probe_health_once(base_url: str,
                      timeout_s: float = HEALTH_PROBE_TIMEOUT_S) -> bool:
    """单次探测服务 A /health（monitor 推送目标即健康端点）。"""
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout_s):
            return True
    except (urllib.error.URLError, OSError):
        return False


class ProcessHandle:
    """subprocess.Popen 封装：stdin 管道、stdout reader 线程、状态查询。

    reader 线程把日志行以 (source, line) 元组投递到 log_sink 队列，
    由 UI 主线程 root.after 消费（Tkinter 主线程铁律）。
    """

    def __init__(self, spec: ProcessSpec,
                 log_sink: "queue.Queue[tuple[str, str]]") -> None:
        self._spec = spec
        self._log_sink = log_sink
        self._proc: subprocess.Popen[str] | None = None
        self._started_at: float | None = None

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def started_at(self) -> float | None:
        return self._started_at

    def is_running(self) -> bool:
        """进程存活判定（内部顺带回收 returncode）。"""
        if self._proc is None:
            return False
        self._proc.poll()
        return self._proc.returncode is None

    def returncode(self) -> int | None:
        if self._proc is None:
            return None
        self._proc.poll()
        return self._proc.returncode

    def _child_env(self) -> dict:
        """子进程环境：强制 Python IO 用 UTF-8.

        子进程 stdout 是管道时，Python 默认按 ANSI 代码页（本机 GBK）写入，
        而父进程按 utf-8 解码 -> 运行日志中文全部乱码，且告警关键字
        （中文）永远匹配不上（2026-09-04 实测）。PYTHONIOENCODING 对齐
        读写两端；PYTHONUTF8=1 兜底覆盖文件系统/stdio 其他路径。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def start(self, stdin_line: str = "") -> None:
        """启动子进程；stdin_line 非空时经管道写入后立即关闭写端。

        monitor 启动模式（"1\\n"=断点续传 / "0\\n"=全新）在运行期才确定，
        故作为参数传入而非固化在 spec。
        """
        if self.is_running():
            raise ProcessStartError(f"{self._spec.name} 已在运行")
        LOG.info("启动 %s: %s", self._spec.name, self._spec.argv)
        self._proc = subprocess.Popen(
            self._spec.argv,
            cwd=str(self._spec.cwd),
            env=self._child_env(),
            stdin=subprocess.PIPE if stdin_line else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NEW_PROCESS_GROUP
            | (CREATE_NO_WINDOW if getattr(sys, "frozen", False) else 0),
        )
        self._started_at = time.time()
        self._write_pid_file()
        self._start_reader(self._proc.stdout)
        if stdin_line and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(stdin_line)
                self._proc.stdin.flush()
            finally:
                self._proc.stdin.close()

    def _start_reader(self, stream) -> None:
        def _read() -> None:
            assert stream is not None
            for line in stream:
                self._log_sink.put((self._spec.name, line.rstrip("\r\n")))

        thread = threading.Thread(target=_read, daemon=True,
                                  name=f"reader-{self._spec.name}")
        thread.start()

    def _send_ctrl_break(self, pid: int) -> bool:
        """向子进程发送 CTRL_BREAK；无控制台父进程走 AttachConsole 兜底.

        常规路径：os.kill 直接发（父有控制台，子继承同一控制台）。
        frozen windowed UI（父无控制台，子经 CREATE_NO_WINDOW 独立控制台）：
        os.kill 会失败——临时 AttachConsole(pid) 挂到子的控制台再
        GenerateConsoleCtrlEvent，完成后 FreeConsole。
        """
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
            return True
        except OSError:
            pass
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            if not k32.AttachConsole(pid):
                LOG.warning("AttachConsole(pid=%d) 失败（err=%d）",
                            pid, k32.GetLastError())
                return False
            try:
                # 屏蔽自身收到事件的影响（UI 无控制台，稳妥起见仍加）
                k32.SetConsoleCtrlHandler(None, True)
                ok = bool(k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid))
                if not ok:
                    LOG.warning("GenerateConsoleCtrlEvent 失败（err=%d）",
                                k32.GetLastError())
                return ok
            finally:
                k32.FreeConsole()
        except Exception as exc:             # noqa: BLE001 兜底路径不抛出
            LOG.warning("AttachConsole 发送 CTRL_BREAK 异常: %s", exc)
            return False

    def stop(self, graceful_timeout_s: float = GRACEFUL_STOP_TIMEOUT_S) -> int | None:
        """优雅停止：CTRL_BREAK（SIGBREAK）→ 等待 → terminate 强杀兜底。

        返回最终 returncode（进程未启动过返回 None）。
        """
        if self._proc is None:
            return None
        if not self.is_running():
            self._clear_pid_file()
            return self._proc.returncode
        pid = self._proc.pid
        LOG.info("停止 %s (pid=%d): 发送 CTRL_BREAK_EVENT", self._spec.name, pid)
        if not self._send_ctrl_break(pid):
            LOG.warning("CTRL_BREAK 发送失败，将依赖强杀兜底")
        try:
            self._proc.wait(timeout=graceful_timeout_s)
        except subprocess.TimeoutExpired:
            LOG.warning("%s 优雅停止超时 %.0fs，强杀", self._spec.name,
                        graceful_timeout_s)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        self._clear_pid_file()
        LOG.info("%s 已停止, returncode=%s", self._spec.name, self._proc.returncode)
        return self._proc.returncode

    def _pid_file_path(self) -> Path:
        return self._spec.cwd / "runtime" / PID_FILE_NAME

    def _write_pid_file(self) -> None:
        path = self._pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        data[self._spec.name] = {"pid": self._proc.pid if self._proc else None,
                                 "started_at": self._started_at}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    def _clear_pid_file(self) -> None:
        path = self._pid_file_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if self._spec.name in data:
            del data[self._spec.name]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")


@dataclass(frozen=True)
class StatusView:
    """UI 状态卡数据。"""

    name: str
    is_running: bool
    returncode: int | None
    started_at: float | None
    adopted: bool = False   # 是否为接管的外部实例


class ProcessManager:
    """两进程的启停次序、健康探测与 pid 文件接管（方案 §6.1/6.2/7.2）。"""

    def __init__(self, base_dir: Path, log_sink: "queue.Queue[tuple[str, str]]",
                 *, service_port: int = 16320,
                 python_exe: str | None = None,
                 service_argv: list[str] | None = None,
                 monitor_argv: list[str] | None = None) -> None:
        self._base_dir = base_dir
        self._service_base_url = f"http://127.0.0.1:{service_port}"
        service_argv = service_argv or resolve_child_argv(base_dir,
                                                          SERVICE_NAME)
        monitor_argv = monitor_argv or resolve_child_argv(base_dir,
                                                          MONITOR_NAME)
        self._handles: dict[str, ProcessHandle] = {
            SERVICE_NAME: ProcessHandle(
                ProcessSpec(name=SERVICE_NAME, argv=service_argv,
                            cwd=base_dir),
                log_sink,
            ),
            MONITOR_NAME: ProcessHandle(
                ProcessSpec(name=MONITOR_NAME, argv=monitor_argv,
                            cwd=base_dir),
                log_sink,
            ),
        }
        self._adopted: dict[str, dict] = {}

    def handle(self, name: str) -> ProcessHandle:
        return self._handles[name]

    def is_running(self, name: str) -> bool:
        if name in self._adopted:
            return is_pid_alive(self._adopted[name]["pid"])
        return self._handles[name].is_running()

    def started_at(self, name: str) -> float | None:
        if name in self._adopted:
            return self._adopted[name].get("started_at")
        return self._handles[name].started_at

    def returncode(self, name: str) -> int | None:
        if name in self._adopted:
            return None
        return self._handles[name].returncode()

    def is_adopted(self, name: str) -> bool:
        return name in self._adopted

    def start_all(self, monitor_mode: str = "resume") -> None:
        """启动次序：服务 A → /health（≤30s）→ monitor（方案 §6.1）。

        monitor_mode: "resume"=断点续传（默认）/ "fresh"=全新启动。
        任一失败抛 ProcessStartError；服务 A health 超时时进程保留供排查。
        """
        if self.is_running(SERVICE_NAME):
            raise ProcessStartError("服务 A 已在运行，请勿重复启动")
        if self.is_running(MONITOR_NAME):
            raise ProcessStartError("消息监控已在运行，请勿重复启动")
        stdin_line = "1\n" if monitor_mode == "resume" else "0\n"
        self._handles[SERVICE_NAME].start()
        try:
            self._wait_health(HEALTH_WAIT_TIMEOUT_S)
        except ProcessStartError:
            raise
        self._handles[MONITOR_NAME].start(stdin_line=stdin_line)
        LOG.info("两进程启动完成（monitor 模式=%s）", monitor_mode)

    def _wait_health(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if probe_health_once(self._service_base_url):
                LOG.info("服务 A /health 就绪")
                return
            if not self._handles[SERVICE_NAME].is_running():
                rc = self._handles[SERVICE_NAME].returncode()
                raise ProcessStartError(f"服务 A 启动即退出（returncode={rc}），"
                                        f"请查看运行日志")
            time.sleep(0.5)
        raise ProcessStartError(
            f"服务 A /health 在 {timeout_s:.0f}s 内未就绪（进程保留，"
            f"可点结束清理或查看运行日志排查）")

    def stop_all(self) -> None:
        """停止次序：先 monitor（优雅停保断点）后服务 A（方案 §6.2）。"""
        if self._adopted:
            self.stop_adopted()
            return
        self._handles[MONITOR_NAME].stop()
        self._handles[SERVICE_NAME].stop()

    def adopt_from_pid_file(self) -> dict[str, dict]:
        """UI 重启后接管已运行实例（方案 §7.2）：pid 存活才接管。"""
        self._adopted.clear()
        path = self._base_dir / "runtime" / PID_FILE_NAME
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        for name, info in data.items():
            pid = info.get("pid")
            if name in self._handles and isinstance(pid, int) and is_pid_alive(pid):
                self._adopted[name] = info
                LOG.info("接管外部实例 %s (pid=%d)", name, pid)
        return dict(self._adopted)

    def stop_adopted(self) -> None:
        """按 pid 优雅停止接管实例（无管道：CTRL_BREAK → 等待 → 强杀）。"""
        for name in list(self._adopted):
            pid = self._adopted[name]["pid"]
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
            except OSError:
                pass
            deadline = time.monotonic() + GRACEFUL_STOP_TIMEOUT_S
            while is_pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.3)
            if is_pid_alive(pid):
                try:
                    os.kill(pid, signal.SIGTERM)  # Windows = TerminateProcess
                except OSError:
                    pass
            LOG.info("接管实例 %s (pid=%d) 已停止", name, pid)
        self._adopted.clear()
        path = self._base_dir / "runtime" / PID_FILE_NAME
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
