"""消息分类：识别消息类别并匹配处理器名.

类别：
* cp001_9080_order：CP001 群 //数值 引用 #下单 9080 ...
* cp001_90100_order：CP001 群 //数值 引用 #下单 90100 ...
* cp003_order：CP003 群 //数值 引用 #下单 9070/9080/90100（共用一套模板）
* unsupported：//到价追保 / //展期成功 / #平仓 / 未注册组合 → 记日志。

路由为「对手方×结构」二维（模板按对手方分化，结构号单维无法区分
CP001/CP003 的同名结构）；对手方由 dispatcher 经群映射解析后传入。
仅传结构号的旧签名保留兜底路径（无法区分对手方，仅匹配 CP001 历史行为）。
"""

from __future__ import annotations

import re

# //数值（成交价）——下单触发
EXEC_PRICE_RE = re.compile(r"^\s*//\s*(\d+(?:\.\d+)?)\s*$")

# 下单指令中的结构号（被引用消息第二个 token）
_STRUCTURE_RE = re.compile(r"^\s*#+\s*下单\s+(\S+)")

# （对手方， 结构号）→ 处理器名；新需求在此加一行
ROUTES: dict[tuple[str, str], str] = {
    ("CP001", "9080"): "cp001_9080_order",
    ("CP001", "90100"): "cp001_90100_order",
    ("CP003", "9070"): "cp003_order",
    ("CP003", "9080"): "cp003_order",
    ("CP003", "90100"): "cp003_order",
}

# 旧签名（无对手方维度）的结构号兜底路由，维持 CP001 历史行为
STRUCTURE_ROUTES: dict[str, str] = {
    "9080": "cp001_9080_order",
    "90100": "cp001_90100_order",
}


def _structure_of(quoted_content: str) -> str | None:
    """从引用的下单指令提取结构号；非下单指令返回 None。"""
    match = _STRUCTURE_RE.match(quoted_content or "")
    if match is None:
        return None
    return match.group(1)


def classify(content: str, quoted_content: str,
             counterparty_id: str | None = None) -> str:
    """返回处理器名；无法处理时返回 "unsupported"。

    counterparty_id：dispatcher 按群映射解析出的对手方编号；
    为 None 时退回结构号单维兜底路由（仅覆盖 CP001）。
    """
    text = (content or "").strip()
    if EXEC_PRICE_RE.match(text):
        structure = _structure_of(quoted_content or "")
        if structure is not None:
            if counterparty_id is not None:
                return ROUTES.get(
                    (counterparty_id, structure), "unsupported")
            return STRUCTURE_ROUTES.get(structure, "unsupported")
    return "unsupported"
