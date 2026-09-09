"""单元测试：字段组装、Excel 取整、编号/序号（tc_builder + master_file）。"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from tc_generator.master_file import get_structure_attributes
from tc_generator.parser import parse_order_line
from tc_generator.tc_builder import (
    build_tc_fields, compute_confirmation_number, compute_contract_copies,
    compute_next_serial, compute_output_filename, format_amount, format_price,
    format_ratio)


class TestFormatAmount:
    @pytest.mark.parametrize("raw,expected", [
        (500000, "500,000"),
        (2000000, "2,000,000"),
        (2068, "2,068"),
        (241.8, "241.80"),
        (15.3, "15.30"),
    ])
    def test_format(self, raw, expected):
        assert format_amount(raw) == expected


class TestFormatPrice:
    """成交价展示：至少两位小数，原始精度超出两位保留（2026-09-08 裁决）.

    背景：//100 曾按旧规则展示 100.0，用户确认应为 100.00。
    """

    @pytest.mark.parametrize("raw,expected", [
        ("100", "100.00"),      # 纯整数补齐两位（本次修复点）
        ("15", "15.00"),
        ("15.3", "15.30"),      # 一位小数补齐两位
        ("241.80", "241.80"),   # 两位保持（9080 样例）
        ("58.144", "58.144"),   # 三位原始精度保留（90100 样例）
    ])
    def test_format(self, raw, expected):
        assert format_price(raw) == expected


class TestFormatRatio:
    @pytest.mark.parametrize("raw,expected", [
        (0.91, "91%"), (0.90, "90%"), (0.1075, "10.75%"),
        (0.80, "80%"), (1.0, "100%"), (0.2075, "20.75%"),
    ])
    def test_format(self, raw, expected):
        assert format_ratio(raw) == expected


class TestComputeContractCopies:
    def test_sample_pdf_value(self):
        # 样例 PDF：500,000 / 241.80 = 2067.82... -> 2,068
        assert compute_contract_copies(500_000, 241.80) == 2068

    def test_excel_half_away_from_zero(self):
        # Excel ROUND(0.5)=1、ROUND(2.5)=3（Python round 是银行家舍入）
        assert compute_contract_copies(5, 10) == 1          # 0.5 -> 1
        assert compute_contract_copies(25, 10) == 3         # 2.5 -> 3
        assert compute_contract_copies(15, 10) == 2         # 1.5 -> 2

    def test_invalid_price(self):
        with pytest.raises(ValueError):
            compute_contract_copies(100, 0)


class TestNumbers:
    def test_confirmation_number(self):
        assert compute_confirmation_number(
            "CP001", "9080", dt.date(2026, 9, 2), 2
        ) == "CP001_ITC9080_20260902_002"

    def test_output_filename(self):
        assert compute_output_filename(
            "CP001", "9080", dt.date(2026, 9, 2), 2
        ) == "TradeConfirm(ITC9080)_CP001_20260902_002.pdf"


class TestComputeNextSerial:
    def test_empty_dir(self, tmp_path):
        assert compute_next_serial(tmp_path, "CP001", "9080",
                                   dt.date(2026, 9, 3)) == 1

    def test_scan_max_plus_one(self, tmp_path):
        for name in ("TradeConfirm(ITC9080)_CP001_20260903_001.pdf",
                     "TradeConfirm(ITC9080)_CP001_20260903_003.pdf"):
            (tmp_path / name).write_bytes(b"%PDF-1.4 fake")
        (tmp_path / "TradeConfirm(ITC9080)_CP001_20260902_009.pdf").write_bytes(b"x")
        assert compute_next_serial(tmp_path, "CP001", "9080",
                                   dt.date(2026, 9, 3)) == 4


class TestMasterFile:
    def test_cp001_9080_defaults(self):
        attrs = get_structure_attributes("CP001", "9080")
        assert attrs.premium_rate == 0.1075
        assert attrs.participation_rate == 0.80
        assert attrs.k1_ratio == 0.91
        assert attrs.k2_ratio == 0.90
        assert attrs.duration == "1M"
        assert attrs.can_margin is True
        assert attrs.option_type == "Issuer-Terminable Call"

    def test_cp001_90100_defaults(self):
        attrs = get_structure_attributes("CP001", "90100")
        assert attrs.premium_rate == 0.1100
        assert attrs.duration == "1M"
        assert attrs.option_type == "Issuer-Terminable Call Option"
        assert attrs.option_strike == 0.90
        assert attrs.k1_ratio is None and attrs.participation_rate is None

    def test_out_of_scope_raises(self):
        # CP003 已于三期注册（9070/9080/90100），未注册对手方与结构仍拒绝
        with pytest.raises(KeyError):
            get_structure_attributes("CP002", "9080")
        with pytest.raises(KeyError):
            get_structure_attributes("CP001", "90999")


class TestBuildTcFields:
    def make_fields(self, execution_price=241.80,
                    trade_date=dt.date(2026, 9, 2), serial=2):
        order = parse_order_line("#下单 9080 300476 胜宏科技 限价 买入 50W")
        attrs = get_structure_attributes("CP001", "9080")
        return order, build_tc_fields(
            order, execution_price, trade_date, attrs, "CP001", serial)

    def test_full_field_set(self):
        _, fields = self.make_fields()
        # 样例 PDF 关键值逐项核对
        assert fields["confirmation_number"] == "CP001_ITC9080_20260902_002"
        assert fields["trade_date"] == "2026/09/02"
        assert fields["underlying_asset"] == "胜宏科技"
        assert fields["underlying_asset_code"] == "300476"
        assert fields["notional_principal"] == "500,000"
        assert fields["execution_price"] == "241.80"
        assert fields["contract_copies"] == "2,068"
        assert fields["currency"] == "CNH"
        assert fields["option_type"] == "Issuer-Terminable Call"
        assert fields["k1_ratio"] == "91%"
        assert fields["k2_ratio"] == "90%"
        assert fields["k1_formula"] == "E * K1_ratio"
        assert fields["participation_rate"] == "80%"
        assert fields["duration"] == "1M"
        assert fields["premium_rate"] == "10.75%"
        assert fields["option_seller"] == "Grandly Global Limited"
        assert fields["option_buyer"] == "Party B"

    def test_margining_clause_literal_n(self):
        _, fields = self.make_fields()
        # 追保条款 n% 为模板字面占位（裁决 #2），比例跟随默认 91%/90%
        assert "n% of Notional Principal" in fields["margining_clause"]
        assert "(91% − n%)" in fields["margining_clause"]
        assert "(90% − n%)" in fields["margining_clause"]

    def test_expiry_field(self):
        # 整月为 2026-10-02，落在国庆假期内应顺延至节后首个交易日
        # （样例 PDF 的 2026/09/28 是追保后 Duration=n.a. 的再出具版，不适用整月规则）
        _, fields = self.make_fields()
        assert fields["expiry_date"] > "2026/10/02"
        from tc_generator.trading_calendar import is_a_share_trading_day
        year, month, day = map(int, fields["expiry_date"].split("/"))
        assert is_a_share_trading_day(dt.date(year, month, day))

    def test_reject_non_9080(self):
        order = parse_order_line("#下单 90100 300017 网宿科技 限价 买入 200W")
        with pytest.raises(ValueError):
            build_tc_fields(order, 15.3, dt.date(2026, 9, 3),
                            get_structure_attributes("CP001", "9080"),
                            "CP001", 1)
