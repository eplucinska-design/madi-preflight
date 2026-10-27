"""Heuristic removal of crop marks, dimensioning and leftover technical marks."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import fitz


PT_PER_MM = 72.0 / 25.4
DIMENSION_TEXT_RE = re.compile(r"(\d+[,.]?\d*\s*(mm|cm|pt)|\d+\s*[x×]\s*\d+|^[<>|/_\\-]+$)", re.IGNORECASE)


def mm_to_pt(value: float) -> float:
    return float(value) * PT_PER_MM


def _float_arg(args: SimpleNamespace, name: str, default: float) -> float:
    value = getattr(args, name, default)
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    try:
        return float(value)
    except Exception:
        return float(default)


def _bool_arg(args: SimpleNamespace, name: str, default: bool = False) -> bool:
    value = getattr(args, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "tak", "yes", "on"}
    return bool(value)


def _page_trim(page: fitz.Page) -> fitz.Rect:
    try:
        trim = page.trimbox
        if trim and not trim.is_empty:
            return fitz.Rect(trim)
    except Exception:
        pass
    return fitz.Rect(page.rect)


def _near_rect_edge(rect: fitz.Rect, reference: fitz.Rect, edge_pt: float) -> bool:
    return (
        rect.x0 <= reference.x0 + edge_pt
        or rect.x1 >= reference.x1 - edge_pt
        or rect.y0 <= reference.y0 + edge_pt
        or rect.y1 >= reference.y1 - edge_pt
        or not rect.intersects(reference)
    )


def _inflate(rect: fitz.Rect, amount: float, page_rect: fitz.Rect) -> fitz.Rect:
    out = fitz.Rect(rect.x0 - amount, rect.y0 - amount, rect.x1 + amount, rect.y1 + amount)
    return out & page_rect


def _drawing_looks_like_mark(drawing: dict, rect: fitz.Rect, max_width_pt: float) -> bool:
    stroke_width = float(drawing.get("width") or 0.0)
    if stroke_width > max_width_pt:
        return False
    if rect.is_empty:
        return False
    thin = min(rect.width, rect.height) <= max(3.0, max_width_pt * 3.0)
    long_line = max(rect.width, rect.height) >= 8.0 and thin
    small_mark = rect.width <= mm_to_pt(35) and rect.height <= mm_to_pt(35)
    no_fill = drawing.get("fill") in (None, (), [])
    return bool(no_fill and (long_line or small_mark))


def _span_looks_like_mark(text: str, rect: fitz.Rect, reference: fitz.Rect, edge_pt: float) -> bool:
    value = " ".join(text.split())
    if not value:
        return False
    if len(value) > 32:
        return False
    if not _near_rect_edge(rect, reference, edge_pt):
        return False
    return bool(DIMENSION_TEXT_RE.search(value))


def _collect_redactions(page: fitz.Page, edge_pt: float, max_width_pt: float, include_inside: bool) -> list[fitz.Rect]:
    page_rect = fitz.Rect(page.rect)
    reference = _page_trim(page)
    redactions: list[fitz.Rect] = []

    for drawing in page.get_drawings(extended=True):
        rect = fitz.Rect(drawing.get("rect") or fitz.Rect())
        if rect.is_empty or not rect.intersects(page_rect):
            continue
        if not include_inside and not _near_rect_edge(rect, reference, edge_pt):
            continue
        if _drawing_looks_like_mark(drawing, rect, max_width_pt):
            redactions.append(_inflate(rect, max(1.5, max_width_pt * 1.5), page_rect))

    text = page.get_text("dict")
    for block in text.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                value = span.get("text", "")
                rect = fitz.Rect(span.get("bbox", fitz.Rect()))
                if rect.is_empty or not rect.intersects(page_rect):
                    continue
                if _span_looks_like_mark(value, rect, reference, edge_pt):
                    redactions.append(_inflate(rect, 1.5, page_rect))

    return redactions


def process_cleanup_marks_pdf(args: SimpleNamespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    edge_pt = mm_to_pt(max(_float_arg(args, "cleanup_marks_edge_mm", 18.0), 0.0))
    max_width_pt = max(_float_arg(args, "cleanup_marks_max_width_pt", 1.5), 0.1)
    include_inside = _bool_arg(args, "cleanup_marks_include_inside", False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(input_path))
    total = 0
    try:
        for page in doc:
            redactions = _collect_redactions(page, edge_pt, max_width_pt, include_inside)
            for rect in redactions:
                page.add_redact_annot(rect, fill=None)
            if redactions:
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )
                total += len(redactions)
        if total == 0:
            logging.warning("Nie znaleziono znacznikow/wymiarowania do automatycznego usuniecia.")
        else:
            logging.info("Usunieto potencjalne znaczniki/wymiarowanie: %s", total)
        doc.save(str(output_path), garbage=4, deflate=True)
    finally:
        doc.close()
