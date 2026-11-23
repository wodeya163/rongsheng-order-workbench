"""多格式订单来源读取：图片 / 表格 / PDF / Word → 订单 17 列行字典

设计要点
--------
* 唯一出口 `read_source()` 产出 `rows`（键为原始表头/字段标签的 dict 列表），
  直接交给 `core/order_fields.parse_rows()`，与 CSV/Excel 导入共用同一套
  别名解析、分组、派生金额与逐行报告 —— 绝不另造第二套字段口径。
* 表格类（xlsx/xlsm/xls/csv/tsv/txt）：逐个工作表试识别，取「表头命中的订单字段最多」
  的那个；旧版 .xls 先经本机 Excel 转 .xlsx。
* Word（docx/doc）：优先取表头能命中订单字段的表；否则退化为正文「键：值」解析
  （纸质单/扫描单常见形态）。旧版 .doc 先经本机 Word 转 .docx。
* PDF：pdfplumber 抽表 → 命中即用；否则抽文本按「分栏/键值」解析；
  纯扫描件用 pypdfium2 渲染成图后 OCR。
* 图片：Tesseract（chi_sim+eng）按词框聚成「行 + 列」，再套同一套表头识别；
  未装 Tesseract 时抛出可读的降级说明，不静默失败。

所有可选依赖都在函数内延迟导入：缺哪个只影响对应格式，模块本身零成本可导入。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import order_fields as ofdefs

# ── 支持格式 ─────────────────────────────────────────────────

TABLE_EXTS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}
WORD_EXTS = {".docx", ".doc"}
PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
SUPPORTED_EXTS = TABLE_EXTS | WORD_EXTS | PDF_EXTS | IMAGE_EXTS

FILE_FILTER = (
    "所有支持格式 (*.xlsx *.xlsm *.xls *.csv *.tsv *.txt *.pdf *.docx *.doc "
    "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;"
    "表格 (*.xlsx *.xlsm *.xls *.csv *.tsv);;"
    "PDF (*.pdf);;Word (*.docx *.doc);;"
    "图片 (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff);;所有文件 (*.*)"
)

KIND_LABELS = {"table": "表格", "word": "Word", "pdf": "PDF", "image": "图片"}

# 17 列标准标签（与「新建/导入订单」完全一致）+ 字段 key ↔ 标签
CANONICAL_LABELS: List[str] = ofdefs.headers()
LABEL_OF: Dict[str, str] = {f["key"]: f["label"] for f in ofdefs.ALL_FIELDS}
KEY_OF: Dict[str, str] = {f["label"]: f["key"] for f in ofdefs.ALL_FIELDS}


def to_canonical(rows: Sequence[Dict[str, Any]],
                 headers: Optional[Sequence[str]] = None
                 ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    任意来源行 → 17 列标准标签行（缺列补空串）。

    统一到标准标签后，界面预览可逐格编辑，确认后仍交给
    `order_fields.parse_rows()`，与「导入订单」走同一套口径。
    返回 (rows, 未识别列名)。
    """
    names = list(headers or (list(rows[0].keys()) if rows else []))
    mapping, ignored = ofdefs.resolve_headers(names)
    out: List[Dict[str, Any]] = []
    for raw in rows:
        row: Dict[str, Any] = {label: "" for label in CANONICAL_LABELS}
        for key, header_name in mapping.items():
            label = LABEL_OF.get(key)
            value = raw.get(header_name)
            if value is None:
                continue
            text = str(value).strip()
            if label in row:
                if not str(row[label]).strip() and text:
                    row[label] = value
            elif label and text:
                row[label] = value
        out.append(row)
    return out, ignored

_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    "/usr/bin/tesseract",
    "/usr/local/bin/tesseract",
)

# 表头识别时最多向下扫多少行（订单表上方常有标题/抬头）
_HEADER_SCAN_ROWS = 15


# ── 通用工具 ─────────────────────────────────────────────────

def supported_exts() -> List[str]:
    """给界面展示用：`*.png / *.xlsx / …`"""
    return sorted(SUPPORTED_EXTS)


def _clean_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("\r", " ").replace("\u3000", " ").strip()


def split_cells(line: str) -> List[str]:
    """一行文本 → 单元格列表：优先制表符/竖线，其次 2+ 空格"""
    text = _clean_cell(line)
    if not text:
        return []
    if "\t" in text:
        return [c.strip() for c in text.split("\t") if c.strip()]
    if "|" in text:
        return [c.strip() for c in re.split(r"\s*\|\s*", text.strip(" |")) if c.strip()]
    parts = [c.strip() for c in re.split(r"\s{2,}", text)]
    return [c for c in parts if c]


