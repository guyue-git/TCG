"""微信安装 / 数据目录 / 当前登录账号检测。

移植自 WeChatDataAnalysis wechat_detection.py 的核心判定逻辑：
* 数据根目录发现（xwechat_files / WeChat Files 等候选名，浅层扫描）
* 账号目录识别（含 db_storage 子目录）
* 当前登录账号：key_info.db 最近活动时间优先，global_config 回退
  （global_config 可能残留上一个登录账号，不能单独作为证据）
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import winproc

LOG = logging.getLogger("wechat_data.detector")

COMMON_WECHAT_PATTERNS = [
    "xwechat_files", "WeChat Files", "Weixin Files", "wechat_files",
    "wechatMSG", "WeChat", "Weixin", "微信",
]
_INTERNAL_DIR_NAMES = {"all_users", "all users", "applet", "wmpf"}
_DRIVE_LETTERS = ("C:", "D:", "E:", "F:", "G:")
_SCAN_SKIP_NAMES = {
    "$recycle.bin", "$winreagent", "config.msi", "documents and settings",
    "intel", "onedrivetemp", "perflogs", "program files",
    "program files (x86)", "programdata", "recovery",
    "system volume information", "windows", "windows.old",
}


class DetectionError(RuntimeError):
    """检测失败（附中文可读原因）。"""


def is_account_dir(path: Path) -> bool:
    """账号目录 = 含 db_storage 子目录的 wxid 目录。"""
    try:
        return path.is_dir() and (path / "db_storage").is_dir()
    except OSError:
        return False


def is_internal_dir_name(name: str) -> bool:
    return str(name or "").strip().lower() in _INTERNAL_DIR_NAMES


def _safe_iter_subdirs(directory: Path) -> List[Path]:
    try:
        with os.scandir(directory) as entries:
            return [Path(entry.path) for entry in entries if entry.is_dir()]
    except (PermissionError, OSError):
        return []


def _is_candidate_dir_name(name: str) -> bool:
    normalized = str(name or "").strip().lower()
    if not normalized:
        return False
    return any(pattern.lower() in normalized for pattern in COMMON_WECHAT_PATTERNS)


def _contains_account_dirs(path: Path, *, depth: int) -> bool:
    if is_account_dir(path):
        return True
    if depth <= 0:
        return False
    return any(
        _contains_account_dirs(child, depth=depth - 1)
        for child in _safe_iter_subdirs(path))


def _build_scan_roots() -> List[Path]:
    """构建浅层扫描根：用户目录族 + 各盘符一层子目录。"""
    roots: List[Path] = []
    seen: set[str] = set()

    def add(path_value: Path) -> None:
        key = str(path_value).lower()
        if key not in seen:
            seen.add(key)
            roots.append(path_value)

    home = Path.home()
    add(home)
    add(home / "Documents")
    add(home / "Desktop")
    add(home / "Downloads")
    user_profile = os.environ.get("USERPROFILE", "").strip()
    if user_profile:
        add(Path(user_profile) / "Documents")
    for drive in _DRIVE_LETTERS:
        drive_root = Path(drive + os.sep)
        if not drive_root.exists():
            continue
        add(drive_root)
        for child in _safe_iter_subdirs(drive_root):
            if child.name.strip().lower() in _SCAN_SKIP_NAMES:
                continue
            add(child)
    return roots


def discover_data_roots(explicit_root: str = "") -> List[Path]:
    """发现微信数据根目录（显式指定优先，其次环境变量，最后浅层扫描）。"""
    explicit = str(explicit_root or "").strip()
    env_root = os.environ.get("WECHAT_DATA_ROOT", "").strip()
    candidates: List[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if env_root and Path(env_root) not in candidates:
        candidates.append(Path(env_root))
    for scan_root in _build_scan_roots():
        if _is_candidate_dir_name(scan_root.name) and _contains_account_dirs(
                scan_root, depth=2):
            candidates.append(scan_root)
            continue
        for child in _safe_iter_subdirs(scan_root):
            if _is_candidate_dir_name(child.name) and _contains_account_dirs(
                    child, depth=2):
                candidates.append(child)

    unique: List[Path] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            continue
        if resolved.exists() and str(resolved).lower() not in seen:
            seen.add(str(resolved).lower())
            unique.append(resolved)
    return unique


def list_account_dirs(data_root: Path) -> List[Path]:
    """列出数据根下的账号目录（跳过 all_users/Applet/WMPF）。"""
    if is_account_dir(data_root):
        return [data_root]
    accounts = [
        child for child in _safe_iter_subdirs(data_root)
        if child.name.lower() not in _INTERNAL_DIR_NAMES and is_account_dir(child)
    ]
    accounts.sort(key=lambda p: p.name.lower())
    return accounts


def parse_global_config(data_root: Path) -> Optional[Dict[str, Optional[str]]]:
    """解析 all_users/config/global_config（AES-128-CFB + MMKV varint）。

    移植自 WeChatDataAnalysis，密钥固定为 b"xwechat_crypt_key"[:16]。
    """
    config_path = data_root / "all_users" / "config" / "global_config"
    try:
        if not config_path.is_file():
            return None
        full_data = config_path.read_bytes()
        if len(full_data) <= 4:
            return None

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
        try:                    # cryptography>=49：CFB 移入 decrepit
            from cryptography.hazmat.decrepit.ciphers.modes import CFB
        except ImportError:     # 旧版本仍在原位置
            from cryptography.hazmat.primitives.ciphers.modes import CFB
        key = b"xwechat_crypt_key"[:16]
        cipher = Cipher(algorithms.AES(key), CFB(b"\x00" * 16),
                        backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(full_data[4:]) + decryptor.finalize()
    except Exception as exc:
        LOG.debug("解析 global_config 失败: %s", exc)
        return None

    def decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
        result = 0
        shift = 0
        while offset < len(data):
            byte = data[offset]
            offset += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                break
            shift += 7
        return result, offset

    def extract_mmkv_string(data: bytes, key_str: str) -> Optional[str]:
        key_bytes = key_str.encode("utf-8")
        idx = data.find(key_bytes)
        if idx < 0:
            return None
        offset = idx + len(key_bytes)
        try:
            _value_len, offset = decode_varint(data, offset)
            if offset >= len(data):
                return None
            str_len, offset = decode_varint(data, offset)
            if str_len > 0 and offset + str_len <= len(data):
                return data[offset:offset + str_len].decode("utf-8", errors="ignore")
        except Exception:
            return None
        return None

    wxid = extract_mmkv_string(decrypted, "mmkv_key_user_name")
    nickname = extract_mmkv_string(decrypted, "mmkv_key_nick_name")
    if wxid or nickname:
        return {"wxid": wxid, "nickname": nickname}
    return None


def detect_current_account(data_root: Path) -> Dict[str, object]:
    """判定当前登录账号：key_info.db 最近活动时间优先，global_config 回退。

    返回 {account, wxid, nickname, source, latest_time}；未判定出返回 account=""。
    """
    current_account = ""
    latest_time: Optional[float] = None

    possible_login_dirs = [
        data_root / "all_users" / "login",
        data_root / "login",
    ]
    for child in _safe_iter_subdirs(data_root):
        possible_login_dirs.append(child / "all_users" / "login")
        possible_login_dirs.append(child / "login")

    for login_dir in dict.fromkeys(possible_login_dirs):
        if not login_dir.is_dir():
            continue
        for item in _safe_iter_subdirs(login_dir):
            key_info_path = item / "key_info.db"
            if not key_info_path.is_file():
                continue
            try:
                activity_paths = [key_info_path,
                                  key_info_path.with_name(key_info_path.name + "-wal")]
                file_time = max(
                    p.stat().st_mtime for p in activity_paths if p.is_file())
            except OSError:
                continue
            if latest_time is None or file_time > latest_time:
                latest_time = file_time
                current_account = item.name

    parsed_config = parse_global_config(data_root)
    config_wxid = str((parsed_config or {}).get("wxid") or "").strip()
    config_nickname = str((parsed_config or {}).get("nickname") or "").strip()

    if current_account:
        source = "key_info_mtime"
        wxid = current_account
    elif config_wxid:
        source = "global_config"
        wxid = config_wxid
    else:
        source = ""
        wxid = ""
    wxid = canonical_account_name(wxid)

    return {
        "account": wxid,
        "wxid": wxid,
        "nickname": config_nickname,
        "source": source,
        "latest_time": latest_time,
        "global_config_wxid": canonical_account_name(config_wxid),
    }


def find_running_weixin() -> List[Tuple[int, str]]:
    """返回运行中的微信进程列表；非 Windows 或未启动返回空。"""
    if not winproc.is_windows():
        return []
    return winproc.list_weixin_pids()


_WXID_DIR_RE = re.compile(r"^wxid_[A-Za-z0-9_]+$")
_WXID_INSTALL_SUFFIX_RE = re.compile(r"^(wxid_.+)_[0-9a-fA-F]{4}$")


def canonical_account_name(name: str) -> str:
    """规范化账号身份：剥掉安装后缀（wxid_xxx_03b8 -> wxid_xxx）。

    微信 4.x 的登录目录/数据目录可能带 4 位十六进制安装后缀，且同名
    变体会并存——账号身份必须以裸 wxid 为准，否则巡检会误判「账号切换」。
    """
    value = str(name or "").strip()
    match = _WXID_INSTALL_SUFFIX_RE.fullmatch(value)
    return match.group(1) if match else value
