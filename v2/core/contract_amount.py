#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合同总金额读取（v3.4.6 · 用户第 21 轮需求）

背景
----
采购合同台账的「合同金额」以前错误地取了关联订单的 `amount_total`。
真实合同（例：`20261107KYCN-B-027委托加工合同-浙江-河北久通.doc`）原文是：

    总计：
    人民币：壹万肆仟肆佰捌拾肆圆整
    （含税不含运，其中不含税金额12817.7元，增值税金额1666.3元）
    14484

即合同金额应为 **14484**，而订单总额是 **18778**。本模块只负责
「从合同文档本身读出总金额」，绝不接触订单表，也不猜数。

设计口径
--------
1. 统一把各种格式降成「行文本」再识别，不另造第二套业务字段：
   Word（.docx/.doc）、Excel（.xlsx/.xlsm/.xls）、CSV/TXT、PDF 文字层、
   扫描件 OCR（可用时）、图片 OCR（可用时）。
2. 旧版二进制 `.doc/.xls` 不依赖 Office：OLE 文件里的正文多为 UTF-16LE，
   直接按「含中文的连续片段」扫描；Office COM 只作解析不到时的增强通道。
3. 三条互不冲突的取数线索，按置信度排序：
   · 明确标签 + 数字（合同金额 / 合同总金额 / 价税合计 / 合计金额 …）
   · 中文大写金额（壹万肆仟肆佰捌拾肆圆整 → 14484）
   · 总计 / 合计 / 人民币（小写）等弱标签、标签行后 3 行内的裸金额
4. **干扰项硬排除**：不含税金额、增值税金额、税额、单价、数量、运费、
   合同额 20%、已付/预付款等，连同其后的数字一并抹掉，避免把
   12817.7 / 1666.3 错当合同总额。
5. 只读、零副作用、永不抛异常：任何解析失败都返回 amount=None，
   由服务层留空并提示人工补录，绝不退回订单金额。

对外入口
--------
`read_contract_amount(path)` → {amount, label, source, score, note, candidates}
`best_amount(hits)`          → 多文件命中里挑置信度最高的一条
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

WORD_EXTS = {".doc", ".docx", ".docm", ".wps", ".wpt", ".rtf", ".odt"}
TABLE_EXTS = {".xls", ".xlsx", ".xlsm", ".et", ".csv", ".tsv", ".txt"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
SUPPORTED_EXTS = WORD_EXTS | TABLE_EXTS | PDF_EXTS | IMAGE_EXTS

# ── 金额标签 ────────────────────────────────────────────────
# 注意：不放单独的「金额」「总价」「含税金额」——明细表表头与
# 「不含税金额」都含这些子串，会把明细行或不含税额误判成总额。
STRONG_LABELS: Tuple[str, ...] = (
    "合同总金额", "合同金额", "合同总价", "合同总价款", "合同价款",
    "合同总额", "总金额", "总价金额", "价税合计", "含税合计", "含税总额",
    "合计金额", "金额合计", "总价款", "总计金额", "结算总额", "采购总额",
    "订单总金额", "总合计", "金额大写", "人民币大写", "大写金额",
)
WEAK_LABELS: Tuple[str, ...] = (
    "总计", "合计", "小写", "人民币", "大写", "总额", "价款", "净额",
)

# 「负面词 + 其后 20 字符内的第一个数字」整段抹除
_NEG_WORDS = ("不含税", "未税", "增值税", "税额", "单价", "数量", "运费",
              "税率", "折扣", "优惠", "保证金", "已付", "预付", "余款",
              "质保", "合同额", "比例", "税率", "件数", "重量")
_NEG_RE = re.compile(
    r"(?:" + "|".join(_NEG_WORDS) + r")"
    r"[^，,；;。\n（()）]{0,20}?"
    r"[¥￥]?\s*\d[\d,]*(?:\.\d+)?\s*(?:%|％|元|圆)?"
)

_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9.])(?:[¥￥]\s*)?"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)(?!\d)"
)
# 日期 / 编号类数字先抹掉，避免「签订日期 2026-11-10」抢在合同金额前面
_DATE_RE = re.compile(
    r"\d{4}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*日?"
    r"|\d{4}\s*年\s*\d{1,2}\s*月"
)
_UPPER_CHARS = "零〇壹贰叁肆伍陆柒捌玖一二三四五六七八九两拾十佰百仟千万亿圆元角分整正"
_UPPER_RE = re.compile(r"[" + _UPPER_CHARS + r"]{4,}")

