"""Master File：对手方账户属性（一期：CP001-9080 / CP001-90100）.

来源：master file 逻辑.docx + CP001-9080 模板默认参数（用户裁决 #3）
    + CP001-90100 模板默认参数（文件/CP001 - 90100.docx）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StructureAttributes:
    """某对手方某结构下的账户属性。

    9080（观察价结构）：K1/K2/Participation/可追保。
    90100（敲出结构）：Option strike 默认比例、强制结算上调条件表。
    """

    premium_rate: float          # Option Premium Rate
    duration: str                # Duration，如 "1M"
    option_type: str             # 期权类型
    participation_rate: float | None = None    # 9080 专属
    k1_ratio: float | None = None              # 9080 专属
    k2_ratio: float | None = None              # 9080 专属
    can_margin: bool = False                   # 9080 专属
    option_strike: float | None = None         # 90100：90%（北交所标的 85%）
    up_limit_table: dict[str, str] | None = None  # 90100：板块 → 上调条款

    def up_limit_for(self, asset_code: str) -> str:
        """按证券代码前缀判定板块并返回强制结算上调条款文本（90100 专属）。"""
        if self.up_limit_table is None:
            raise ValueError("该结构无强制结算上调条件表")
        return self.up_limit_table[board_of(asset_code)]


def up_limit_text_for(asset_code: str) -> str:
    """按证券代码前缀返回强制结算上调条款文本（模块级便捷函数）。"""
    return UP_LIMIT_TABLE[board_of(asset_code)]


# 板块判定规则（master file 逻辑.docx Upside 表）：
#   920 开头 → 北交所（30% 上限 × 2 日）；688 开头 → 科创板、300/301 开头 →
#   创业板（均为 20% × 2 日）；其余 A 股 → 主板（10% × 3 日）。
_BOARD_RULES: list[tuple[str, str]] = [
    ("920", "bse"),
    ("688", "star"),
    ("301", "chinext"),
    ("300", "chinext"),
]

UP_LIMIT_BSE = ("The closing price has been at the 30% up-limit for "
                "two consecutive trading days; or ")
UP_LIMIT_STAR_CHINEXT = ("The closing price has been at the 20% up-limit for "
                         "two consecutive trading days; or ")
UP_LIMIT_MAIN = ("The closing price has been at the 10% up-limit for "
                 "three consecutive trading days; or ")

UP_LIMIT_TABLE: dict[str, str] = {
    "bse": UP_LIMIT_BSE,
    "star": UP_LIMIT_STAR_CHINEXT,
    "chinext": UP_LIMIT_STAR_CHINEXT,
    "main": UP_LIMIT_MAIN,
}


def board_of(asset_code: str) -> str:
    """按证券代码前缀判定板块：bse / star / chinext / main。"""
    code = (asset_code or "").strip()
    for prefix, board in _BOARD_RULES:
        if code.startswith(prefix):
            return board
    return "main"


# CP001 Master File（9080 结构默认参数：K1 91% / K2 90% / 1M / 10.75% / r 80%）
CP001_9080 = StructureAttributes(
    premium_rate=0.1075,
    participation_rate=0.80,
    k1_ratio=0.91,
    k2_ratio=0.90,
    duration="1M",
    can_margin=True,
    option_type="Issuer-Terminable Call",
)

# CP001 Master File（90100 结构默认参数：strike 90% / 1M / 11.00%）
CP001_90100 = StructureAttributes(
    premium_rate=0.1100,
    duration="1M",
    option_type="Issuer-Terminable Call Option",
    option_strike=0.90,
    up_limit_table=UP_LIMIT_TABLE,
)

# CP003 Master File（来源：master file 逻辑.docx + CP003 - 9070,9080,90100.docx）
# 三结构共用一套确认书模板（用户裁决），差异仅 premium/participation；
# 90100 premium 取 docx 11.50%（样例 PDF 11.25% 为旧版数值，已裁决以 docx 为准）。
CP003_90100 = StructureAttributes(
    premium_rate=0.1150,
    participation_rate=1.00,
    k1_ratio=0.91,
    k2_ratio=0.90,
    duration="1M",
    can_margin=True,
    option_type="Issuer-Terminable Call",
)

CP003_9080 = StructureAttributes(
    premium_rate=0.1100,
    participation_rate=0.80,
    k1_ratio=0.91,
    k2_ratio=0.90,
    duration="1M",
    can_margin=True,
    option_type="Issuer-Terminable Call",
)

CP003_9070 = StructureAttributes(
    premium_rate=0.1025,
    participation_rate=0.70,
    k1_ratio=0.91,
    k2_ratio=0.90,
    duration="1M",
    can_margin=True,
    option_type="Issuer-Terminable Call",
)

# 注册表：CP001（一期）+ CP003（三期，9070/9080/90100 共用模板）
MASTER_FILE: dict[str, dict[str, StructureAttributes]] = {
    "CP001": {
        "9080": CP001_9080,
        "90100": CP001_90100,
    },
    "CP003": {
        "9070": CP003_9070,
        "9080": CP003_9080,
        "90100": CP003_90100,
    },
}


def get_structure_attributes(counterparty_id: str,
                             structure: str) -> StructureAttributes:
    """按对手方编号与结构取属性；未注册组合显式报错。"""
    cp_attrs = MASTER_FILE.get(counterparty_id)
    if cp_attrs is None:
        raise KeyError(
            f"对手方 {counterparty_id} 不在 Master File"
            f"（已注册：{sorted(MASTER_FILE)}）")
    attrs = cp_attrs.get(structure)
    if attrs is None:
        raise KeyError(
            f"结构 {structure} 不在 {counterparty_id} 的 Master File"
            f"（已注册：{sorted(cp_attrs)}）")
    return attrs


# ---------------- 90100 strike 计算规则 ----------------
# Strike price = Round Down(E × Option strike)：
#   E < 20   → 保留 3 位小数（样例 docx：51.433×90% → 46.289）
#   E ≥ 20   → 保留 2 位小数（样例 PDF：58.144×90% → 52.32）
STRIKE_SMALL_PRICE_THRESHOLD = 20
STRIKE_DIGITS_SMALL = 3
STRIKE_DIGITS_NORMAL = 2
