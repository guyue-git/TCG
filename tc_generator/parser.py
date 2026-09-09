"""消息解析：下单指令行 + 触发消息（//数值）.

下单指令（被引用消息预览）格式：
    #下单 9080 300017 网宿科技 限价 买入 200W
    ①模板标识 ②结构 ③代码 ④名称 ⑤下单类型 ⑥方向 ⑦名义本金

注意：JSONL 中引用记录的 content = quote_marker("#") + 原消息预览，
原消息本身以 "#" 开头，因此实际可能形如 "##下单 9080 ..."，解析时容忍。

触发消息（direct 记录）：//13、//13.5 之类，// 后的数值为成交价。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ORDER_TAG = "下单"
SUPPORTED_STRUCTURE = "9080"

# 下单类型前缀：用于在「名称+类型」混合段中定位类型起点。
# 真实群消息的限价单会带价格，且价格可与「限价」分体或连写：
#   限价241.80（7 token，一期联调形态）/ 限价 6.4（8 token，2026-09-04 实测）
#   市价 / 均价X分钟
_ORDER_TYPE_PREFIXES = ("限价", "市价", "均价")
_DIRECTION_WORDS = ("买入", "卖出")

# 触发消息：// 后跟纯数值（可含小数）
_EXEC_PRICE_RE = re.compile(r"^\s*//\s*(\d+(?:\.\d+)?)\s*$")

# 名义本金：200W / 50w / 100万 / 3000W / 纯数字
_NOTIONAL_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([Ww万]?)$")


@dataclass(frozen=True)
class OrderInstruction:
    """解析后的对手方下单指令。"""

    template_tag: str            # ①：下单
    structure: str               # ②：9080
    asset_code: str              # ③：300017
    asset_name: str              # ④：网宿科技
    order_type: str              # ⑤：限价 / 市价 / 均价X分钟
    direction: str               # ⑥：买入 / 卖出
    notional_principal: int      # ⑦：200W -> 2,000,000


def parse_notional_principal(text: str) -> int | None:
    """解析名义本金；W/万 为单位（×10,000），纯数字按元。"""
    match = _NOTIONAL_RE.match(text.strip())
    if match is None:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    if unit in ("W", "w", "万"):
        amount *= 10_000
    if amount <= 0 or amount != int(amount):
        return None
    return int(amount)


def parse_order_line(content: str) -> OrderInstruction | None:
    """解析 '#下单 ...' 引用预览行；格式不符返回 None。

    结构（按空白切分，兼容 ## 前缀与换行）：
        下单 <结构> <代码> <名称> <下单类型...> <方向> <名义本金>

    名称与下单类型均为自由文本且可能被空格拆成多个 token（限价 6.4），
    故按两端锚定：首 token=下单，token[1]=结构，token[2]=代码，
    末 token=本金，次末 token=方向；代码与方向之间的中间段先按已知
    下单类型前缀定位类型起点，定位失败且中间段恰为 2 token 时沿用
    旧「名称+类型」位置语义。
    """
    text = (content or "").strip()
    text = text.lstrip("#").strip()      # 去掉 quote_marker 与原消息自带的 #
    if not text.startswith(ORDER_TAG):
        return None
    tokens = text.split()
    if len(tokens) < 7 or tokens[0] != ORDER_TAG:
        return None
    structure = tokens[1]
    direction, notional_raw = tokens[-2], tokens[-1]
    if direction not in _DIRECTION_WORDS:
        return None
    middle = tokens[3:-2]                # 名称 token + 类型 token（≥2 个）
    if len(middle) < 2:
        return None
    type_idx = next(
        (i for i, tok in enumerate(middle)
         if tok.startswith(_ORDER_TYPE_PREFIXES)),
        None)
    if type_idx is None:
        if len(middle) != 2:
            return None
        asset_name, order_type = middle
    else:
        asset_name = "".join(middle[:type_idx])
        order_type = "".join(middle[type_idx:])
    if not asset_name:
        return None
    notional = parse_notional_principal(notional_raw)
    if notional is None:
        return None
    return OrderInstruction(
        template_tag=ORDER_TAG,
        structure=structure,
        asset_code=tokens[2],
        asset_name=asset_name,
        order_type=order_type,
        direction=direction,
        notional_principal=notional,
    )


def is_execution_price_message(content: str) -> bool:
    """判断 direct 消息是否为触发消息：//数值。"""
    return _EXEC_PRICE_RE.match(content or "") is not None


def extract_execution_price(content: str) -> float | None:
    """从触发消息提取成交价；非触发消息返回 None。"""
    match = _EXEC_PRICE_RE.match(content or "")
    if match is None:
        return None
    return float(match.group(1))


def extract_execution_price_text(content: str) -> str | None:
    """从触发消息提取成交价原始文本（保留用户书写精度）.

    "//241.80" -> "241.80"；"//58.144" -> "58.144"；"//100" -> "100"
    （原始文本原样返回，补齐小数位由 tc_builder.format_price 决定）
    """
    match = _EXEC_PRICE_RE.match(content or "")
    if match is None:
        return None
    return match.group(1)


def classify_trigger(content: str) -> str:
    """对 // 开头消息分类：price / 追保 / 展期 / other（一期只处理 price）。"""
    text = (content or "").strip()
    if is_execution_price_message(text):
        return "price"
    if text.startswith("//到价追保"):
        return "追保"
    if text.startswith("//展期成功"):
        return "展期"
    return "other"
