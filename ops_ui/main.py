"""运维管理台主界面（方案 §四-§六）.

单窗口五块：状态卡横条 / 操作条 / 日志区（PDF 产出 + 运行日志 Tab）/
告警区 / 底栏。托盘：pystray 独立 daemon 线程，回调经事件队列投递回
Tk 主线程；× 关闭 = 退出程序（弹确认可选停止主流程），不最小化托盘；
托盘仅为窗口打开期间的快捷入口（启动/结束/退出）。

线程模型（Tkinter 主线程铁律）：
* 子进程日志：reader 线程 -> log_sink 队列 -> root.after(100) 消费
* 启动/停止/健康探测：工作线程 -> _ui_events 回调队列 -> root.after 消费
* 自检：python -m ops_ui.main --selftest 构建窗口 800ms 后自动退出
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from ops_ui.alerts import Alert, AlertCenter
from ops_ui.pdf_scanner import PdfEntry, PdfScanner
from ops_ui.process_manager import (
    MONITOR_NAME,
    SERVICE_NAME,
    ProcessManager,
    ProcessStartError,
    probe_health_once,
)

LOG = logging.getLogger(__name__)

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:   # 托盘缺失时优雅降级为普通窗口
    HAS_TRAY = False

FONT_STACK = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)

COLOR_OK = "#1d7a3e"
COLOR_STOP = "#6b6b6b"
COLOR_BAD = "#b02020"
COLOR_WARN = "#9a6a00"
COLOR_MUTED = "#8a8a8a"

REFRESH_AUTO_MS = 5000
HEALTH_FAIL_ALERT_THRESHOLD = 3
MAX_LOG_LINES = 10000


def format_size(size_bytes: int) -> str:
    if size_bytes < 10 * 1024:
        return f"{size_bytes} B"
    return f"{size_bytes / 1024:.1f} KB"


def format_mtime(mtime: float) -> str:
    local = dt.datetime.fromtimestamp(mtime)
    now = dt.datetime.now()
    if local.date() == now.date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%m-%d %H:%M")


def format_uptime(started_at: float | None) -> str:
    if started_at is None:
        return "—"
    seconds = int(time.time() - started_at)
    hours, rest = divmod(max(seconds, 0), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


class OpsApp:
    """主窗口应用（逻辑与视图薄耦合：manager/scanner/alerts 可独立单测）。"""

    def __init__(self, root: tk.Tk, base_dir: Path) -> None:
        self._root = root
        self._base_dir = base_dir
        self._log_sink: queue.Queue[tuple[str, str]] = queue.Queue()
        self._ui_events: queue.Queue[Callable[[], None]] = queue.Queue()
        self._manager = ProcessManager(base_dir, self._log_sink)
        self._scanner = PdfScanner(base_dir / "output")
        self._alerts = AlertCenter()
        self._busy = False
        self._refresh_mode = tk.StringVar(value="auto")
        self._monitor_mode = tk.StringVar(value="resume")
        self._health_fail_count = 0
        self._last_op = "—"
        self._current_entries: list[PdfEntry] = []
        # 崩溃检测基线与每进程日志尾部（告警摘要用）
        self._was_running: dict[str, bool] = {SERVICE_NAME: False,
                                              MONITOR_NAME: False}
        self._recent_logs: dict[str, deque[str]] = {
            SERVICE_NAME: deque(maxlen=50), MONITOR_NAME: deque(maxlen=50)}
        self._adopted = self._manager.adopt_from_pid_file()
        if self._adopted:
            self._last_op = "接管已运行实例"
        self._build_ui()
        self._setup_tray()
        self._root.after(100, self._drain_events)
        self._root.after(100, self._drain_logs)
        self._root.after(REFRESH_AUTO_MS, self._tick_refresh)
        self._root.after(1000, self._tick_clock)
        self._start_health_probe()
        self._update_status_cards()
        self._update_buttons()
        self._update_pdf_table()

    # ---------- 布局 ----------

    def _build_ui(self) -> None:
        self._root.title("交易确认书运维管理台")
        self._root.geometry("1120x720")
        self._root.minsize(960, 600)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        self._status_vars: dict[str, tk.StringVar] = {}
        status_row = tk.Frame(self._root)
        status_row.pack(fill="x", padx=10, pady=(10, 4))
        self._status_labels: dict[str, tk.Label] = {}
        for name in (f"{SERVICE_NAME}:16320", MONITOR_NAME, "今日产出",
                     "最近操作"):
            cell = tk.Frame(status_row, relief="groove", borderwidth=1,
                            padx=10, pady=6)
            cell.pack(side="left", fill="both", expand=True, padx=4)
            tk.Label(cell, text=name, font=FONT_SMALL,
                     fg=COLOR_MUTED).pack(anchor="w")
            var = tk.StringVar(value="—")
            label = tk.Label(cell, textvariable=var, font=FONT_BOLD,
                             anchor="w")
            label.pack(anchor="w")
            self._status_vars[name] = var
            self._status_labels[name] = label

        action_bar = tk.Frame(self._root)
        action_bar.pack(fill="x", padx=10, pady=6)
        self._btn_start = tk.Button(
            action_bar, text="▶ 启动", width=12, font=FONT_BOLD,
            fg=COLOR_OK, command=self._on_start)
        self._btn_start.pack(side="left", padx=4)
        self._btn_stop = tk.Button(
            action_bar, text="■ 结束", width=12, font=FONT_BOLD,
            fg=COLOR_BAD, state="disabled", command=self._on_stop)
        self._btn_stop.pack(side="left", padx=4)
        tk.Button(action_bar, text="↻ 手动刷新", width=10,
                  command=self._on_manual_refresh).pack(side="left", padx=4)
        tk.Label(action_bar, text="启动模式", font=FONT_SMALL,
                 fg=COLOR_MUTED).pack(side="left", padx=(16, 2))
        tk.Radiobutton(action_bar, text="断点续传", variable=self._monitor_mode,
                       value="resume").pack(side="left")
        tk.Radiobutton(action_bar, text="全新启动", variable=self._monitor_mode,
                       value="fresh").pack(side="left")
        tk.Label(action_bar, text="刷新", font=FONT_SMALL,
                 fg=COLOR_MUTED).pack(side="left", padx=(16, 2))
        tk.Radiobutton(action_bar, text="自动(5s)", variable=self._refresh_mode,
                       value="auto", command=self._on_mode_change
                       ).pack(side="left")
        tk.Radiobutton(action_bar, text="手动", variable=self._refresh_mode,
                       value="manual", command=self._on_mode_change
                       ).pack(side="left")

        body = tk.Frame(self._root)
        body.pack(fill="both", expand=True, padx=10, pady=4)
        log_area = tk.Frame(body)
        log_area.pack(side="left", fill="both", expand=True)

        self._notebook = ttk.Notebook(log_area)
        self._notebook.pack(fill="both", expand=True)

        pdf_tab = tk.Frame(self._notebook)
        self._notebook.add(pdf_tab, text="PDF 产出")
        columns = ("file", "mtime", "size", "cp", "tpl", "serial")
        self._pdf_tree = ttk.Treeview(pdf_tab, columns=columns,
                                      show="headings")
        headers = {"file": ("文件名", 420), "mtime": ("生成时间", 110),
                   "size": ("大小", 80), "cp": ("对手方", 70),
                   "tpl": ("模板", 100), "serial": ("序号", 60)}
        for key, (text, width) in headers.items():
            self._pdf_tree.heading(key, text=text)
            self._pdf_tree.column(key, width=width,
                                  anchor="w" if key == "file" else "center")
        pdf_scroll = ttk.Scrollbar(pdf_tab, orient="vertical",
                                   command=self._pdf_tree.yview)
        self._pdf_tree.configure(yscrollcommand=pdf_scroll.set)
        self._pdf_tree.pack(side="left", fill="both", expand=True,
                            padx=(2, 0), pady=2)
        pdf_scroll.pack(side="left", fill="y", pady=2)
        self._pdf_tree.bind("<Double-1>", self._on_pdf_double_click)
        self._pdf_menu = tk.Menu(self._root, tearoff=0)
        self._pdf_menu.add_command(label="打开文件",
                                   command=self._open_selected_pdf)
        self._pdf_menu.add_command(label="打开所在文件夹",
                                   command=self._open_pdf_folder)
        self._pdf_menu.add_command(label="复制路径",
                                   command=self._copy_pdf_path)
        self._pdf_tree.bind("<Button-3>", self._show_pdf_menu)

        runlog_tab = tk.Frame(self._notebook)
        self._notebook.add(runlog_tab, text="运行日志")
        self._run_log = tk.Text(runlog_tab, wrap="none", state="disabled",
                                font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(runlog_tab, orient="vertical",
                                   command=self._run_log.yview)
        self._run_log.configure(yscrollcommand=log_scroll.set)
        self._run_log.pack(side="left", fill="both", expand=True,
                           padx=(2, 0), pady=2)
        log_scroll.pack(side="left", fill="y", pady=2)
        self._run_log.tag_configure("error", foreground=COLOR_BAD)
        self._run_log.tag_configure("warn", foreground=COLOR_WARN)
        self._run_log.tag_configure("monitor", foreground="#3a5f8a")

        alert_panel = tk.Frame(body, width=340)
        alert_panel.pack(side="left", fill="both", padx=(8, 0))
        alert_panel.pack_propagate(False)
        tk.Label(alert_panel, text="告警（双击确认置灰）", font=FONT_BOLD
                 ).pack(anchor="w", pady=(2, 4))
        alert_columns = ("level", "title", "time", "detail")
        self._alert_tree = ttk.Treeview(alert_panel, columns=alert_columns,
                                        show="headings")
        for key, text, width in (("level", "级别", 44), ("title", "标题", 108),
                                 ("time", "时间", 66), ("detail", "摘要", 108)):
            self._alert_tree.heading(key, text=text)
            self._alert_tree.column(key, width=width, anchor="w")
        self._alert_tree.column("level", anchor="center")
        self._alert_tree.pack(fill="both", expand=True)
        self._alert_tree.tag_configure("error", foreground=COLOR_BAD)
        self._alert_tree.tag_configure("warn", foreground=COLOR_WARN)
        self._alert_tree.tag_configure("ack", foreground="#aaaaaa")
        self._alert_tree.bind("<Double-1>", self._on_alert_ack)

        footer = tk.Frame(self._root, relief="groove", borderwidth=1)
        footer.pack(fill="x", side="bottom")
        output_dir = self._scanner.output_dir
        tk.Label(footer, text=f"产出目录: {output_dir}", font=FONT_SMALL,
                 fg=COLOR_MUTED).pack(side="left", padx=8, pady=3)
        tk.Label(footer,
                 text="× 关闭 = 退出程序 · 托盘右键可 启动/结束/退出",
                 font=FONT_SMALL, fg=COLOR_MUTED).pack(side="right", padx=8)

    # ---------- 状态卡 / 按钮 ----------

    def _update_status_cards(self) -> None:
        svc_running = self._manager.is_running(SERVICE_NAME)
        mon_running = self._manager.is_running(MONITOR_NAME)
        svc_rc = self._manager.returncode(SERVICE_NAME)
        mon_rc = self._manager.returncode(MONITOR_NAME)
        svc_adopted = "（接管）" if self._manager.is_adopted(SERVICE_NAME) else ""
        mon_adopted = "（接管）" if self._manager.is_adopted(MONITOR_NAME) else ""
        if not self._busy:
            # 崩溃检测（方案 §6.4）：运行中 -> 非 0 退出 = 异常退出
            self._detect_crash(SERVICE_NAME, svc_running, svc_rc)
            self._detect_crash(MONITOR_NAME, mon_running, mon_rc)
        if self._busy:
            svc_text, svc_color = "⏳ 处理中…", COLOR_WARN
            mon_text, mon_color = "⏳ 处理中…", COLOR_WARN
        else:
            svc_text, svc_color = self._status_text(svc_running, svc_rc)
            mon_text, mon_color = self._status_text(mon_running, mon_rc)
            if svc_running:
                svc_text += f" {format_uptime(self._manager.started_at(SERVICE_NAME))}{svc_adopted}"
                if self._health_fail_count >= HEALTH_FAIL_ALERT_THRESHOLD:
                    svc_text += " · 无响应"
                    svc_color = COLOR_WARN
            if mon_running:
                mon_text += f" {format_uptime(self._manager.started_at(MONITOR_NAME))}{mon_adopted}"
        today = dt.date.today()
        today_count = sum(
            1 for entry in self._current_entries
            if dt.datetime.fromtimestamp(entry.mtime).date() == today)
        self._set_card(f"{SERVICE_NAME}:16320", svc_text, svc_color)
        self._set_card(MONITOR_NAME, mon_text, mon_color)
        self._set_card("今日产出", f"{today_count} 份 PDF", "#222222")
        self._set_card("最近操作", self._last_op, "#222222")

    def _set_card(self, key: str, text: str, color: str) -> None:
        self._status_vars[key].set(text)
        self._status_labels[key].configure(fg=color)

    def _detect_crash(self, name: str, is_running: bool,
                      returncode: int | None) -> None:
        """运行 -> 非零退出：告警 + 托盘气泡（主动停止 rc=0 不告警）。"""
        was = self._was_running.get(name, False)
        self._was_running[name] = is_running
        if was and not is_running and returncode not in (None, 0):
            tail = " | ".join(list(self._recent_logs.get(name, []))[-3:])
            detail = (f"{name} 异常退出 code={returncode}"
                      f"（日志尾部: {tail}）" if tail else
                      f"{name} 异常退出 code={returncode}，详见运行日志 Tab")
            self._alerts.add("error", "进程异常退出", detail)
            self._update_alert_tree()
            self._notify_tray("进程异常退出",
                              f"{name} 已退出 (code={returncode})，"
                              f"请查看告警与运行日志")

    def _notify_tray(self, title: str, message: str) -> None:
        if self._tray_icon is not None:
            try:
                self._tray_icon.notify(message, title)
            except Exception:   # noqa: BLE001 气泡失败不影响主流程
                LOG.debug("托盘气泡失败", exc_info=True)

    @staticmethod
    def _status_text(running: bool, returncode: int | None) -> tuple[str, str]:
        if running:
            return "● 运行中", COLOR_OK
        if returncode not in (None, 0):
            return f"▲ 异常退出 (code={returncode})", COLOR_BAD
        return "○ 已停止", COLOR_STOP

    def _update_buttons(self) -> None:
        running = (self._manager.is_running(SERVICE_NAME)
                   or self._manager.is_running(MONITOR_NAME))
        if self._busy:
            self._btn_start.configure(state="disabled")
            self._btn_stop.configure(state="disabled")
        else:
            self._btn_start.configure(state="disabled" if running else "normal")
            self._btn_stop.configure(state="normal" if running else "disabled")

    # ---------- 启动 / 结束 ----------

    def _on_start(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._update_buttons()
        mode = self._monitor_mode.get()

        def _work() -> None:
            try:
                self._manager.start_all(monitor_mode=mode)
                self._ui_events.put(lambda: self._after_start(None))
            except (ProcessStartError, OSError, subprocess.SubprocessError) as exc:
                detail = str(exc)
                self._ui_events.put(lambda: self._after_start(detail))

        threading.Thread(target=_work, daemon=True, name="starter").start()

    def _after_start(self, error: str | None) -> None:
        self._busy = False
        if error is None:
            self._last_op = f"{dt.datetime.now():%H:%M} 启动"
        else:
            self._alerts.add("error", "启动失败", error)
            messagebox.showerror("启动失败", error, parent=self._root)
        self._update_status_cards()
        self._update_buttons()
        self._update_alert_tree()

    def _on_stop(self) -> None:
        if self._busy:
            return
        confirmed = messagebox.askyesno(
            "确认停止主流程？",
            "停止后微信新消息将不再生成确认书。\n"
            "未处理消息已落盘，下次启动「断点续传」或手动 --replay 可补。\n\n"
            "是否继续？",
            icon="warning", default="no", parent=self._root)
        if not confirmed:
            return
        self._busy = True
        self._update_buttons()

        def _work() -> None:
            try:
                self._manager.stop_all()
            finally:
                self._ui_events.put(lambda: self._after_stop())

        threading.Thread(target=_work, daemon=True, name="stopper").start()

    def _after_stop(self) -> None:
        self._busy = False
        self._health_fail_count = 0
        self._last_op = f"{dt.datetime.now():%H:%M} 结束（已确认）"
        self._update_status_cards()
        self._update_buttons()

    def _on_manual_refresh(self) -> None:
        self._update_status_cards()
        self._update_pdf_table()
        self._update_alert_tree()

    def _on_mode_change(self) -> None:
        pass  # 模式切换仅改 StringVar；自动/手动由 _tick_refresh 读取

    # ---------- 周期任务 ----------

    def _tick_clock(self) -> None:
        self._root.after(1000, self._tick_clock)
        if self._manager.is_running(SERVICE_NAME) or \
                self._manager.is_running(MONITOR_NAME):
            self._update_status_cards()

    def _tick_refresh(self) -> None:
        self._root.after(REFRESH_AUTO_MS, self._tick_refresh)
        if self._refresh_mode.get() == "auto" and not self._busy:
            self._update_status_cards()
            self._update_pdf_table()
            self._update_alert_tree()

    def _start_health_probe(self) -> None:
        def _probe() -> None:
            healthy = probe_health_once("http://127.0.0.1:16320")
            self._ui_events.put(
                lambda: self._after_health_probe(healthy))

        threading.Thread(target=_probe, daemon=True,
                         name="health-probe").start()

    def _after_health_probe(self, is_healthy: bool) -> None:
        running = self._manager.is_running(SERVICE_NAME)
        if not running:
            self._health_fail_count = 0
            # 必须续期：否则探测链在"UI 先启、服务后启"的日常路径下
            # 于首次探测后永久死亡，服务 A 假死将永远不被发现
            self._root.after(5000, self._start_health_probe)
            return
        if is_healthy:
            self._health_fail_count = 0
        else:
            self._health_fail_count += 1
            if self._health_fail_count == HEALTH_FAIL_ALERT_THRESHOLD:
                self._alerts.add("warn", "服务 A 健康检查超时",
                                 "/health 连续 3 次无响应，请查看运行日志")
                self._update_alert_tree()
        self._update_status_cards()
        self._root.after(5000, self._start_health_probe)

    # ---------- 日志与告警 ----------

    def _drain_logs(self) -> None:
        self._root.after(100, self._drain_logs)
        while True:
            try:
                source, line = self._log_sink.get_nowait()
            except queue.Empty:
                break
            self._append_run_log(source, line)
            self._alerts.feed_log_line(source, line)

    def _append_run_log(self, source: str, line: str) -> None:
        if source in self._recent_logs:
            self._recent_logs[source].append(line)
        self._run_log.configure(state="normal")
        tag = ""
        if "ERROR" in line:
            tag = "error"
        elif "WARNING" in line:
            tag = "warn"
        prefix = f"[{source}] "
        self._run_log.insert("end", prefix, ("monitor",) if source == MONITOR_NAME else ())
        self._run_log.insert("end", line + "\n", (tag,) if tag else ())
        if int(self._run_log.index("end-1c").split(".")[0]) > MAX_LOG_LINES:
            self._run_log.delete("1.0", f"{MAX_LOG_LINES // 2}.0")
        self._run_log.configure(state="disabled")
        self._run_log.see("end")

    def _update_alert_tree(self) -> None:
        self._alert_tree.delete(*self._alert_tree.get_children())
        for alert in self._alerts.recent(limit=20):
            tag = "ack" if alert.acknowledged else alert.level
            self._alert_tree.insert("", "end", iid=str(id(alert)), values=(
                "▲" if alert.level == "error" else "△",
                alert.title,
                dt.datetime.fromtimestamp(alert.ts).strftime("%H:%M"),
                alert.detail[:80]), tags=(tag,))

    def _on_alert_ack(self, _event) -> None:
        selection = self._alert_tree.selection()
        for item_id in selection:
            for alert in self._alerts.recent(limit=50):
                if str(id(alert)) == item_id:
                    self._alerts.acknowledge(alert)
        if selection:
            self._update_alert_tree()

    # ---------- PDF 表 ----------

    def _update_pdf_table(self) -> None:
        try:
            self._current_entries = self._scanner.scan()
        except OSError as exc:
            LOG.warning("PDF 扫描失败: %s", exc)
            return
        self._pdf_tree.delete(*self._pdf_tree.get_children())
        for entry in self._current_entries:
            self._pdf_tree.insert("", "end", iid=str(entry.path), values=(
                entry.file_name, format_mtime(entry.mtime),
                format_size(entry.size), entry.counterparty,
                entry.template, entry.serial))

    def _selected_entry(self) -> PdfEntry | None:
        selection = self._pdf_tree.selection()
        if not selection:
            return None
        target = Path(selection[0])
        for entry in self._current_entries:
            if entry.path == target:
                return entry
        return None

    def _on_pdf_double_click(self, _event) -> None:
        self._open_selected_pdf()

    def _show_pdf_menu(self, event) -> None:
        row = self._pdf_tree.identify_row(event.y)
        if row:
            self._pdf_tree.selection_set(row)
            self._pdf_menu.tk_popup(event.x_root, event.y_root)

    def _open_selected_pdf(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        os.startfile(entry.path)  # noqa: S606 Windows 默认程序打开

    def _open_pdf_folder(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        subprocess.Popen(["explorer", "/select,", str(entry.path)])  # noqa: S606

    def _copy_pdf_path(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        self._root.clipboard_clear()
        self._root.clipboard_append(str(entry.path))

    # ---------- 托盘 ----------

    def _setup_tray(self) -> None:
        self._tray_icon = None
        if not HAS_TRAY:
            LOG.warning("pystray/pillow 未安装，托盘功能降级（× 直接退出）")
            return
        image = self._make_tray_image()
        menu = pystray.Menu(
            pystray.MenuItem("打开主窗口", self._tray_show, default=True),
            pystray.MenuItem("▶ 启动", self._tray_start, enabled=lambda _: not self._busy),
            pystray.MenuItem("■ 结束", self._tray_stop, enabled=lambda _: not self._busy),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self._tray_icon = pystray.Icon("ops_ui", image,
                                       "交易确认书运维管理台", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True,
                         name="tray").start()

    @staticmethod
    def _make_tray_image():
        image = Image.new("RGBA", (64, 64), (30, 90, 170, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((10, 10, 54, 54), fill=(255, 255, 255, 255))
        draw.ellipse((16, 16, 48, 48), fill=(30, 90, 170, 255))
        return image

    def _tray_show(self, _icon=None, _item=None) -> None:
        self._ui_events.put(self._root.deiconify)

    def _tray_start(self, _icon=None, _item=None) -> None:
        self._ui_events.put(self._on_start)

    def _tray_stop(self, _icon=None, _item=None) -> None:
        self._ui_events.put(self._on_stop)

    def _tray_quit(self, _icon=None, _item=None) -> None:
        self._ui_events.put(self._quit_with_confirm)

    def _quit_with_confirm(self) -> None:
        running = (self._manager.is_running(SERVICE_NAME)
                   or self._manager.is_running(MONITOR_NAME))
        if running:
            confirmed = messagebox.askyesno(
                "退出运维管理台",
                "退出前是否同时停止主流程？\n\n"
                "「是」= 停止主流程并退出\n"
                "「否」= 主流程保持运行，仅退出界面",
                icon="question", parent=self._root)
            if not confirmed:
                return
        self._quit(stopping=running)

    def _quit(self, stopping: bool) -> None:
        def _work() -> None:
            if stopping:
                try:
                    self._manager.stop_all()
                finally:
                    self._ui_events.put(self._destroy_all)
        if stopping:
            self._busy = True
            threading.Thread(target=_work, daemon=True,
                             name="quitter").start()
        else:
            self._destroy_all()

    def _destroy_all(self) -> None:
        if self._tray_icon is not None:
            self._tray_icon.stop()
        self._root.after(0, self._root.destroy)

    def _on_close_window(self) -> None:
        """× 关闭 = 退出程序（不最小化托盘，2026-09-04 用户裁决）。

        与托盘「退出」同路径：有进程在跑时弹确认（是=停止并退出 /
        否=仅退出界面），无进程运行时直接退出。托盘保留为窗口打开
        期间的快捷入口（启动/结束/退出），不再承担「隐藏窗口」职责。
        """
        self._quit_with_confirm()

    # ---------- 事件泵 ----------

    def _drain_events(self) -> None:
        self._root.after(100, self._drain_events)
        while True:
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:   # noqa: BLE001 UI 事件异常不拖垮主循环
                LOG.exception("UI 事件处理异常")


def _resolve_base_dir() -> Path:
    """数据/配置锚点目录。

    frozen（PyInstaller）态用 exe 所在目录；源码态用项目根（ops_ui 的
    上一级）。frozen 态若仍用 __file__ 会指向 _MEIPASS 临时解包目录，
    ini/收件箱/产出/日志将随临时目录退出而丢失（2026-09-04 审计 #6）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    base_dir = _resolve_base_dir()
    root = tk.Tk()
    OpsApp(root, base_dir)
    if "--selftest" in sys.argv:
        root.after(800, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
