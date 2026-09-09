"""微信 4.x 数据库密钥提取 —— 从运行中的微信进程内存恢复 SQLCipher 密钥。

移植自 WeChatDataAnalysis key_v4.py，做两处工程化调整：

1. YARA 特征匹配改为等价的纯 Python 字节扫描（免去 yara-python 编译依赖）：
   特征 = 6 字节任意 + 26 字节固定尾巴，特征起点即 8 字节小端指针，
   指针指向 32 字节密钥候选。
2. 密钥验证改用标准库 hashlib.pbkdf2_hmac（C 实现，与原 pycryptodome
   PBKDF2-HMAC-SHA512 逐字节等价），线程池并行而非进程池（避免 frozen
   打包下的多进程 spawn 陷阱）。

只读访问（PROCESS_VM_READ），绝不写入或终止微信进程。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional

from . import winproc
from .sqlcipher_spec import (KEY_SIZE, PAGE_SIZE, RESERVE_SIZE, SALT_SIZE,
                             derive_sqlcipher_enc_key, derive_mac_key)

LOG = logging.getLogger("wechat_data.key_extractor")

# 每 8MB 一块读取内存区域，块间保留 31 字节重叠避免特征跨块漏检
_REGION_READ_CHUNK = 8 * 1024 * 1024
_PATTERN_OVERLAP = 31
_MAX_CANDIDATE_POINTERS = 400_000

# YARA 特征等价物：{ ?? x6 } + 26 字节固定尾巴
_STUB_FIXED_TAIL = bytes.fromhex(
    "00000000000000000000"        # 10 x 00
    "2000000000000000"            # u64 0x20
    "2f00000000000000"            # u64 0x2f
)
assert len(_STUB_FIXED_TAIL) == 26


def scan_key_stub_pointers(memory_blob: bytes) -> List[int]:
    """在一块内存中扫描特征，返回密钥候选指针（8 字节小端）。"""
    pointers: List[int] = []
    tail = _STUB_FIXED_TAIL
    start = 0
    while True:
        pos = memory_blob.find(tail, start)
        if pos < 0:
            break
        if pos >= 6:
            value = struct.unpack_from("<Q", memory_blob, pos - 6)[0]
            if value:
                pointers.append(value)
        start = pos + 1
        if len(pointers) >= _MAX_CANDIDATE_POINTERS:
            break
    return pointers


def is_potential_key(key: bytes) -> bool:
    """熵与字符分布初筛：过滤全零/重复/可打印文本等非密钥候选。"""
    if len(key) != KEY_SIZE:
        return False
    if len(set(key)) < 15:
        return False
    printable_count = sum(32 <= b <= 126 for b in key)
    if printable_count > 24:
        return False
    return True


def verify_key_candidate(key_bytes: bytes, page1: bytes) -> Optional[str]:
    """验证单个候选（视为 passphrase）：PBKDF2 派生 + page-1 HMAC 校验。

    返回通过校验的 passphrase hex；失败返回 None。
    """
    if len(page1) < PAGE_SIZE:
        return None
    salt = page1[:SALT_SIZE]
    stored_hmac = page1[PAGE_SIZE - 64:PAGE_SIZE]
    enc_key = derive_sqlcipher_enc_key(key_bytes, salt)
    mac_key = derive_mac_key(enc_key, salt)
    # 与 key_v4.is_ok 一致：HMAC 覆盖 [16, PAGE_SIZE-RESERVE+IV)，追加小端页号 1
    mac = hmac.new(mac_key, page1[16:PAGE_SIZE - RESERVE_SIZE + 16],
                   hashlib.sha512)
    mac.update(struct.pack("<I", 1))
    if hmac.compare_digest(mac.digest(), stored_hmac):
        return key_bytes.hex()
    return None


def _collect_candidate_keys(
        pid: int,
        handle: int,
        read_bytes_fn: Optional[Callable[[int, int, int], bytes]]) -> List[bytes]:
    """扫描进程内存收集去重后的密钥候选（32 字节）。"""
    regions = winproc.iter_private_readable_regions(handle)
    pointers: List[int] = []
    seen_pointers: set[int] = set()
    for base_address, region_size in regions:
        chunk_start = 0
        while chunk_start < region_size:
            read_size = min(_REGION_READ_CHUNK + _PATTERN_OVERLAP,
                            region_size - chunk_start)
            if read_size <= _PATTERN_OVERLAP:
                break
            memory = winproc.read_process_memory(
                handle, base_address + chunk_start, read_size)
            if memory:
                for pointer in scan_key_stub_pointers(memory):
                    if pointer not in seen_pointers:
                        seen_pointers.add(pointer)
                        pointers.append(pointer)
                        if len(pointers) >= _MAX_CANDIDATE_POINTERS:
                            break
            chunk_start += _REGION_READ_CHUNK
            if len(pointers) >= _MAX_CANDIDATE_POINTERS:
                break
        if len(pointers) >= _MAX_CANDIDATE_POINTERS:
            break
    LOG.info("特征扫描完成: 候选指针 %d 个（内存区域 %d 块）",
             len(pointers), len(regions))

    unique_keys: List[bytes] = []
    seen_keys: set[bytes] = set()
    for pointer in pointers:
        if read_bytes_fn is not None:
            key = read_bytes_fn(pid, pointer, KEY_SIZE)
        else:
            key = winproc.read_process_memory(handle, pointer, KEY_SIZE) or b""
        if len(key) == KEY_SIZE and key not in seen_keys:
            seen_keys.add(key)
            unique_keys.append(key)
    return unique_keys


def extract_db_key_from_process(
        pid: int,
        probe_db_path: str | Path,
        *,
        read_bytes_fn: Optional[Callable[[int, int, int], bytes]] = None,
        xor_keys: Optional[list[bytes]] = None,
) -> Optional[str]:
    """从指定微信进程内存提取并通过探针库验证 SQLCipher 密钥。

    返回 64 位 hex 密钥（passphrase 语义，可直接用于解密）；失败返回 None。
    """
    probe_path = Path(probe_db_path)
    if not probe_path.is_file():
        LOG.error("密钥验证探针库不存在: %s", probe_path)
        return None
    with probe_path.open("rb") as fh:
        page1 = fh.read(PAGE_SIZE)
    if len(page1) < PAGE_SIZE:
        LOG.error("探针库文件过小，无法验证密钥: %s", probe_path)
        return None

    handle = winproc.open_process_readonly(pid)
    if not handle:
        last_error = winproc._kernel32.GetLastError() if winproc.is_windows() else 0
        LOG.error("无法打开微信进程 pid=%s（错误码 %s）。"
                  "请确认以管理员身份运行，且微信已登录。", pid, last_error)
        return None
    try:
        candidates = _collect_candidate_keys(pid, handle, read_bytes_fn)
    finally:
        winproc.close_process_handle(handle)

    filtered = [k for k in candidates if is_potential_key(k)]
    LOG.info("密钥候选: 原始 %d 个，熵过滤后 %d 个", len(candidates), len(filtered))
    if not filtered:
        return None

    variants = expand_key_variants(filtered, xor_keys)
    if xor_keys:
        LOG.info("验证变体（含 dll_key XOR）: %d 个", len(variants))

    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_verify_with_stop, k, page1, stop)
                   for k in variants]
        for future in futures:
            result = future.result()
            if result:
                stop.set()
                LOG.info("密钥验证通过（值已脱敏，长度=%d 字节）", len(result) // 2)
                return result
    LOG.warning("全部 %d 个验证变体均未通过 page-1 HMAC 验证", len(variants))
    return None


def _verify_with_stop(key: bytes, page1: bytes,
                      stop: threading.Event) -> Optional[str]:
    if stop.is_set():
        return None
    return verify_key_candidate(key, page1)


# ---- Weixin.dll internal_db_key 候选扫描（移植项目 A dll_key_scan.py） ----

_DLL_PATTERN = None


def _dll_pattern():
    """mov rdx, imm64 ×4 + test rax,rax 特征（懒编译）。"""
    global _DLL_PATTERN
    if _DLL_PATTERN is None:
        import re
        _DLL_PATTERN = re.compile(
            rb"\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?"
            rb"\x48\xBA(.{8}).{3,8}?\x48\xBA(.{8}).{3,8}?"
            rb"\x48\x85\xC0", re.DOTALL)
    return _DLL_PATTERN


def _pe_code_sections(data: bytes) -> list[tuple[int, int, int]]:
    """手工解析 PE 头，返回代码段 [(文件偏移, 大小, VA)]（不依赖 pefile）。"""
    import struct
    if len(data) < 0x200 or data[:2] != b"MZ":
        return []
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return []
    coff = e_lfanew + 4
    num_sections = struct.unpack_from("<H", data, coff + 2)[0]
    opt_size = struct.unpack_from("<H", data, coff + 16)[0]
    opt_start = coff + 20
    magic = struct.unpack_from("<H", data, opt_start)[0]
    if magic == 0x20B:                                   # PE32+
        image_base = struct.unpack_from("<Q", data, opt_start + 24)[0]
    else:                                                # PE32
        image_base = struct.unpack_from("<I", data, opt_start + 28)[0]
    sections = []
    sec_start = opt_start + opt_size
    for i in range(num_sections):
        off = sec_start + i * 40
        if off + 40 > len(data):
            break
        characteristics = struct.unpack_from("<I", data, off + 36)[0]
        if not characteristics & 0x20000000:             # IMAGE_SCN_CNT_CODE
            continue
        virtual_address = struct.unpack_from("<I", data, off + 12)[0]
        raw_size = struct.unpack_from("<I", data, off + 16)[0]
        raw_offset = struct.unpack_from("<I", data, off + 20)[0]
        sections.append((raw_offset, raw_size, image_base + virtual_address))
    return sections


def scan_dll_xor_keys(dll_path: str | Path) -> list[bytes]:
    """扫描 Weixin.dll 代码段，提取 32 字节 internal_db_key 候选。

    候选需与进程内存候选做 XOR 后再验证（部分微信 4.x 版本的内存
    密钥 = passphrase XOR dll_key）。
    """
    path = Path(dll_path)
    if not path.is_file():
        return []
    data = path.read_bytes()
    pattern = _dll_pattern()
    keys: list[bytes] = []
    for raw_offset, raw_size, _va in _pe_code_sections(data):
        chunk = data[raw_offset:raw_offset + raw_size]
        for match in pattern.finditer(chunk):
            key = (match.group(1) + match.group(2)
                   + match.group(3) + match.group(4))
            if len(key) == KEY_SIZE and key not in keys:
                keys.append(key)
    LOG.info("Weixin.dll internal_db_key 候选: %d 个（%s）", len(keys), path.name)
    return keys


def _locate_weixin_dll(exe_path: Optional[str] = None) -> Optional[Path]:
    """定位微信安装目录下的 Weixin.dll（exe 同级/版本子目录）。"""
    roots: list[Path] = []
    if exe_path:
        roots.append(Path(exe_path).parent)
    located_exe = _locate_weixin_exe()
    if located_exe:
        roots.append(Path(located_exe).parent)
    for root in roots:
        candidates: list[Path] = [root / "Weixin.dll", root.parent / "Weixin.dll"]
        try:
            candidates.extend(sorted(root.glob("*/Weixin.dll")))
            candidates.extend(sorted(root.glob("*/*/Weixin.dll")))
        except OSError:
            pass
        for candidate in candidates:
            if candidate.is_file():
                return candidate
    return None


def expand_key_variants(filtered_keys: list[bytes],
                        xor_keys: Optional[list[bytes]]) -> list[bytes]:
    """展开验证变体：原始候选 + 与每个 dll_key 异或后的候选（去重保序）。"""
    variants: list[bytes] = []
    seen: set[bytes] = set()
    for key in filtered_keys:
        if key not in seen:
            seen.add(key)
            variants.append(key)
        for xor_key in (xor_keys or []):
            if len(xor_key) != KEY_SIZE:
                continue
            xored = bytes(a ^ b for a, b in zip(key, xor_key))
            if xored not in seen:
                seen.add(xored)
                variants.append(xored)
    return variants


# ---- wx_key Hook 后备（项目 A 生产路径，版本适应性更强） ----

try:
    import wx_key as _wx_key
except ImportError:                              # 可选依赖：wheel 在 vendor/
    _wx_key = None

LAUNCH_WAIT_SECONDS = 30.0
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def wx_key_available() -> bool:
    return _wx_key is not None


def _hook_and_poll(pid: int, timeout_seconds: float) -> Optional[str]:
    """对指定 pid 启动 Hook 并轮询密钥；失败/超时返回 None。"""
    assert _wx_key is not None
    if not _wx_key.initialize_hook(pid):
        LOG.warning("Hook 初始化失败 pid=%s: %s", pid,
                    _wx_key.get_last_error_msg())
        return None
    try:
        start = time.monotonic()
        while time.monotonic() - start < timeout_seconds:
            key_data = _wx_key.poll_key_data()
            if key_data and "key" in key_data:
                key = str(key_data["key"]).strip()
                if key:
                    return key
            while True:
                msg, level = _wx_key.get_status_message()
                if msg is None:
                    break
                if level >= 2:
                    LOG.warning("[wx_key] %s", msg)
            time.sleep(0.1)
        return None
    finally:
        try:
            _wx_key.cleanup_hook()
        except Exception:                        # noqa: BLE001 - 清理必须尽力
            pass


def extract_db_key_via_hook(pid: int, timeout_seconds: float = 10.0) -> Optional[str]:
    """对已运行的微信进程做短时 Hook 探测（不杀进程）。"""
    if _wx_key is None:
        return None
    LOG.info("wx_key Hook 探测（不杀进程）pid=%s", pid)
    return _hook_and_poll(int(pid), timeout_seconds)


def _query_process_image(pid: int) -> Optional[str]:
    if not winproc.is_windows():
        return None
    handle = winproc.open_process_readonly(pid)
    if not handle:
        return None
    try:
        import ctypes as _ct
        buf = _ct.create_unicode_buffer(winproc.MAX_PATH * 2)
        length = _ct.c_ulong(winproc.MAX_PATH * 2)
        kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
        if not kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, _ct.byref(length)):
            return None
        return buf.value
    except Exception:                            # noqa: BLE001
        return None
    finally:
        winproc.close_process_handle(handle)


def _kill_all_weixin() -> None:
    """查杀全部微信进程（项目 A 同款行为；仅取密钥后备流程使用）。"""
    import ctypes as _ct
    for pid, name in winproc.list_weixin_pids():
        handle = winproc.open_process_terminate(pid)
        if not handle:
            LOG.warning("无法终止微信进程 pid=%s（权限不足）", pid)
            continue
        try:
            _ct.windll.kernel32.TerminateProcess(handle, 0)
            LOG.warning("已终止微信进程 pid=%s（%s）——将自动重启取密钥", pid, name)
        finally:
            winproc.close_process_handle(handle)
    time.sleep(1.5)


def _launch_weixin(exe_path: str,
                   timeout_seconds: float = LAUNCH_WAIT_SECONDS) -> Optional[int]:
    """启动微信并等待主进程出现，返回新 pid。"""
    import subprocess
    old_pids = {pid for pid, _ in winproc.list_weixin_pids()}
    try:
        subprocess.Popen(
            [exe_path], cwd=str(Path(exe_path).parent),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    except OSError as exc:
        LOG.error("启动微信失败: %s", exc)
        return None
    start = time.monotonic()
    while time.monotonic() - start < timeout_seconds:
        for pid, _name in winproc.list_weixin_pids():
            if pid not in old_pids:
                LOG.info("微信已重启 pid=%s（等待登录后将自动捕获密钥）", pid)
                return pid
        time.sleep(0.5)
    LOG.error("重启后未发现新的微信进程")
    return None


def query_process_image(pid: int) -> Optional[str]:
    """查询进程主程序镜像路径（用于定位 Weixin.dll）。"""
    return _query_process_image(pid)


def _locate_weixin_exe() -> Optional[str]:
    """微信未运行时定位主程序：App Paths / 卸载表 / 常见安装路径。"""
    import os
    try:
        import winreg
    except ImportError:
        return None
    candidates: list[str] = []

    def add(path_value: Optional[str]) -> None:
        raw = str(path_value or "").strip().strip('"')
        if raw.lower().endswith(".exe") and Path(raw).is_file() and \
                raw not in candidates:
            candidates.append(raw)

    # 1) App Paths 注册表（HKLM/HKCU）
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Weixin.exe",
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WeChat.exe"):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    add(winreg.QueryValueEx(key, "")[0])
            except OSError:
                continue

    # 2) 卸载表 InstallLocation / DisplayIcon
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for root_sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                         r"SOFTWARE\WOW6432Node\Microsoft\Windows"
                         r"\CurrentVersion\Uninstall"):
            try:
                root_key = winreg.OpenKey(hive, root_sub)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root_key, i)
                    except OSError:
                        break
                    i += 1
                    if not any(tag in sub_name.lower()
                               for tag in ("wechat", "weixin", "微信")):
                        continue
                    try:
                        with winreg.OpenKey(root_key, sub_name) as key:
                            for value_name in ("InstallLocation", "DisplayIcon"):
                                try:
                                    value, _ = winreg.QueryValueEx(key, value_name)
                                except OSError:
                                    continue
                                raw = str(value).split(",")[0].strip().strip('"')
                                if raw.lower().endswith(".exe"):
                                    add(raw)
                                elif raw:
                                    for exe in ("Weixin.exe", "WeChat.exe"):
                                        add(str(Path(raw) / exe))
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(root_key)

    # 3) 常见安装路径
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env, "").strip()
        if not base:
            continue
        for vendor_dir in ("Tencent\\Weixin", "Tencent\\WeChat",
                           "Tencent\\微信", "WeChat"):
            for exe in ("Weixin.exe", "WeChat.exe"):
                add(str(Path(base) / vendor_dir / exe))
    return candidates[0] if candidates else None


def extract_db_key_via_relaunch(timeout_seconds: float = 120.0) -> Optional[str]:
    """完整后备：杀掉并重启微信，等待登录后 Hook 捕获密钥。

    与项目 A 生产流程一致：密钥派生发生在登录时刻，因此对常驻进程
    Hook 不一定能捕获，需要重启后在新进程的登录窗口等待。
    """
    if _wx_key is None:
        return None
    exe_candidates = []
    for pid, _name in winproc.list_weixin_pids():
        image = _query_process_image(pid)
        if image and image.lower().endswith(".exe"):
            exe_candidates.append(image)
    exe_path = exe_candidates[0] if exe_candidates else _locate_weixin_exe()
    if not exe_path:
        LOG.error("无法定位微信主程序：微信未运行，且注册表/常见路径未命中。"
                  "请先启动一次微信，或在 config.ini 配置安装路径。")
        return None
    LOG.warning("进入「重启微信取密钥」流程：微信将短暂退出并重新启动，"
                "如弹出登录窗口请完成登录（最长等待 %.0f 秒）", timeout_seconds)
    if exe_candidates:
        _kill_all_weixin()
    new_pid = _launch_weixin(exe_path)
    if not new_pid:
        return None
    return _hook_and_poll(new_pid, timeout_seconds)
