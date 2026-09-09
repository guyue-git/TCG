"""处理器：CP003 下单确认书（9070/9080/90100 共用一套模板）.

输入（monitor 拼接的文本）：
    message.content         = "//11.73"
    message.quoted_content  = "#下单 9080 300017 网宿科技 限价 买入 50W"
    （quoted_content 可能带 quote_marker 叠加前缀 "##下单 ..."，容忍）

产出：output/TradeConfirm(ITC{结构})_CP003_{YYYYMMDD}_{NNN}.pdf

业务规则（master file 逻辑.docx + CP003 - 9070,9080,90100.docx）：
    三结构共用观察价式模板，差异仅 premium/participation（attrs 承载）；
    90100 premium 以 docx 11.50% 为准（用户裁决，样例 PDF 11.25% 为旧值）。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

from tc_generator.master_file import get_structure_attributes
from tc_generator.parser import (extract_execution_price,
                                 extract_execution_price_text,
                                 parse_order_line)
from tc_generator.renderer import render_tc_pdf
from tc_generator.tc_builder import (build_tc_fields, compute_next_serial,
                                     compute_output_filename)

from ..message import IncomingMessage
from ..handler import HandlerContext

LOG = logging.getLogger(__name__)

NAME = "cp003_order"

TEMPLATE_NAME = "tc_cp003.html.j2"


def handle(message: IncomingMessage, ctx: HandlerContext) -> Path | None:
    """CP003 下单确认书生成：解析 → 组字段 → Edge 渲染 PDF。"""
    counterparty_id = ctx.group_map.get(message.group)
    if counterparty_id is None:
        LOG.warning("[cp003_order] 群 %r 未配置对手方映射，跳过", message.group)
        return None

    execution_price = extract_execution_price(message.content)
    if execution_price is None:
        LOG.warning("[cp003_order] 成交价解析失败: %r，跳过", message.content)
        return None
    price_text = extract_execution_price_text(message.content)

    order = parse_order_line(message.quoted_content)
    if order is None:
        LOG.warning("[cp003_order] 下单指令解析失败: %r，跳过",
                    message.quoted_content)
        return None

    if message.create_time:
        trade_date = dt.datetime.fromtimestamp(message.create_time).date()
    else:
        trade_date = dt.date.today()

    try:
        attrs = get_structure_attributes(counterparty_id, order.structure)
    except KeyError as exc:
        LOG.info("[cp003_order] 超出支持范围，仅记录: %s", exc)
        return None

    output_dir = ctx.base_dir / ctx.output_dirname
    serial_no = compute_next_serial(output_dir, counterparty_id,
                                    order.structure, trade_date)
    fields = build_tc_fields(
        order, execution_price, trade_date, attrs, counterparty_id,
        serial_no, counterparty_name=ctx.counterparty_name,
        execution_price_text=price_text)
    output_pdf = output_dir / compute_output_filename(
        counterparty_id, order.structure, trade_date, serial_no)
    render_tc_pdf(fields, output_pdf, edge_path=ctx.edge_path,
                  template_name=TEMPLATE_NAME)
    LOG.info("[cp003_order] 已生成确认书: %s（群=%s, 编号=%s）",
             output_pdf.name, message.group, fields["confirmation_number"])
    return output_pdf
