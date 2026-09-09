"""inbox 持久化：服务 A 收到的消息按序追加写入 JSONL.

目的：
* 崩溃重放（重放模式重建队列）；
* 审计（谁在什么时候推了什么）；
* 服务 A 自身不再依赖 wechat_monitor 的 JSONL 格式（解耦）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path

from .message import IncomingMessage

LOG = logging.getLogger(__name__)

MAX_FILENAME_PART_LEN = 100


def safe_name(name: str) -> str:
    """群名转合法文件名（与 monitor 同规则）。"""
    cleaned = re.sub(r'[\\/*?:"<>|\s]+', "_", name or "").strip("_")
    return cleaned[:MAX_FILENAME_PART_LEN] or "unknown_group"


class InboxStore:
    """inbox JSONL 追加存储。"""

    def __init__(self, inbox_dir: Path):
        self.inbox_dir = Path(inbox_dir)
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, message: IncomingMessage) -> Path:
        day = self._day_str(message.create_time)
        return self.inbox_dir / f"inbox_{safe_name(message.group)}_{day}.jsonl"

    @staticmethod
    def _day_str(create_time: int | None) -> str:
        """时间戳 → 日期串；非法值（越界/单位漂移）退回当天不抛出.

        正常路径已由 IncomingMessage.from_dict 合法化，此处兜底防御
        直接构造的消息对象（fromtimestamp 越界会抛 OverflowError/OSError）。
        """
        if create_time:
            try:
                return dt.datetime.fromtimestamp(create_time).strftime("%Y%m%d")
            except (OverflowError, OSError, ValueError):
                LOG.warning("create_time 非法，分桶回退当天: %r", create_time)
        return dt.date.today().strftime("%Y%m%d")

    def append(self, message: IncomingMessage) -> None:
        """追加一条消息；写失败仅记日志（不阻断处理）。"""
        path = self._path_for(message)
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
                fh.flush()
        except OSError as exc:
            LOG.exception("inbox 写入失败 (%s): %s", path.name, exc)

    def load_all(self) -> list[IncomingMessage]:
        """重放用：按文件名与行序读取全部消息。"""
        messages: list[IncomingMessage] = []
        for path in sorted(self.inbox_dir.glob("inbox_*.jsonl")):
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        messages.append(
                            IncomingMessage.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        LOG.warning("inbox 行损坏，已跳过: %s", path.name)
        return messages
