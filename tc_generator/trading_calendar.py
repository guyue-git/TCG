"""A股交易日判断与到期日顺延逻辑（基准：中国法定节假日 + 周末）.

规则（用户已裁决）：
* A股交易日 = 周一至周五 且 非法定节假日（调休上班的周末仍不算交易日）。
* chinese_calendar 库数据缺失年份时，回退到 data/trading_day_overrides.csv：
  - CSV 命中日期 -> 按条目强制开市/休市；
  - CSV 已预设该年份（存在该年任意条目）-> 其余工作日默认开市；
  - 该年份完全未预设 -> 显式 RuntimeError，绝不静默给错日期。
  （2027-2030 已按新放假办法推算预填，每年官方通知发布后需核对修正。）
"""

from __future__ import annotations

import csv
import datetime as dt
import logging
from functools import lru_cache
from pathlib import Path

from .resources import package_resource

LOG = logging.getLogger(__name__)

OVERRIDES_FILENAME = "trading_day_overrides.csv"

# 类型：date -> 是否交易日（True=强制开市，False=强制休市）
_OVERRIDES: dict[dt.date, bool] | None = None


def _load_overrides() -> dict[dt.date, bool]:
    """读取 overrides CSV（模块目录 data/ 下），仅加载一次。"""
    global _OVERRIDES
    if _OVERRIDES is not None:
        return _OVERRIDES
    _OVERRIDES = {}
    path = package_resource("data", OVERRIDES_FILENAME)  # frozen 兼容取路径
    if not path.exists():
        return _OVERRIDES
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw_date = (row.get("date") or "").strip()
            raw_flag = (row.get("is_open") or "").strip().lower()
            if not raw_date or raw_flag not in ("0", "1"):
                continue
            try:
                day = dt.datetime.strptime(raw_date, "%Y%m%d").date()
            except ValueError:
                LOG.warning("overrides 行日期格式非法，已跳过: %r", raw_date)
                continue
            _OVERRIDES[day] = raw_flag == "1"
    LOG.info("已加载 %d 条交易日覆盖规则", len(_OVERRIDES))
    return _OVERRIDES


def is_a_share_trading_day(day: dt.date) -> bool:
    """判断是否 A 股交易日：周末（含调休上班）一律不算，法定节假日不算。"""
    overrides = _load_overrides()
    if day in overrides:
        return overrides[day]
    if day.weekday() >= 5:  # 周六/周日：即使调休上班也不是交易日
        return False
    try:
        from chinese_calendar import is_holiday
    except ImportError as exc:
        raise RuntimeError(
            f"交易日历无法判断 {day}（chinese_calendar 库不可用，"
            f"且 {OVERRIDES_FILENAME} 无对应覆盖规则）。") from exc
    try:
        return not is_holiday(day)
    except NotImplementedError as exc:
        # 库数据未覆盖该年份：若 CSV 已预设该年份（存在该年任意条目），
        # 则非周末且无强制休市条目的日期默认开市（节假日已由 CSV 逐日休市）；
        # 完全未预设的年份必须显式报错，绝不静默给错日期。
        if any(override_day.year == day.year for override_day in overrides):
            return True
        raise RuntimeError(
            f"交易日历无法判断 {day}（chinese_calendar 未覆盖该年份，"
            f"且 {OVERRIDES_FILENAME} 未预设 {day.year} 年）。"
            f"请升级 chinesecalendar 或在 CSV 中补充该年份节假日。") from exc


def compute_expiry_date(trade_date: dt.date) -> dt.date:
    """计算到期日：trade_date 向后一个整月（月末截断），非交易日逐日顺延.

    模板四情形：
      情况1: 2026-07-10 -> 2026-08-10（整月）
      情况2: 2026-07-16 -> 2026-08-16 为周日 -> 顺延 2026-08-17
      情况3: 2026-08-31 -> 9 月无 31 号 -> 截断为 2026-09-30
      情况4: 2026-04-30 -> 2026-05-30 为周六 -> 顺延至 2026-06-01
    """
    if trade_date.month == 12:
        next_year, next_month = trade_date.year + 1, 1
    else:
        next_year, next_month = trade_date.year, trade_date.month + 1
    # 月末截断：下月最后一天与「同日」取小
    if next_month == 12:
        last_day = (dt.date(next_year + 1, 1, 1) - dt.timedelta(days=1)).day
    else:
        last_day = (dt.date(next_year, next_month + 1, 1)
                    - dt.timedelta(days=1)).day
    expiry = dt.date(next_year, next_month, min(trade_date.day, last_day))
    while not is_a_share_trading_day(expiry):
        expiry += dt.timedelta(days=1)
    return expiry
