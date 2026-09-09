"""告警聚合（方案 §5.2/§6.4）.

告警三来源：
1. 运行日志关键字（feed_log_line，如 Edge 渲染失败）
2. 进程异常退出（UI 层调用 add）
3. 服务 A /health 连续超时（UI 层调用 add）

告警不自动清除，保留最近 MAX_ALERTS 条，UI 点击确认后置灰。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Pattern

LOG = logging.getLogger(__name__)

MAX_ALERTS = 50

# (模式, 级别, 标题)——匹配 monitor/服务 A 的日志行（logging 格式：
# "%(asctime)s %(levelname)s %(message)s"，故 ERROR/WARNING 带空格前缀）。
# 内核文案兼容：2026-09-08 起 renderer 输出「渲染内核…」，旧版「Edge …」
# 保留匹配（混合版本部署期两套文案都在）。
ALERT_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"backend .* not reachable", "error",
     "WeChatDataAnalysis 未就绪（请先启动采集器并登录微信）"),
    (r"未产出 PDF", "error", "PDF 生成失败"),
    (r"(渲染内核|Edge 渲染)第 \d+ 次尝试失败", "warn", "PDF 渲染重试失败"),
    (r"目标文件已存在，拒绝覆盖", "error", "PDF 序号冲突（FileExistsError）"),
    (r"推送被拒|[Uu]nauthorized|accepted=false", "warn", "消息推送异常"),
    (r"下单指令解析失败|成交价解析失败", "error",
     "消息解析失败被跳过（确认书未生成，检查消息格式）"),
    (r"未配置对手方映射", "error", "群未配置对手方映射（确认书未生成）"),
    (r"待补投|推送最终失败", "warn", "消息待补投（服务 A 曾不可达）"),
    (r"no available data for year|交易日历无法判断", "error",
     "交易日历数据缺失"),
    (r"\bERROR\b", "warn", "运行日志 ERROR"),
)


@dataclass(frozen=True)
class Alert:
    """单条告警。"""

    level: str      # "error" / "warn"
    title: str
    detail: str
    ts: float
    acknowledged: bool = False


class AlertCenter:
    """告警的生成、去重、确认与查询（非 UI 依赖，便于单测）。"""

    def __init__(self, patterns: tuple[tuple[str, str, str], ...] = ALERT_PATTERNS,
                 max_alerts: int = MAX_ALERTS) -> None:
        self._compiled: list[tuple[Pattern[str], str, str]] = [
            (re.compile(pattern), level, title)
            for pattern, level, title in patterns
        ]
        self._max_alerts = max_alerts
        self._alerts: list[Alert] = []

    def add(self, level: str, title: str, detail: str) -> Alert:
        alert = Alert(level=level, title=title, detail=detail[:200],
                      ts=time.time())
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]
        LOG.log(logging.ERROR if level == "error" else logging.WARNING,
                "告警: %s - %s", title, alert.detail)
        return alert

    def feed_log_line(self, source: str, line: str) -> Alert | None:
        """运行日志行匹配关键字；命中生成告警（同标题 60s 内去重）。"""
        for regex, level, title in self._compiled:
            if regex.search(line):
                if self._is_duplicate(title):
                    return None
                return self.add(level, title, f"[{source}] {line.strip()}")
        return None

    def _is_duplicate(self, title: str, window_s: float = 60.0) -> bool:
        now = time.time()
        return any(a.title == title and now - a.ts < window_s
                   for a in self._alerts)

    def acknowledge(self, alert: Alert) -> None:
        """确认置灰（按对象身份替换）。"""
        self._alerts = [
            a if a != alert else a.__class__(a.level, a.title, a.detail, a.ts,
                                             True)
            for a in self._alerts
        ]

    def acknowledge_all(self) -> None:
        self._alerts = [
            a.__class__(a.level, a.title, a.detail, a.ts, True)
            for a in self._alerts
        ]

    def recent(self, limit: int = 20) -> list[Alert]:
        """最新在前。"""
        return list(reversed(self._alerts[-limit:]))

    def has_unacknowledged(self) -> bool:
        return any(not a.acknowledged for a in self._alerts)
