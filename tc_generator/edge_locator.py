r"""渲染内核可执行文件定位（打包/换机环境适配，2026-09-04 审计项 #1-3）.

主内核为 Chrome for Testing headless-shell（2026-09-08 裁决），Edge 为
fallback。查找优先级（先命中先返回）：
1. 显式配置（ini 的 edge_path / 调用方传入）——非空即用，存在性由
   预检负责（此处仅拒绝空串）；
2. 环境变量 EDGE_PATH；
3. 打包分发目录下的 chrome-headless-shell（仅 frozen 态：exe 同目录的
   chrome-headless-shell\chrome-headless-shell.exe，随包自动发现）；
4. Edge 注册表 App Paths（HKLM/HKCU，管理员安装与用户安装都会登记）；
5. Edge 常见安装路径探测（Program Files / Program Files (x86) /
   LOCALAPPDATA）；
6. Edge PATH 搜索。

找不到时抛 EdgeNotFoundError（中文可读，含已尝试的位置），
由调用方决定提示方式。仅支持 Windows；非 Windows 直接报错。
"""

from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache as _lru_cache
from pathlib import Path

LOG = logging.getLogger(__name__)

EDGE_EXECUTABLE = "msedge.exe"
HEADLESS_SHELL_EXECUTABLE = "chrome-headless-shell.exe"

# frozen 态随包分发目录名（与 packaging/alert.spec 的 Tree prefix 对应）
_BUNDLED_SHELL_DIRNAME = "chrome-headless-shell"

# 注册表 App Paths 登记点（默认值为完整 exe 路径）
_APP_PATHS_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
)

# 常见安装根（目录存在性探测在 locate_edge 内做）
_COMMON_ROOTS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application",
    r"C:\Program Files\Microsoft\Edge\Application",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application"),
)


class EdgeNotFoundError(RuntimeError):
    """未找到可用的渲染内核（headless-shell 或 Edge）。"""


def _candidate_from_bundled_shell() -> str | None:
    r"""frozen 态探测 exe 同目录的随包 headless-shell；未打包返回 None.

    布局（方案 A，与 frozen base_dir=exe 目录约定一致）：
        <exe 目录>\chrome-headless-shell\chrome-headless-shell.exe
    源码态（非 frozen）无「随包」概念，直接返回 None。
    """
    if not getattr(sys, "frozen", False):
        return None
    base_dir = Path(sys.executable).resolve().parent
    candidate = base_dir / _BUNDLED_SHELL_DIRNAME / HEADLESS_SHELL_EXECUTABLE
    if candidate.is_file():
        return str(candidate)
    # 兼容用户把 exe 直接放在产物根的布局
    direct = base_dir / HEADLESS_SHELL_EXECUTABLE
    if direct.is_file():
        return str(direct)
    return None


def _candidate_from_registry() -> str | None:
    """从 App Paths 注册表读 msedge.exe 完整路径；不可用返回 None。"""
    try:
        import winreg
    except ImportError:                     # 非 Windows
        return None
    for key_path in _APP_PATHS_KEYS:
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            if value:
                candidate = os.path.expandvars(value.strip().strip('"'))
                if candidate.lower().endswith(EDGE_EXECUTABLE):
                    return candidate
    return None


def _candidate_from_common_roots() -> str | None:
    """在常见安装根下探测（含版本子目录与 Application 直装两种布局）。"""
    for root in _COMMON_ROOTS:
        base = Path(root)
        if not base.is_dir():
            continue
        direct = base / EDGE_EXECUTABLE
        if direct.is_file():
            return str(direct)
        # 版本化布局：<root>\<版本号>\msedge.exe（取最高版本号）
        versioned = [
            p for p in base.iterdir()
            if p.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p.name)
        ]
        if versioned:
            newest = max(versioned, key=lambda p: [int(x) for x in p.name.split(".")])
            candidate = newest / EDGE_EXECUTABLE
            if candidate.is_file():
                return str(candidate)
    return None


def _candidate_from_path_env() -> str | None:
    """PATH 搜索兜底。"""
    exe = None
    for dir_ in os.environ.get("PATH", "").split(os.pathsep):
        if not dir_:
            continue
        candidate = Path(dir_) / EDGE_EXECUTABLE
        if candidate.is_file():
            exe = str(candidate)
            break
    return exe


def locate_edge(explicit: str = "") -> str:
    """按优先级定位渲染内核，返回可执行文件绝对路径.

    explicit：ini/调用方显式配置。非空时直接返回（不做存在性检查——
    存在性统一由 renderer 预检与 EdgeNotFoundError 语义负责，保持
    「显式配置 = 用户意志」的直觉）。为空时走自动发现（结果缓存，
    注册表/目录扫描每次渲染都查一遍代价不值）。
    """
    explicit = (explicit or "").strip()
    if explicit:
        return str(Path(os.path.expandvars(explicit)))
    return _locate_auto_cached()


@_lru_cache(maxsize=1)
def _locate_auto_cached() -> str:
    return _locate_auto()


def _locate_auto() -> str:
    tried: list[str] = []
    env_value = (os.environ.get("EDGE_PATH") or "").strip()
    if env_value:
        tried.append(f"环境变量 EDGE_PATH={env_value}")
        if Path(os.path.expandvars(env_value)).is_file():
            return str(Path(os.path.expandvars(env_value)))

    bundled = _candidate_from_bundled_shell()
    if bundled:
        return bundled

    reg = _candidate_from_registry()
    if reg and Path(reg).is_file():
        return reg
    if reg:
        tried.append(f"注册表登记={reg}")

    common = _candidate_from_common_roots()
    if common:
        return common
    tried.append("常见安装目录")

    from_path = _candidate_from_path_env()
    if from_path:
        return from_path

    raise EdgeNotFoundError(
        "未找到可用的渲染内核（确认书 PDF 生成需要）。已尝试："
        + ("；".join(tried) if tried else "环境变量/随包目录/注册表/"
           "常见安装目录/PATH")
        + "。请在 service_a_config.ini 的 edge_path 显式指定内核 exe 完整路径"
        "（chrome-headless-shell.exe 或 msedge.exe）。")


def ensure_edge_available(edge_path: str) -> str:
    """renderer 预检入口：定位并校验存在性，返回可用渲染内核路径.

    显式路径不存在 → 带明确指引抛错（不静默回退自动发现，
    避免用户配置被悄悄忽略后难排查）。
    """
    explicit = (edge_path or "").strip()
    resolved = locate_edge(explicit)
    if not Path(resolved).is_file():
        raise EdgeNotFoundError(
            f"edge_path 指定的文件不存在: {resolved}"
            "（请检查 service_a_config.ini 配置，或将其留空以自动发现"
            "随包 headless-shell / 系统 Edge）")
    LOG.info("渲染内核就绪: %s", resolved)
    return resolved