_CN_DIGIT: Dict[str, int] = {
    "零": 0, "〇": 0, "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
    "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9, "两": 2,
}
_CN_UNIT: Dict[str, int] = {"拾": 10, "十": 10, "佰": 100, "百": 100,
                            "仟": 1000, "千": 1000}
_CN_BIG: Dict[str, int] = {"万": 10_000, "亿": 100_000_000}

# OLE 二进制里「含中文的连续片段」（Word .doc 正文多为 UTF-16LE）
_BINARY_SEG_RE = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]"
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef0-9A-Za-z"
    r"\s,.:：;；、|¥￥元圆整角分（）()\-—_/]{2,180}"
)

MAX_AMOUNT = 1e12


# ── 中文大写金额 ────────────────────────────────────────────

def chinese_upper_to_number(text: str) -> Optional[float]:
    """
    壹万肆仟肆佰捌拾肆圆整 → 14484.0；贰万叁仟伍佰圆陆角伍分 → 23500.65
    只接受「至少 4 个连续大写数字/单位字符」且含元/圆/整/大写语境的片段，
    「质保壹年」「一式贰份」之类不会误入。
    """
    raw = str(text or "")
    if len(raw) < 4:
        return None
    core = "".join(ch for ch in raw if ch in _CN_DIGIT or ch in _CN_UNIT
                   or ch in _CN_BIG or ch in "元圆角分整正零〇")
    if len(core) < 4 or not any(ch in core for ch in "拾十佰百仟千万亿元圆角分整正"):
        return None

    # 元 / 圆 之前为整数部分，之后为角分
    head, tail = core, ""
    hit = re.search(r"[元圆]", core)
    if hit:
        head, tail = core[:hit.start()], core[hit.start() + 1:]

    total = 0
    section = 0
    current = 0
    seen = False
    for ch in head:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            section += (current or 1) * _CN_UNIT[ch]
            current = 0
            seen = True
        elif ch in _CN_BIG:
            section += current
            total += section * _CN_BIG[ch]
            section = current = 0
            seen = True
        # 「零」及未知字符忽略
    integer = total + section + current
    if not seen or integer <= 0:
        return None

    decimal = Decimal(0)
    m = re.search(r"([零〇壹贰叁肆伍陆柒捌玖一二三四五六七八九两])角", tail)
    if m:
        decimal += Decimal(_CN_DIGIT[m.group(1)]) / 10
    m = re.search(r"([零〇壹贰叁肆伍陆柒捌玖一二三四五六七八九两])分", tail)
    if m:
        decimal += Decimal(_CN_DIGIT[m.group(1)]) / 100
    value = float(Decimal(integer) + decimal)
    if not 0 < value < MAX_AMOUNT:
        return None
    return round(value, 2)


# ── 行文本抽取 ──────────────────────────────────────────────

def _clean_line(text: Any) -> str:
    s = "" if text is None else str(text)
    s = s.replace("\x07", " | ").replace("\r", "\n").replace("\t", " | ")
    s = s.translate(str.maketrans("１２３４５６７８９０．，￥",
                                  "1234567890.,¥"))
    return re.sub(r"[ \u3000]+", " ", s).strip()


