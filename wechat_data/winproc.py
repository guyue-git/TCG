"""Windows 进程内存访问（纯 ctypes，无第三方依赖）。

移植自 WeChatDataAnalysis key_v4.py 的进程访问层：
OpenProcess / ReadProcessMemory / VirtualQueryEx / 进程快照枚举。
仅请求 PROCESS_VM_READ | PROCESS_QUERY_INFORMATION（只读，不杀进程）。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import List, Optional, Set, Tuple

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MAX_PATH = 260
TH32CS_SNAPPROCESS = 0x00000002

_WEIXIN_PROCESS_NAMES = {"weixin.exe", "wechat.exe"}

# 可读页保护属性：READONLY/READWRITE/WRITECOPY/EXECUTE_READ/EXECUTE_READWRITE/
# EXECUTE_WRITECOPY
_READABLE_PROTECT = {0x02, 0x04, 0x06, 0x08, 0x20, 0x40, 0x80}


def is_windows() -> bool:
    return os.name == "nt"


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


if is_windows():
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _OpenProcess = _kernel32.OpenProcess
    _OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _OpenProcess.restype = wintypes.HANDLE

    _ReadProcessMemory = _kernel32.ReadProcessMemory
    _ReadProcessMemory.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _ReadProcessMemory.restype = wintypes.BOOL

    _VirtualQueryEx = _kernel32.VirtualQueryEx
    _VirtualQueryEx.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION), ctypes.c_size_t]
    _VirtualQueryEx.restype = ctypes.c_size_t

    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _CreateToolhelp32Snapshot = _kernel32.CreateToolhelp32Snapshot
    _CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    _Process32FirstW = _kernel32.Process32FirstW
    _Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _Process32FirstW.restype = wintypes.BOOL

    _Process32NextW = _kernel32.Process32NextW
    _Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _Process32NextW.restype = wintypes.BOOL
else:  # 非 Windows：占位，调用时显式报错
    _kernel32 = None


def list_weixin_pids() -> List[Tuple[int, str]]:
    """枚举运行中的微信进程，返回 (pid, 进程名) 列表。"""
    if not is_windows():
        return []
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value or not snapshot:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    result: List[Tuple[int, str]] = []
    try:
        ok = _Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            name = str(entry.szExeFile or "")
            if name.lower() in _WEIXIN_PROCESS_NAMES:
                result.append((int(entry.th32ProcessID), name))
            ok = _Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    return result


def open_process_readonly(pid: int) -> Optional[int]:
    """以只读权限打开进程；失败（如权限不足）返回 None。"""
    if not is_windows():
        return None
    return _OpenProcess(
        PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, int(pid)) or None


PROCESS_TERMINATE = 0x0001


def open_process_terminate(pid: int) -> Optional[int]:
    """以可终止权限打开进程（仅「重启微信取密钥」后备流程使用）。"""
    if not is_windows():
        return None
    return _OpenProcess(PROCESS_TERMINATE, False, int(pid)) or None


def close_process_handle(handle: int) -> None:
    if handle:
        _CloseHandle(handle)


def read_process_memory(handle: int, address: int, size: int) -> Optional[bytes]:
    """读取进程内存；失败返回 None（由调用方决定跳过）。"""
    buffer = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    ok = _ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(bytes_read))
    if not ok:
        return None
    return buffer.raw[: bytes_read.value]


def iter_private_readable_regions(
        handle: int,
        *,
        max_total_bytes: int = 2 * 1024 * 1024 * 1024) -> List[Tuple[int, int]]:
    """枚举 MEM_COMMIT|MEM_PRIVATE 的可读内存区域，返回 (地址, 大小) 列表。"""
    regions: List[Tuple[int, int]] = []
    total = 0
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0
    while True:
        if not _VirtualQueryEx(
                handle, ctypes.c_void_p(address), ctypes.byref(mbi),
                ctypes.sizeof(mbi)):
            break
        region_size = int(mbi.RegionSize or 0)
        if (mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE
                and int(mbi.Protect) in _READABLE_PROTECT and region_size > 0):
            regions.append((int(mbi.BaseAddress), region_size))
            total += region_size
            if total >= max_total_bytes:
                break
        next_address = int(mbi.BaseAddress or 0) + region_size
        if next_address <= address:
            break
        address = next_address
        if address >= 0x7FFFFFFF0000:
            break
    return regions