def _dedupe_headers(cells: Sequence[str]) -> List[str]:
    """同名表头加序号后缀，避免 dict 覆盖丢列"""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for index, raw in enumerate(cells):
        name = _clean_cell(raw) or f"列{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}#{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def _cluster_tokens(tokens: Sequence[Tuple[float, float, float, float, str]]) -> List[List[str]]:
    """
    词框 (left, top, width, height, text) → 「行 × 列」文本。

    先按 top 聚行（阈值 = 0.6 倍中位字高），再按词间距（> 0.55 倍中位字高）切列。
    PDF 与 OCR 共用这一套：PDF 抽出的中文常是「单空格或无空格」，纯文本切分拿不到列，
    靠坐标聚类才稳。
    """
    if not tokens:
        return []
    ordered = sorted(tokens, key=lambda t: (t[1], t[0]))
    heights = sorted(t[3] for t in ordered if t[3] and t[3] > 0)
    median_h = heights[len(heights) // 2] if heights else 10.0

    rows: List[List[Tuple[float, float, float, float, str]]] = []
    current = [ordered[0]]
    for token in ordered[1:]:
        if abs(token[1] - current[-1][1]) <= median_h * 0.6:
            current.append(token)
        else:
            rows.append(current)
            current = [token]
    rows.append(current)

    gap_limit = max(8.0, median_h * 0.55)
    lines: List[List[str]] = []
    for row in rows:
        row.sort(key=lambda t: t[0])
        cells: List[List[Tuple[float, float, float, float, str]]] = [[row[0]]]
        for prev, nxt in zip(row, row[1:]):
            gap = nxt[0] - (prev[0] + prev[2])
            if gap > gap_limit:
                cells.append([nxt])
            else:
                cells[-1].append(nxt)
        lines.append(["".join(c[4] for c in cell).strip() for cell in cells])
    return lines


def _matrix_to_rows(matrix: Iterable[Sequence[Any]]) -> Optional[Dict[str, Any]]:
    """
    二维表 → {headers, rows, score}

    在头部若干行里找「命中订单字段最多」的一行当表头（订单表上方常有
    标题、抬头、合计行），其后按同列数切行；遇到空行或列数明显不足即认为表格结束。
    """
    lines: List[List[str]] = []
    for raw in matrix:
        cells = [_clean_cell(c) for c in raw]
        if not any(cells):
            continue
        while cells and not cells[-1]:
            cells.pop()
        lines.append(cells)
    if not lines:
        return None

    best: Optional[Tuple[int, int, List[str]]] = None
    for index, cells in enumerate(lines[:_HEADER_SCAN_ROWS]):
        labels = repair_labels(cells)
        if len([c for c in labels if c]) < 2:
            continue
        mapping, _ = ofdefs.resolve_headers(labels)
        score = len(mapping)
        if score >= 2 and (best is None or score > best[0]):
            best = (score, index, labels)

    if best is None:
        return None

    score, head_index, header_cells = best
    headers = _dedupe_headers(header_cells)
    width = len(headers)
    need = max(2, (width + 1) // 2)
    rows: List[Dict[str, Any]] = []
    for cells in lines[head_index + 1:]:
        filled = len([c for c in cells if c])
        if filled == 0:
            continue
        if filled < need:
            break
        padded = list(cells[:width]) + [""] * max(0, width - len(cells))
        rows.append({headers[i]: padded[i] for i in range(width)})
    return {"headers": headers, "rows": rows, "score": score}


_LABEL_PREFIX = re.compile(r"^[（(【\[]?\s*\d+\s*[)）】\].、:：\-\s]*")
_CJK_GAP = re.compile(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])")
_LABEL_TRIM = " \t\"'“”‘’·•,，.。:：;；|\\/_-—~*"


def clean_label(text: Any) -> str:
    """
    表头噪声归一：OCR 常见的「”数量」「数 量」「物资 名称;」都能还原。
    只用于表头/键名，不作用于数据单元格（避免破坏物资名称里的标点）。
    """
    value = _clean_cell(text)
    value = _CJK_GAP.sub("", value)
    return value.strip(_LABEL_TRIM)


def repair_labels(labels: Sequence[str]) -> List[str]:
    """
    表头模糊修复：OCR 常把「数量」读成「数=」「效量」，别名精确匹配会漏。
    只处理精确匹配失败的标签，要求最佳相似度 ≥0.5 且明显优于次优，避免「单价/总价」误换。
    """
    import difflib

    alias = ofdefs._alias_index()          # 别名/标签 → 字段 key（同包内复用）
    label_of = {f["key"]: f["label"]
                for f in ofdefs.HEAD_FIELDS + ofdefs.ITEM_FIELDS + ofdefs.EXTRA_FIELDS}
    out: List[str] = []
    for raw in labels:
        label = clean_label(raw)
        if not label:
            out.append("")
            continue
        mapping, _ = ofdefs.resolve_headers([label])
        if mapping:
            key = next(iter(mapping))     # resolve_headers 返回 {字段key: 表头名}
            out.append(label_of.get(key, label))
            continue
        scored: List[Tuple[float, str]] = []
        for candidate in alias:
            ratio = difflib.SequenceMatcher(None, label, candidate).ratio()
            if ratio >= 0.5:
                scored.append((ratio, candidate))
        scored.sort(reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
            key = alias[scored[0][1]]
            out.append(label_of.get(key, label))
        else:
            out.append(label)
    return out


def _kv_rows(text: str) -> Optional[Dict[str, Any]]:
    """
    正文「键：值」→ 一行订单（纸质单/扫描单常见）。

    同一份文本里重复出现「物资名称」即视为新明细行；订单头字段只取首次出现。
    """
    head_keys = {f["key"] for f in ofdefs.HEAD_FIELDS}
    pairs: List[Tuple[str, str]] = []
    for raw in str(text or "").splitlines():
        line = _clean_cell(raw)
        if not line:
            continue
        if "：" in line:
            key, value = line.split("：", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = _LABEL_PREFIX.sub("", key)
        key = clean_label(key)
        value = value.strip(" 　*·-—")
        if not key or not value:
            continue
        mapping, _ = ofdefs.resolve_headers([key])
        if not mapping:
            continue
        pairs.append((next(iter(mapping)), value))   # resolve_headers 返回 {字段key: 表头名}

    if not pairs:
        return None

    head: Dict[str, str] = {}
    items: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for key, value in pairs:
        if key in head_keys:
            head.setdefault(key, value)
            continue
        if key == "item_name" and current and current.get("item_name"):
            items.append(current)
            current = None
        if current is None:
            current = {}
        current.setdefault(key, value)
    if current:
        items.append(current)
    if not items:
        return None

    label_of = {f["key"]: f["label"]
                for f in ofdefs.HEAD_FIELDS + ofdefs.ITEM_FIELDS + ofdefs.EXTRA_FIELDS}
    ordered = [f["key"] for f in ofdefs.HEAD_FIELDS + ofdefs.ITEM_FIELDS + ofdefs.EXTRA_FIELDS]
    rows: List[Dict[str, Any]] = []
    headers: List[str] = []
    for item in items:
        merged = dict(head)
        merged.update(item)
        row = {label_of.get(k, k): v for k, v in merged.items()}
        rows.append(row)
        for key in ordered:
            label = label_of.get(key, key)
            if key in merged and label not in headers:
                headers.append(label)
    return {"headers": headers, "rows": rows, "score": 0}


def rows_from_text(text: str) -> Optional[Dict[str, Any]]:
    """文本 → 先按「行×列」识别表头；失败再退化为「键：值」"""
    lines = [_clean_cell(l) for l in str(text or "").splitlines()]
    matrix = [split_cells(l) for l in lines if l]
    result = _matrix_to_rows(matrix)
    if result and result["rows"]:
        return result
    return _kv_rows(text)


# ── OCR ──────────────────────────────────────────────────────

def tesseract_cmd() -> str:
    env = os.environ.get("TESSERACT_CMD", "").strip()
    if env and Path(env).is_file():
        return env
    for candidate in _TESSERACT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("tesseract") or ""


def _tesseract():
    """返回已配置好的 pytesseract 模块；不可用时抛 RuntimeError"""
    try:
        import pytesseract
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "读取图片需要 pytesseract：pip install pytesseract") from exc
    cmd = tesseract_cmd()
    if not cmd:
        raise RuntimeError(
            "未检测到 Tesseract-OCR。\n"
            "请安装 Tesseract（含 chi_sim 中文包）后重试，"
            "或把 tesseract.exe 路径写入环境变量 TESSERACT_CMD。\n"
            "也可直接改用表格 / Word / 文字型 PDF。")
    pytesseract.pytesseract.tesseract_cmd = cmd
    return pytesseract


def ocr_available() -> bool:
    try:
        _tesseract()
        return True
    except Exception:
        return False


def docx_available() -> bool:
    """Word 通道依赖（python-docx）是否可用；打包自检用"""
    try:
        import docx  # noqa: F401
        return True
    except Exception:
        return False


def pdf_available() -> bool:
    """PDF 通道依赖（pdfplumber 抽表 + pypdfium2 渲染）是否可用"""
    try:
        import pdfplumber  # noqa: F401
        import pypdfium2  # noqa: F401
        return True
    except Exception:
        return False


def ocr_langs() -> str:
    """优先中英混合；缺中文包时退回 eng，避免 OCR 直接报错"""
    try:
        pytesseract = _tesseract()
        langs = set(pytesseract.get_languages(config=""))
    except Exception:
        return "eng"
    if "chi_sim" in langs and "eng" in langs:
        return "chi_sim+eng"
    if "chi_sim" in langs:
        return "chi_sim"
    return "eng"


def ocr_lines(image: Any) -> List[List[str]]:
    """
    图片 → 「行 × 列」文本。

    用 `image_to_data` 拿词框，先按 Tesseract 的 block/par/line 归行，
    再按词间距（> 0.55 倍中位字高）切列 —— 比整行 OCR 后按空格切分稳得多，
    因为中文 OCR 结果词间常常不留空格。
    """
    from PIL import Image

    pytesseract = _tesseract()
    from pytesseract import Output

    img = image if isinstance(image, Image.Image) else Image.open(image)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    # 小图放大，提升小字号识别率
    longest = max(img.size)
    if longest and longest < 1400:
        scale = 1400 / longest
        img = img.resize((max(1, int(img.width * scale)),
                          max(1, int(img.height * scale))))

    data = pytesseract.image_to_data(img, lang=ocr_langs(),
                                     output_type=Output.DICT)
    tokens: List[Tuple[float, float, float, float, str]] = []
    for i, word in enumerate(data.get("text", [])):
        text = (word or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = 100.0
        if conf < 0:
            continue
        tokens.append((float(data["left"][i]), float(data["top"][i]),
                       float(data["width"][i]), float(data["height"][i]), text))
    return _cluster_tokens(tokens)


# ── 各格式读取 ───────────────────────────────────────────────

def _convert_with_office(path: Path, app: str, suffix: str,
                         notes: List[str]) -> Optional[Path]:
    """旧版 .doc/.xls 用本机 Office 转成新格式；失败返回 None（由调用方给提示）"""
    try:
        import win32com.client as win32  # type: ignore
    except ImportError:
        notes.append(f"未安装 pywin32，无法自动转换 {path.suffix} 旧格式")
        return None
    target = Path(tempfile.mkdtemp(prefix="docread_")) / (path.stem + suffix)
    try:
        if app == "Word":
            handle = win32.DispatchEx("Word.Application")
            handle.Visible = False
            try:
                doc = handle.Documents.Open(str(path), ReadOnly=True)
                doc.SaveAs2(str(target), FileFormat=16)   # wdFormatXMLDocument
                doc.Close(False)
            finally:
                handle.Quit()
        else:
            handle = win32.DispatchEx("Excel.Application")
            handle.Visible = False
            try:
                book = handle.Workbooks.Open(str(path), ReadOnly=True)
                book.SaveAs(str(target), FileFormat=51)   # xlOpenXMLWorkbook
                book.Close(False)
            finally:
                handle.Quit()
    except Exception as exc:
        notes.append(f"{app} 转换 {path.suffix} 失败（{type(exc).__name__}: {exc}）")
        return None
    notes.append(f"旧格式已由本机 {app} 转为 {suffix}")
    return target if target.is_file() else None


def _from_table(path: Path, notes: List[str]) -> Optional[Dict[str, Any]]:
    ext = path.suffix.lower()
    if ext == ".xls":
        converted = _convert_with_office(path, "Excel", ".xlsx", notes)
        if converted is None:
            raise RuntimeError(
                "读取旧版 .xls 需要本机安装 Microsoft Excel（或先另存为 .xlsx / .csv）")
        path = converted
        ext = ".xlsx"

    if ext in (".csv", ".tsv", ".txt"):
        from . import excelio
        if ext == ".txt":
            text = path.read_text(encoding="utf-8", errors="replace")
            for enc in ("utf-8", "gb18030"):
                try:
                    text = path.read_text(encoding=enc)
                    break
                except UnicodeDecodeError:
                    continue
            return rows_from_text(text)
        rows = excelio.import_rows(str(path))
        if not rows:
            return None
        headers = list(rows[0].keys())
        mapping, _ = ofdefs.resolve_headers(headers)
        return {"headers": headers, "rows": rows, "score": len(mapping)}

    import openpyxl

    book = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    best: Optional[Tuple[int, int, Dict[str, Any], str]] = None
    try:
        for sheet in book.worksheets:
            matrix = []
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= 5000:
                    break
                matrix.append(row)
            result = _matrix_to_rows(matrix)
            if not result or not result["rows"]:
                continue
            candidate = (result["score"], len(result["rows"]), result, sheet.title)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    finally:
        book.close()

    if best is None:
        return None
    result = best[2]
    notes.append(f"工作表「{best[3]}」命中 {result['score']} 个订单字段 / "
                 f"{len(result['rows'])} 行")
    return result


def _from_word(path: Path, notes: List[str]) -> Optional[Dict[str, Any]]:
    if path.suffix.lower() == ".doc":
        converted = _convert_with_office(path, "Word", ".docx", notes)
        if converted is None:
            raise RuntimeError(
                "读取旧版 .doc 需要本机安装 Microsoft Word（或先另存为 .docx）")
        path = converted

    from docx import Document

    doc = Document(str(path))
    best: Optional[Tuple[int, int, Dict[str, Any], int]] = None
    for index, table in enumerate(doc.tables, 1):
        matrix = [[cell.text for cell in row.cells] for row in table.rows]
        result = _matrix_to_rows(matrix)
        if not result or not result["rows"]:
            continue
        candidate = (result["score"], len(result["rows"]), result, index)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    if best is not None:
        result = best[2]
        notes.append(f"Word 第 {best[3]} 张表命中 {result['score']} 个订单字段 / "
                     f"{len(result['rows'])} 行")
        return result

    text = "\n".join(p.text for p in doc.paragraphs)
    result = rows_from_text(text)
    if result:
        notes.append("Word 未发现订单表格，已按正文「键：值」解析")
    return result


def _pdf_render_pages(path: Path, max_pages: int = 3):
    """扫描件 → 页面图片（pypdfium2；未装则返回空列表）"""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []
    images = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for index in range(min(len(pdf), max_pages)):
            page = pdf[index]
            bitmap = page.render(scale=2.2)
            images.append(bitmap.to_pil())
    finally:
        pdf.close()
    return images


def _from_pdf(path: Path, notes: List[str]) -> Optional[Dict[str, Any]]:
    texts: List[str] = []
    lines: List[List[str]] = []
    tables: List[Tuple[int, int, Dict[str, Any]]] = []
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages[:8]:
                for table in (page.extract_tables() or []):
                    result = _matrix_to_rows(table)
                    if result and result["rows"]:
                        tables.append((result["score"], len(result["rows"]), result))
                try:
                    words = page.extract_words()
                except Exception:
                    words = []
                if words:
                    lines.extend(_cluster_tokens([
                        (w["x0"], w["top"], w["x1"] - w["x0"],
                         w["bottom"] - w["top"], w["text"]) for w in words]))
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                if page_text:
                    texts.append(page_text)
    except Exception as exc:
        notes.append(f"pdfplumber 解析失败（{type(exc).__name__}），尝试其他通道")

    if tables:
        tables.sort(key=lambda t: (t[0], t[1]), reverse=True)
        result = tables[0][2]
        notes.append(f"PDF 表格命中 {result['score']} 个订单字段 / "
                     f"{len(result['rows'])} 行")
        return result

    # 无表格线时按词框坐标还原列（中文 PDF 抽出的文本多为单空格，纯文本切分不可靠）
    if lines:
        result = _matrix_to_rows(lines)
        if result and result["rows"]:
            notes.append(f"PDF 按词框还原表格：命中 {result['score']} 个订单字段 / "
                         f"{len(result['rows'])} 行")
            return result

    text = "\n".join(texts)
    if text.strip():
        result = rows_from_text(text)
        if result:
            notes.append("PDF 未发现表格，已按文本「分栏 / 键值」解析")
            return result

    # 纯扫描 PDF：渲染后 OCR
    images = _pdf_render_pages(path)
    if not images:
        notes.append("PDF 既无表格也无文字层，且缺少 pypdfium2 无法渲染页面做 OCR")
        return None
    notes.append(f"PDF 无文字层，已 OCR 前 {len(images)} 页")
    ocr_rows: List[List[str]] = []
    for image in images:
        ocr_rows.extend(ocr_lines(image))
    result = (_matrix_to_rows(ocr_rows)
              or _kv_rows("\n".join(" ".join(cells) for cells in ocr_rows)))
    if result:
        notes.append(f"OCR 识别 {len(result['rows'])} 行")
    return result


def _from_image(path: Path, notes: List[str]) -> Optional[Dict[str, Any]]:
    lines = ocr_lines(path)
    if not lines:
        notes.append("OCR 未识别到任何文字")
        return None
    notes.append(f"OCR 识别 {len(lines)} 行文本（{ocr_langs()}）")
    result = _matrix_to_rows(lines)
    if result and result["rows"]:
        return result
    result = _kv_rows("\n".join(" ".join(cells) for cells in lines))
    if result:
        notes.append("图片按「键：值」解析")
    return result


# ── 对外入口 ─────────────────────────────────────────────────

def read_source(path: str) -> Dict[str, Any]:
    """
    读取任意受支持的订单来源。

    返回 {
      kind: table|word|pdf|image, path, headers, rows, score, notes
    }
    `rows` 可直接交给 core/order_fields.parse_rows()。
    识别不出任何明细时抛 RuntimeError，并把每步尝试写进 message。

    v3.1.1：先过 `cloudfile.ensure_readable()` —— OneDrive 等按需同步的
    「仅联机」占位文件会被水合成本地实体副本，避免 openpyxl / docx /
    pdfplumber 直接读云端壳时无限阻塞或抛 Errno 22。
    """
    from . import cloudfile
    local = cloudfile.ensure_readable(path)
    p = Path(local)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    ext = p.suffix.lower()
    notes: List[str] = []
    if local != str(path):
        notes.append("云盘「仅联机」文件已同步到本地缓存后读取")

    if ext in IMAGE_EXTS:
        kind, result = "image", _from_image(p, notes)
    elif ext in PDF_EXTS:
        kind, result = "pdf", _from_pdf(p, notes)
    elif ext in WORD_EXTS:
        kind, result = "word", _from_word(p, notes)
    elif ext in TABLE_EXTS:
        kind, result = "table", _from_table(p, notes)
    else:
        raise RuntimeError(
            f"不支持的文件类型：{ext or '（无扩展名）'}\n"
            f"支持：{' / '.join(sorted(SUPPORTED_EXTS))}")

    if not result or not result.get("rows"):
        detail = ("\n".join(f"· {n}" for n in notes) if notes else "· 未识别到表格或「键：值」结构")
        raise RuntimeError(
            "未能从该文件识别出订单明细。\n" + detail +
            "\n\n提示：表格类文件请保留一行表头（物资名称、数量等）；"
            "图片/扫描件请保证清晰、正拍；也可先用「下载订单导入模板」整理成标准表再导入。")

    canonical, ignored_columns = to_canonical(result["rows"], result.get("headers"))
    if not canonical:
        raise RuntimeError("识别结果为空：该文件没有可用的数据行")

    return {
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "path": str(path),                 # 展示原始路径，不暴露临时缓存
        "headers": result.get("headers", []),
        "rows": canonical,                 # 17 列标准标签，可直接编辑
        "raw_rows": result["rows"],        # 原始抽取结果，便于排查
        "ignored_columns": ignored_columns,
        "score": result.get("score", 0),
        "notes": notes,
    }


def ensure_grouping(rows: List[Dict[str, Any]], default_key: str) -> bool:
    """
    分组兜底：一份单据/一张扫描件通常就是一张订单。

    若所有行的「订单项目编号 / ERP订单编号 / 订单分组」全为空，则写入统一的
    订单分组（默认取文件名），避免解析层「都留空 → 每行独立成单」把一张单拆成 N 张。
    返回是否注入。
    """
    def all_blank(label: str) -> bool:
        return all(not str(row.get(label) or "").strip() for row in rows)

    if all_blank("订单项目编号") and all_blank("ERP订单编号") and all_blank("订单分组"):
        for row in rows:
            row["订单分组"] = default_key
        return True
    return False


def summarize(source: Dict[str, Any]) -> str:
    """一行式来源摘要，给界面提示用"""
    kind = source.get("kind_label") or source.get("kind")
    rows = len(source.get("rows") or [])
    score = source.get("score") or 0
    return f"{kind} · 识别 {rows} 行 · 命中 {score} 个订单字段"
