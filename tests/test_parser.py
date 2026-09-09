"""单元测试：消息解析（parser）。"""

from __future__ import annotations

import pytest

from tc_generator.parser import (
    OrderInstruction, classify_trigger, extract_execution_price,
    is_execution_price_message, parse_notional_principal, parse_order_line)


class TestParseNotionalPrincipal:
    @pytest.mark.parametrize("raw,expected", [
        ("200W", 2_000_000),
        ("50W", 500_000),
        ("100万", 1_000_000),
        ("3000W", 30_000_000),
        ("500000", 500_000),
        ("2.5W", 25_000),
    ])
    def test_valid(self, raw, expected):
        assert parse_notional_principal(raw) == expected

    @pytest.mark.parametrize("raw", ["", "W", "-50W", "0", "abc", "50.5万W"])
    def test_invalid(self, raw):
        assert parse_notional_principal(raw) is None


class TestParseOrderLine:
    def test_standard_line(self):
        order = parse_order_line("#下单 9080 300017 网宿科技 限价 买入 200W")
        assert order == OrderInstruction(
            template_tag="下单", structure="9080", asset_code="300017",
            asset_name="网宿科技", order_type="限价", direction="买入",
            notional_principal=2_000_000)

    def test_jsonl_double_hash_prefix(self):
        # quote_marker("#") + 原消息("#下单 ...") -> "##下单 ..."
        order = parse_order_line("##下单 9080 300017 网宿科技 限价 买入 200W")
        assert order is not None
        assert order.asset_code == "300017"

    def test_limit_price_detached_8_tokens(self):
        """真实群消息形态：限价与价格被空格拆开（8 token）。

        2026-09-04 实测缺口：旧实现按「恰好 7 token」位置切分，
        限价 6.4 形态整条解析失败 -> 确认书静默不出（4 条只出 1 份）。
        """
        order = parse_order_line("##下单 90100\n002388 新亚制程 限价 6.4 买入 50W")
        assert order == OrderInstruction(
            template_tag="下单", structure="90100", asset_code="002388",
            asset_name="新亚制程", order_type="限价6.4", direction="买入",
            notional_principal=500_000)

    def test_limit_price_attached_7_tokens(self):
        """一期联调形态：限价与价格连写（7 token），保持兼容。"""
        order = parse_order_line("#下单 9080 300476 胜宏科技 限价241.80 买入 50W")
        assert order is not None
        assert (order.asset_code, order.asset_name, order.order_type) == \
            ("300476", "胜宏科技", "限价241.80")

    def test_market_order_real_form(self):
        order = parse_order_line("##下单 90100\n600539 狮头股份 市价 买入 50W")
        assert order is not None
        assert (order.structure, order.asset_name, order.order_type) == \
            ("90100", "狮头股份", "市价")

    def test_avg_price_type(self):
        order = parse_order_line("#下单 9080 300017 网宿科技 均价5分钟 卖出 3000W")
        assert order is not None
        assert (order.asset_name, order.order_type, order.direction) == \
            ("网宿科技", "均价5分钟", "卖出")

    def test_direction_must_be_known_word(self):
        assert parse_order_line("#下单 9080 300017 网宿科技 限价 持有 200W") is None

    @pytest.mark.parametrize("raw", [
        "",
        None,
        "#平仓 9080 300017 网宿科技 限价 买入 200W",   # 非下单标识
        "#追保 9080 300017 网宿科技 限价 买入 200W",
        "#下单 90100 300017 网宿科技 限价 买入 200W",  # 90100 非一期，但应能解析
        "#下单 9080 300017 网宿科技 限价 买入",        # 缺名义本金
        "#下单 9080 300017 网宿科技 限价 买入 badW",   # 名义本金非法
    ])
    def test_unparseable_returns_none(self, raw):
        if raw == "#下单 90100 300017 网宿科技 限价 买入 200W":
            order = parse_order_line(raw)
            assert order is not None and order.structure == "90100"
        else:
            assert parse_order_line(raw) is None


class TestExecutionPriceMessage:
    @pytest.mark.parametrize("content,price", [
        ("//13", 13.0),
        ("//13.5", 13.5),
        ("// 241.80", 241.8),
        ("//0.5", 0.5),
    ])
    def test_valid(self, content, price):
        assert is_execution_price_message(content)
        assert extract_execution_price(content) == pytest.approx(price)

    @pytest.mark.parametrize("content", [
        "", "//", "//abc", "//13x", "13", "/13", "//#下单",
        "//到价追保", "//展期成功",
    ])
    def test_invalid(self, content):
        assert not is_execution_price_message(content)
        assert extract_execution_price(content) is None


class TestClassifyTrigger:
    @pytest.mark.parametrize("content,kind", [
        ("//13", "price"),
        ("//241.80", "price"),
        ("//到价追保", "追保"),
        ("//展期成功", "展期"),
        ("//随便什么", "other"),
        ("#下单 9080", "other"),
    ])
    def test_kinds(self, content, kind):
        assert classify_trigger(content) == kind
