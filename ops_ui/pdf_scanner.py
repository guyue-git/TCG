"""PDF 产出扫描与文件名解析（方案 §5.1/§7.4）.

文件名格式：TradeConfirm(ITC90100)_CP001_20260903_001.pdf
感知方式：全量扫描 + 增量比对（5s 轮询由 UI 层驱动），稳定性判定 =
连续 2 次扫描大小不变才入表（业务侧已是原子替换，此为双保险）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger(__name__)

PDF_GLOB = "TradeConfirm(*)_*_*.pdf"

# TradeConfirm(ITC90100)_CP001_20260903_001.pdf
_NAME_RE = re.compile(
    r"^TradeConfirm\((?P<template>[A-Z0-9]+)\)"
    r"_(?P<counterparty>[A-Za-z0-9]+)"
    r"_(?P<trade_date>\d{8})"
    r"_(?P<serial>\d+)\.pdf$")

_STABLE_ROUNDS = 2


@dataclass(frozen=True)
class PdfEntry:
    """表格行数据（mtime/size 为原始值，展示格式由 UI 层负责）。"""

    path: Path
    file_name: str
    mtime: float
    size: int
    template: str
    counterparty: str
    trade_date: str
    serial: str


def parse_pdf_name(name: str) -> dict[str, str] | None:
    """解析产出文件名；不匹配（如手工文件）返回 None。"""
    match = _NAME_RE.match(name)
    if match is None:
        return None
    return match.groupdict()


class PdfScanner:
    """output/ 目录扫描器：全量列举 + 大小稳定判定。"""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)
        # path -> 上次扫描 size（None 表示已确认稳定，不再跟踪）
        self._pending: dict[Path, int | None] = {}
        self._entries: dict[Path, PdfEntry] = {}

    def scan(self) -> list[PdfEntry]:
        """执行一轮扫描，返回稳定文件列表（mtime 倒序）。"""
        self._refresh()
        entries = sorted(self._entries.values(),
                         key=lambda e: e.mtime, reverse=True)
        return entries

    def _refresh(self) -> None:
        seen: set[Path] = set()
        for file_path in self._output_dir.glob(PDF_GLOB):
            if not file_path.is_file():
                continue
            seen.add(file_path)
            try:
                stat = file_path.stat()
            except OSError as exc:   # 竞态：扫描间隙文件被移动
                LOG.debug("stat 失败，跳过 %s: %s", file_path, exc)
                continue
            self._track(file_path, stat.st_size, stat.st_mtime)
        # 已消失的文件出表
        gone = (set(self._pending) | set(self._entries)) - seen
        for file_path in gone:
            self._pending.pop(file_path, None)
            self._entries.pop(file_path, None)

    def _track(self, file_path: Path, size: int, mtime: float) -> None:
        if file_path in self._entries:   # 已稳定入表：仅当 mtime/size 变化才重判
            entry = self._entries[file_path]
            if entry.size == size:
                return
            self._entries.pop(file_path, None)
        last_size = self._pending.get(file_path)
        if last_size is None or last_size != size:
            # 首见或大小变化：重新计数
            self._pending[file_path] = size
            return
        # 连续两轮大小一致 -> 稳定
        parsed = parse_pdf_name(file_path.name)
        if parsed is None:
            LOG.warning("产出文件名不符合规范，仅显示基础信息: %s",
                        file_path.name)
            parsed = {"template": "-", "counterparty": "-",
                      "trade_date": "-", "serial": "-"}
        self._entries[file_path] = PdfEntry(
            path=file_path,
            file_name=file_path.name,
            mtime=mtime,
            size=size,
            template=parsed["template"],
            counterparty=parsed["counterparty"],
            trade_date=parsed["trade_date"],
            serial=parsed["serial"],
        )
        self._pending[file_path] = None   # 标记稳定

    @property
    def output_dir(self) -> Path:
        return self._output_dir
