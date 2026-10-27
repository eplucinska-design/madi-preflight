"""Basic PDF imposition helpers for MADI Preflight."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
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


ORDERS = {"repeat", "collate", "cut_stack", "booklet"}
ROTATIONS = {"auto", "0", "90", "180", "270"}
ALIGN_X = {"left": 0.0, "center": 0.5, "right": 1.0}
ALIGN_Y = {"top": 0.0, "center": 0.5, "bottom": 1.0}
MAX_SHEETS = 2000
# CMYK 100/100/100/100: registration colour, present on every separation.
REGISTRATION = (1.0, 1.0, 1.0, 1.0)


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
    crop_marks: bool
    draw_numbers: bool
    draw_sheet_labels: bool
    fit_mode: str
    order: str = "repeat"
    rotation: str = "auto"
    scale_percent: float = 100.0
    align: str = "center"
    mark_length_mm: float = 4.0
    mark_offset_mm: float = 2.0
    duplex_pairing: str = "sequential"
    bleed_mm: float = 3.0
    registration_marks: bool = False
    creep_mm: float = 0.0
    job_name: str = ""


def _choice(args: SimpleNamespace, name: str, default: str, allowed: set[str]) -> str:
    value = str(getattr(args, name, default)).strip().lower()
    if value not in allowed:
        raise ValueError(f"Nieprawidlowa wartosc impozycji: {name}.")
    return value


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
    rotation = _choice(args, "imposer_rotation", "auto", ROTATIONS)
    if rotation == "auto" and not _bool_arg(args, "imposer_auto_rotate", True):
        rotation = "0"
    align = _choice(args, "imposer_align", "center", {"center"} | {f"{v}-{h}" for v in ALIGN_Y for h in ALIGN_X})
    scale_percent = _float_arg(args, "imposer_scale_percent", 100.0)
    if not 1.0 <= scale_percent <= 1000.0:
        raise ValueError("Skala impozycji musi miescic sie w zakresie 1-1000%.")
    fit_mode = str(getattr(args, "imposer_fit_mode", "Fit")).strip().lower()
    if fit_mode in {"non-prop fill", "nonprop fill", "nonprop_fill", "stretch"}:
        fit_mode = "fill"
    if fit_mode not in {"fit", "fill", "actual", "trim"}:
        raise ValueError("Nieprawidlowe dopasowanie uzytku.")

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
        crop_marks=_bool_arg(args, "imposer_crop_marks", False),
        draw_numbers=_bool_arg(args, "imposer_draw_numbers", False),
        draw_sheet_labels=_bool_arg(args, "imposer_draw_sheet_labels", True),
        fit_mode=fit_mode,
        order=_choice(args, "imposer_order", "repeat", ORDERS),
        rotation=rotation,
        scale_percent=scale_percent,
        align=align,
        mark_length_mm=max(_float_arg(args, "imposer_mark_length_mm", 4.0), 0.0),
        mark_offset_mm=max(_float_arg(args, "imposer_mark_offset_mm", 2.0), 0.0),
        duplex_pairing=_choice(args, "imposer_duplex_pairing", "sequential", {"sequential", "alternate"}),
        bleed_mm=max(_float_arg(args, "imposer_bleed_mm", 3.0), 0.0),
        registration_marks=_bool_arg(args, "imposer_registration_marks", False),
        creep_mm=max(_float_arg(args, "imposer_creep_mm", 0.0), 0.0),
        job_name=str(getattr(args, "imposer_job_name", "") or "")[:120],
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
    if cell_w > auto_w + 0.01 or cell_h > auto_h + 0.01:
        raise ValueError(
            f"Uklad {settings.columns} x {settings.rows} pozycji {cell_w / PT_PER_MM:.1f} x {cell_h / PT_PER_MM:.1f} mm "
            f"z odstepami nie miesci sie na arkuszu w marginesach."
        )
    # Fixed-size positions form one block centred in the area between the margins (as in Montax).
    block_w = cell_w * settings.columns + gap_x * (settings.columns - 1)
    block_h = cell_h * settings.rows + gap_y * (settings.rows - 1)
    origin_x = left + (sheet_w - left - right - block_w) / 2
    origin_y = top + (mm_to_pt(settings.sheet_height_mm) - top - bottom - block_h) / 2
    actual_col = settings.columns - 1 - col if mirror else col
    x0 = origin_x + actual_col * (cell_w + gap_x)
    y0 = origin_y + row * (cell_h + gap_y)
    return fitz.Rect(x0, y0, x0 + cell_w, y0 + cell_h)


def _units(page_count: int, duplex: bool) -> list[tuple[int | None, ...]]:
    """Printable units: a page, or a front/back pair (1-2, 3-4...) for duplex."""
    if not duplex:
        return [(index,) for index in range(page_count)]
    return [(index, index + 1 if index + 1 < page_count else None) for index in range(0, page_count, 2)]


def sheet_plan(settings: ImposerSettings, page_count: int) -> list[list[list[int | None]]]:
    """Sheets -> sides -> slots (source page index or None).

    Shared by export and preview, so both place exactly the same pages.
    With duplex pairing "alternate", back slot k holds the page following
    the one in front slot k; "sequential" keeps the earlier behaviour.
    Mirroring of the back is applied when the side is rendered. Which
    variant matches a job depends on its approved imposition pattern.
    """
    slots = settings.rows * settings.columns
    if settings.order == "booklet":
        return _booklet_plan(page_count)
    if settings.duplex and settings.duplex_pairing == "sequential":
        return _sequential_duplex_plan(settings, page_count, slots)
    units = _units(page_count, settings.duplex)
    if settings.order == "collate":
        sequence = [unit for _ in range(settings.repeat_count) for unit in units]
    else:
        sequence = [unit for unit in units for _ in range(settings.repeat_count)]
    sheet_count = max((len(sequence) + slots - 1) // slots, 1)
    if sheet_count > MAX_SHEETS:
        raise ValueError(f"Impozycja przekracza limit {MAX_SHEETS} arkuszy.")
    sides = 2 if settings.duplex else 1
    plan: list[list[list[int | None]]] = []
    for sheet in range(sheet_count):
        positions = []
        for slot in range(slots):
            # Cut & stack: each position is a stack; sheet n holds item n of every stack.
            index = slot * sheet_count + sheet if settings.order == "cut_stack" else sheet * slots + slot
            positions.append(sequence[index] if index < len(sequence) else None)
        plan.append([[unit[side] if unit is not None else None for unit in positions] for side in range(sides)])
    return plan


def _sequential_duplex_plan(settings: ImposerSettings, page_count: int, slots: int) -> list[list[list[int | None]]]:
    """Previous behaviour: consecutive sides take consecutive pages (front 1,2 / back 3,4)."""
    pages = list(range(page_count))
    if settings.order == "collate":
        sequence = [page for _ in range(settings.repeat_count) for page in pages]
    else:
        sequence = [page for page in pages for _ in range(settings.repeat_count)]
    side_count = max((len(sequence) + slots - 1) // slots, 1)
    side_count += side_count % 2
    if side_count // 2 > MAX_SHEETS:
        raise ValueError(f"Impozycja przekracza limit {MAX_SHEETS} arkuszy.")
    sides = []
    for side in range(side_count):
        positions = []
        for slot in range(slots):
            index = slot * side_count + side if settings.order == "cut_stack" else side * slots + slot
            positions.append(sequence[index] if index < len(sequence) else None)
        sides.append(positions)
    return [sides[index:index + 2] for index in range(0, side_count, 2)]


def _rotation(settings: ImposerSettings, src_rect: fitz.Rect, cell: fitz.Rect) -> int:
    if settings.rotation != "auto":
        return int(settings.rotation)
    return 90 if (src_rect.width > src_rect.height) != (cell.width > cell.height) else 0


def _rotate_insets(insets: tuple[float, float, float, float], rotate: int) -> tuple[float, float, float, float]:
    """Map (left, top, right, bottom) insets through show_pdf_page rotation (counter-clockwise)."""
    left, top, right, bottom = insets
    return {
        0: (left, top, right, bottom),
        90: (top, right, bottom, left),
        180: (right, bottom, left, top),
        270: (bottom, left, top, right),
    }[rotate]


def placement(settings: ImposerSettings, page: fitz.Page, cell: fitz.Rect) -> tuple[fitz.Rect, int, fitz.Rect]:
    """Target rectangle, rotation and trim rectangle of one page placed in a cell."""
    src = page.rect
    rotate = _rotation(settings, src, cell)
    width, height = (src.height, src.width) if rotate in (90, 270) else (src.width, src.height)
    if settings.fit_mode == "fill":
        target = fitz.Rect(cell)
    else:
        scale = min(cell.width / width, cell.height / height) if settings.fit_mode == "fit" else settings.scale_percent / 100.0
        w, h = width * scale, height * scale
        if settings.fit_mode == "actual" and (w > cell.width + 0.5 or h > cell.height + 0.5):
            raise ValueError(
                f"Uzytek {w / PT_PER_MM:.1f} x {h / PT_PER_MM:.1f} mm nie miesci sie w pozycji "
                f"{cell.width / PT_PER_MM:.1f} x {cell.height / PT_PER_MM:.1f} mm."
            )
        vertical, _, horizontal = settings.align.partition("-")
        fx = ALIGN_X.get(horizontal or "center", 0.5)
        fy = ALIGN_Y.get(vertical, 0.5)
        x0 = cell.x0 + (cell.width - w) * fx
        y0 = cell.y0 + (cell.height - h) * fy
        target = fitz.Rect(x0, y0, x0 + w, y0 + h)
    crop, trim = page.cropbox, page.trimbox
    insets = (max(trim.x0 - crop.x0, 0), max(trim.y0 - crop.y0, 0), max(crop.x1 - trim.x1, 0), max(crop.y1 - trim.y1, 0))
    left, top, right, bottom = _rotate_insets(insets, rotate)
    sx, sy = target.width / width, target.height / height
    trim_rect = fitz.Rect(target.x0 + left * sx, target.y0 + top * sy, target.x1 - right * sx, target.y1 - bottom * sy)
    return target, rotate, trim_rect


def _booklet_plan(page_count: int) -> list[list[list[int | None]]]:
    """Saddle-stitched booklet, 2 pages per side: sheet s front [N-2s, 1+2s], back [2+2s, N-1-2s] (1-based),
    with the page count padded to a multiple of 4 by blank pages at the end."""
    total = (page_count + 3) // 4 * 4
    page = lambda index: index if index < page_count else None
    return [[[page(total - 1 - 2 * s), page(2 * s)], [page(1 + 2 * s), page(total - 2 - 2 * s)]] for s in range(total // 4)]


def placement_trim(settings: ImposerSettings, page: fitz.Page, cell: fitz.Rect, inner: tuple[bool, bool, bool, bool],
                   creep: float = 0.0):
    """Montax-style placement: the page TrimBox at 100 % centred in the position, bleed clipped to the
    imposition bleed and, on sides facing another position, to half the gap. Returns target, rotation,
    trim rectangle on the sheet and the source clip."""
    rotate = int(settings.rotation) if settings.rotation != "auto" else 0
    bleed = mm_to_pt(settings.bleed_mm)
    half_x, half_y = mm_to_pt(settings.gap_x_mm) / 2, mm_to_pt(settings.gap_y_mm) / 2
    want = (min(bleed, half_x) if inner[0] else bleed, min(bleed, half_y) if inner[1] else bleed,
            min(bleed, half_x) if inner[2] else bleed, min(bleed, half_y) if inner[3] else bleed)
    crop, trim = page.cropbox, page.trimbox
    tr = fitz.Rect(trim.x0 - crop.x0, trim.y0 - crop.y0, trim.x1 - crop.x0, trim.y1 - crop.y0)
    src_want = _rotate_insets(want, (360 - rotate) % 360)
    clip = fitz.Rect(tr.x0 - src_want[0], tr.y0 - src_want[1], tr.x1 + src_want[2], tr.y1 + src_want[3]) & page.rect
    got = _rotate_insets((tr.x0 - clip.x0, tr.y0 - clip.y0, clip.x1 - tr.x1, clip.y1 - tr.y1), rotate)
    tw, th = (tr.height, tr.width) if rotate in (90, 270) else (tr.width, tr.height)
    x0, y0 = cell.x0 + (cell.width - tw) / 2, cell.y0 + (cell.height - th) / 2
    trim_rect = fitz.Rect(x0, y0, x0 + tw, y0 + th)
    target = fitz.Rect(trim_rect.x0 - got[0], trim_rect.y0 - got[1], trim_rect.x1 + got[2], trim_rect.y1 + got[3])
    if creep > 0 and rotate == 0:
        # Creep compensation: content moves towards the spine, the trim stays; content past the spine is cut.
        if inner[2]:
            target.x0 += creep
            clip.x1 -= creep
        elif inner[0]:
            target.x1 -= creep
            clip.x0 += creep
    return target, rotate, trim_rect, clip


def _draw_crop_marks(page: fitz.Page, rect: fitz.Rect, settings: ImposerSettings, bleed: float,
                     outer: tuple[bool, bool, bool, bool] = (True, True, True, True)) -> None:
    length = mm_to_pt(settings.mark_length_mm)
    # Marks never reach into the bleed of the placed page.
    offset = max(mm_to_pt(settings.mark_offset_mm), bleed)
    if length <= 0:
        return
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
    # Segment order above: left, top, right, top, left, bottom, right, bottom (sides the segment points to).
    sides = (0, 1, 2, 1, 0, 3, 2, 3)
    for (start, end), side in zip(coords, sides):
        if outer[side]:
            page.draw_line(fitz.Point(*start), fitz.Point(*end), color=REGISTRATION, width=0.25)


def _sheet_description(settings: ImposerSettings, side_label: str, sheet_number: int, sheet_total: int) -> str:
    import datetime
    parts = [settings.job_name] if settings.job_name else []
    parts.append(f"arkusz {sheet_number}/{sheet_total}" if sheet_total else f"arkusz {sheet_number}")
    parts.append(side_label)
    parts.append(f"{settings.sheet_width_mm:g}x{settings.sheet_height_mm:g} mm")
    parts.append(datetime.date.today().isoformat())
    return "  |  ".join(parts)


def _draw_registration_marks(sheet: fitz.Page, settings: ImposerSettings, mirror: bool) -> None:
    """Registration targets (circle and cross, all separations) centred on each side, outside the block of positions."""
    cells = [_slot_rect(settings, r, c, mirror) for r in range(settings.rows) for c in range(settings.columns)]
    block = fitz.Rect(cells[0])
    for cell in cells[1:]:
        block |= cell
    page = sheet.rect
    radius, arm = mm_to_pt(2.5), mm_to_pt(4)
    need = arm + mm_to_pt(1)
    centres = []
    if block.y0 - page.y0 >= 2 * need:
        centres.append(fitz.Point(block.x0 + block.width / 2, block.y0 / 2))
    if page.y1 - block.y1 >= 2 * need:
        centres.append(fitz.Point(block.x0 + block.width / 2, (block.y1 + page.y1) / 2))
    if block.x0 - page.x0 >= 2 * need:
        centres.append(fitz.Point(block.x0 / 2, block.y0 + block.height / 2))
    if page.x1 - block.x1 >= 2 * need:
        centres.append(fitz.Point((block.x1 + page.x1) / 2, block.y0 + block.height / 2))
    for c in centres:
        sheet.draw_circle(c, radius, color=REGISTRATION, width=0.25)
        sheet.draw_line(fitz.Point(c.x - arm, c.y), fitz.Point(c.x + arm, c.y), color=REGISTRATION, width=0.25)
        sheet.draw_line(fitz.Point(c.x, c.y - arm), fitz.Point(c.x, c.y + arm), color=REGISTRATION, width=0.25)


def _draw_blank(sheet: fitz.Page, rect: fitz.Rect) -> None:
    sheet.draw_rect(rect, color=(0.65, 0.65, 0.65), fill=(0.88, 1.0, 1.0), width=0.4)
    sheet.insert_textbox(rect, "Puste", fontsize=18, color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER)


def _render_side(
    out: fitz.Document,
    src: fitz.Document,
    settings: ImposerSettings,
    slots: list[int | None],
    side_label: str,
    mirror: bool,
    sheet_number: int,
    preview: bool = False,
    sheet_total: int = 0,
    creep: float = 0.0,
) -> None:
    sheet = out.new_page(width=mm_to_pt(settings.sheet_width_mm), height=mm_to_pt(settings.sheet_height_mm))
    if settings.draw_sheet_labels:
        sheet.insert_text(
            fitz.Point(mm_to_pt(settings.margin_left_mm), max(mm_to_pt(settings.margin_top_mm) - 8, 12)),
            _sheet_description(settings, side_label, sheet_number, sheet_total),
            fontsize=7,
            color=(0.0, 0.0, 0.0, 1.0),  # K100, not RGB
        )
    if settings.registration_marks:
        _draw_registration_marks(sheet, settings, mirror)
    for slot_index, source_page_index in enumerate(slots):
        row = slot_index // settings.columns
        col = slot_index % settings.columns
        cell = _slot_rect(settings, row, col, mirror)
        if source_page_index is None:
            # Empty positions get no production ink; the fill exists only in preview.
            if settings.add_blanks and preview:
                _draw_blank(sheet, cell)
            continue
        actual_col = settings.columns - 1 - col if mirror else col
        inner = (actual_col > 0, row > 0, actual_col < settings.columns - 1, row < settings.rows - 1)
        if settings.fit_mode == "trim":
            target, rotate, trim, clip = placement_trim(settings, src[source_page_index], cell, inner, creep)
            sheet.show_pdf_page(target, src, source_page_index, keep_proportion=False, rotate=rotate, clip=clip)
        else:
            target, rotate, trim = placement(settings, src[source_page_index], cell)
            sheet.show_pdf_page(target, src, source_page_index, keep_proportion=False, rotate=rotate)
        if settings.draw_frames:
            sheet.draw_rect(cell, color=(0.0, 0.45, 1.0), width=0.35)
        if settings.crop_marks:
            bleed = max(trim.x0 - target.x0, trim.y0 - target.y0, target.x1 - trim.x1, target.y1 - trim.y1, 0)
            # Montax-style layouts mark only the outside of the block, so marks never land on a neighbour.
            outer = tuple(not side for side in inner) if settings.fit_mode == "trim" else (True, True, True, True)
            _draw_crop_marks(sheet, trim, settings, bleed, outer)
        if settings.draw_numbers:
            label = str(source_page_index + 1)
            marker = fitz.Rect(cell.x0 + cell.width / 2 - 11, cell.y0 + cell.height / 2 - 11, cell.x0 + cell.width / 2 + 11, cell.y0 + cell.height / 2 + 11)
            sheet.draw_oval(marker, color=(1.0, 0.0, 0.55), fill=(0.85, 1.0, 1.0), width=0.4)
            sheet.insert_textbox(marker, label, fontsize=10, color=(0, 0, 0), align=fitz.TEXT_ALIGN_CENTER)


def render_sheets(src: fitz.Document, settings: ImposerSettings, out: fitz.Document, only_sheet: int | None = None) -> int:
    """Render all sheets (export) or one sheet (preview); returns the sheet count."""
    if settings.rows * settings.columns <= 0:
        raise ValueError("Imposer wymaga co najmniej jednej pozycji na arkuszu.")
    if src.page_count <= 0:
        raise ValueError("PDF wejsciowy nie zawiera stron.")
    if settings.order == "booklet":
        # Booklet: two pages side by side meeting at the spine, both sides of the sheet, trim placement.
        settings = replace(settings, rows=1, columns=2, duplex=True, mirror_back=False, fit_mode="trim", gap_x_mm=0.0, rotation="0")
    if settings.fit_mode == "trim" and not settings.position_width_mm:
        # Automatic position size = TrimBox of the first page (as Montax "Auto positions size").
        trim = src[0].trimbox
        width, height = trim.width / PT_PER_MM, trim.height / PT_PER_MM
        if settings.rotation in ("90", "270"):
            width, height = height, width
        settings = replace(settings, position_width_mm=width, position_height_mm=height)
    plan = sheet_plan(settings, src.page_count)
    if only_sheet is not None and not 0 <= only_sheet < len(plan):
        raise ValueError("Numer arkusza poza zakresem.")
    for index, sides in enumerate(plan):
        if only_sheet is not None and index != only_sheet:
            continue
        for side, slots in enumerate(sides):
            label = ("Tyl" if side else "Przod") if settings.duplex else "Arkusz"
            creep = mm_to_pt(settings.creep_mm) * index if settings.order == "booklet" else 0.0
            _render_side(out, src, settings, slots, label, bool(side and settings.mirror_back), index + 1,
                         preview=only_sheet is not None, sheet_total=len(plan), creep=creep)
    return len(plan)


def process_imposer_pdf(args: SimpleNamespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    settings = settings_from_args(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(input_path)) as src, fitz.open() as out:
        render_sheets(src, settings, out)
        out.save(str(output_path), garbage=4, deflate=True)
    logging.info(
        "Imposer: zapisano %s, arkusz %.2f x %.2f mm, uklad %d x %d, kolejnosc %s.",
        output_path,
        settings.sheet_width_mm,
        settings.sheet_height_mm,
        settings.columns,
        settings.rows,
        settings.order,
    )


def check_imposition(src: fitz.Document, settings: ImposerSettings) -> dict:
    """Imposition check (as Montax): facts about the layout with level info / warning / error."""
    issues = []
    add = lambda level, message: issues.append({"level": level, "message": message})
    if settings.order == "booklet":
        settings = replace(settings, rows=1, columns=2, duplex=True, mirror_back=False, fit_mode="trim", gap_x_mm=0.0, rotation="0")
    if settings.fit_mode == "trim" and not settings.position_width_mm and src.page_count:
        trim = src[0].trimbox
        width, height = trim.width / PT_PER_MM, trim.height / PT_PER_MM
        if settings.rotation in ("90", "270"):
            width, height = height, width
        settings = replace(settings, position_width_mm=width, position_height_mm=height)
    try:
        cells = [_slot_rect(settings, r, c, False) for r in range(settings.rows) for c in range(settings.columns)]
        plan = sheet_plan(settings, src.page_count)
    except ValueError as error:
        add("error", str(error))
        return {"issues": issues, "sheets": 0}
    sides = len(plan[0]) if plan else 1
    add("info", f"Arkusze: {len(plan)} ({'dwustronne' if sides == 2 else 'jednostronne'}), pozycje na stronie arkusza: "
                f"{settings.rows * settings.columns}, format arkusza {settings.sheet_width_mm:g} x {settings.sheet_height_mm:g} mm.")
    empty = sum(1 for sheet in plan for side in sheet for slot in side if slot is None)
    if settings.order == "booklet":
        added = (src.page_count + 3) // 4 * 4 - src.page_count
        if added:
            add("info", f"Skladka wymaga wielokrotnosci 4 stron: dodano {added} puste strony na koncu.")
        if settings.creep_mm > 0 and len(plan) > 1:
            add("info", f"Korekta pelzania: przesuniecie do grzbietu od 0 do {settings.creep_mm * (len(plan) - 1):.2f} mm (arkusz wewnetrzny).")
    elif empty:
        add("warning", f"Puste pozycje: {empty} (zostana bez druku).")
    if settings.fit_mode == "trim":
        pos_w, pos_h = settings.position_width_mm, settings.position_height_mm
        different, larger, no_bleed = [], [], []
        for number, page in enumerate(src, 1):
            trim = page.trimbox
            tw, th = trim.width / PT_PER_MM, trim.height / PT_PER_MM
            if settings.rotation in ("90", "270"):
                tw, th = th, tw
            if tw > pos_w + 0.5 or th > pos_h + 0.5:
                larger.append(number)
            elif abs(tw - pos_w) > 0.5 or abs(th - pos_h) > 0.5:
                different.append(number)
            if settings.bleed_mm > 0 and page.cropbox == trim:
                no_bleed.append(number)
        short = lambda pages: ", ".join(map(str, pages[:12])) + (" ..." if len(pages) > 12 else "")
        if larger:
            add("error", f"Strony wieksze niz pozycja {pos_w:.1f} x {pos_h:.1f} mm: {short(larger)} (nachodza na sasiednie uzytki).")
        if different:
            add("warning", f"Strony o innym formacie netto niz pozycja: {short(different)} (zostana wysrodkowane).")
        if no_bleed:
            add("warning", f"Strony bez spadu (TrimBox = CropBox): {short(no_bleed)}.")
    if settings.crop_marks:
        block = fitz.Rect(cells[0])
        for cell in cells[1:]:
            block |= cell
        reach = mm_to_pt(settings.mark_offset_mm) + mm_to_pt(settings.mark_length_mm)
        sheet = fitz.Rect(0, 0, mm_to_pt(settings.sheet_width_mm), mm_to_pt(settings.sheet_height_mm))
        if not sheet.contains(fitz.Rect(block.x0 - reach, block.y0 - reach, block.x1 + reach, block.y1 + reach)):
            add("warning", "Znaczniki ciecia wychodza poza arkusz: zwieksz marginesy lub zmniejsz dlugosc znacznikow.")
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: order[item["level"]])
    return {"issues": issues, "sheets": len(plan)}
