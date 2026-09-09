"""打包资源路径解析（2026-09-04 审计项 #4/#5）.

源码态：资源与代码同包（tc_generator/templates、tc_generator/data）。
PyInstaller frozen 态：数据文件被解包到 ``sys._MEIPASS``，按 spec 的
``--add-data tc_generator/templates;tc_generator/templates`` 布局，
位于 ``_MEIPASS/tc_generator/`` 之下。

``package_resource(*parts)`` 统一两态差异：源码态相对本包目录取，
frozen 态相对 ``_MEIPASS/tc_generator`` 取，调用方传相对包根的子路径。
"""

from __future__ import annotations

import sys
from pathlib import Path


def package_resource(*parts: str) -> Path:
    """返回 tc_generator 包内资源的绝对路径（兼容源码态与 frozen 态）。"""
    if getattr(sys, "frozen", False):            # PyInstaller 运行态
        base = Path(getattr(sys, "_MEIPASS", "")) / "tc_generator"
    else:                                        # 源码态
        base = Path(__file__).parent
    return base.joinpath(*parts)