def _lines_from_docx(path: Path) -> List[str]:
    from docx import Document                                  # 延迟导入
    doc = Document(str(path))
    lines = [_clean_line(p.text) for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            lines.append(" | ".join(_clean_line(c.text) for c in row.cells))
    return [x for x in lines if x]


def _lines_from_workbook(path: Path) -> List[str]:
    import openpyxl                                            # 延迟导入
    book = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    lines: List[str] = []
    try:
        for sheet in book.worksheets:
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= 5000:
                    break
                cells = [_clean_cell_value(v) for v in row]
                cells = [c for c in cells if c]
                if cells:
                    lines.append(" | ".join(cells))
    finally:
        book.close()
    return lines


def _clean_cell_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _clean_line(value)


def _lines_from_text_file(path: Path) -> List[str]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030", "utf-16", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return [x for x in (_clean_line(l) for l in text.splitlines()) if x]


def _lines_from_pdf(path: Path) -> List[str]:
    lines: List[str] = []
    try:
        import pdfplumber                                       # 延迟导入
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:10]:
                try:
                    lines.extend(_clean_line(l)
                                 for l in (page.extract_text() or "").splitlines())
                except Exception:
                    pass
                try:
                    for table in page.extract_tables() or []:
                        for row in table:
                            cells = [_clean_line(c) for c in row if c]
                            if cells:
                                lines.append(" | ".join(cells))
                except Exception:
                    pass
    except Exception:
        pass
    if not any(lines):
        try:                                                    # 纯文字 PDF 兜底
            from PyPDF2 import PdfReader
            for page in PdfReader(str(path)).pages[:10]:
                lines.extend(_clean_line(l)
                             for l in (page.extract_text() or "").splitlines())
        except Exception:
            pass
    return [x for x in lines if x]


