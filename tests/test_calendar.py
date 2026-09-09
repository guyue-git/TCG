"""单元测试：交易日历与到期日顺延（模板四情形 + 节假日）。"""

from __future__ import annotations

import datetime as dt

import pytest

from tc_generator.trading_calendar import (
    compute_expiry_date, is_a_share_trading_day)


class TestIsTradingDay:
    def test_normal_weekday(self):
        assert is_a_share_trading_day(dt.date(2026, 9, 2))  # 周三

    def test_weekend_not_trading(self):
        assert not is_a_share_trading_day(dt.date(2026, 8, 16))  # 周日

    def test_statutory_holiday_not_trading(self):
        assert not is_a_share_trading_day(dt.date(2026, 10, 1))  # 国庆

    def test_uncovered_year_raises(self):
        # chinese_calendar 数据只到 2026，未覆盖年份必须显式报错而非静默
        with pytest.raises(RuntimeError):
            is_a_share_trading_day(dt.date(2035, 1, 2))


class TestComputeExpiryDate:
    # 模板 docx 明确给出的四情形
    def test_case1_whole_month(self):
        assert compute_expiry_date(dt.date(2026, 7, 10)) == dt.date(2026, 8, 10)

    def test_case2_sunday_defer_one_day(self):
        assert compute_expiry_date(dt.date(2026, 7, 16)) == dt.date(2026, 8, 17)

    def test_case3_month_end_clamp(self):
        assert compute_expiry_date(dt.date(2026, 8, 31)) == dt.date(2026, 9, 30)

    def test_case4_weekend_chain_defer(self):
        # 2026-05-30 周六 -> 05-31 周日 -> 06-01 周一
        assert compute_expiry_date(dt.date(2026, 4, 30)) == dt.date(2026, 6, 1)

    def test_expiry_on_holiday_defers(self):
        # 2026-09-02 整月为 2026-10-02，国庆假期 -> 顺延到节后首个交易日
        # 2026 国庆假期以 chinese_calendar 实际数据为准，这里验证顺延且为交易日
        expiry = compute_expiry_date(dt.date(2026, 9, 2))
        assert expiry > dt.date(2026, 10, 2)
        assert is_a_share_trading_day(expiry)

    def test_december_rollover(self, monkeypatch, tmp_path):
        # 12 月整月应跨年到次年 1 月；2027 超出 chinese_calendar 数据范围，
        # 走 overrides 覆盖规则路径（0.1075 案例同款机制）
        import tc_generator.trading_calendar as cal
        overrides = {
            dt.date(2027, 1, 15): True,   # 周五，强制开市
        }
        monkeypatch.setattr(cal, "_OVERRIDES", overrides)
        expiry = cal.compute_expiry_date(dt.date(2026, 12, 15))
        assert (expiry.year, expiry.month) == (2027, 1)
        assert cal.is_a_share_trading_day(expiry)

    def test_uncovered_year_without_override_raises(self, monkeypatch):
        # 覆盖规则缺失时必须显式报错（正确性优先，绝不静默给错日期）
        import tc_generator.trading_calendar as cal
        monkeypatch.setattr(cal, "_OVERRIDES", {})
        with pytest.raises(RuntimeError):
            cal.compute_expiry_date(dt.date(2026, 12, 15))


class TestPresetYears2027To2030:
    """2027-2030 预设年份（overrides CSV 预填）交易日判断与到期日回归。

    预填口径：lunardate 精确计算农历节日 + 寿星公式清明 + 新放假办法
    （2024-11 修订）连休窗口推算；每年官方通知发布后需核对修正。
    """

    def test_overrides_all_weekdays(self):
        # CSV 只应填工作日节假日；周末由代码规则覆盖
        from tc_generator.trading_calendar import _load_overrides
        overrides = _load_overrides()
        assert overrides, "overrides CSV 为空，预填数据缺失"
        assert all(day.weekday() < 5 for day in overrides)

    @pytest.mark.parametrize("day,expected", [
        (dt.date(2027, 1, 1), False),   # 元旦（周五）
        (dt.date(2027, 1, 4), True),    # 普通周一
        (dt.date(2027, 1, 30), False),  # 春节补班周六 -> 周末规则仍非交易日
        (dt.date(2027, 2, 10), False),  # 春节窗口周三
        (dt.date(2027, 2, 15), True),   # 春节窗口后周一
        (dt.date(2027, 10, 5), False),  # 国庆周二
        (dt.date(2027, 10, 11), True),  # 国庆窗口后周一
        (dt.date(2028, 1, 26), False),  # 2028 春节（周三）
        (dt.date(2028, 9, 29), True),   # 2028 国庆前周五
        (dt.date(2029, 2, 19), False),  # 2029 春节窗口周一
        (dt.date(2029, 6, 18), False),  # 2029 端午借调周一
        (dt.date(2030, 2, 6), False),   # 2030 春节窗口周三
        (dt.date(2030, 9, 12), False),  # 2030 中秋（周四）
        (dt.date(2030, 10, 8), True),   # 2030 国庆窗口后周二
    ])
    def test_preset_trading_day(self, day, expected):
        assert is_a_share_trading_day(day) is expected

    def test_year_not_preset_still_raises(self):
        # 2031 完全未预设 -> 显式报错（回退语义不允许静默放行未预设年份）
        with pytest.raises(RuntimeError):
            is_a_share_trading_day(dt.date(2031, 1, 2))

    @pytest.mark.parametrize("trade_date,expected", [
        (dt.date(2027, 1, 4), dt.date(2027, 2, 4)),    # 春节窗口前落定
        (dt.date(2027, 9, 3), dt.date(2027, 10, 8)),   # 顺延跨国庆窗口
        (dt.date(2028, 9, 2), dt.date(2028, 10, 9)),   # 顺延跨国庆中秋合并
        (dt.date(2029, 1, 15), dt.date(2029, 2, 20)),  # 2/19 窗口内休 -> 顺延
        (dt.date(2030, 1, 6), dt.date(2030, 2, 11)),   # 顺延跨春节窗口+周末
        (dt.date(2030, 8, 12), dt.date(2030, 9, 16)),  # 顺延跨中秋窗口
    ])
    def test_preset_expiry_date(self, trade_date, expected):
        # 预设年份到期日不再抛 RuntimeError，且落定为交易日
        expiry = compute_expiry_date(trade_date)
        assert expiry == expected
        assert is_a_share_trading_day(expiry)
