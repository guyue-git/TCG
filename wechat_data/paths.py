"""运行时路径解析 —— 全部相对 base_dir 锚定，不依赖 cwd，不硬编码盘符。

规则（打包铁律）：
* frozen 态（PyInstaller exe）base_dir = exe 所在目录；
* 源码运行态 base_dir = 本包上级目录（alert 项目根）；
* 所有默认值可用环境变量覆盖（WECHAT_ALERT_RUNTIME_DIR）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_base_dir() -> Path:
    """返回应用锚定目录：frozen 态取 exe 所在目录，否则取项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_runtime_dir() -> Path:
    """运行时可变数据根目录（快照、密钥缓存等）。"""
    override = os.environ.get("WECHAT_ALERT_RUNTIME_DIR", "").strip()
    base = Path(override) if override else get_base_dir() / "runtime"
    return base


def get_snapshot_root() -> Path:
    """解密快照根目录：runtime/wechat_databases/<账号>/。"""
    override = os.environ.get("WECHAT_SNAPSHOT_DIR", "").strip()
    if override:
        return Path(override)
    return get_runtime_dir() / "wechat_databases"


def get_key_store_path() -> Path:
    """按账号隔离的密钥缓存文件路径。"""
    return get_runtime_dir() / "wechat_keys.json"
