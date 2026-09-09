"""PDF 渲染：Jinja2 HTML 模板 + Chromium 系无头内核 print-to-pdf.

渲染内核（2026-09-08 裁决）：主内核为 Chrome for Testing headless-shell
（单二进制直渲，无 Edge launcher 分离僵死问题）；Edge 降为 fallback，
定位逻辑见 edge_locator.py。两内核共用同一最小参数集：--headless 对
headless-shell 无副作用（实测），对 Edge 必需；WeasyPrint 路线已废弃
（本机缺 GTK DLL），模板层不变。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape

from .edge_locator import ensure_edge_available
from .resources import package_resource

LOG = logging.getLogger(__name__)

TEMPLATES_DIR = package_resource("templates")   # frozen 态经 _MEIPASS 解析
TC_TEMPLATE_NAME = "tc_9080.html.j2"

# 样例 PDF 排版规则：独立的 K1/K2 数字为下标小号（K₁/K₂），
# 而 K1_ratio / K2_ratio 中的数字保持正常大小。
# 另实测：结算计算与免责声明中的独立 K 本身为 9.96pt、下标 6.48pt（Word 源
# 文档中这两处字号被单独调大），其余位置 K 为正文字号、下标 6pt。
_STANDALONE_K_RE = re.compile(r"K([12])(?!_ratio)")


def k_subscript(value: object, big_k: bool = False,
                wrap_class: str | None = None) -> Markup:
    """Jinja2 过滤器：把独立的 K1/K2 渲染为 K<sub>1</sub>/K<sub>2</sub>。

    big_k=True 时（结算计算/免责声明），额外包一层 .kbig 以复现样例中
    K=9.96pt、下标 6.48pt 的字号。
    wrap_class 不为空时，再把每个 K<sub>n</sub> 包一层指定 class 的 span
    （CP003 样例免责声明：11.04pt 正文中 K 缩小为 9.48pt，下标 6pt）。
    """
    escaped = escape(str(value))

    def _maybe_wrap(html: str) -> str:
        if wrap_class:
            return f'<span class="{wrap_class}">{html}</span>'
        return html

    if big_k:
        replaced = _STANDALONE_K_RE.sub(
            lambda m: f'<span class="kbig">K<sub>{m.group(1)}</sub></span>',
            escaped)
        return Markup(replaced)
    return Markup(_STANDALONE_K_RE.sub(
        lambda m: _maybe_wrap(f"K<sub>{m.group(1)}</sub>"), escaped))

# 统一最小参数集（2026-09-08 裁决：不分叉参数表，真实双跑回归兜底）：
# --headless 对 Edge 必需，对 headless-shell 无副作用（其本身即无头专用
# 二进制）；--no-first-run / --disable-extensions 为 Edge 专属优化，已裁撤。
# --user-data-dir 保留：headless-shell 虽无单例冲突，隔离 profile 更稳。
_RENDER_ARGS = [
    "--headless",
    "--disable-gpu",
    "--no-pdf-header-footer",
]

# 快照/清理目标：两内核的进程映像名（诊断与残留清理都只认自家进程树）
_KERNEL_IMAGE_NAMES = ("msedge.exe", "chrome-headless-shell.exe")

# Edge headless 偶发失败（进程启动竞争/临时 profile 锁/瞬时资源不足）时
# 的退避重试间隔：首跑 + 2 次重试，共 3 次尝试。
_EDGE_RETRY_DELAYS = (0.5, 1.0)


def _edge_process_snapshot() -> str:
    """失败诊断：列出运行中渲染内核进程的 PID 与命令行摘要（只读）.

    背景：2026-09-08 目标机三连败时报"16 个 Edge 进程"但用户未开浏览器
    （Edge launcher 分离导致渲染进程树僵死），事后无法回溯进程归属。
    故在失败路径自动落快照，当场抓现行。任何异常返回降级文本。
    """
    name_filter = " OR ".join(
        f"Name='{name}'" for name in _KERNEL_IMAGE_NAMES)
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process "
             f"-Filter \"{name_filter}\" "
             "| ForEach-Object { \"PID=$($_.ProcessId) CMD=$($_.CommandLine)\" }",
             ],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        lines = [ln.strip() for ln in completed.stdout.splitlines()
                 if ln.strip().startswith("PID=")]
        if not lines:
            return "无渲染内核进程"
        # 命令行可能很长：截断到 200 字符保留关键参数（user-data-dir 等）
        summary = "; ".join(
            ln[:200] + ("…" if len(ln) > 200 else "") for ln in lines)
        return f"共 {len(lines)} 个: {summary}"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "进程快照获取失败（诊断降级）"


def _kill_leftover_by_profile(profile_dir: Path) -> str:
    """失败清理：按 user-data-dir 命令行特征杀残留的内核进程树.

    背景：目标机 Edge launcher 分离模式下，spawn 的主 PID 先退出
    （exit=0）而真实渲染进程树僵死不退，每失败一次泄漏 ~15 进程并锁定
    profile 目录。因此不能靠记录 spawn PID 清理，须按命令行中本次尝试
    独有的 --user-data-dir=<profile_dir> 特征筛选后 taskkill /T /F。
    仅匹配两内核映像名，且排除 PowerShell 自身（其 -Command 参数含特征串）。
    任何异常返回降级文本，不影响主异常链。
    """
    name_filter = " OR ".join(
        f"Name='{name}'" for name in _KERNEL_IMAGE_NAMES)
    # 命令行特征匹配放 PowerShell 侧 -like（通配符仅 * ?，反斜杠为字面量）；
    # 不用 WQL LIKE——其 \ 转义规则对 Windows 路径（\U、中文目录等）会报
    # WBEM_E_INVALID_QUERY 无效查询（2026-09-08 实测）
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process "
             f"-Filter \"{name_filter}\" "
             f"| Where-Object {{ $_.CommandLine -like '*{profile_dir}*' }} "
             "| Where-Object { $_.ProcessId -ne $PID } "
             "| ForEach-Object { taskkill /PID $_.ProcessId /T /F >$null 2>&1; "
             "\"killed=$($_.ProcessId)\" }",
             ],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        killed = [ln.strip() for ln in completed.stdout.splitlines()
                  if ln.strip().startswith("killed=")]
        if not killed:
            return "无残留进程"
        return f"已清理进程树: {', '.join(killed)}"
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return "残留进程清理失败（诊断降级，可手动 taskkill）"


def render_tc_html(fields: dict[str, str], template_dir: Path = TEMPLATES_DIR,
                   template_name: str = TC_TEMPLATE_NAME) -> str:
    """Jinja2 渲染 HTML 字符串；StrictUndefined 保证字段缺一即报错。"""
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        undefined=StrictUndefined,
        autoescape=True,
    )
    env.filters["k_subscript"] = k_subscript
    template = env.get_template(template_name)
    return template.render(**fields)


def _run_edge_once(cmd: list[str], tmp_pdf: Path) -> None:
    """单次运行渲染内核；未产出 PDF 即抛 RuntimeError（由调用方决定重试）.

    stdout/stderr 全量带出：内核失败时可能写 stderr（如 components 错误）
    也可能写 stdout（版本差异），仅看 stderr 会得到空串误导排查。
    CREATE_NO_WINDOW：windowed 态（tc_ops_ui 拉起的服务 A）下防止弹出
    控制台窗口；源码态无副作用（本 renderer 不是信号载体）。
    """
    completed = subprocess.run(
        cmd, capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
    if not tmp_pdf.exists():
        raise RuntimeError(
            f"渲染内核未产出 PDF（exit={completed.returncode}）"
            f" stderr={completed.stderr[-500:]!r}"
            f" stdout={completed.stdout[-500:]!r}")


def render_tc_pdf(fields: dict[str, str], output_pdf: Path,
                  edge_path: str,
                  template_dir: Path = TEMPLATES_DIR,
                  template_name: str = TC_TEMPLATE_NAME) -> Path:
    """渲染 HTML 并经无头内核 print-to-pdf 生成 PDF；先写临时文件再原子替换.

    参数 edge_path 沿用历史名，实际语义为「渲染内核可执行文件路径」：
    允许为空 = 自动发现（ini 显式 > 环境/打包目录 headless-shell >
    Edge，见 edge_locator）；显式路径会在渲染前做存在性预检，缺失即抛
    EdgeNotFoundError（中文可读），不再出现原始 FileNotFoundError。
    内核偶发失败（无产物/超时）按 _EDGE_RETRY_DELAYS 退避重试，每次
    失败即清理该次尝试的残留进程树；目标文件已存在（FileExistsError）
    为业务错误，不重试。
    """
    edge_path = str(ensure_edge_available(edge_path))
    html_text = render_tc_html(fields, template_dir, template_name)
    output_pdf = Path(output_pdf)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="tc_gen_"))
    try:
        tmp_html = tmp_root / f"tc_{uuid.uuid4().hex}.html"
        tmp_pdf = tmp_root / f"tc_{uuid.uuid4().hex}.pdf"
        # 一次性独立 profile：与用户浏览器/后台常驻 Edge 等既有实例隔离；
        # 每次尝试换新 profile —— 失败的尝试可能留下半初始化 profile 与
        # 残留句柄（2026-09-08 目标机 WinError 32），重试不得进入污染现场
        tmp_html.write_text(html_text, encoding="utf-8")
        html_url = tmp_html.resolve().as_uri()
        last_error: Exception | None = None
        cmd: list[str] = []
        for attempt, delay in enumerate((0.0, *_EDGE_RETRY_DELAYS), start=1):
            if delay:
                time.sleep(delay)
            profile_dir = tmp_root / f"edge_profile_{attempt}"
            tmp_pdf.unlink(missing_ok=True)  # 清上次可能的半成品
            cmd = [
                edge_path, *_RENDER_ARGS,
                f"--user-data-dir={profile_dir}",
                f"--print-to-pdf={tmp_pdf}",
                html_url,
            ]
            try:
                _run_edge_once(cmd, tmp_pdf)
                break
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                last_error = exc
                LOG.warning("渲染内核第 %d 次尝试失败: %s", attempt, exc)
                # 立即清理本次尝试的残留进程树（launcher 分离僵死场景，
                # 不清理则每败泄漏 ~15 进程并锁定 profile 目录）
                cleanup = _kill_leftover_by_profile(profile_dir)
                LOG.warning("残留进程清理结果: %s", cleanup)
        else:
            # 最终失败：附进程快照（命令行级）当场定位干扰源。
            # 不再附 _count_edge_processes 数字（目标机实测与快照矛盾，
            # tasklist 过滤结果不可靠），以快照为唯一事实来源。
            snapshot = _edge_process_snapshot()
            raise RuntimeError(f"{last_error}；[{snapshot}]") from last_error
        # 原子替换到最终路径，防止半成品文件被当作有效产出
        if output_pdf.exists():
            raise FileExistsError(
                f"目标文件已存在，拒绝覆盖: {output_pdf}")
        tmp_pdf.replace(output_pdf)
    finally:
        # 兜底清理：Edge 残留句柄（如 crashpad 未退）会让 rmtree 抛
        # PermissionError（WinError 32），污染主异常链——2026-09-08 目标机
        # 实证。清不掉只留几 MB 临时文件，ignore_errors 不掩盖业务结果
        shutil.rmtree(tmp_root, ignore_errors=True)
    LOG.info("渲染内核出 PDF 完成: %s (%d bytes)",
             output_pdf.name, output_pdf.stat().st_size)
    return output_pdf