def _lines_from_ole_binary(path: Path) -> List[str]:
    """
    旧版 .doc/.xls（OLE 复合文档）无 Office 时的兜底通道。

    Word 正文流以 UTF-16LE 保存，整文件解码会夹杂大量二进制乱码；
    只保留「以中文开头、含常见合同字符」的片段即可还原
    「人民币：壹万肆仟肆佰捌拾肆圆整」「总计：」「14484」等关键行。
    """
    raw = path.read_bytes()
    texts = [raw.decode("utf-16-le", "ignore"), raw.decode("gb18030", "ignore")]
    lines: List[str] = []
    key_re = re.compile(
        r"金额|合计|总计|价税|人民币|大写|价款|总额|结算|小写|净额|"
        r"[壹贰叁肆伍陆柒捌玖]|圆整|元整")
    for text in texts:
        for seg in _BINARY_SEG_RE.findall(text):
            for piece in re.split(r"[\r\n]+", seg):
                piece = _clean_line(piece)
                if len(piece) < 3 or not re.search(r"[\u4e00-\u9fff]", piece):
                    continue
                if (re.search(r"[0-9:：|¥￥元圆整角分]", piece)
                        and (key_re.search(piece)
                             or re.search(r"[¥￥]\s*\d|\d{4,}", piece))):
                    lines.append(piece)
    # 表格里「总计」的数值常在独立单元格：只补【附近有金额标签、且不在
    # 不含税/增值税等干扰短语之后】的裸数字行，避免把编号年份与税额塞进候选。
    utf16 = texts[0]
    tag_re = re.compile(r"总计|合计|金额|人民币|大写|价款|总额|小写|净额")
    bad_re = re.compile("|".join(_NEG_WORDS))
    for m in re.finditer(r"(?<![\d.])\d{4,9}(?:\.\d{1,2})?(?![\d.])",
                         utf16):
        context = utf16[max(0, m.start() - 60):m.start()]
        if not tag_re.search(context) or bad_re.search(context):
            continue
        if 100 <= float(m.group()) < MAX_AMOUNT:
            lines.append(m.group())
        if len(lines) >= 2000:
            break
    # 去重保序
    seen = set()
    out: List[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
        if len(out) >= 2000:
            break
    return out


def _lines_from_word_com(path: Path) -> List[str]:
    """Word/WPS COM 增强通道：仅在纯 Python 通道识别不到金额时尝试"""
    try:
        import win32com.client as win32                        # 延迟导入
        app = win32.DispatchEx("Word.Application")
        app.Visible = False
        try:
            doc = app.Documents.Open(str(path.resolve()), ReadOnly=True,
                                     AddToRecentFiles=False)
            try:
                text = doc.Content.Text or ""
                for table in doc.Tables:
                    try:
                        text += "\n" + (table.Range.Text or "")
                    except Exception:
                        pass
            finally:
                doc.Close(False)
        finally:
            app.Quit()
    except Exception:
        return []
    return [x for x in (_clean_line(l) for l in text.splitlines()) if x]


def _lines_from_image(path: Path) -> List[str]:
    try:
        from . import docread
        if not docread.ocr_available():
            return []
        return [" | ".join(cells) for cells in docread.ocr_lines(str(path))]
    except Exception:
        return []


def _lines_from_scanned_pdf(path: Path) -> List[str]:
    try:
        from . import docread
        if not docread.ocr_available():
            return []
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        out: List[str] = []
        try:
            for index in range(min(len(pdf), 2)):
                image = pdf[index].render(scale=2.0).to_pil()
                out.extend(" | ".join(cells)
                           for cells in docread.ocr_lines(image))
        finally:
            pdf.close()
        return out
    except Exception:
        return []


def _iter_lines(path: Path, allow_ocr: bool) -> Tuple[str, List[str], List[str]]:
    """→ (通道标签, 行文本列表, 诊断说明)"""
    ext = path.suffix.lower()
    notes: List[str] = []
    if ext in IMAGE_EXTS:
        return "图片 OCR", (_lines_from_image(path) if allow_ocr else []), notes
    if ext in PDF_EXTS:
        lines = _lines_from_pdf(path)
        notes.append(f"PDF 文字层 {len(lines)} 行")
        if not lines and allow_ocr:
            lines = _lines_from_scanned_pdf(path)
            notes.append("无文字层，已尝试 OCR 前 2 页")
        return "PDF", lines, notes
    if ext in (".csv", ".tsv", ".txt"):
        return "文本", _lines_from_text_file(path), notes
    if ext in (".xlsx", ".xlsm", ".et"):
        try:
            return "Excel", _lines_from_workbook(path), notes
        except Exception as exc:
            notes.append(f"openpyxl 读取失败：{type(exc).__name__}")
    if ext in (".docx", ".docm"):
        try:
            return "Word", _lines_from_docx(path), notes
        except Exception as exc:
            notes.append(f"python-docx 读取失败：{type(exc).__name__}")
    # 旧版 OLE / 非常规扩展：二进制扫描优先（快、无需 Office）
    if path.is_file():
        lines = _lines_from_ole_binary(path)
        notes.append(f"OLE/二进制扫描 {len(lines)} 行")
        if lines:
            return "二进制正文", lines, notes
    if ext in (".doc", ".xls", ".wps", ".wpt", ".rtf", ".odt"):
        lines = _lines_from_word_com(path)
        notes.append(f"Office COM 正文 {len(lines)} 行")
        return "Office COM", lines, notes
    return "未支持", [], notes


# ── 金额识别 ────────────────────────────────────────────────

def _strip_negative(text: str) -> str:
    """抹掉「不含税金额12817.7元 / 合同额20%」等干扰短语与日期数字"""
    return _NEG_RE.sub(" ", _DATE_RE.sub(" ", text))


def _candidate(amount: float, score: int, label: str, source: str,
               text: str, kind: str) -> Dict[str, Any]:
    return {"amount": round(float(amount), 2), "score": score,
            "label": label, "source": source, "text": text[:120], "kind": kind}


def extract_from_lines(lines: Sequence[str], source: str = "") -> List[Dict[str, Any]]:
    """行文本 → 金额候选（按出现顺序，调用方按 score 取最佳）"""
    found: List[Dict[str, Any]] = []
    pending: List[Tuple[int, str]] = []       # (截止行号, 标签)
    for index, raw in enumerate(lines):
        line = _clean_line(raw)
        if not line:
            continue
        cleaned = _strip_negative(line)
        strong = next((k for k in STRONG_LABELS if k in cleaned), "")
        weak = next((k for k in WEAK_LABELS if k in cleaned), "")
        has_upper_word = any(k in cleaned for k in ("大写", "人民币", "圆整", "元整"))
        is_label_line = bool(strong or weak)
        if is_label_line:
            pending.append((index + 3, strong or weak))

        # ① 中文大写金额
        for m in _UPPER_RE.finditer(cleaned):
            value = chinese_upper_to_number(m.group())
            if value is None:
                continue
            score = 100 if strong else (95 if has_upper_word or is_label_line else 70)
            upper_label = (strong or ("人民币大写" if has_upper_word
                                      else weak or "中文大写金额"))
            found.append(_candidate(value, score, upper_label,
                                    source, line, "upper"))

        label = strong or weak
        label_pos = cleaned.find(label) if label else 0
        # ② 阿拉伯数字金额（只接受带标签的行；标签之后的数值优先）
        if is_label_line:
            for m in _NUM_RE.finditer(cleaned):
                try:
                    value = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if not 0 < value < MAX_AMOUNT:
                    continue
                base = 100 if strong else 80
                score = (base + (12 if m.start() >= label_pos else -25)
                         + (6 if ("," in m.group(1) or "." in m.group(1)) else 0))
                found.append(_candidate(value, score, label, source,
                                        line, "number"))

        # ③ 标签行之后 3 行内的裸金额（「总计：」与数值分处两个单元格/段落）
        active = [p for p in pending if p[0] >= index and index > p[0] - 3]
        if active and not is_label_line:
            label = active[-1][1]
            for m in _NUM_RE.finditer(cleaned):
                try:
                    value = float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if value >= 1000:                  # 过滤数量 / 单价等小数字
                    found.append(_candidate(value, 55, label, source,
                                            line, "followup"))
                    break
        pending = [p for p in pending if p[0] > index]
    return found


def _pick(candidates: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not candidates:
        return None
    # 同分取先出现者；大写与数字互相印证时额外加 3 分
    counts: Dict[float, int] = {}
    for c in candidates:
        counts[c["amount"]] = counts.get(c["amount"], 0) + 1
    ranked = [(c["score"] + (3 if counts[c["amount"]] > 1 else 0), -i, c)
              for i, c in enumerate(candidates)]
    return max(ranked, key=lambda t: (t[0], t[1]))[2]


def best_amount(hits: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """多份合同文件的命中结果里挑最佳；无命中返回空 hit"""
    valid = [h for h in hits if h and h.get("amount") is not None]
    if not valid:
        return {"amount": None, "label": "", "source": "", "score": 0,
                "note": "", "candidates": []}
    return max(valid, key=lambda h: (h.get("score") or 0, h.get("amount") or 0))


def read_contract_amount(path: str, *, allow_ocr: bool = True) -> Dict[str, Any]:
    """
    读取一份合同文档的总金额。**只读、零副作用、永不抛异常**。

    返回 {amount: float|None, label, source, score, note, candidates}；
    `amount=None` 表示未识别到，调用方应留空并提示人工补录，禁止回退订单金额。
    """
    empty: Dict[str, Any] = {"amount": None, "label": "", "source": "",
                             "score": 0, "note": "", "candidates": []}
    p = Path(str(path or ""))
    if not p.is_file():
        empty["note"] = "文件不存在"
        return empty
    if p.suffix.lower() not in SUPPORTED_EXTS:
        empty["note"] = f"不支持解析的附件类型：{p.suffix or '无扩展名'}"
        return empty
    try:
        kind, lines, notes = _iter_lines(p, allow_ocr)
        candidates = extract_from_lines(lines, p.name)
        hit = _pick(candidates)
        if hit is None and kind == "二进制正文" and p.suffix.lower() in (".doc", ".xls"):
            # 二进制通道没认出来，再借本机 Word 拿完整正文
            lines = _lines_from_word_com(p)
            candidates = extract_from_lines(lines, p.name)
            hit = _pick(candidates)
            kind = "Office COM"
            notes.append("Office COM 增强解析")
        result = dict(empty)
        result["note"] = "；".join(notes)
        result["candidates"] = candidates[:20]
        if hit:
            result.update({"amount": hit["amount"], "label": hit["label"],
                           "source": p.name, "score": hit["score"]})
        elif not lines:
            result["note"] = (result["note"] + "；" if result["note"] else "") \
                + "未提取到可识别文字（扫描件或加密文档）"
        return result
    except Exception as exc:                                    # noqa: BLE001
        empty["note"] = f"解析异常已忽略：{type(exc).__name__}: {exc}"
        return empty
