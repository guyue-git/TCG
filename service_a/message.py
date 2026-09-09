"""消息数据结构：monitor 推送的统一载体.

一条推送 = 一次消息事件，包含：
* 主消息（//开头的触发消息）的文本与元数据；
* 其引用的消息（#下单/#追保/#展期指令）的文本（由 monitor 拼接好）；
* 顺序号（monitor 单调递增，用于服务 A 按序处理与幂等）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

LOG = logging.getLogger(__name__)

# create_time 合法区间：2000-01-01 起、当前时间 + 2 天止（容忍时钟漂移）。
# 后端字段异常（单位漂移 ms/us、平台越界）会让 fromtimestamp 抛
# OverflowError/OSError —— 毒消息不得逃逸出提交链路（静默吞消息），
# 故在反序列化层直接置 None，消费方（inbox/handler）退回当天。
_TS_MIN = 946684800
_TS_MAX_SKEW_S = 2 * 86400


@dataclass(frozen=True)
class IncomingMessage:
    """monitor → 服务 A 的消息事件。"""

    seq: int                  # monitor 侧单调递增序号（幂等键）
    group: str                # 群名（映射对手方）
    sender: str | None        # 发送者
    msg_id: int | None        # 微信消息 id
    create_time: int | None   # 秒级时间戳
    content: str              # 主消息文本（如 "//15.3"）
    quoted_content: str = ""  # 引用消息文本（monitor 拼接，如 "#下单 9080 ..."）
    quoted_msg_id: int | None = None

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "group": self.group,
            "sender": self.sender,
            "msg_id": self.msg_id,
            "create_time": self.create_time,
            "content": self.content,
            "quoted_content": self.quoted_content,
            "quoted_msg_id": self.quoted_msg_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IncomingMessage":
        return cls(
            seq=int(data["seq"]),
            group=data.get("group") or "",
            sender=data.get("sender"),
            msg_id=_opt_int(data.get("msg_id")),
            create_time=_valid_timestamp(data.get("create_time")),
            content=data.get("content") or "",
            quoted_content=data.get("quoted_content") or "",
            quoted_msg_id=_opt_int(data.get("quoted_msg_id")),
        )


def _opt_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_timestamp(value) -> int | None:
    """秒级时间戳合法化：越界/异常一律 None（消费方退回当天）。"""
    ts = _opt_int(value)
    if ts is None or not (_TS_MIN <= ts <= time.time() + _TS_MAX_SKEW_S):
        if ts is not None:
            # 仅为可观测性：非法值不该出现，出现了要让日志知道
            LOG.warning("create_time 越界已置空: %r", value)
        return None
    return ts
