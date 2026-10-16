"""Basic PDF imposition helpers for MADI Preflight."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import fitz


PT_PER_MM = 72.0 / 25.4

SHEET_PRESETS_MM: dict[str, tuple[float, float]] = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "SRA3": (320.0, 450.0),
    "B4": (250.0, 353.0),
    "B3": (353.0, 500.0),
}


def mm_to_pt(value: float) -> float:
    return float(value) * PT_PER_MM


def sheet_preset_mm(name: str) -> tuple[float, float] | None:
    return SHEET_PRESETS_MM.get((name or "").strip().upper())


def _float_arg(args: SimpleNamespace, name: str, default: float = 0.0) -> float:
    value = getattr(args, name, default)
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
    return float(value)


def _int_arg(args: SimpleNamespace, name: str, default: int = 1) -> int:
    value = getattr(args, name, default)
    if isinstance(value, str):
        value = value.strip()
        if value.lower() == "auto":
            return default
    return int(float(value))


def _bool_arg(args: SimpleNamespace, name: str, default: bool = False) -> bool:
    value = getattr(args, name, default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "tak", "yes", "on"}
    return bool(value)


@dataclass
class ImposerSettings:
    sheet_width_mm: float
    sheet_height_mm: float
    rows: int
    columns: int
    margin_top_mm: float
    margin_bottom_mm: float
    margin_left_mm: float
    margin_right_mm: float
    gap_x_mm: float
    gap_y_mm: float
    position_width_mm: float | None
    position_height_mm: float | None
    repeat_count: int
    duplex: bool
    mirror_back: bool
    auto_rotate: bool
    add_blanks: bool
    draw_frames: bool
    draw_numbers: bool
    draw_sheet_labels: bool
    fit_mode: str


def settings_from_args(args: SimpleNamespace) -> ImposerSettings:
    preset = sheet_preset_mm(getattr(args, "imposer_sheet", "SRA3"))
    if preset and getattr(args, "imposer_sheet", "").strip().lower() != "custom":
        sheet_width_mm, sheet_height_mm = preset
    else:
        sheet_width_mm = _float_arg(args, "imposer_sheet_width_mm", 320.0)
        sheet_height_mm = _float_arg(args, "imposer_sheet_height_mm", 450.0)

    repeat_raw = str(getattr(args, "imposer_repeat_count", "1")).strip().lower()
    repeat_count = 1 if repeat_raw in {"", "auto"} else max(_int_arg(args, "imposer_repeat_count", 1), 1)
    auto_positions = _bool_arg(args, "imposer_auto_positions", True)

    return ImposerSettings(
        sheet_width_mm=sheet_width_mm,
        sheet_height_mm=sheet_height_mm,
        rows=max(_int_arg(args, "imposer_rows", 1), 1),
        columns=max(_int_arg(args, "imposer_columns", 1), 1),
        margin_top_mm=max(_float_arg(args, "imposer_margin_top_mm", 0.0), 0.0),
        margin_bottom_mm=max(_float_arg(args, "imposer_margin_bottom_mm", 0.0), 0.0),
        margin_left_mm=max(_float_arg(args, "imposer_margin_left_mm", 0.0), 0.0),
        margin_right_mm=max(_float_arg(args, "imposer_margin_right_mm", 0.0), 0.0),
        gap_x_mm=max(_float_arg(args, "imposer_gap_x_mm", 0.0), 0.0),
        gap_y_mm=max(_float_arg(args, "imposer_gap_y_mm", 0.0), 0.0),
        position_width_mm=None if auto_positions else max(_float_arg(args, "imposer_position_width_mm", 0.0), 0.0),
        position_height_mm=None if auto_positions else max(_float_arg(args, "imposer_position_height_mm", 0.0), 0.0),
        repeat_count=repeat_count,
        duplex=_bool_arg(args, "imposer_duplex", False),
        mirror_back=_bool_arg(args, "imposer_mirror_back", True),
        auto_rotate=_bool_arg(args, "imposer_auto_rotate", True),
        add_blanks=_bool_arg(args, "imposer_add_blanks", True),
        draw_frames=_bool_arg(args, "imposer_draw_frames", False),
        draw_numbers=_bool_arg(args, "imposer_draw_numbers", False),
        draw_sheet_labels=_bool_arg(args, "imposer_draw_sheet_labels", True),
        fit_mode=str(getattr(args, "imposer_fit_mode", "Fit")).strip().lower(),
    )


def _slot_rect(settings: ImposerSettings, row: int, col: int, mirror: bool) -> fitz.Rect:
    sheet_w = mm_to_pt(settings.sheet_width_mm)
    left = mm_to_pt(settings.margin_left_mm)
    top = mm_to_pt(settings.margin_top_mm)
    right = mm_to_pt(settings.margin_right_mm)
    bottom = mm_to_pt(settings.margin_bottom_mm)
    gap_x = mm_to_pt(settings.gap_x_mm)
    gap_y = mm_to_pt(settings.gap_y_mm)
    usable_w = sheet_w - left - right - gap_x * max(settings.columns - 1, 0)
    usable_h = mm_to_pt(settings.sheet_height_mm) - top - bottom - gap_y * max(settings.rows - 1, 0)
    if usable_w <= 0 or usable_h <= 0:
        raise ValueError("Marginesy i odstępy Imposera są większe niż format arkusza.")
    auto_w = usable_w / settings.columns
    auto_h = usable_h / settings.rows
    cell_w = mm_to_pt(settings.position_width_mm) if settings.position_width_mm else auto_w
    cell_h = mm_to_pt(settings.position_height_mm) if settings.position_height_mm else auto_h
    if cell_w <= 0 or cell_h <= 0:
        raise ValueError("Wymiary użytku w Imposerze muszą być większe niż 0.")
    actual_col = settings.columns - 1 - col if mirror else col
    x0 = left + actual_col * (auto_w + gap_x)
    y0 = top + row * (auto_h + gap_y)
    return fitz.Rect(x0, y0, x0 + cell_w, y0 + cell_h)


def _slot_pages(src: fitz.Document, repeat_count: int) -> list[int]:
    pages: list[int] = []
    for page_index in range(src.page_count):
        pages.extend([page_index] * repeat_count)
    return pages


def _extend_slots(slots: list[int | None], slots_per_side: int, add_blanks: bool) -> list[int | None]:
    if not add_blanks:
        return slots
    padded = list(slots)
    while len(padded) < slots_per_side:
        padded.append(None)
    return padded


def _draw_crop_marks(page: fitz.Page, rect: fitz.Rect) -> None:
    color = (0.0, 0.0, 0.0)
    length = mm_to_pt(4.0)
    offset = mm_to_pt(2.0)
    width = 0.25
    coords = (
        ((rect.x0 - offset - length, rect.y0), (rect.x0 - offset, rect.y0)),
        ((rect.x0, rect.y0 - offset - length), (rect.x0, rect.y0 - offset)),
        ((rect.x1 + offset, rect.y0), (rect.x1 + offset + length, rect.y0)),
        ((rect.x1, rect.y0 - offset - length), (rect.x1, rect.y0 - offset)),
        ((rect.x0 - offset - length, rect.y1), (rect.x0 - offset, rect.y1)),
        ((rect.x0, rect.y1 + offset), (rect.x0, rect.y1 + offset + length)),
        ((rect.x1 + offset, rect.y1), (rect.x1 + offset + length, rect.y1)),
        ((rect.x1, rect.y1 + offset), (rect.x1, rect.y1 + offset + length)),
    )
    for start, end in coords:
        page.draw_line(fitz.Point(*start), fitz.Point(*end), color=color, width=width)


def _place_pdf_page(
    sheet: fitz.Page,
    src: fitz.Document,
    source_page_index: int,
    rect: fitz.Rect,
    settings: ImposerSettings,
) -> None:
    src_rect = src[source_page_index].rect
    rotate = 0
    if settings.auto_rotate:
        src_landscape = src_rect.width > src_rect.height
        dst_landscape = rect.width > rect.height
        if src_landscape != dst_landscape:
            rotate = 90
    keep = settings.fit_mode not in {"fill", "non-prop fill", "nonprop fill", "nonprop_fill"}
    sheet.show_pdf_page(rect, src, source_page_index, keep_proportion=keep, rotate=rotate)


def _draw_blank(sheet: fitz.Page, rect: fitz.Rect) -> None:
    sheet.draw_rect(rect, color=(0.65, 0.65, 0.65), fill=(0.88, 1.0, 1.0), width=0.4)
    sheet.insert_textbox(rect, "Blank", fontsize=18, color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER)


def _render_side(
    out: fitz.Document,
    src: fitz.Document,
    settings: ImposerSettings,
    slots: list[int | None],
    side_label: str,
    mirror: bool,
    sheet_number: int,
) -> None:
    sheet = out.new_page(width=mm_to_pt(settings.sheet_width_mm), height=mm_to_pt(settings.sheet_height_mm))
    if settings.draw_sheet_labels:
        sheet.insert_text(
            fitz.Point(mm_to_pt(settings.margin_left_mm), max(mm_to_pt(settings.margin_top_mm) - 8, 12)),
            f"{side_label} - arkusz {sheet_number}",
            fontsize=8,
            color=(0.2, 0.2, 0.2),
        )
    for slot_index, source_page_index in enumerate(slots):
        row = slot_index // settings.columns
        col = slot_index % settings.columns
        rect = _slot_rect(settings, row, col, mirror)
        if source_page_index is None:
            if settings.add_blanks:
                _draw_blank(sheet, rect)
        else:
            _place_pdf_page(sheet, src, source_page_index, rect, settings)
        if settings.draw_frames:
            sheet.draw_rect(rect, color=(0.0, 0.45, 1.0), width=0.35)
            _draw_crop_marks(sheet, rect)
        if settings.draw_numbers:
            label = str(source_page_index + 1) if source_page_index is not None else "Blank"
            marker = fitz.Rect(rect.x0 + rect.width / 2 - 11, rect.y0 + rect.height / 2 - 11, rect.x0 + rect.width / 2 + 11, rect.y0 + rect.height / 2 + 11)
            sheet.draw_oval(marker, color=(1.0, 0.0, 0.55), fill=(0.85, 1.0, 1.0), width=0.4)
            sheet.insert_textbox(marker, label, fontsize=10, color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER)


def process_imposer_pdf(args: SimpleNamespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    settings = settings_from_args(args)
    slots_per_side = settings.rows * settings.columns
    if slots_per_side <= 0:
        raise ValueError("Imposer wymaga co najmniej jednej pozycji na arkuszu.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with fitz.open(str(input_path)) as src:
        if src.page_count <= 0:
            raise ValueError("PDF wejsciowy nie zawiera stron.")
        pages = _slot_pages(src, settings.repeat_count)
        out = fitz.open()
        try:
            if settings.duplex:
                chunk_size = slots_per_side * 2
                sheet_number = 0
                for start in range(0, len(pages), chunk_size):
                    sheet_number += 1
                    front = _extend_slots([page for page in pages[start : start + slots_per_side]], slots_per_side, settings.add_blanks)
                    back = _extend_slots([page for page in pages[start + slots_per_side : start + chunk_size]], slots_per_side, settings.add_blanks)
                    _render_side(out, src, settings, front, "Front side", False, sheet_number)
                    _render_side(out, src, settings, back, "Back side", settings.mirror_back, sheet_number)
            else:
                sheet_number = 0
                for start in range(0, len(pages), slots_per_side):
                    sheet_number += 1
                    side = _extend_slots([page for page in pages[start : start + slots_per_side]], slots_per_side, settings.add_blanks)
                    _render_side(out, src, settings, side, "Sheet", False, sheet_number)
            out.save(str(output_path), garbage=4, deflate=True)
        finally:
            out.close()
    logging.info(
        "Imposer: zapisano %s, arkusz %.2f x %.2f mm, uklad %d x %d.",
        output_path,
        settings.sheet_width_mm,
        settings.sheet_height_mm,
        settings.columns,
        settings.rows,
    )
