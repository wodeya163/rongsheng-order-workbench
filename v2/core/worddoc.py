#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Word 审批单与本机文档通道（2026-11-07 v2.4）

职责：
  1. build_approval_docx() —— 用 python-docx 生成中文化《生产排产审批单》，
     预填订单头 8 字段、排产物资 9 字段、附件清单、编制/审核/批准栏。
  1b. build_order_approval_docx() —— 合并 / 整单排产的一张审批单覆盖多项物资。
  1c. build_purchase_contract_docx()（v3.4.0）—— 生成中文化《采购合同》，
      物资清单直接读订单明细，供「采购合同管理」的生成动作调用。
  2. open_in_word() —— 点击「新建生产排产审批」后【启动本机 Word 编制文件】：
     优先 COM 显式拉起 Word.Application（可见）并打开文档；Word 未安装时
     回落 os.startfile，交给系统文件关联（WPS / Word 均可）。
  3. open_file() —— 打开已导入的图片 / 图纸。

设计约束：本模块只在用户点击时执行，离屏自检与单元测试不会真的启动 Word；
          Word 不可用时返回 (False, 说明) 由界面提示，绝不抛异常中断业务。
"""
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .. import APP_TITLE               # v3.4.3 品牌单一数据源（Word 页脚）
from .errors import ValidationError

FONT_CN = "微软雅黑"
FONT_FALLBACK = "宋体"


def _apply_cn_font(run, size: float = 10.5, bold: bool = False) -> None:
    """中文必须显式设置 eastAsia，否则 Word 会回落成等线/宋体混排"""
    from docx.shared import Pt

    run.font.name = FONT_CN
    run.font.size = Pt(size)
    run.bold = bold
    try:
        from docx.oxml.ns import qn
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT_CN)
    except Exception:
        pass


def _cell(cell, text: Any, bold: bool = False, size: float = 10.0) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    _apply_cn_font(p.add_run("" if text is None else str(text)), size=size, bold=bold)


def _kv_table(doc, rows: Sequence[Tuple[str, Any]], cols: int = 4) -> None:
    """两列一组的键值表：[(k,v), ...] → 每行 cols/2 组「键 | 值」"""
    pairs = list(rows)
    width = max(1, cols // 2)
    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"
    for i in range(0, len(pairs), width):
        cells = table.add_row().cells
        for j in range(width):
            key, value = pairs[i + j] if i + j < len(pairs) else ("", "")
            _cell(cells[j * 2], key, bold=True)
            _cell(cells[j * 2 + 1], value)
    doc.add_paragraph()


def build_approval_docx(path: str, ctx: Dict[str, Any]) -> str:
    """
    生成《生产排产审批单》.docx 并返回绝对路径。

    ctx 键（均由 OrderScheduleService.approval_context() 组装）：
      schedule_no, created_at, order_no, project_no, customer, order_type,
      status, erp_order_no, required_date, amount_total, item_qty_total,
      item: {line_no, item_code, item_name, production_name, spec, unit,
             qty_ordered, allocated_qty, unit_price, amount, param_desc},
      files: [{kind, file_name}, ...], remark
    """
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as exc:  # pragma: no cover - 打包缺失时给明确提示
        raise ValidationError("缺少 python-docx，无法生成 Word 审批单") from exc

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)

    normal = doc.styles["Normal"]
    normal.font.name = FONT_FALLBACK
    normal.font.size = Pt(10.5)
    try:
        from docx.oxml.ns import qn
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_FALLBACK)
    except Exception:
        pass

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(title.add_run("生产排产审批单"), size=18, bold=True)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(sub.add_run(
        f"订单排产单号：{ctx.get('schedule_no') or '—'}　|　编制日期："
        f"{ctx.get('created_at') or date.today().isoformat()}"), size=9)

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("一、订单信息"), size=12, bold=True)
    _kv_table(doc, [
        ("系统单号", ctx.get("order_no") or "—"),
        ("订单项目编号", ctx.get("project_no") or "—"),
        ("客户名称", ctx.get("customer") or "—"),
        ("订单类型", ctx.get("order_type") or "—"),
        ("订单状态", ctx.get("status") or "—"),
        ("ERP订单编号", ctx.get("erp_order_no") or "—"),
        ("交货日期", ctx.get("required_date") or "—"),
        ("价税合计(¥)", ctx.get("amount_total") if ctx.get("amount_total") is not None else "—"),
        ("物资数量(项)", ctx.get("item_qty_total") if ctx.get("item_qty_total") is not None else "—"),
        ("编制人", ctx.get("created_by") or "—"),
    ])

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("二、排产物资"), size=12, bold=True)
    item = ctx.get("item") or {}
    t = doc.add_table(rows=0, cols=4)
    t.style = "Table Grid"
    item_rows = [
        ("明细行号", item.get("line_no", "—"), "物资代码", item.get("item_code") or "—"),
        ("物资名称", item.get("item_name") or "—",
         "实际生产物资名称", item.get("production_name") or "—"),
        ("规格", item.get("spec") or "—", "单位", item.get("unit") or "—"),
        ("订单数量(实值)", item.get("qty_ordered", "—"),
         "本次排产数量", item.get("allocated_qty", "—")),
        ("单价(¥)", item.get("unit_price", "—"), "总价(¥)", item.get("amount", "—")),
        ("参数描述", item.get("param_desc") or "—", "", ""),
    ]
    for a, b, c, d in item_rows:
        cells = t.add_row().cells
        _cell(cells[0], a, bold=True)
        _cell(cells[1], b)
        _cell(cells[2], c, bold=bool(c))
        _cell(cells[3], d)
    doc.add_paragraph()

    files: List[Dict[str, Any]] = list(ctx.get("files") or [])
    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("三、随单附件"), size=12, bold=True)
    ft = doc.add_table(rows=1, cols=3)
    ft.style = "Table Grid"
    for cell, text in zip(ft.rows[0].cells, ("类别", "文件名", "说明")):
        _cell(cell, text, bold=True)
    if files:
        for f in files:
            cells = ft.add_row().cells
            _cell(cells[0], f.get("kind") or "—")
            _cell(cells[1], f.get("file_name") or "—")
            _cell(cells[2], "见附件目录")
            # 让第 2 列更宽，避免长文件名被挤成竖排
    else:
        cells = ft.add_row().cells
        _cell(cells[0], "—")
        _cell(cells[1], "暂无图片 / 图纸")
        _cell(cells[2], "可在订单排产对话框导入")
    try:
        from docx.shared import Cm as _Cm
        for row in ft.rows:
            row.cells[0].width = _Cm(2.2)
            row.cells[1].width = _Cm(9.5)
            row.cells[2].width = _Cm(5.0)
    except Exception:
        pass
    doc.add_paragraph()

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("四、审批"), size=12, bold=True)
    at = doc.add_table(rows=2, cols=4)
    at.style = "Table Grid"
    for cell, text in zip(at.rows[0].cells, ("编制", "审核", "批准", "日期")):
        _cell(cell, text, bold=True)
    for cell in at.rows[1].cells:
        _cell(cell, "")

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("五、备注"), size=12, bold=True)
    p = doc.add_paragraph()
    _apply_cn_font(p.add_run(str(ctx.get("remark") or "（无）")), size=10)

    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(footer.add_run(
        f"本文件由「{APP_TITLE}」自动生成 · 订单排产 "
        f"{ctx.get('schedule_no') or ''} · 数量守恒以订单明细实值为准"), size=8)

    doc.save(str(target))
    return str(target.resolve())


def build_order_approval_docx(path: str, ctx: Dict[str, Any],
                              scope: str = "整单") -> str:
    """
    《生产排产审批单》.docx（v2.9.3 整单 / v3.0 支持合并）：

    · 整单：一张文档覆盖订单全部物资，供整单排产时多种物资共用一个审批文件；
    · 合并：一张文档覆盖对话框里勾选的多项物资（明细多选合并排产）；
    · 分项：仍由 build_approval_docx 生成单物资文档，不走这里。

    ctx 键（由 OrderScheduleService.order_approval_context() 组装）：
      order_no, project_no, customer, order_type, status, erp_order_no,
      required_date, amount_total, item_qty_total, created_at, scope, scope_note,
      items: [{line_no, item_code, item_name, production_name, spec, unit,
               qty_ordered, allocated_qty, unit_price, amount}, ...]
    """
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as exc:  # pragma: no cover - 打包缺失时给明确提示
        raise ValidationError("缺少 python-docx，无法生成 Word 审批单") from exc

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.2)

    normal = doc.styles["Normal"]
    normal.font.name = FONT_FALLBACK
    normal.font.size = Pt(10.5)
    try:
        from docx.oxml.ns import qn
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_FALLBACK)
    except Exception:
        pass

    scope = scope if scope in ("分项", "合并", "整单") else "整单"
    scope_note = ctx.get("scope_note") or f"{scope}排产 · 一种或多种物资共用本审批单"

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(title.add_run(f"生产排产审批单（{scope}）"), size=18, bold=True)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(sub.add_run(
        f"订单号：{ctx.get('order_no') or '—'}　|　编制日期："
        f"{ctx.get('created_at') or date.today().isoformat()}　|　{scope_note}"), size=9)

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("一、订单信息"), size=12, bold=True)
    _kv_table(doc, [
        ("系统单号", ctx.get("order_no") or "—"),
        ("订单项目编号", ctx.get("project_no") or "—"),
        ("客户名称", ctx.get("customer") or "—"),
        ("订单类型", ctx.get("order_type") or "—"),
        ("订单状态", ctx.get("status") or "—"),
        ("ERP订单编号", ctx.get("erp_order_no") or "—"),
        ("交货日期", ctx.get("required_date") or "—"),
        ("价税合计(¥)", ctx.get("amount_total") if ctx.get("amount_total") is not None else "—"),
        ("物资数量(项)", ctx.get("item_qty_total") if ctx.get("item_qty_total") is not None else "—"),
    ])

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run(f"二、排产物资（{scope}清单）"), size=12, bold=True)
    items: List[Dict[str, Any]] = list(ctx.get("items") or [])
    heads = ("行", "物资代码", "物资名称", "实际生产物资名称", "规格",
             "单位", "订单数量", "本次排产")
    it = doc.add_table(rows=1, cols=len(heads))
    it.style = "Table Grid"
    for cell, text in zip(it.rows[0].cells, heads):
        _cell(cell, text, bold=True)
    for m in items:
        cells = it.add_row().cells
        values = (m.get("line_no"), m.get("item_code"), m.get("item_name"),
                  m.get("production_name"), m.get("spec"), m.get("unit"),
                  m.get("qty_ordered"), m.get("allocated_qty"))
        for cell, v in zip(cells, values):
            _cell(cell, v)
    if not items:
        cells = it.add_row().cells
        _cell(cells[0], "该订单暂无物资明细")
    try:
        widths = (1.0, 2.6, 3.4, 3.4, 2.6, 1.2, 1.8, 1.8)
        for row in it.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Cm(w)
    except Exception:
        pass
    doc.add_paragraph()

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("三、审批"), size=12, bold=True)
    at = doc.add_table(rows=2, cols=4)
    at.style = "Table Grid"
    for cell, text in zip(at.rows[0].cells, ("编制", "审核", "批准", "日期")):
        _cell(cell, text, bold=True)
    for cell in at.rows[1].cells:
        _cell(cell, "")

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("四、备注"), size=12, bold=True)
    p = doc.add_paragraph()
    _apply_cn_font(p.add_run(
        str(ctx.get("remark")
            or "各物资明细的随单附件见各自订单排产单目录。")), size=10)

    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(footer.add_run(
        f"本文件由「{APP_TITLE}」自动生成 · "
        f"{scope}排产 {ctx.get('order_no') or ''} · 数量守恒以订单明细实值为准"), size=8)

    doc.save(str(target))
    return str(target.resolve())


def build_purchase_contract_docx(path: str, ctx: Dict[str, Any]) -> str:
    """
    v3.4.0《采购合同》.docx 模板。

    由 PurchaseContractService.contract_context() 组装 ctx：
      doc_no, contract_name, contract_type, supplier, buyer, contact,
      amount, currency, sign_date, delivery_date, payment_terms,
      order_no, project_no, customer, erp_order_no, required_date,
      items: [{line_no, item_code, item_name, production_name, spec,
               unit, qty_ordered, unit_price, amount}, ...], remark

    物资清单直接读取订单明细，不落第二份数量快照；合同金额由台账字段给出。
    """
    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.shared import Cm, Pt
    except ImportError as exc:  # pragma: no cover - 打包缺失时给明确提示
        raise ValidationError("缺少 python-docx，无法生成 Word 采购合同") from exc

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Cm(2.0)

    normal = doc.styles["Normal"]
    normal.font.name = FONT_FALLBACK
    normal.font.size = Pt(10.5)
    try:
        from docx.oxml.ns import qn
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), FONT_FALLBACK)
    except Exception:
        pass

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(title.add_run(str(ctx.get("contract_name") or "采购合同")),
                   size=18, bold=True)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(sub.add_run(
        f"合同编号：{ctx.get('doc_no') or '—'}　|　签订日期："
        f"{ctx.get('sign_date') or date.today().isoformat()}"), size=9)

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("一、合同双方与依据"), size=12, bold=True)
    _kv_table(doc, [
        ("供方（乙方）", ctx.get("supplier") or "—"),
        ("需方（甲方）", ctx.get("buyer") or ctx.get("customer") or "—"),
        ("供方联系人", ctx.get("contact") or "—"),
        ("合同类型", ctx.get("contract_type") or "物资采购"),
        ("关联订单号", ctx.get("order_no") or "—"),
        ("订单项目编号", ctx.get("project_no") or "—"),
        ("ERP订单编号", ctx.get("erp_order_no") or "—"),
        ("订单交货日期", ctx.get("required_date") or "—"),
    ])

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("二、采购物资清单"), size=12, bold=True)
    items: List[Dict[str, Any]] = list(ctx.get("items") or [])
    heads = ("行", "物资代码", "物资名称", "规格", "单位",
             "数量", "单价(¥)", "总价(¥)")
    it = doc.add_table(rows=1, cols=len(heads))
    it.style = "Table Grid"
    for cell, text in zip(it.rows[0].cells, heads):
        _cell(cell, text, bold=True)
    for m in items:
        cells = it.add_row().cells
        values = (m.get("line_no"), m.get("item_code"), m.get("item_name"),
                  m.get("spec"), m.get("unit"), m.get("qty_ordered"),
                  m.get("unit_price"), m.get("amount"))
        for cell, v in zip(cells, values):
            _cell(cell, v)
    if not items:
        cells = it.add_row().cells
        _cell(cells[0], "本合同未关联订单物资明细，请手工补充清单")
    try:
        widths = (1.0, 2.4, 3.6, 2.6, 1.2, 1.6, 2.0, 2.0)
        for row in it.rows:
            for cell, w in zip(row.cells, widths):
                cell.width = Cm(w)
    except Exception:
        pass
    doc.add_paragraph()

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("三、合同金额与交付"), size=12, bold=True)
    _kv_table(doc, [
        ("合同金额", f"{ctx.get('amount') if ctx.get('amount') is not None else 0}"),
        ("币种", ctx.get("currency") or "CNY"),
        ("交货日期", ctx.get("delivery_date") or "—"),
        ("付款方式", ctx.get("payment_terms") or "按双方约定执行"),
    ])

    for heading, text in (
        ("四、质量与技术要求",
         "供方所供物资应符合订单明细所列规格、参数描述及国家/行业标准，"
         "随货提供合格证、材质证明与必要的检验报告；需方有权到货验收，"
         "不合格物资由供方无条件更换或退货并承担相应费用。"),
        ("五、包装、运输与交付",
         "供方负责按物资特性妥善包装并运送至需方指定地点，运输、保险及"
         "交付前风险由供方承担；交货期以本合同第三条约定为准，"
         "延期交付应提前书面告知需方并协商处理。"),
        ("六、违约责任与争议解决",
         "任何一方未履行本合同约定义务，应承担由此给对方造成的直接损失；"
         "因本合同产生的争议，双方先行友好协商，协商不成时向需方所在地"
         "人民法院提起诉讼。"),
    ):
        hp = doc.add_paragraph()
        _apply_cn_font(hp.add_run(heading), size=12, bold=True)
        p = doc.add_paragraph()
        _apply_cn_font(p.add_run(text), size=10)

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("七、签署"), size=12, bold=True)
    st = doc.add_table(rows=3, cols=4)
    st.style = "Table Grid"
    for cell, text in zip(st.rows[0].cells,
                          ("需方（甲方）", "供方（乙方）", "签订日期", "备注")):
        _cell(cell, text, bold=True)
    _cell(st.rows[1].cells[0], ctx.get("buyer") or ctx.get("customer") or "")
    _cell(st.rows[1].cells[1], ctx.get("supplier") or "")
    _cell(st.rows[1].cells[2], ctx.get("sign_date") or "")
    _cell(st.rows[1].cells[3], "")
    for cell, text in zip(st.rows[2].cells,
                          ("盖章 / 代表签字", "盖章 / 代表签字", "", "")):
        _cell(cell, text)
    doc.add_paragraph()

    h = doc.add_paragraph()
    _apply_cn_font(h.add_run("八、备注"), size=12, bold=True)
    p = doc.add_paragraph()
    _apply_cn_font(p.add_run(
        str(ctx.get("remark") or "本合同一式两份，甲乙双方各执一份，"
            "经双方签字盖章后生效；未尽事宜另行签订补充协议。")), size=10)

    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _apply_cn_font(footer.add_run(
        f"本文件由「{APP_TITLE}」自动生成 · "
        f"采购合同 {ctx.get('doc_no') or ''} · "
        "物资清单以订单明细实值为准"), size=8)

    doc.save(str(target))
    return str(target.resolve())


def open_in_word(path: str) -> Tuple[bool, str]:
    """
    启动本机 Word 打开编制文件；Word 不可用时回落系统文件关联。
    返回 (是否成功, 说明文字)，【绝不抛异常】，便于界面友好提示。
    """
    p = str(Path(path).resolve())
    if not os.path.isfile(p):
        return False, f"文件不存在：{p}"
    com_error = ""
    try:  # ① 显式启动本机 Microsoft Word
        import win32com.client as win32  # type: ignore
        app = win32.DispatchEx("Word.Application")
        try:
            app.Visible = True
        except Exception:
            pass
        app.Documents.Open(p)
        return True, "已启动本机 Microsoft Word 并打开审批单，可直接编制"
    except Exception as exc:
        com_error = f"{type(exc).__name__}: {exc}"
    try:  # ② 回落系统关联（WPS / Word / 其他 .docx 编辑器）
        os.startfile(p)  # noqa: S606 - Windows 文件关联，用户主动点击触发
        return True, "已用系统默认程序打开（Word / WPS），可直接编制"
    except Exception as exc:
        return False, (f"无法启动 Word（{com_error}；{type(exc).__name__}: {exc}）。"
                       f"审批单已生成，可手动打开：{p}")


def open_file(path: str) -> Tuple[bool, str]:
    """打开图片 / 图纸 / 审批文档（系统文件关联）"""
    p = str(Path(path).resolve()) if os.path.isabs(path) else str(path)
    if not os.path.isfile(p):
        return False, f"文件不存在：{p}"
    try:
        os.startfile(p)  # noqa: S606
        return True, "已打开"
    except Exception as exc:
        return False, f"无法打开：{exc}"
