"""单元测试：CP001-90100 敲出结构（strike 舍入/板块判定/字段组装）."""

from __future__ import annotations

import datetime as dt

import pytest

from tc_generator.master_file import (board_of, get_structure_attributes,
                                      up_limit_text_for)
from tc_generator.parser import parse_order_line
from tc_generator.tc_builder import (build_tc_fields, compute_strike_price,
                                     format_ratio_explicit, format_strike,
                                     round_down)


class TestRoundDown:
    @pytest.mark.parametrize("raw,digits,expected", [
        (52.3296, 2, 52.32),      # 样例 PDF：58.144×90%
        (46.2897, 3, 46.289),     # 样例 docx：51.433×90%
        (62.28, 2, 62.28),        # 样例 docx：69.2×90%
        (19.999, 2, 19.99),       # 截断非四舍五入
        (17.9991, 3, 17.999),
    ])
    def test_round_down(self, raw, digits, expected):
        assert round_down(raw, digits) == expected


class TestComputeStrikePrice:
    def test_sample_pdf_e_58_144(self):
        # 样例 PDF：E=58.144 ≥ 20 → 2 位小数 → 52.32
        strike, digits = compute_strike_price(58.144, 0.90)
        assert strike == 52.32 and digits == 2

    def test_sample_docx_e_51_433(self):
        # 样例 docx：E=51.433 ≥ 20 → 2 位小数；docx 显示 62.28 对应 E=69.2
        strike, digits = compute_strike_price(51.433, 0.90)
        assert digits == 2

    def test_small_price_three_decimals(self):
        # E < 20 → 3 位小数
        strike, digits = compute_strike_price(19.999, 0.90)
        assert digits == 3 and strike == 17.999

    def test_threshold_boundary(self):
        # E=20 归入 2 位小数（严格小于 20 才用 3 位）
        strike, digits = compute_strike_price(20, 0.90)
        assert digits == 2 and strike == 18.00

    def test_invalid_price(self):
        with pytest.raises(ValueError):
            compute_strike_price(0, 0.90)


class TestBoardOf:
    @pytest.mark.parametrize("code,board", [
        ("920001", "bse"),
        ("688825", "star"),
        ("300476", "chinext"),
        ("301236", "chinext"),
        ("600519", "main"),
        ("000001", "main"),
        ("002594", "main"),
    ])
    def test_board(self, code, board):
        assert board_of(code) == board


class TestUpLimitText:
    def test_star_20_two_days(self):
        # 样例 PDF：688825 科创板 → 20% × 2 日
        text = up_limit_text_for("688825")
        assert "20% up-limit" in text and "two consecutive" in text

    def test_main_10_three_days(self):
        text = up_limit_text_for("600519")
        assert "10% up-limit" in text and "three consecutive" in text

    def test_bse_30_two_days(self):
        text = up_limit_text_for("920001")
        assert "30% up-limit" in text and "two consecutive" in text


class TestStrikeFormat:
    def test_two_decimals(self):
        assert format_strike(52.32, 2) == "52.32"

    def test_three_decimals(self):
        assert format_strike(46.289, 3) == "46.289"


class TestFormatRatioExplicit:
    def test_premium_11(self):
        assert format_ratio_explicit(0.11, 2) == "11.00%"


class TestBuildTcFields90100:
    def make_fields(self, execution_price=58.144,
                    trade_date=dt.date(2026, 8, 24), serial=1,
                    code="688825", name="长鑫科技",
                    price_text="58.144"):
        order = parse_order_line(f"#下单 90100 {code} {name} 限价 买入 160W")
        attrs = get_structure_attributes("CP001", "90100")
        return order, build_tc_fields(
            order, execution_price, trade_date, attrs, "CP001", serial,
            counterparty_name="长鑫科技", execution_price_text=price_text)

    def test_sample_pdf_values(self):
        _, fields = self.make_fields()
        # 样例 PDF 关键值逐项核对
        assert fields["confirmation_number"] == "CP001_ITC90100_20260824_001"
        assert fields["trade_date"] == "2026/08/24"
        assert fields["notional_principal"] == "1,600,000"
        assert fields["contract_copies"] == "27,518"
        assert fields["option_type"] == "Issuer-Terminable Call Option"
        assert fields["option_strike"] == "90%"
        assert fields["duration"] == "1M"
        assert fields["expiry_date"] == "2026/09/24"
        assert fields["premium_rate"] == "11.00%"
        assert fields["execution_price"] == "58.144"
        assert fields["strike_price"] == "52.32"
        assert fields["seller_discretionary_price"] == "At or Below 52.32"
        assert fields["option_seller"] == "Grandly Global Limited"
        assert fields["option_buyer"] == "Party B"

    def test_compulsory_lines(self):
        _, fields = self.make_fields()
        lines = fields["compulsory_settlement_lines"]
        assert lines[0] == ("plain", "Compulsory Settlement Provisions:")
        assert "20% up-limit" in lines[1][1]      # 688825 科创板
        assert lines[2][1] == ("2. Seller discretionary option termination "
                               "price is at 52.32.")

    def test_no_k_fields_or_margining(self):
        # 敲出结构无 K1/K2/追保字段
        _, fields = self.make_fields()
        for absent in ("k1_ratio", "k2_ratio", "participation_rate",
                       "margining_clause", "settlement_lines"):
            assert absent not in fields

    def test_disclaimer_is_termination_price_version(self):
        _, fields = self.make_fields()
        assert "seller discretionary option termination price" \
            in fields["disclaimer"]
        assert "K1" not in fields["disclaimer"]

    def test_price_text_precision(self):
        # trigger 写 //58.1 → 至少两位小数展示 58.10（2026-09-08 裁决）；
        # strike 仍按 float 计算
        _, fields = self.make_fields(execution_price=58.1,
                                     price_text="58.1")
        assert fields["execution_price"] == "58.10"

    def test_bse_asset_uses_30_uplimit(self):
        _, fields = self.make_fields(code="920001", name="北交所标的")
        assert "30% up-limit" in \
            fields["compulsory_settlement_lines"][1][1]

    def test_reject_other_structures(self):
        order = parse_order_line("#下单 9080 300017 网宿科技 限价 买入 200W")
        with pytest.raises(ValueError):
            build_tc_fields(order, 15.3, dt.date(2026, 9, 3),
                            get_structure_attributes("CP001", "90100"),
                            "CP001", 1)