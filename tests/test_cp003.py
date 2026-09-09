# -*- coding: utf-8 -*-
"""CP003 共用模板（9070/9080/90100）测试：master file / 字段组装 / 路由 / 渲染."""

from __future__ import annotations

import datetime as dt

import pytest

from service_a.classifier import classify
from tc_generator.master_file import get_structure_attributes
from tc_generator.parser import parse_order_line
from tc_generator.renderer import render_tc_html
from tc_generator.tc_builder import build_tc_fields

TRADE_DATE = dt.date(2026, 8, 25)


def _order(structure: str, code: str = "002973", name: str = "侨银股份",
           notional: int = 1_000_000):
    return parse_order_line(
        f"#下单 {structure} {code} {name} 限价 买入 {notional}")


def _fields(structure: str, serial_no: int = 1):
    order = _order(structure)
    attrs = get_structure_attributes("CP003", structure)
    return build_tc_fields(order, 11.73, TRADE_DATE, attrs, "CP003",
                           serial_no, execution_price_text="11.73")


# ---------------- master file ----------------

class TestMasterFile:
    def test_cp003_premium_rates(self):
        assert get_structure_attributes("CP003", "90100").premium_rate \
            == pytest.approx(0.1150)  # docx 11.50%（样例 PDF 11.25% 为旧值）
        assert get_structure_attributes("CP003", "9080").premium_rate \
            == pytest.approx(0.1100)
        assert get_structure_attributes("CP003", "9070").premium_rate \
            == pytest.approx(0.1025)

    def test_cp003_participation_rates(self):
        assert get_structure_attributes("CP003", "90100") \
            .participation_rate == pytest.approx(1.00)
        assert get_structure_attributes("CP003", "9080") \
            .participation_rate == pytest.approx(0.80)
        assert get_structure_attributes("CP003", "9070") \
            .participation_rate == pytest.approx(0.70)

    def test_cp003_common_attributes(self):
        for structure in ("9070", "9080", "90100"):
            attrs = get_structure_attributes("CP003", structure)
            assert attrs.k1_ratio == pytest.approx(0.91)
            assert attrs.k2_ratio == pytest.approx(0.90)
            assert attrs.can_margin is True
            assert attrs.option_type == "Issuer-Terminable Call"
            assert attrs.duration == "1M"

    def test_unregistered_combination_rejected(self):
        with pytest.raises(KeyError):
            get_structure_attributes("CP003", "9081")
        with pytest.raises(KeyError):
            get_structure_attributes("CP002", "9080")


# ---------------- 字段组装 ----------------

class TestBuildFields:
    def test_sample_values_90100(self):
        """样例：1,000,000 / 11.73 → 85,251；编号 CP003_ITC90100_20260825_002。"""
        fields = _fields("90100", serial_no=2)
        assert fields["contract_copies"] == "85,251"
        assert fields["confirmation_number"] == "CP003_ITC90100_20260825_002"
        assert fields["notional_principal"] == "1,000,000"
        assert fields["execution_price"] == "11.73"

    def test_premium_two_decimals(self):
        assert _fields("90100")["premium_rate"] == "11.50%"
        assert _fields("9080")["premium_rate"] == "11.00%"
        assert _fields("9070")["premium_rate"] == "10.25%"

    def test_participation_format(self):
        assert _fields("90100")["participation_rate"] == "100%"
        assert _fields("9080")["participation_rate"] == "80%"
        assert _fields("9070")["participation_rate"] == "70%"

    def test_k_fields_present(self):
        fields = _fields("9070")
        assert fields["k1_ratio"] == "91%"
        assert fields["k2_ratio"] == "90%"
        assert fields["k1_formula"] == "E * K1_ratio"
        assert "91%" in fields["margining_clause"]

    def test_expiry_same_rule(self):
        # 2026/08/25 → 整月 2026/09/25（中秋非交易日）→ 顺延 2026/09/28
        assert _fields("9080")["expiry_date"] == "2026/09/28"

    def test_seller_buyer_fixed(self):
        fields = _fields("9080")
        assert fields["option_seller"] == "Grandly Global Limited"
        assert fields["option_buyer"] == "Party B"


# ---------------- 分类路由（对手方×结构） ----------------

class TestClassifyRoutes:
    def test_cp003_three_structures_route_to_shared_handler(self):
        for structure in ("9070", "9080", "90100"):
            assert classify("//11.73", f"#下单 {structure} 002973 侨银股份 限价 买入 100W",
                            "CP003") == "cp003_order"

    def test_cp001_routes_unchanged(self):
        assert classify("//15.3", "#下单 9080 300017 网宿科技 限价 买入 200W",
                        "CP001") == "cp001_9080_order"
        assert classify("//58.144", "#下单 90100 688825 长鑫科技 限价 买入 160W",
                        "CP001") == "cp001_90100_order"

    def test_unregistered_combination_unsupported(self):
        assert classify("//11.73", "#下单 9081 002973 侨银股份 限价 买入 100W",
                        "CP003") == "unsupported"
        assert classify("//11.73", "#下单 9070 002973 侨银股份 限价 买入 100W",
                        "CP001") == "unsupported"

    def test_legacy_two_arg_signature_kept(self):
        """旧签名（无对手方）维持 CP001 历史行为。"""
        assert classify("//15.3",
                        "#下单 9080 300017 网宿科技 限价 买入 200W") \
            == "cp001_9080_order"

    def test_non_order_or_non_price_unsupported(self):
        assert classify("//到价追保", "#追保 ...", "CP003") == "unsupported"
        assert classify("随便聊聊", "#下单 9080 300017 网宿科技 限价 买入 200W",
                        "CP003") == "unsupported"


# ---------------- 渲染 ----------------

class TestRenderCp003:
    HTML = render_tc_html(_fields("90100"), template_name="tc_cp003.html.j2")

    def test_full_width_colon_label(self):
        assert "Number of contract copies：" in self.HTML

    def test_sample_labels(self):
        for label in ("Underlying Asset:", "Execution Price (“E”):",
                      "Participation Rate (“r”)",
                      "Counterparty additional margining clause"):
            assert label in self.HTML

    def test_font_base_9_48(self):
        assert "body { font-size: 9.48pt; }" in self.HTML

    def test_value_cells_not_bold(self):
        """粗细由 tc_base 承载（normal）；全文 bold 仅标题一处。"""
        assert self.HTML.count("font-weight: bold") == 1  # 仅 h1.tc-title
        assert "font-weight: normal" in self.HTML

    def test_k_subscript_no_kbig(self):
        """CP003 样例免责/结算中的 K 为常规字号，不带 .kbig。"""
        assert 'class="kbig"' not in self.HTML

    def test_option_type_long_style(self):
        """样例中 Option type 值为 9.48pt YaHei 长文本样式（非 UI 9pt）。"""
        assert '<td class="long">Issuer-Terminable Call</td>' in self.HTML

    def test_disclaimer_k_shrunk(self):
        """样例免责声明：11.04 正文内 K 缩小为 9.48（kbody 包裹）。"""
        assert 'class="kbody">K<sub>1</sub></span>' in self.HTML
        assert "K<sub>1</sub>" in self.HTML and "K<sub>2</sub>" in self.HTML
