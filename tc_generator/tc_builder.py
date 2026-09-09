"""Trade Confirmation 字段组装与编号/序号规则.

* contract copies = Excel 语义 ROUND(Notional / E, 0)（四舍五入远离零）。
* Confirmation number = CP{对手方}_ITC{结构}_{YYYYMMDD}_{NNN}（以样例 PDF 实际格式
  CP001_ITC9080_20260902_002 为准；docx 示例中 ITC_9080 多一个下划线，视为笔误）。
* 序号无状态：扫描当日产出文件取最大序号 + 1（方案评审决策 3）。
* 90100 结构：Strike price = Round Down(E × Option strike)（E<20 → 3 位小数，
  E≥20 → 2 位小数）；无 K1/K2/追保条款；免责声明为敲出价版本。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from .master_file import (STRIKE_DIGITS_NORMAL, STRIKE_DIGITS_SMALL,
                          STRIKE_SMALL_PRICE_THRESHOLD, StructureAttributes,
                          board_of)
from .parser import OrderInstruction
from .trading_calendar import compute_expiry_date

OUTPUT_FILENAME_TEMPLATE = "TradeConfirm(ITC{structure})_{cp}_{yyyymmdd}_{serial:03d}.pdf"
SERIAL_PATTERN_TEMPLATE = (
    r"^TradeConfirm\(ITC{structure}\)_{cp}_{yyyymmdd}_(\d{{3}})\.pdf$")

HEADER_BRAND = "GRANDLY GLOBAL LIMITED"
TITLE = "Confirmation of Over-the-Counter Option Trade"
NARRATIVE_PRE = (
    "This Confirmation is made between the following Party A and Party B on "
    "the following Trade Date. This document is intended to confirm the terms "
    "and conditions of the option trade entered into by "
    "Grandly Global Limited ('Party A') and ")
NARRATIVE_POST = " ('Party B')."
DISCLAIMER = (
    "*If spot price of the underlying asset is observed to be at or below the "
    'Option observation price 1 ("K1") at any point, then the option seller '
    "(Party A) has the sole discretion to terminate the trade without payment, "
"without prior notice to the option buyer (Party B).")
# 结算段落按样例 PDF 的缩进结构拆行：plain=顶格 / indent=a)b) 项 / bullet=Wingdings 项目符号项
SETTLEMENT_LINES: list[tuple[str, str]] = [
    ("plain", "Settlement Calculation"),
    ("plain", '(i) the Final Settlement Price ("S") shall be determined as follows:'),
    ("indent",
     "a) if stock price reaches K1: Issuer shall unwind the position on best "
     "effort basis, and S = min (K1, unwind price)"),
    ("indent", "b) if stock price reaches K2: S = K2"),
    ("plain",
     '(ii) With respect to Participation Rate ("r"), Final Settlement Price '
     '("S") of the Underlying Asset, Strike Price ("K"), Execution Price ("E") '
     'and Option observation price 2 ("K2"), the Final Settlement Amount per '
     "contract copy is calculated as follows:"),
    ("bullet", "S- K2, in the case of K2 < S ≤ E;"),
    ("bullet", "(E- K2) + r*(S-E), in the case of E < S."),
]
MARGINING_CLAUSE_TEMPLATE = (
    "Prior to stock price reaching K1, Counterparty may, if so acknowledged "
    "and agreed to by the Issuer, pay an additional premium equal to n% of "
    "Notional Principal, in which case Issuer shall adjust K1_ratio to "
    "({k1_pct:.0f}% − n%) and K2_ratio to ({k2_pct:.0f}% − n%), respectively.")

# 90100 免责声明（样例 PDF 文字：敲出价版本，无 K1/K2）
DISCLAIMER_90100 = (
    "*If spot price of the underlying asset is observed to be at or below the "
    "seller discretionary option termination price at any point, then the "
    "option seller (Party A) has the sole discretion to terminate the trade "
    "without payment, without prior notice to the option buyer (Party B).")

# CP003 共用模板覆盖的结构（CP003 - 9070,9080,90100.docx：三结构同模板）
CP003_SHARED_STRUCTURES = ("9070", "9080", "90100")


def format_amount(amount: float | int) -> str:
    """千分位格式化：500000 -> '500,000'；241.80 -> '241.80'。

    两位小数为样例 PDF 的金额展示基准；整数不带小数（500000 ->
    '500,000'）。更高精度（如 58.144）由 _format_execution_price 走
    trigger 原始文本路径，不经过本函数。
    """
    if float(amount) == int(amount):
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


def format_ratio(ratio: float) -> str:
    """比率展示：0.91 -> '91%'；0.1075 -> '10.75%'。

    整数百分比若样例以两位小数展示（90100 费率 11.00%），由调用方
    传 format_ratio_explicit 决定；此处保持常规格式。
    """
    pct = round(ratio * 100, 4)
    text = f"{pct:g}"
    return f"{text}%"


def format_ratio_explicit(ratio: float, decimals: int) -> str:
    """固定小数位比率展示：0.11, 2 -> '11.00%'（90100 样例费率格式）。"""
    return f"{ratio * 100:.{decimals}f}%"


def format_price(price_text: str) -> str:
    """价格展示：至少两位小数，原始精度超出两位时保留.

    样例规则：9080 成交价 241.80（//241.80）、90100 成交价 58.144
    （//58.144，原始三位精度）。
    2026-09-08 用户裁决：不足两位的补齐两位（//100 -> 100.00、
    //15.3 -> 15.30），替代旧「纯整数按一位小数（15.0）」规则。
    """
    text = (price_text or "").strip()
    if "." not in text:
        return f"{text}.00"
    whole, frac = text.split(".", 1)
    if len(frac) < 2:
        return f"{text}0"
    return text


def compute_contract_copies(notional_principal: int,
                            execution_price: float) -> int:
    """Excel 语义 ROUND(Notional / E, 0)：正数四舍五入远离零。"""
    if execution_price <= 0:
        raise ValueError(f"execution_price 必须为正数，得到 {execution_price}")
    return math.floor(notional_principal / execution_price + 0.5)


def compute_confirmation_number(counterparty_id: str, structure: str,
                                trade_date: dt.date, serial_no: int) -> str:
    """生成确认书编号，如 CP001_ITC9080_20260902_002。"""
    return (f"{counterparty_id}_ITC{structure}_"
            f"{trade_date:%Y%m%d}_{serial_no:03d}")


def compute_output_filename(counterparty_id: str, structure: str,
                            trade_date: dt.date, serial_no: int) -> str:
    """生成产出 PDF 文件名，如 TradeConfirm(ITC9080)_CP001_20260902_002.pdf。"""
    return OUTPUT_FILENAME_TEMPLATE.format(
        structure=structure, cp=counterparty_id,
        yyyymmdd=f"{trade_date:%Y%m%d}", serial=serial_no)


def compute_next_serial(output_dir: Path, counterparty_id: str,
                        structure: str, trade_date: dt.date) -> int:
    """扫描当日产出文件取最大序号；无产出从 001 开始。"""
    pattern = re.compile(SERIAL_PATTERN_TEMPLATE.format(
        structure=re.escape(structure), cp=re.escape(counterparty_id),
        yyyymmdd=f"{trade_date:%Y%m%d}"))
    max_serial = 0
    if output_dir.exists():
        for path in output_dir.iterdir():
            match = pattern.match(path.name)
            if match:
                max_serial = max(max_serial, int(match.group(1)))
    return max_serial + 1


def round_down(value: float, digits: int) -> float:
    """Round Down（向下截断，非四舍五入）；Decimal 规避二进制浮点陷阱。"""
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_DOWN))


def format_strike(strike: float, digits: int) -> str:
    """strike 展示：按精度位数输出（52.32 / 46.289），千分位无意义故不加。"""
    return f"{strike:.{digits}f}"


def compute_strike_price(execution_price: float,
                         option_strike: float) -> tuple[float, int]:
    """90100 Strike price：Round Down(E × Option strike)。

    E < 20 → 3 位小数；E ≥ 20 → 2 位小数。
    返回 (strike, 保留位数)。
    """
    if execution_price <= 0:
        raise ValueError(f"execution_price 必须为正数，得到 {execution_price}")
    digits = (STRIKE_DIGITS_SMALL if execution_price
              < STRIKE_SMALL_PRICE_THRESHOLD else STRIKE_DIGITS_NORMAL)
    raw = execution_price * option_strike
    return round_down(raw, digits), digits


def build_compulsory_settlement_lines(strike_text: str,
                                      up_limit_text: str) -> list[tuple[str, str]]:
    """90100 强制结算条款两行（kind 与 9080 结算段落同构，indent 复用缩进样式）。"""
    return [
        ("plain", "Compulsory Settlement Provisions:"),
        ("indent", f"1. {up_limit_text}"),
        ("indent", "2. Seller discretionary option termination price is at "
                   f"{strike_text}."),
    ]


def build_tc_fields(order: OrderInstruction, execution_price: float,
                    trade_date: dt.date, attrs: StructureAttributes,
                    counterparty_id: str, serial_no: int,
                    counterparty_name: str = "COUNTERPARTY",
                    execution_price_text: str | None = None) -> dict[str, str]:
    """组装字段（键名与确认书模板行一致）；按对手方×结构分派。

    CP003 的 9070/9080/90100 共用一套观察价式字段（用户裁决），优先分派；
    CP001 维持 9080 / 90100 各自分支。
    execution_price_text：trigger 消息中成交价的原始文本（保留用户书写
    精度，如 "241.80"/"58.144"）；缺省时按 float 常规格式化。
    """
    if counterparty_id == "CP003" \
            and order.structure in CP003_SHARED_STRUCTURES:
        return _build_tc_fields_cp003(
            order, execution_price, trade_date, attrs, counterparty_id,
            serial_no, counterparty_name, execution_price_text)
    if order.structure == "9080":
        return _build_tc_fields_9080(
            order, execution_price, trade_date, attrs, counterparty_id,
            serial_no, counterparty_name, execution_price_text)
    if order.structure == "90100":
        return _build_tc_fields_90100(
            order, execution_price, trade_date, attrs, counterparty_id,
            serial_no, counterparty_name, execution_price_text)
    raise ValueError(f"不支持的结构，得到 {order.structure}")


def _format_execution_price(execution_price: float,
                            execution_price_text: str | None) -> str:
    """成交价展示：优先用 trigger 原始文本精度，缺省退回千分位格式。"""
    if execution_price_text:
        return format_price(execution_price_text)
    return format_amount(execution_price)


def _build_tc_fields_9080(order: OrderInstruction, execution_price: float,
                          trade_date: dt.date, attrs: StructureAttributes,
                          counterparty_id: str, serial_no: int,
                          counterparty_name: str,
                          execution_price_text: str | None) -> dict[str, str]:
    """9080（观察价结构）22 字段组装。"""
    if attrs.k1_ratio is None or attrs.k2_ratio is None \
            or attrs.participation_rate is None:
        raise ValueError("9080 结构缺少 K1/K2/Participation 默认参数")
    copies = compute_contract_copies(order.notional_principal, execution_price)
    expiry_date = compute_expiry_date(trade_date)
    trade_date_text = f"{trade_date:%Y/%m/%d}"
    return {
        "brand": HEADER_BRAND,
        "title": TITLE,
        "narrative_pre": NARRATIVE_PRE,
        "narrative_cp": counterparty_name,
        "narrative_post": NARRATIVE_POST,
        "confirmation_number": compute_confirmation_number(
            counterparty_id, order.structure, trade_date, serial_no),
        "trade_date": trade_date_text,
        "underlying_asset": order.asset_name,
        "underlying_asset_code": order.asset_code,
        "notional_principal": format_amount(order.notional_principal),
        "execution_price": _format_execution_price(
            execution_price, execution_price_text),
        "contract_copies": format_amount(copies),
        "currency": "CNH",
        "option_type": attrs.option_type,
        "k1_ratio": format_ratio(attrs.k1_ratio),
        "k2_ratio": format_ratio(attrs.k2_ratio),
        "k1_formula": "E * K1_ratio",
        "k2_formula": "E * K2_ratio",
        "participation_rate": format_ratio(attrs.participation_rate),
        "duration": attrs.duration,
        "expiry_date": f"{expiry_date:%Y/%m/%d}",
        "premium_rate": format_ratio(attrs.premium_rate),
        "margining_clause": MARGINING_CLAUSE_TEMPLATE.format(
            k1_pct=attrs.k1_ratio * 100, k2_pct=attrs.k2_ratio * 100),
        "settlement_lines": SETTLEMENT_LINES,
        "option_seller": "Grandly Global Limited",
        "option_buyer": "Party B",
        "disclaimer": DISCLAIMER,
    }


def _build_tc_fields_cp003(order: OrderInstruction, execution_price: float,
                           trade_date: dt.date, attrs: StructureAttributes,
                           counterparty_id: str, serial_no: int,
                           counterparty_name: str,
                           execution_price_text: str | None) -> dict[str, str]:
    """CP003 共用模板（9070/9080/90100）字段组装。

    字段集与 9080 观察价式一致（含 K1/K2 与追保条款，无 strike 行，
    以 CP003 样例 PDF 为准）；premium 按文档两位小数展示（11.00%/11.50%/
    10.25%），participation 常规格式（100%/80%/70%）。
    """
    if attrs.k1_ratio is None or attrs.k2_ratio is None \
            or attrs.participation_rate is None:
        raise ValueError("CP003 结构缺少 K1/K2/Participation 默认参数")
    copies = compute_contract_copies(order.notional_principal, execution_price)
    expiry_date = compute_expiry_date(trade_date)
    trade_date_text = f"{trade_date:%Y/%m/%d}"
    return {
        "brand": HEADER_BRAND,
        "title": TITLE,
        "narrative_pre": NARRATIVE_PRE,
        "narrative_cp": counterparty_name,
        "narrative_post": NARRATIVE_POST,
        "confirmation_number": compute_confirmation_number(
            counterparty_id, order.structure, trade_date, serial_no),
        "trade_date": trade_date_text,
        "underlying_asset": order.asset_name,
        "underlying_asset_code": order.asset_code,
        "notional_principal": format_amount(order.notional_principal),
        "execution_price": _format_execution_price(
            execution_price, execution_price_text),
        "contract_copies": format_amount(copies),
        "currency": "CNH",
        "option_type": attrs.option_type,
        "k1_ratio": format_ratio(attrs.k1_ratio),
        "k2_ratio": format_ratio(attrs.k2_ratio),
        "k1_formula": "E * K1_ratio",
        "k2_formula": "E * K2_ratio",
        "participation_rate": format_ratio(attrs.participation_rate),
        "duration": attrs.duration,
        "expiry_date": f"{expiry_date:%Y/%m/%d}",
        "premium_rate": format_ratio_explicit(attrs.premium_rate, 2),
        "margining_clause": MARGINING_CLAUSE_TEMPLATE.format(
            k1_pct=attrs.k1_ratio * 100, k2_pct=attrs.k2_ratio * 100),
        "settlement_lines": SETTLEMENT_LINES,
        "option_seller": "Grandly Global Limited",
        "option_buyer": "Party B",
        "disclaimer": DISCLAIMER,
    }


def _build_tc_fields_90100(order: OrderInstruction, execution_price: float,
                           trade_date: dt.date, attrs: StructureAttributes,
                           counterparty_id: str, serial_no: int,
                           counterparty_name: str,
                           execution_price_text: str | None) -> dict[str, str]:
    """90100（敲出结构）字段组装（字段顺序按样例 PDF）。"""
    if attrs.option_strike is None:
        raise ValueError("90100 结构缺少 option_strike 默认参数")
    copies = compute_contract_copies(order.notional_principal, execution_price)
    expiry_date = compute_expiry_date(trade_date)
    trade_date_text = f"{trade_date:%Y/%m/%d}"
    strike, digits = compute_strike_price(execution_price, attrs.option_strike)
    strike_text = format_strike(strike, digits)
    up_limit_text = attrs.up_limit_for(order.asset_code)
    compulsory_lines = build_compulsory_settlement_lines(
        strike_text, up_limit_text)
    return {
        "brand": HEADER_BRAND,
        "title": TITLE,
        "narrative_pre": NARRATIVE_PRE,
        "narrative_cp": counterparty_name,
        "narrative_post": NARRATIVE_POST,
        "confirmation_number": compute_confirmation_number(
            counterparty_id, order.structure, trade_date, serial_no),
        "trade_date": trade_date_text,
        "underlying_asset": order.asset_name,
        "underlying_asset_code": order.asset_code,
        "notional_principal": format_amount(order.notional_principal),
        "contract_copies": format_amount(copies),
        "currency": "CNH",
        "option_type": attrs.option_type,
        "option_strike": format_ratio(attrs.option_strike),
        "duration": attrs.duration,
        "expiry_date": f"{expiry_date:%Y/%m/%d}",
        "premium_rate": format_ratio_explicit(attrs.premium_rate, 2),
        "execution_price": _format_execution_price(
            execution_price, execution_price_text),
        "strike_price": strike_text,
        "seller_discretionary_price": f"At or Below {strike_text}",
        "compulsory_settlement_lines": compulsory_lines,
        "option_seller": "Grandly Global Limited",
        "option_buyer": "Party B",
        "disclaimer": DISCLAIMER_90100,
    }
