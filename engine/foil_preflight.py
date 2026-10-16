#!/usr/bin/env python3
"""
Create a print PDF with foil/spot elements moved to a separate plate page.

The processor is intentionally conservative: it preserves PDF page content
streams and removes complete graphics-state blocks that paint with the selected
spot/process color. This matches common Acrobat/Illustrator output where each
placed object is wrapped in q ... Q.
"""

from __future__ import annotations

import argparse
import copy
import logging
import math
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pikepdf
from pikepdf._core import ContentStreamInstruction
from pikepdf.models import parse_content_stream, unparse_content_stream


PT_PER_MM = 72.0 / 25.4
PAGE_SIZE_PRESETS_MM = {
    "A5": (148.0, 210.0),
    "A4": (210.0, 297.0),
    "A3": (297.0, 420.0),
    "SRA3": (320.0, 450.0),
    "B4": (250.0, 353.0),
    "B3": (353.0, 500.0),
}
PAGE_SIZE_DIELINE_BOUNDS = "dieline_bounds"
PAINT_FILL = {"f", "F", "f*", "B", "B*", "b", "b*"}
PAINT_STROKE = {"S", "s", "B", "B*", "b", "b*"}
PAINT_TEXT = {"Tj", "TJ", "'", '"'}
PAINT_XOBJECT = {"Do"}
COLOR_OPS = {"cs", "CS", "sc", "SC", "scn", "SCN", "g", "G", "rg", "RG", "k", "K"}
PATH_OPS = {"m", "l", "c", "v", "y", "h", "re"}
CLIP_OPS = {"W", "W*", "n"}
PLATE_CONTEXT_OPS = {"cm", "w", "J", "j", "M", "d", "i", "ri"}


@dataclass
class TargetColor:
    spot: Optional[str] = None
    cmyk: Optional[tuple[float, float, float, float]] = None
    rgb: Optional[tuple[float, float, float]] = None
    gray: Optional[float] = None
    tolerance: float = 0.05


@dataclass
class GraphicsState:
    fill_space: Optional[str] = None
    stroke_space: Optional[str] = None
    fill_selected: bool = False
    stroke_selected: bool = False
    text_render_mode: int = 0


@dataclass
class Analysis:
    selected_paints: int = 0
    other_paints: int = 0


@dataclass
class SplitResult:
    keep: list
    plate: list
    warnings: list[str]


@dataclass
class RemoveTargetSpec:
    name: str
    target: TargetColor
    paint_kinds: set[str]


@dataclass
class PageSetup:
    trim_width_mm: Optional[float] = None
    trim_height_mm: Optional[float] = None
    scale_content: bool = False
    scale_mode: str = "actual"
    scale_percent: float = 100.0

    @property
    def from_source(self) -> bool:
        return self.trim_width_mm is None or self.trim_height_mm is None


@dataclass
class DielineSpec:
    name: str
    target: TargetColor
    output: Optional[TargetColor] = None
    enabled: bool = True
    spot_space_name: Optional[str] = None
    output_spot_space_name: Optional[str] = None
    silent_missing: bool = False


@dataclass
class GeneratedPageEdit:
    page_index: int
    before: Optional[str] = None
    after: Optional[str] = None
    contour: Optional[str] = None
    contour_is_spot: bool = False
    trim_box: Optional[tuple[float, float, float, float]] = None


@dataclass
class GeneratedShapeGeometry:
    page_width_pt: float
    page_height_pt: float
    trim_box: tuple[float, float, float, float]
    cut_path: list[str]
    bleed_path: list[str]
    source_clip: Optional[object] = None


def pdf_number(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text else "0"


def parse_percent_components(text: str, count: int) -> tuple[float, ...]:
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if len(parts) != count:
        raise argparse.ArgumentTypeError(f"Expected {count} comma-separated values.")
    values: list[float] = []
    for part in parts:
        value = float(part)
        if value > 1:
            value /= 100.0
        if not 0 <= value <= 1:
            raise argparse.ArgumentTypeError("Color components must be in 0-1 or 0-100 range.")
        values.append(value)
    return tuple(values)


def resolve_page_setup(args: argparse.Namespace) -> PageSetup:
    scale_content = bool(getattr(args, "scale_content", False))
    scale_mode = (getattr(args, "scale_mode", "actual") or "actual").strip().lower()
    scale_percent = float(getattr(args, "scale_percent", 100.0) or 100.0)
    def setup(width: Optional[float] = None, height: Optional[float] = None) -> PageSetup:
        return PageSetup(
            trim_width_mm=width,
            trim_height_mm=height,
            scale_content=scale_content,
            scale_mode=scale_mode,
            scale_percent=scale_percent,
        )

    page_size = (getattr(args, "page_size", "source") or "source").strip()
    if page_size.lower() == "source":
        return setup()
    if page_size == PAGE_SIZE_DIELINE_BOUNDS:
        return setup()
    if page_size.lower() == "custom":
        width = getattr(args, "page_width_mm", None)
        height = getattr(args, "page_height_mm", None)
        if width is None or height is None:
            raise argparse.ArgumentTypeError("Custom page size requires width and height in mm.")
        width = float(width)
        height = float(height)
    else:
        key = page_size.upper()
        if key not in PAGE_SIZE_PRESETS_MM:
            raise argparse.ArgumentTypeError(f"Unknown page size: {page_size}")
        width, height = PAGE_SIZE_PRESETS_MM[key]
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("Page width and height must be positive.")
    return setup(width, height)


def namestr(value) -> str:
    return str(value)


def decode_pdf_name(value: object) -> str:
    text = namestr(value).strip()
    if text.startswith("/"):
        text = text[1:]

    def repl(match: re.Match[str]) -> str:
        try:
            return chr(int(match.group(1), 16))
        except Exception:
            return match.group(0)

    return re.sub(r"#([0-9a-fA-F]{2})", repl, text)


def normalize_spot_name(value: str) -> str:
    text = decode_pdf_name(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def spot_names_match(target: str, candidate: str) -> bool:
    target_key = normalize_spot_name(target)
    candidate_key = normalize_spot_name(candidate)
    if not target_key or not candidate_key:
        return False
    if target_key == candidate_key:
        return True
    aliases = {
        "cut": {"cutcontour", "cutline", "ciecie", "liniaciecia", "wykrojnik"},
        "cutcontour": {"cut", "cutline", "ciecie", "liniaciecia", "wykrojnik"},
        "perf": {"perforacja", "perforation"},
        "perforacja": {"perf", "perforation"},
        "bigowanie": {"crease", "creasing"},
        "crease": {"bigowanie", "creasing"},
        "hs": {"hotstamping", "hotfoil", "hot"},
        "hot": {"hs", "hotstamping", "hotfoil"},
    }
    if candidate_key in aliases.get(target_key, set()):
        return True
    if len(target_key) >= 3 and target_key in candidate_key:
        return True
    if len(candidate_key) >= 3 and candidate_key in target_key:
        return True
    return False


def spot_names_from_colorspace(colorspace) -> list[str]:
    names: list[str] = []
    try:
        if isinstance(colorspace, pikepdf.Array) and len(colorspace) >= 2:
            kind = decode_pdf_name(colorspace[0])
            if kind == "Separation":
                names.append(decode_pdf_name(colorspace[1]))
            elif kind == "DeviceN":
                device_names = colorspace[1]
                if isinstance(device_names, pikepdf.Array):
                    names.extend(decode_pdf_name(name) for name in device_names)
    except Exception:
        return names
    return [name for name in names if name]


def colorspace_target_match_reason(resource_name: str, colorspace, target: TargetColor) -> Optional[str]:
    if target.spot is None:
        return None
    if resource_name and spot_names_match(target.spot, resource_name):
        return decode_pdf_name(resource_name)
    for spot_name in spot_names_from_colorspace(colorspace):
        if spot_names_match(target.spot, spot_name):
            return spot_name
    return None



def floats(operands: Iterable) -> tuple[float, ...]:
    out: list[float] = []
    for item in operands:
        out.append(float(item))
    return tuple(out)


def close_tuple(values: Sequence[float], target: Sequence[float], tolerance: float) -> bool:
    if len(values) != len(target):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(values, target))


def colorspace_matches_target(colorspace, target: TargetColor) -> bool:
    if target.spot is None:
        return False
    return colorspace_target_match_reason("", colorspace, target) is not None


def page_colorspaces(page: pikepdf.Page) -> dict[str, object]:
    resources = page.get("/Resources", pikepdf.Dictionary())
    spaces = resources.get("/ColorSpace", pikepdf.Dictionary()) if isinstance(resources, pikepdf.Dictionary) else {}
    if not isinstance(spaces, pikepdf.Dictionary):
        return {}
    return {namestr(name): value for name, value in spaces.items()}


def selected_colorspace_names(page: pikepdf.Page, target: TargetColor) -> set[str]:
    return {
        name
        for name, value in page_colorspaces(page).items()
        if colorspace_target_match_reason(name, value, target) is not None
    }


def available_spot_names(page: pikepdf.Page) -> list[str]:
    spots: set[str] = set()
    for value in page_colorspaces(page).values():
        spots.update(spot_names_from_colorspace(value))
    return sorted(spots, key=normalize_spot_name)


def warn_spot_not_found(page_number: int, page: pikepdf.Page, target: TargetColor) -> None:
    if not target.spot:
        return
    available = available_spot_names(page)
    suffix = f" Available spots: {', '.join(available)}." if available else " No spot colors found in page resources."
    logging.warning("Page %s: spot color %r not found in page color spaces.%s", page_number, target.spot, suffix)


def update_color_state(instruction, state: GraphicsState, selected_spaces: set[str], target: TargetColor) -> None:
    op = namestr(instruction.operator)
    operands = instruction.operands

    if op == "cs" and operands:
        state.fill_space = namestr(operands[0])
        state.fill_selected = False
    elif op == "CS" and operands:
        state.stroke_space = namestr(operands[0])
        state.stroke_selected = False
    elif op in {"sc", "scn"}:
        values = floats(operands)
        if state.fill_space in selected_spaces:
            state.fill_selected = bool(values and max(values) > target.tolerance)
        else:
            state.fill_selected = False
    elif op in {"SC", "SCN"}:
        values = floats(operands)
        if state.stroke_space in selected_spaces:
            state.stroke_selected = bool(values and max(values) > target.tolerance)
        else:
            state.stroke_selected = False
    elif op == "k":
        state.fill_space = "/DeviceCMYK"
        state.fill_selected = (
            close_tuple(floats(operands), target.cmyk, target.tolerance) if target.cmyk is not None else False
        )
    elif op == "K":
        state.stroke_space = "/DeviceCMYK"
        state.stroke_selected = (
            close_tuple(floats(operands), target.cmyk, target.tolerance) if target.cmyk is not None else False
        )
    elif op == "rg":
        state.fill_space = "/DeviceRGB"
        state.fill_selected = (
            close_tuple(floats(operands), target.rgb, target.tolerance) if target.rgb is not None else False
        )
    elif op == "RG":
        state.stroke_space = "/DeviceRGB"
        state.stroke_selected = (
            close_tuple(floats(operands), target.rgb, target.tolerance) if target.rgb is not None else False
        )
    elif op == "g":
        state.fill_space = "/DeviceGray"
        state.fill_selected = (
            close_tuple(floats(operands), (target.gray,), target.tolerance) if target.gray is not None else False
        )
    elif op == "G":
        state.stroke_space = "/DeviceGray"
        state.stroke_selected = (
            close_tuple(floats(operands), (target.gray,), target.tolerance) if target.gray is not None else False
        )
    elif op == "Tr" and operands:
        try:
            state.text_render_mode = int(operands[0])
        except Exception:
            state.text_render_mode = 0


def is_color_op(instruction) -> bool:
    return namestr(instruction.operator) in COLOR_OPS


def references_selected_color(instruction, state: GraphicsState, selected_spaces: set[str], paint_kinds: set[str]) -> bool:
    op = namestr(instruction.operator)
    operands = instruction.operands
    if op in {"cs", "CS"} and operands:
        if op == "cs" and "fill" not in paint_kinds:
            return False
        if op == "CS" and "stroke" not in paint_kinds:
            return False
        return namestr(operands[0]) in selected_spaces
    if op in {"sc", "scn"}:
        return "fill" in paint_kinds and state.fill_space in selected_spaces
    if op in {"SC", "SCN"}:
        return "stroke" in paint_kinds and state.stroke_space in selected_spaces
    return False


def active_selected_state(state: GraphicsState, paint_kinds: set[str]) -> bool:
    return ("fill" in paint_kinds and state.fill_selected) or ("stroke" in paint_kinds and state.stroke_selected)


def paint_selection(instruction, state: GraphicsState, paint_kinds: set[str]) -> Optional[bool]:
    op = namestr(instruction.operator)
    if op in PAINT_FILL and op in PAINT_STROKE:
        return active_selected_state(state, paint_kinds)
    if op in PAINT_FILL:
        return "fill" in paint_kinds and state.fill_selected
    if op in PAINT_STROKE:
        return "stroke" in paint_kinds and state.stroke_selected
    if op in PAINT_TEXT:
        return False
    if op in PAINT_XOBJECT:
        return False
    return None


def find_matching_q(instructions: Sequence, start: int) -> int:
    depth = 0
    for index in range(start, len(instructions)):
        op = namestr(instructions[index].operator)
        if op == "q":
            depth += 1
        elif op == "Q":
            depth -= 1
            if depth == 0:
                return index
    return -1


def analyze(
    instructions: Sequence,
    state: GraphicsState,
    selected_spaces: set[str],
    target: TargetColor,
    paint_kinds: set[str],
) -> Analysis:
    current = copy.deepcopy(state)
    stack: list[GraphicsState] = []
    result = Analysis()
    for instruction in instructions:
        op = namestr(instruction.operator)
        if op == "q":
            stack.append(copy.deepcopy(current))
            continue
        if op == "Q":
            if stack:
                current = stack.pop()
            continue
        update_color_state(instruction, current, selected_spaces, target)
        selected = paint_selection(instruction, current, paint_kinds)
        if selected is True:
            result.selected_paints += 1
        elif selected is False:
            result.other_paints += 1
    return result


def strip_color_ops(instructions: Sequence) -> list:
    return [instruction for instruction in instructions if not is_color_op(instruction)]


def plate_path_ops(instructions: Sequence) -> list:
    allowed = PATH_OPS | CLIP_OPS | PLATE_CONTEXT_OPS | PAINT_FILL | PAINT_STROKE | {"q", "Q"}
    return [instruction for instruction in instructions if namestr(instruction.operator) in allowed]


def split_for_plate(
    instructions: Sequence,
    state: GraphicsState,
    selected_spaces: set[str],
    target: TargetColor,
    page_number: int,
    paint_kinds: Optional[set[str]] = None,
    plate_context: bool = False,
) -> SplitResult:
    keep: list = []
    plate: list = []
    warnings: list[str] = []
    current = copy.deepcopy(state)
    path_buffer: list = []
    i = 0
    paint_kinds = paint_kinds or {"fill", "stroke"}

    def flush_path_to_keep() -> None:
        nonlocal path_buffer
        if path_buffer:
            keep.extend(path_buffer)
            path_buffer = []

    def flush_path_to_plate() -> None:
        if path_buffer:
            plate.extend(path_buffer)

    while i < len(instructions):
        instruction = instructions[i]
        op = namestr(instruction.operator)

        if op == "q":
            end = find_matching_q(instructions, i)
            if end == -1:
                warnings.append(f"Page {page_number}: unmatched q operator; kept original block.")
                keep.append(instruction)
                i += 1
                continue

            block = list(instructions[i : end + 1])
            body = block[1:-1]
            block_analysis = analyze(body, copy.deepcopy(current), selected_spaces, target, paint_kinds)

            if block_analysis.selected_paints and not block_analysis.other_paints:
                flush_path_to_keep()
                plate.extend(plate_path_ops(block))
            elif block_analysis.selected_paints:
                flush_path_to_keep()
                inner = split_for_plate(
                    body,
                    copy.deepcopy(current),
                    selected_spaces,
                    target,
                    page_number,
                    paint_kinds=paint_kinds,
                    plate_context=True,
                )
                warnings.extend(inner.warnings)
                if inner.keep:
                    keep.append(block[0])
                    keep.extend(inner.keep)
                    keep.append(block[-1])
                if inner.plate:
                    plate.append(block[0])
                    plate.extend(inner.plate)
                    plate.append(block[-1])
            else:
                flush_path_to_keep()
                keep.extend(block)
            i = end + 1
            continue

        drop_color_ref = references_selected_color(instruction, current, selected_spaces, paint_kinds)
        update_color_state(instruction, current, selected_spaces, target)
        selected = paint_selection(instruction, current, paint_kinds)
        if selected is True:
            flush_path_to_plate()
            plate.append(instruction)
            path_buffer = []
            i += 1
            continue
        if not drop_color_ref and not (is_color_op(instruction) and active_selected_state(current, paint_kinds)):
            if op in PATH_OPS:
                path_buffer.append(instruction)
                i += 1
                continue
            if op in CLIP_OPS:
                keep.extend(path_buffer)
                keep.append(instruction)
                if plate_context:
                    plate.extend(path_buffer)
                    plate.append(instruction)
                path_buffer = []
                i += 1
                continue
            if selected is False:
                flush_path_to_keep()
                keep.append(instruction)
                i += 1
                continue
            flush_path_to_keep()
            keep.append(instruction)
            if plate_context and op in PLATE_CONTEXT_OPS:
                plate.append(instruction)
        i += 1

    flush_path_to_keep()
    return SplitResult(keep=keep, plate=plate, warnings=warnings)


def remove_selected_colorspaces(page: pikepdf.Page, selected_spaces: set[str]) -> None:
    resources = page.get("/Resources", None)
    if not isinstance(resources, pikepdf.Dictionary):
        return
    spaces = resources.get("/ColorSpace", None)
    if not isinstance(spaces, pikepdf.Dictionary):
        return
    for name in list(spaces.keys()):
        if namestr(name) in selected_spaces:
            del spaces[name]


def box_values(page: pikepdf.Page, preferred: str) -> tuple[float, float, float, float]:
    box = page.get(preferred, page.get("/CropBox", page.MediaBox))
    return tuple(float(v) for v in box)


def box_size(box: Sequence[float]) -> tuple[float, float]:
    llx, lly, urx, ury = [float(v) for v in box]
    return urx - llx, ury - lly


def has_required_bleed(page: pikepdf.Page, bleed_mm: float) -> bool:
    bleed = bleed_mm * PT_PER_MM
    trim = page.get("/TrimBox", None)
    bleed_box = page.get("/BleedBox", None)
    if trim is None or bleed_box is None:
        return False
    trim_w, trim_h = box_size(trim)
    bleed_w, bleed_h = box_size(bleed_box)
    return bleed_w >= trim_w + (2 * bleed) - 0.5 and bleed_h >= trim_h + (2 * bleed) - 0.5


def target_dimensions(source_box: Sequence[float], bleed_mm: float, page_setup: PageSetup) -> tuple[float, float, float, float]:
    source_width, source_height = box_size(source_box)
    inset = bleed_mm * PT_PER_MM
    if page_setup.from_source:
        return source_width, source_height, inset, inset
    trim_width = float(page_setup.trim_width_mm) * PT_PER_MM
    trim_height = float(page_setup.trim_height_mm) * PT_PER_MM
    return trim_width + (2 * inset), trim_height + (2 * inset), inset, inset


def target_dimensions_from_trim(
    trim_source_box: Sequence[float],
    bleed_mm: float,
    page_setup: PageSetup,
) -> tuple[float, float, float, float]:
    source_width, source_height = box_size(trim_source_box)
    inset = bleed_mm * PT_PER_MM
    if page_setup.from_source:
        return source_width + (2 * inset), source_height + (2 * inset), inset, inset
    trim_width = float(page_setup.trim_width_mm) * PT_PER_MM
    trim_height = float(page_setup.trim_height_mm) * PT_PER_MM
    return trim_width + (2 * inset), trim_height + (2 * inset), inset, inset


def set_print_boxes(
    page: pikepdf.Page,
    bleed_source_box: Sequence[float],
    bleed_mm: float,
    page_setup: PageSetup,
    source_is_trim: bool = False,
) -> tuple[float, float, float, float]:
    llx, lly, urx, ury = [float(v) for v in bleed_source_box]
    source_width = urx - llx
    source_height = ury - lly
    if source_is_trim:
        width, height, trim_inset_x, trim_inset_y = target_dimensions_from_trim(bleed_source_box, bleed_mm, page_setup)
    else:
        width, height, trim_inset_x, trim_inset_y = target_dimensions(bleed_source_box, bleed_mm, page_setup)
    offset_x = (width - source_width) / 2
    offset_y = (height - source_height) / 2
    page.MediaBox = pikepdf.Array([0, 0, width, height])
    page.CropBox = pikepdf.Array([0, 0, width, height])
    page.BleedBox = pikepdf.Array([0, 0, width, height])
    page.TrimBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    page.ArtBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    return llx - offset_x, lly - offset_y, width, height


def wrap_translated_content(pdf: pikepdf.Pdf, page: pikepdf.Page, instructions: Sequence, tx: float, ty: float) -> None:
    body = unparse_content_stream(instructions)
    prefix = f"q\n1 0 0 1 {pdf_number(-tx)} {pdf_number(-ty)} cm\n".encode("ascii")
    suffix = b"\nQ\n"
    page.Contents = pdf.make_stream(prefix + body + suffix)


def create_form_xobject(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    instructions: Sequence,
    source_box: Sequence[float],
) -> pikepdf.Stream:
    form = pdf.make_stream(unparse_content_stream(instructions))
    form.Type = pikepdf.Name("/XObject")
    form.Subtype = pikepdf.Name("/Form")
    form.FormType = 1
    form.BBox = pikepdf.Array([float(v) for v in source_box])
    form.Resources = page.get("/Resources", pikepdf.Dictionary())
    return form


def draw_form_rect(name: str, clip: Sequence[float], matrix: Sequence[float]) -> bytes:
    x, y, width, height = clip
    a, b, c, d, e, f = matrix
    return (
        "q\n"
        f"{pdf_number(x)} {pdf_number(y)} {pdf_number(width)} {pdf_number(height)} re\n"
        "W n\n"
        f"{pdf_number(a)} {pdf_number(b)} {pdf_number(c)} {pdf_number(d)} {pdf_number(e)} {pdf_number(f)} cm\n"
        f"{name} Do\n"
        "Q\n"
    ).encode("ascii")


def page_setup_scales(
    source_width: float,
    source_height: float,
    target_width: float,
    target_height: float,
    page_setup: PageSetup,
) -> tuple[float, float]:
    if source_width <= 0 or source_height <= 0 or target_width <= 0 or target_height <= 0:
        return 1.0, 1.0
    if not page_setup.scale_content:
        return 1.0, 1.0
    percent = max(float(page_setup.scale_percent or 100.0), 0.001) / 100.0
    mode = (page_setup.scale_mode or "actual").strip().lower().replace(" ", "_")
    if mode in {"fit", "proportional_fit", "dopasuj"}:
        scale = min(target_width / source_width, target_height / source_height) * percent
        return scale, scale
    if mode in {"fill", "proportional_fill", "wypelnij"}:
        scale = max(target_width / source_width, target_height / source_height) * percent
        return scale, scale
    if mode in {"nonprop_fill", "non-prop_fill", "non_prop_fill", "fill_nonprop"}:
        return (target_width / source_width) * percent, (target_height / source_height) * percent
    return percent, percent


def fitted_content_placement(
    source_box: Sequence[float],
    page_width: float,
    page_height: float,
    trim_inset_x: float,
    trim_inset_y: float,
    page_setup: PageSetup,
    source_is_trim: bool = False,
) -> tuple[float, float, float, float, float, float, tuple[float, float, float, float]]:
    llx, lly, urx, ury = [float(v) for v in source_box]
    source_width = urx - llx
    source_height = ury - lly
    if source_is_trim:
        area_x = trim_inset_x
        area_y = trim_inset_y
        area_width = page_width - (2 * trim_inset_x)
        area_height = page_height - (2 * trim_inset_y)
    else:
        area_x = 0.0
        area_y = 0.0
        area_width = page_width
        area_height = page_height
    sx, sy = page_setup_scales(source_width, source_height, area_width, area_height, page_setup)
    content_width = source_width * sx
    content_height = source_height * sy
    content_x = area_x + (area_width - content_width) / 2.0
    content_y = area_y + (area_height - content_height) / 2.0
    clip = (area_x, area_y, area_width, area_height)
    return content_x, content_y, content_width, content_height, sx, sy, clip


def content_matrix(source_box: Sequence[float], content_x: float, content_y: float, sx: float, sy: float) -> tuple[float, float, float, float, float, float]:
    llx, lly, _urx, _ury = [float(v) for v in source_box]
    return sx, 0, 0, sy, content_x - (llx * sx), content_y - (lly * sy)


def wrap_fitted_content(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    instructions: Sequence,
    source_box: Sequence[float],
    bleed_mm: float,
    page_setup: PageSetup,
    source_is_trim: bool = False,
) -> None:
    if source_is_trim:
        width, height, trim_inset_x, trim_inset_y = target_dimensions_from_trim(source_box, bleed_mm, page_setup)
    else:
        width, height, trim_inset_x, trim_inset_y = target_dimensions(source_box, bleed_mm, page_setup)

    form_name = "/FmFitBase"
    form = create_form_xobject(pdf, page, instructions, source_box)
    page.MediaBox = pikepdf.Array([0, 0, width, height])
    page.CropBox = pikepdf.Array([0, 0, width, height])
    page.BleedBox = pikepdf.Array([0, 0, width, height])
    page.TrimBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    page.ArtBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    page.Resources = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({form_name: form})})

    content_x, content_y, _cw, _ch, sx, sy, clip = fitted_content_placement(
        source_box,
        width,
        height,
        trim_inset_x,
        trim_inset_y,
        page_setup,
        source_is_trim=source_is_trim,
    )
    page.Contents = pdf.make_stream(draw_form_rect(form_name, clip, content_matrix(source_box, content_x, content_y, sx, sy)))


def wrap_with_generated_bleed(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    instructions: Sequence,
    source_box: Sequence[float],
    bleed_mm: float,
    page_setup: PageSetup,
    mode: str,
) -> None:
    llx, lly, urx, ury = [float(v) for v in source_box]
    source_width = urx - llx
    source_height = ury - lly
    bleed = bleed_mm * PT_PER_MM
    width, height, trim_inset_x, trim_inset_y = target_dimensions_from_trim(source_box, bleed_mm, page_setup)
    content_x, content_y, content_width, content_height, sx, sy, trim_clip = fitted_content_placement(
        source_box,
        width,
        height,
        trim_inset_x,
        trim_inset_y,
        page_setup,
        source_is_trim=True,
    )
    form_name = "/FmBleedBase"
    form = create_form_xobject(pdf, page, instructions, source_box)

    page.MediaBox = pikepdf.Array([0, 0, width, height])
    page.CropBox = pikepdf.Array([0, 0, width, height])
    page.BleedBox = pikepdf.Array([0, 0, width, height])
    page.TrimBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    page.ArtBox = pikepdf.Array([trim_inset_x, trim_inset_y, width - trim_inset_x, height - trim_inset_y])
    page.Resources = pikepdf.Dictionary({"/XObject": pikepdf.Dictionary({form_name: form})})

    chunks: list[bytes] = []
    if mode == "stretch":
        stretch_sx = width / source_width
        stretch_sy = height / source_height
        chunks.append(draw_form_rect(form_name, (0, 0, width, height), (stretch_sx, 0, 0, stretch_sy, -llx * stretch_sx, -lly * stretch_sy)))
    elif mode == "mirror":
        left = content_x - bleed
        right = content_x + content_width
        bottom = content_y - bleed
        top = content_y + content_height
        chunks.extend(
            [
                draw_form_rect(form_name, (left, content_y, bleed, content_height), (-sx, 0, 0, sy, content_x + (llx * sx), content_y - (lly * sy))),
                draw_form_rect(form_name, (right, content_y, bleed, content_height), (-sx, 0, 0, sy, right + (urx * sx), content_y - (lly * sy))),
                draw_form_rect(form_name, (content_x, bottom, content_width, bleed), (sx, 0, 0, -sy, content_x - (llx * sx), content_y + (lly * sy))),
                draw_form_rect(form_name, (content_x, top, content_width, bleed), (sx, 0, 0, -sy, content_x - (llx * sx), top + (ury * sy))),
                draw_form_rect(form_name, (left, bottom, bleed, bleed), (-sx, 0, 0, -sy, content_x + (llx * sx), content_y + (lly * sy))),
                draw_form_rect(form_name, (left, top, bleed, bleed), (-sx, 0, 0, -sy, content_x + (llx * sx), top + (ury * sy))),
                draw_form_rect(form_name, (right, bottom, bleed, bleed), (-sx, 0, 0, -sy, right + (urx * sx), content_y + (lly * sy))),
                draw_form_rect(form_name, (right, top, bleed, bleed), (-sx, 0, 0, -sy, right + (urx * sx), top + (ury * sy))),
            ]
        )

    chunks.append(
        draw_form_rect(
            form_name,
            trim_clip,
            content_matrix(source_box, content_x, content_y, sx, sy),
        )
    )
    page.Contents = pdf.make_stream(b"".join(chunks))


def make_plate_stream(
    pdf: pikepdf.Pdf,
    instructions: Sequence,
    tx: float,
    ty: float,
    width: float,
    height: float,
    plate_cmyk: tuple[float, float, float, float],
) -> pikepdf.Stream:
    c, m, y, k = plate_cmyk
    prefix = (
        "q\n"
        f"0 0 {pdf_number(width)} {pdf_number(height)} re\n"
        "W n\n"
        f"{pdf_number(c)} {pdf_number(m)} {pdf_number(y)} {pdf_number(k)} k\n"
        f"{pdf_number(c)} {pdf_number(m)} {pdf_number(y)} {pdf_number(k)} K\n"
        f"1 0 0 1 {pdf_number(-tx)} {pdf_number(-ty)} cm\n"
    ).encode("ascii")
    body = unparse_content_stream(instructions)
    return pdf.make_stream(prefix + body + b"\nQ\n")


def target_from_values(mode: str, value: str, tolerance: float) -> TargetColor:
    mode = (mode or "").lower()
    if mode == "spot":
        if not value.strip():
            raise argparse.ArgumentTypeError("Spot color name cannot be empty.")
        return TargetColor(spot=value.strip(), tolerance=tolerance)
    if mode == "cmyk":
        return TargetColor(cmyk=parse_percent_components(value, 4), tolerance=tolerance)
    if mode == "rgb":
        return TargetColor(rgb=parse_percent_components(value, 3), tolerance=tolerance)
    if mode == "gray":
        return TargetColor(gray=parse_percent_components(value, 1)[0], tolerance=tolerance)
    raise argparse.ArgumentTypeError(f"Unknown color mode: {mode}")


def output_target_from_values(
    mode: str,
    color_value: str,
    name_value: str,
    legacy_value: str,
    tolerance: float,
) -> Optional[TargetColor]:
    mode = (mode or "same").lower()
    color_value = (color_value or "").strip()
    name_value = (name_value or "").strip()
    legacy_value = (legacy_value or "").strip()
    if mode == "same":
        return None
    if mode == "spot":
        spot_name = name_value or legacy_value
        if not spot_name:
            raise argparse.ArgumentTypeError("Spot output name cannot be empty.")
        alternate = parse_percent_components(color_value, 4) if color_value else None
        return TargetColor(spot=spot_name, cmyk=alternate, tolerance=tolerance)
    value = color_value or legacy_value
    return target_from_values(mode, value, tolerance)


def content_instruction(operands: Sequence[float], operator: str) -> ContentStreamInstruction:
    return ContentStreamInstruction(list(operands), pikepdf.Operator(operator))


def paint_kinds_from_scope(scope: str) -> set[str]:
    scope = (scope or "all").lower()
    if scope == "stroke":
        return {"stroke"}
    if scope == "fill":
        return {"fill"}
    return {"fill", "stroke"}


def build_remove_specs(args: argparse.Namespace) -> list[RemoveTargetSpec]:
    scope = getattr(args, "remove_scope", "all")
    specs: list[RemoveTargetSpec] = []
    if getattr(args, "remove_source", "") == "dieline":
        for item in build_dieline_specs(args):
            if not bool(getattr(item, "enabled", True)):
                continue
            specs.append(RemoveTargetSpec(item.name, item.target, paint_kinds_from_scope(scope)))
        return specs

    target = TargetColor(
        spot=getattr(args, "spot", None),
        cmyk=parse_percent_components(args.cmyk, 4) if getattr(args, "cmyk", None) else None,
        rgb=parse_percent_components(args.rgb, 3) if getattr(args, "rgb", None) else None,
        gray=parse_percent_components(args.gray, 1)[0] if getattr(args, "gray", None) else None,
        tolerance=args.tolerance,
    )
    if any((target.spot, target.cmyk, target.rgb, target.gray is not None)):
        specs.append(RemoveTargetSpec("color", target, paint_kinds_from_scope(scope)))
    return specs


def remove_matching_objects_from_page(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    specs: Sequence[RemoveTargetSpec],
    page_number: int,
) -> int:
    instructions = parse_content_stream(page)
    removed = 0
    keep = instructions
    for spec in specs:
        selected_spaces = selected_colorspace_names(page, spec.target)
        if spec.target.spot and not selected_spaces:
            warn_spot_not_found(page_number, page, spec.target)
        split = split_for_plate(
            keep,
            GraphicsState(),
            selected_spaces,
            spec.target,
            page_number,
            paint_kinds=spec.paint_kinds,
        )
        for warning in split.warnings:
            logging.warning(warning)
        if split.plate:
            removed += len(split.plate)
        keep = split.keep
    page.Contents = pdf.make_stream(unparse_content_stream(keep))
    return removed


def near_black_cmyk(values: Sequence[float], min_k: float, max_cmy: float) -> bool:
    if len(values) != 4:
        return False
    c, m, y, k = values
    return k >= min_k and max(c, m, y) <= max_cmy


def near_black_rgb(values: Sequence[float], max_rgb: float) -> bool:
    return len(values) == 3 and max(values) <= max_rgb


def near_black_gray(values: Sequence[float], max_gray: float) -> bool:
    return len(values) == 1 and values[0] <= max_gray


def rewrite_black_instructions(
    instructions: Sequence,
    output_cmyk: tuple[float, float, float, float],
    min_k: float,
    max_cmy: float,
    max_rgb_gray: float,
) -> tuple[list, int]:
    rewritten: list = []
    changed = 0
    for instruction in instructions:
        op = namestr(instruction.operator)
        try:
            values = floats(instruction.operands)
        except Exception:
            values = ()

        if op == "k" and near_black_cmyk(values, min_k, max_cmy):
            rewritten.append(content_instruction(output_cmyk, "k"))
            changed += 1
        elif op == "K" and near_black_cmyk(values, min_k, max_cmy):
            rewritten.append(content_instruction(output_cmyk, "K"))
            changed += 1
        elif op == "rg" and near_black_rgb(values, max_rgb_gray):
            rewritten.append(content_instruction(output_cmyk, "k"))
            changed += 1
        elif op == "RG" and near_black_rgb(values, max_rgb_gray):
            rewritten.append(content_instruction(output_cmyk, "K"))
            changed += 1
        elif op == "g" and near_black_gray(values, max_rgb_gray):
            rewritten.append(content_instruction(output_cmyk, "k"))
            changed += 1
        elif op == "G" and near_black_gray(values, max_rgb_gray):
            rewritten.append(content_instruction(output_cmyk, "K"))
            changed += 1
        else:
            rewritten.append(instruction)
    return rewritten, changed


def target_stroke_prefix(target: TargetColor, spot_space_name: Optional[str]) -> bytes:
    if target.spot:
        if not spot_space_name:
            raise RuntimeError(f"Spot color {target.spot!r} was not found in page resources.")
        return f"{spot_space_name} CS 1 SCN\n".encode("ascii")
    if target.cmyk is not None:
        c, m, y, k = target.cmyk
        return f"{pdf_number(c)} {pdf_number(m)} {pdf_number(y)} {pdf_number(k)} K\n".encode("ascii")
    if target.rgb is not None:
        r, g, b = target.rgb
        return f"{pdf_number(r)} {pdf_number(g)} {pdf_number(b)} RG\n".encode("ascii")
    if target.gray is not None:
        return f"{pdf_number(target.gray)} G\n".encode("ascii")
    return b"0 G\n"


def pdf_name(value: str) -> pikepdf.Name:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    if not safe:
        safe = "Spot"
    return pikepdf.Name("/" + safe)


def ensure_spot_colorspace(
    pdf: pikepdf.Pdf,
    resources: pikepdf.Dictionary,
    spot_name: str,
    resource_name: str,
    alternate_cmyk: tuple[float, float, float, float] = (0.0, 1.0, 0.0, 0.0),
) -> str:
    if "/ColorSpace" not in resources or not isinstance(resources.get("/ColorSpace"), pikepdf.Dictionary):
        resources["/ColorSpace"] = pikepdf.Dictionary()
    color_spaces = resources["/ColorSpace"]
    res_name = "/" + "".join(ch if ch.isalnum() else "_" for ch in resource_name)
    c, m, y, k = alternate_cmyk
    color_spaces[res_name] = pikepdf.Array(
        [
            pikepdf.Name("/Separation"),
            pdf_name(spot_name),
            pikepdf.Name("/DeviceCMYK"),
            pikepdf.Dictionary(
                {
                    "/FunctionType": 2,
                    "/Domain": pikepdf.Array([0, 1]),
                    "/C0": pikepdf.Array([0, 0, 0, 0]),
                    "/C1": pikepdf.Array([c, m, y, k]),
                    "/N": 1,
                }
            ),
        ]
    )
    return res_name


def ensure_extgstate_overprint(pdf: pikepdf.Pdf, resources: pikepdf.Dictionary, resource_name: str) -> str:
    if "/ExtGState" not in resources or not isinstance(resources.get("/ExtGState"), pikepdf.Dictionary):
        resources["/ExtGState"] = pikepdf.Dictionary()
    ext = resources["/ExtGState"]
    name = "/" + "".join(ch if ch.isalnum() else "_" for ch in resource_name)
    ext[name] = pdf.make_indirect(
        pikepdf.Dictionary(
            {
                "/Type": pikepdf.Name("/ExtGState"),
                "/OP": True,
                "/op": True,
                "/OPM": 1,
                "/CA": 1,
                "/ca": 1,
                "/BM": pikepdf.Name("/Normal"),
            }
        )
    )
    return name


def page_resources(page: pikepdf.Page) -> pikepdf.Dictionary:
    resources = page.obj.get("/Resources")
    if not isinstance(resources, pikepdf.Dictionary):
        resources = pikepdf.Dictionary()
        page.obj["/Resources"] = resources
    return resources


def set_pdf_boxes(
    page: pikepdf.Page,
    width: float,
    height: float,
    trim_box: Optional[tuple[float, float, float, float]] = None,
) -> None:
    page.MediaBox = pikepdf.Array([0, 0, width, height])
    page.CropBox = pikepdf.Array([0, 0, width, height])
    page.BleedBox = pikepdf.Array([0, 0, width, height])
    if trim_box is None:
        trim_box = (0, 0, width, height)
    page.TrimBox = pikepdf.Array(list(trim_box))
    page.ArtBox = pikepdf.Array(list(trim_box))


def append_or_wrap_content(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    before: Optional[str] = None,
    after: Optional[str] = None,
    append: Optional[str] = None,
) -> None:
    existing = page.obj.get("/Contents")
    pieces: list[object] = []
    if before:
        pieces.append(pdf.make_indirect(pikepdf.Stream(pdf, before.encode("ascii"))))
    if existing is not None:
        if isinstance(existing, pikepdf.Array):
            pieces.extend(existing)
        else:
            pieces.append(existing)
    if after:
        pieces.append(pdf.make_indirect(pikepdf.Stream(pdf, after.encode("ascii"))))
    if append:
        pieces.append(pdf.make_indirect(pikepdf.Stream(pdf, append.encode("ascii"))))
    if not pieces:
        page.obj.pop("/Contents", None)
    elif len(pieces) == 1:
        page.obj["/Contents"] = pieces[0]
    else:
        page.obj["/Contents"] = pikepdf.Array(pieces)


def shape_mode_includes_cut(mode: str) -> bool:
    return mode in {"cut", "both"}


def shape_mode_includes_bleed(mode: str) -> bool:
    return mode in {"bleed", "both"}


def circle_path(cx: float, cy: float, radius: float) -> list[str]:
    k = 0.5522847498307936
    c = radius * k
    return [
        f"{pdf_number(cx + radius)} {pdf_number(cy)} m",
        f"{pdf_number(cx + radius)} {pdf_number(cy + c)} {pdf_number(cx + c)} {pdf_number(cy + radius)} {pdf_number(cx)} {pdf_number(cy + radius)} c",
        f"{pdf_number(cx - c)} {pdf_number(cy + radius)} {pdf_number(cx - radius)} {pdf_number(cy + c)} {pdf_number(cx - radius)} {pdf_number(cy)} c",
        f"{pdf_number(cx - radius)} {pdf_number(cy - c)} {pdf_number(cx - c)} {pdf_number(cy - radius)} {pdf_number(cx)} {pdf_number(cy - radius)} c",
        f"{pdf_number(cx + c)} {pdf_number(cy - radius)} {pdf_number(cx + radius)} {pdf_number(cy - c)} {pdf_number(cx + radius)} {pdf_number(cy)} c",
        "h",
    ]


def rounded_rect_path(x: float, y: float, width: float, height: float, radius: float) -> list[str]:
    radius = max(0.0, min(radius, width / 2.0, height / 2.0))
    if radius <= 0.0001:
        return [
            f"{pdf_number(x)} {pdf_number(y)} m",
            f"{pdf_number(x + width)} {pdf_number(y)} l",
            f"{pdf_number(x + width)} {pdf_number(y + height)} l",
            f"{pdf_number(x)} {pdf_number(y + height)} l",
            "h",
        ]
    k = 0.5522847498307936
    c = radius * k
    r = radius
    return [
        f"{pdf_number(x + r)} {pdf_number(y)} m",
        f"{pdf_number(x + width - r)} {pdf_number(y)} l",
        f"{pdf_number(x + width - r + c)} {pdf_number(y)} {pdf_number(x + width)} {pdf_number(y + r - c)} {pdf_number(x + width)} {pdf_number(y + r)} c",
        f"{pdf_number(x + width)} {pdf_number(y + height - r)} l",
        f"{pdf_number(x + width)} {pdf_number(y + height - r + c)} {pdf_number(x + width - r + c)} {pdf_number(y + height)} {pdf_number(x + width - r)} {pdf_number(y + height)} c",
        f"{pdf_number(x + r)} {pdf_number(y + height)} l",
        f"{pdf_number(x + r - c)} {pdf_number(y + height)} {pdf_number(x)} {pdf_number(y + height - r + c)} {pdf_number(x)} {pdf_number(y + height - r)} c",
        f"{pdf_number(x)} {pdf_number(y + r)} l",
        f"{pdf_number(x)} {pdf_number(y + r - c)} {pdf_number(x + r - c)} {pdf_number(y)} {pdf_number(x + r)} {pdf_number(y)} c",
        "h",
    ]


def shape_clip_stream(path_commands: Sequence[str]) -> str:
    return "q\n" + "\n".join(path_commands) + "\nW n\n"


def generated_cutline_stream(
    path_commands: Sequence[str],
    stroke_width_pt: float,
    spot: bool = False,
    overprint: bool = True,
) -> str:
    lines = ["q"]
    if overprint:
        lines.append("/GS_MADIMO_OP gs")
    if spot:
        lines.extend(["/CS_MADIMO_GENERATED_CUT CS", "1 SCN"])
    else:
        lines.append("0 1 0 0 K")
    lines.extend(
        [
            f"{pdf_number(stroke_width_pt)} w",
            "1 J",
            "1 j",
            *path_commands,
            "S",
            "Q",
        ]
    )
    return "\n".join(lines) + "\n"


def ensure_generated_cut_resources(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    spot_name: str,
    alternate_cmyk: tuple[float, float, float, float] = (0.0, 1.0, 0.0, 0.0),
    overprint: bool = True,
) -> None:
    resources = page_resources(page)
    if overprint:
        ensure_extgstate_overprint(pdf, resources, "GS_MADIMO_OP")
    ensure_spot_colorspace(
        pdf,
        resources,
        spot_name,
        "CS_MADIMO_GENERATED_CUT",
        alternate_cmyk=alternate_cmyk,
    )


def apply_generated_page_edits(
    temp_pdf: Path,
    output_pdf: Path,
    edits: Sequence[GeneratedPageEdit],
    spot_name: str,
) -> None:
    pdf = pikepdf.open(temp_pdf)
    for edit in edits:
        page = pdf.pages[edit.page_index]
        width = float(page.MediaBox[2]) - float(page.MediaBox[0])
        height = float(page.MediaBox[3]) - float(page.MediaBox[1])
        set_pdf_boxes(page, width, height, edit.trim_box)
        if edit.contour_is_spot:
            ensure_generated_cut_resources(pdf, page, spot_name)
        elif edit.contour:
            resources = page_resources(page)
            ensure_extgstate_overprint(pdf, resources, "GS_MADIMO_OP")
        append_or_wrap_content(pdf, page, before=edit.before, after=edit.after, append=edit.contour)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output_pdf, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)


def fit_rect_contain(src_width: float, src_height: float, dst: object) -> object:
    import fitz

    scale = min(dst.width / src_width, dst.height / src_height)
    width = src_width * scale
    height = src_height * scale
    x0 = dst.x0 + (dst.width - width) / 2.0
    y0 = dst.y0 + (dst.height - height) / 2.0
    return fitz.Rect(x0, y0, x0 + width, y0 + height)


def simple_shape_geometry(args: argparse.Namespace) -> GeneratedShapeGeometry:
    shape_type = getattr(args, "shape_type", "circle")
    mode = getattr(args, "shape_generation_mode", "both")
    bleed_mm = float(getattr(args, "shape_bleed_mm", 3.0)) if shape_mode_includes_bleed(mode) else 0.0
    bleed_pt = bleed_mm * PT_PER_MM

    if shape_type == "circle":
        diameter_pt = float(args.circle_diameter_mm) * PT_PER_MM
        page_width = diameter_pt + (2 * bleed_pt)
        page_height = page_width
        cx = page_width / 2.0
        cy = page_height / 2.0
        cut_path = circle_path(cx, cy, diameter_pt / 2.0)
        bleed_path = circle_path(cx, cy, (diameter_pt / 2.0) + bleed_pt)
    else:
        width_pt = float(args.rect_width_mm) * PT_PER_MM
        height_pt = float(args.rect_height_mm) * PT_PER_MM
        radius_pt = float(args.rect_radius_mm) * PT_PER_MM
        page_width = width_pt + (2 * bleed_pt)
        page_height = height_pt + (2 * bleed_pt)
        cut_path = rounded_rect_path(bleed_pt, bleed_pt, width_pt, height_pt, radius_pt)
        bleed_path = rounded_rect_path(
            0,
            0,
            page_width,
            page_height,
            radius_pt + bleed_pt if radius_pt > 0 else 0,
        )

    trim = (bleed_pt, bleed_pt, page_width - bleed_pt, page_height - bleed_pt)
    return GeneratedShapeGeometry(page_width, page_height, trim, cut_path, bleed_path)


def mask_filter_size(radius_px: int) -> int:
    return max(3, radius_px * 2 + 1)


def signed_area(points: Sequence[tuple[float, float]]) -> float:
    area = 0.0
    if len(points) < 3:
        return 0.0
    previous = points[-1]
    for current in points:
        area += previous[0] * current[1] - current[0] * previous[1]
        previous = current
    return area / 2.0


def extract_boundary_loops_from_mask(mask, min_area_px2: float = 80.0) -> list[list[tuple[float, float]]]:
    if mask.mode != "L":
        mask = mask.convert("L")
    width, height = mask.size
    data = mask.tobytes()
    starts: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add_edge(start: tuple[int, int], end: tuple[int, int]) -> None:
        starts.setdefault(start, []).append(end)

    for y in range(height):
        row = y * width
        for x in range(width):
            idx = row + x
            if data[idx] == 0:
                continue
            if y == 0 or data[idx - width] == 0:
                add_edge((x, y), (x + 1, y))
            if x == width - 1 or data[idx + 1] == 0:
                add_edge((x + 1, y), (x + 1, y + 1))
            if y == height - 1 or data[idx + width] == 0:
                add_edge((x + 1, y + 1), (x, y + 1))
            if x == 0 or data[idx - 1] == 0:
                add_edge((x, y + 1), (x, y))

    loops: list[list[tuple[float, float]]] = []
    while starts:
        start = next(iter(starts))
        current = start
        points: list[tuple[int, int]] = [start]
        guard = 0
        while guard < 2_000_000:
            guard += 1
            outgoing = starts.get(current)
            if not outgoing:
                break
            nxt = outgoing.pop()
            if not outgoing:
                del starts[current]
            current = nxt
            points.append(current)
            if current == start:
                break
        if len(points) < 5 or points[-1] != start:
            continue
        loop = [(float(x), float(y)) for x, y in points[:-1]]
        area = signed_area(loop)
        if area > min_area_px2:
            loops.append(loop)
    loops.sort(key=lambda pts: abs(signed_area(pts)), reverse=True)
    return loops


def perpendicular_distance(point: tuple[float, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    sx, sy = start
    ex, ey = end
    px, py = point
    dx = ex - sx
    dy = ey - sy
    if dx == 0 and dy == 0:
        return math.hypot(px - sx, py - sy)
    return abs(dy * px - dx * py + ex * sy - ey * sx) / math.hypot(dx, dy)


def rdp_open(points: Sequence[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        max_distance = -1.0
        index = start
        for i in range(start + 1, end):
            distance = perpendicular_distance(points[i], points[start], points[end])
            if distance > max_distance:
                max_distance = distance
                index = i
        if max_distance > epsilon:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [point for point, should_keep in zip(points, keep) if should_keep]


def simplify_closed_loop(points: Sequence[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    if len(points) <= 8 or epsilon <= 0:
        return list(points)
    start_index = min(range(len(points)), key=lambda i: (points[i][0], points[i][1]))
    rotated = list(points[start_index:]) + list(points[:start_index])
    simplified = rdp_open(rotated + [rotated[0]], epsilon)
    if simplified and simplified[-1] == simplified[0]:
        simplified.pop()
    return simplified if len(simplified) >= 4 else rotated


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def irregular_smoothing_radius_px(args: argparse.Namespace, dpi: float) -> int:
    smooth_mm = max(0.0, float(getattr(args, "irregular_smooth_mm", 2.0)))
    return max(0, int(round(smooth_mm / 25.4 * dpi * 2.0)))


def irregular_simplify_epsilon_px(args: argparse.Namespace, dpi: float) -> float:
    smooth_mm = max(0.0, float(getattr(args, "irregular_smooth_mm", 2.0)))
    simplify_percent = clamp_float(float(getattr(args, "irregular_simplify_percent", 15.0)), 0.0, 100.0)
    simplify_mm = smooth_mm * 0.9 + (simplify_percent / 100.0) * 8.0
    return max(0.5, simplify_mm / 25.4 * dpi)


def render_irregular_mask(page: object, args: argparse.Namespace, total_offset_mm: float):
    import fitz
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

    dpi = 96
    scale = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    white_threshold = int(getattr(args, "irregular_white", 248))
    diff = ImageChops.difference(image, Image.new("RGB", image.size, "white")).convert("L")
    threshold = max(0, min(255, 255 - white_threshold))
    base_mask = diff.point(lambda value: 255 if value > threshold else 0, mode="L")

    ignore_px = max(0, int(round(float(getattr(args, "irregular_ignore_edge_mm", 18.0)) / 25.4 * dpi)))
    if ignore_px > 0:
        cleaned = base_mask.copy()
        draw = ImageDraw.Draw(cleaned)
        width, height = cleaned.size
        draw.rectangle((0, 0, width, ignore_px), fill=0)
        draw.rectangle((0, height - ignore_px, width, height), fill=0)
        draw.rectangle((0, 0, ignore_px, height), fill=0)
        draw.rectangle((width - ignore_px, 0, width, height), fill=0)
        if cleaned.getbbox():
            base_mask = cleaned

    smooth_mm = max(0.0, float(getattr(args, "irregular_smooth_mm", 2.0)))
    workspace_margin_mm = max(0.0, float(total_offset_mm)) + smooth_mm + 1.0
    pad_px = max(0, int(math.ceil(workspace_margin_mm / 25.4 * dpi)))
    if pad_px > 0:
        base_mask = ImageOps.expand(base_mask, border=pad_px, fill=0)

    close_px = max(0, int(round(float(getattr(args, "irregular_close_mm", 10.0)) / 25.4 * dpi)))
    if close_px > 0:
        size = mask_filter_size(close_px)
        base_mask = base_mask.filter(ImageFilter.MaxFilter(size))
        base_mask = base_mask.filter(ImageFilter.MinFilter(size))

    offset_px = max(0, int(round(total_offset_mm / 25.4 * dpi)))
    if offset_px > 0:
        base_mask = base_mask.filter(ImageFilter.MaxFilter(mask_filter_size(offset_px)))

    smooth_px = irregular_smoothing_radius_px(args, dpi)
    if smooth_px > 0:
        soften_px = max(1, smooth_px // 2)
        base_mask = base_mask.filter(ImageFilter.MaxFilter(mask_filter_size(soften_px)))
        base_mask = base_mask.filter(ImageFilter.GaussianBlur(smooth_px))
        base_mask = base_mask.point(lambda value: 255 if value >= 128 else 0, mode="L")
        if smooth_px >= 4:
            base_mask = base_mask.filter(ImageFilter.GaussianBlur(max(1, smooth_px // 2)))
            base_mask = base_mask.point(lambda value: 255 if value >= 128 else 0, mode="L")

    return base_mask, scale, pad_px


def loop_to_pdf_path(
    loop: Sequence[tuple[float, float]],
    scale: float,
    crop: object,
    page_height_pt: float,
    pad_px: int = 0,
    smooth_curves: bool = True,
) -> list[str]:
    points = []
    for x_px, y_px in loop:
        x_top = (x_px - pad_px) / scale
        y_top = (y_px - pad_px) / scale
        points.append((x_top - crop.x0, page_height_pt - (y_top - crop.y0)))
    if len(points) < 3:
        return []
    commands = [f"{pdf_number(points[0][0])} {pdf_number(points[0][1])} m"]
    if smooth_curves and len(points) >= 4:
        count = len(points)
        for i in range(count):
            p0 = points[(i - 1) % count]
            p1 = points[i]
            p2 = points[(i + 1) % count]
            p3 = points[(i + 2) % count]
            c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
            commands.append(
                f"{pdf_number(c1[0])} {pdf_number(c1[1])} {pdf_number(c2[0])} {pdf_number(c2[1])} {pdf_number(p2[0])} {pdf_number(p2[1])} c"
            )
    else:
        for x, y in points[1:]:
            commands.append(f"{pdf_number(x)} {pdf_number(y)} l")
    commands.append("h")
    return commands


def loop_bbox_pt(loop: Sequence[tuple[float, float]], scale: float, pad_px: int = 0):
    import fitz

    xs = [(point[0] - pad_px) / scale for point in loop]
    ys = [(point[1] - pad_px) / scale for point in loop]
    return fitz.Rect(min(xs), min(ys), max(xs), max(ys))


def irregular_shape_geometry(page: object, args: argparse.Namespace) -> GeneratedShapeGeometry:
    import fitz

    cut_offset = float(getattr(args, "irregular_offset_mm", 3.0))
    bleed = float(getattr(args, "shape_bleed_mm", 3.0)) if shape_mode_includes_bleed(getattr(args, "shape_generation_mode", "both")) else 0.0
    cut_mask, scale, cut_pad_px = render_irregular_mask(page, args, cut_offset)
    bleed_mask, _scale, bleed_pad_px = render_irregular_mask(page, args, cut_offset + bleed)
    def loops_from_active_mask(mask) -> list[list[tuple[float, float]]]:
        bbox = mask.getbbox()
        if not bbox:
            return []
        x0, y0, _x1, _y1 = bbox
        cropped = mask.crop(bbox)
        loops = extract_boundary_loops_from_mask(cropped)
        return [[(x + x0, y + y0) for x, y in loop] for loop in loops]

    cut_loops = loops_from_active_mask(cut_mask)
    bleed_loops = loops_from_active_mask(bleed_mask)
    if not cut_loops or not bleed_loops:
        raise RuntimeError("Nie udało się wygenerować nieregularnego obrysu grafiki.")
    dpi = scale * 72.0
    simplify_px = irregular_simplify_epsilon_px(args, dpi)
    cut_loop = simplify_closed_loop(cut_loops[0], simplify_px)
    bleed_loop = simplify_closed_loop(bleed_loops[0], simplify_px)

    crop = loop_bbox_pt(bleed_loop, scale, bleed_pad_px)
    if crop.width < 1 or crop.height < 1:
        raise RuntimeError("Nie udało się obliczyć pola strony dla wygenerowanego obrysu.")
    net_bbox = loop_bbox_pt(cut_loop, scale, cut_pad_px)
    trim_box = (
        max(0.0, net_bbox.x0 - crop.x0),
        max(0.0, crop.y1 - net_bbox.y1),
        min(crop.width, net_bbox.x1 - crop.x0),
        min(crop.height, crop.y1 - net_bbox.y0),
    )
    cut_path = loop_to_pdf_path(cut_loop, scale, crop, crop.height, pad_px=cut_pad_px)
    bleed_path = loop_to_pdf_path(bleed_loop, scale, crop, crop.height, pad_px=bleed_pad_px)
    return GeneratedShapeGeometry(crop.width, crop.height, trim_box, cut_path, bleed_path, source_clip=crop)


def make_dieline_stream(
    pdf: pikepdf.Pdf,
    specs: Sequence[DielineSpec],
    plate_parts: dict[str, list],
    tx: float,
    ty: float,
    width: float,
    height: float,
) -> pikepdf.Stream:
    chunks: list[bytes] = [
        (
            "q\n"
            f"0 0 {pdf_number(width)} {pdf_number(height)} re\n"
            "W n\n"
            f"1 0 0 1 {pdf_number(-tx)} {pdf_number(-ty)} cm\n"
        ).encode("ascii")
    ]
    for spec in specs:
        instructions = plate_parts.get(spec.name, [])
        if not instructions:
            continue
        chunks.append(b"q\n")
        output = spec.output or spec.target
        chunks.append(target_stroke_prefix(output, spec.output_spot_space_name or spec.spot_space_name))
        chunks.append(unparse_content_stream(instructions))
        chunks.append(b"\nQ\n")
    chunks.append(b"Q\n")
    return pdf.make_stream(b"".join(chunks))


def clone_resources(resources) -> pikepdf.Dictionary:
    if isinstance(resources, pikepdf.Dictionary):
        return copy.copy(resources)
    return pikepdf.Dictionary()


def matrix_multiply(m1: Sequence[float], m2: Sequence[float]) -> tuple[float, float, float, float, float, float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def transform_point(matrix: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return a * x + c * y + e, b * x + d * y + f


def extend_bbox(
    bbox: Optional[tuple[float, float, float, float]],
    point: tuple[float, float],
) -> tuple[float, float, float, float]:
    x, y = point
    if bbox is None:
        return x, y, x, y
    llx, lly, urx, ury = bbox
    return min(llx, x), min(lly, y), max(urx, x), max(ury, y)


def instructions_bbox(instructions: Sequence) -> Optional[tuple[float, float, float, float]]:
    ctm: tuple[float, float, float, float, float, float] = (1, 0, 0, 1, 0, 0)
    stack: list[tuple[float, float, float, float, float, float]] = []
    bbox: Optional[tuple[float, float, float, float]] = None
    path_points: list[tuple[float, float]] = []
    current_point: Optional[tuple[float, float]] = None

    def add_raw_point(x: float, y: float) -> None:
        nonlocal current_point
        point = transform_point(ctm, x, y)
        path_points.append(point)
        current_point = (x, y)

    def flush_path() -> None:
        nonlocal bbox, path_points
        for point in path_points:
            bbox = extend_bbox(bbox, point)
        path_points = []

    for instruction in instructions:
        op = namestr(instruction.operator)
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            if stack:
                ctm = stack.pop()
        elif op == "cm":
            values = floats(instruction.operands)
            if len(values) != 6:
                continue
            ctm = matrix_multiply(ctm, values)
        elif op == "m":
            values = floats(instruction.operands)
            if len(values) < 2:
                continue
            add_raw_point(values[0], values[1])
        elif op == "l":
            values = floats(instruction.operands)
            if len(values) < 2:
                continue
            add_raw_point(values[0], values[1])
        elif op == "c":
            values = floats(instruction.operands)
            if len(values) < 6:
                continue
            add_raw_point(values[0], values[1])
            add_raw_point(values[2], values[3])
            add_raw_point(values[4], values[5])
        elif op == "v":
            values = floats(instruction.operands)
            if len(values) < 4:
                continue
            if current_point:
                add_raw_point(current_point[0], current_point[1])
            add_raw_point(values[0], values[1])
            add_raw_point(values[2], values[3])
        elif op == "y":
            values = floats(instruction.operands)
            if len(values) < 4:
                continue
            add_raw_point(values[0], values[1])
            add_raw_point(values[2], values[3])
            add_raw_point(values[2], values[3])
        elif op == "re":
            values = floats(instruction.operands)
            if len(values) < 4:
                continue
            x, y, width, height = values[:4]
            add_raw_point(x, y)
            add_raw_point(x + width, y)
            add_raw_point(x + width, y + height)
            add_raw_point(x, y + height)
        elif op in PAINT_FILL or op in PAINT_STROKE:
            flush_path()
        elif op == "n":
            path_points = []
            current_point = None
    flush_path()
    return bbox


def combined_instructions_bbox(parts: dict[str, list]) -> Optional[tuple[float, float, float, float]]:
    bbox: Optional[tuple[float, float, float, float]] = None
    for instructions in parts.values():
        part_bbox = instructions_bbox(instructions)
        if part_bbox:
            bbox = extend_bbox(bbox, (part_bbox[0], part_bbox[1]))
            bbox = extend_bbox(bbox, (part_bbox[2], part_bbox[3]))
    return bbox


def build_dieline_specs(args: argparse.Namespace) -> list[DielineSpec]:
    specs = []
    if getattr(args, "dieline_specs", None):
        for item in args.dieline_specs:
            enabled = bool(getattr(item, "enabled", True))
            if not enabled:
                continue
            output_mode = getattr(item, "output_mode", "same")
            output = output_target_from_values(
                output_mode,
                getattr(item, "output_color", ""),
                getattr(item, "output_name", ""),
                getattr(item, "output_value", ""),
                args.tolerance,
            )
            specs.append(
                DielineSpec(
                    name=getattr(item, "name"),
                    target=target_from_values(getattr(item, "mode"), getattr(item, "value"), args.tolerance),
                    output=output,
                    enabled=True,
                    silent_missing=bool(getattr(item, "silent_missing", False)),
                )
            )
        return specs

    for name in ("cut", "crease", "perf"):
        mode = getattr(args, f"{name}_mode", None)
        value = getattr(args, f"{name}_value", None)
        enabled = getattr(args, f"{name}_enabled", False)
        if enabled and mode and value:
            output_mode = getattr(args, f"{name}_output_mode", "same")
            output = output_target_from_values(
                output_mode,
                getattr(args, f"{name}_output_color", ""),
                getattr(args, f"{name}_output_name", ""),
                getattr(args, f"{name}_output_value", ""),
                args.tolerance,
            )
            specs.append(DielineSpec(name=name, target=target_from_values(mode, value, args.tolerance), output=output))
    return specs


def build_matrix_target(args: argparse.Namespace) -> TargetColor:
    target = TargetColor(
        spot=args.spot,
        cmyk=parse_percent_components(args.cmyk, 4) if args.cmyk else None,
        rgb=parse_percent_components(args.rgb, 3) if args.rgb else None,
        gray=parse_percent_components(args.gray, 1)[0] if args.gray else None,
        tolerance=args.tolerance,
    )
    if not any((target.spot, target.cmyk, target.rgb, target.gray is not None)):
        raise RuntimeError("Matrix mode requires --spot, --cmyk, --rgb, or --gray.")
    return target


def process_dieline_pdf(args: argparse.Namespace) -> None:
    specs = build_dieline_specs(args)
    if not specs:
        raise RuntimeError("No dieline colors were enabled.")

    page_setup = resolve_page_setup(args)
    bleed_mode = getattr(args, "bleed_mode", "mirror")
    preserve_original = bool(getattr(args, "preserve_original", True))

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    original_count = len(pdf.pages)
    if preserve_original:
        for index in range(original_count):
            pdf.pages.append(pdf.pages[index])
        work_start = original_count
        work_end = original_count * 2
    else:
        work_start = 0
        work_end = original_count

    first_work_page = pdf.pages[work_start]
    first_bleed = list(first_work_page.get("/BleedBox", first_work_page.get("/CropBox", first_work_page.MediaBox)))
    first_trim = list(first_work_page.get("/TrimBox", first_work_page.get("/CropBox", first_work_page.MediaBox)))
    first_missing_bleed = not has_required_bleed(first_work_page, args.bleed_mm)
    dieline_resources = clone_resources(first_work_page.get("/Resources", pikepdf.Dictionary()))

    plate_parts: dict[str, list] = {spec.name: [] for spec in specs}
    processed_pages: list[tuple[pikepdf.Page, list, set[str]]] = []
    warnings: list[str] = []

    for page_number, page_index in enumerate(range(work_start, work_end), start=1):
        page = pdf.pages[page_index]
        instructions = parse_content_stream(page)
        spaces_to_remove: set[str] = set()

        for spec in specs:
            selected_spaces = selected_colorspace_names(page, spec.target)
            if spec.target.spot and not selected_spaces and not spec.silent_missing:
                warn_spot_not_found(page_number, page, spec.target)
            if selected_spaces and not spec.spot_space_name:
                spec.spot_space_name = sorted(selected_spaces)[0]
            spaces_to_remove.update(selected_spaces)
            split = split_for_plate(
                instructions,
                GraphicsState(),
                selected_spaces,
                spec.target,
                page_number,
                paint_kinds={"stroke"},
            )
            warnings.extend(split.warnings)
            plate_parts[spec.name].extend(split.plate)
            instructions = split.keep

        processed_pages.append((page, instructions, spaces_to_remove))

    if not any(plate_parts.values()):
        raise RuntimeError("No matching dieline stroke objects were found.")

    dieline_bbox = None
    if getattr(args, "page_size", "source") == PAGE_SIZE_DIELINE_BOUNDS:
        dieline_bbox = combined_instructions_bbox(plate_parts)
        if dieline_bbox is None:
            raise RuntimeError("Could not calculate dieline bounds.")
        if preserve_original:
            for index in range(original_count):
                original_page = pdf.pages[index]
                original_instructions = parse_content_stream(original_page)
                tx, ty, _width, _height = set_print_boxes(
                    original_page,
                    dieline_bbox,
                    args.bleed_mm,
                    PageSetup(),
                    source_is_trim=True,
                )
                wrap_translated_content(pdf, original_page, original_instructions, tx, ty)

    for page, instructions, spaces_to_remove in processed_pages:
        for space_name in spaces_to_remove:
            remove_selected_colorspaces(page, {space_name})
        if dieline_bbox:
            tx, ty, _width, _height = set_print_boxes(
                page,
                dieline_bbox,
                args.bleed_mm,
                PageSetup(),
                source_is_trim=True,
            )
            wrap_translated_content(pdf, page, instructions, tx, ty)
            continue

        missing_bleed = bleed_mode in {"mirror", "stretch"} and not has_required_bleed(page, args.bleed_mm)
        if missing_bleed:
            source_box = list(page.get("/TrimBox", page.get("/CropBox", page.MediaBox)))
            wrap_with_generated_bleed(pdf, page, instructions, source_box, args.bleed_mm, page_setup, bleed_mode)
        else:
            source_box = list(page.get("/BleedBox", page.get("/CropBox", page.MediaBox)))
            tx, ty, _width, _height = set_print_boxes(page, source_box, args.bleed_mm, page_setup)
            wrap_translated_content(pdf, page, instructions, tx, ty)

    pdf.pages.append(pdf.pages[work_start])
    dieline_page = pdf.pages[-1]
    plate_source_is_trim = bool(dieline_bbox) or (bleed_mode in {"mirror", "stretch"} and first_missing_bleed)
    plate_source_box = dieline_bbox if dieline_bbox else (first_trim if plate_source_is_trim else first_bleed)
    tx, ty, width, height = set_print_boxes(
        dieline_page,
        plate_source_box,
        args.bleed_mm,
        PageSetup() if dieline_bbox else page_setup,
        source_is_trim=plate_source_is_trim,
    )
    dieline_page.Resources = dieline_resources
    for spec in specs:
        if spec.output and spec.output.spot:
            spec.output_spot_space_name = ensure_spot_colorspace(
                pdf,
                dieline_page.Resources,
                spec.output.spot,
                f"CS_{spec.name}",
                alternate_cmyk=spec.output.cmyk or (0.0, 1.0, 0.0, 0.0),
            )
    dieline_page.Contents = make_dieline_stream(pdf, specs, plate_parts, tx, ty, width, height)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def process_combined_pdf(args: argparse.Namespace) -> None:
    matrix_target = build_matrix_target(args)
    dieline_specs = build_dieline_specs(args)
    if not dieline_specs:
        raise RuntimeError("Combined mode requires at least one enabled dieline color.")

    plate_cmyk = parse_percent_components(args.plate_cmyk, 4)
    page_setup = resolve_page_setup(args)
    bleed_mode = getattr(args, "bleed_mode", "mirror")
    preserve_original = bool(getattr(args, "preserve_original", True))

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    original_count = len(pdf.pages)
    if preserve_original:
        for index in range(original_count):
            pdf.pages.append(pdf.pages[index])
        work_start = original_count
        work_end = original_count * 2
    else:
        work_start = 0
        work_end = original_count

    first_work_page = pdf.pages[work_start]
    first_bleed = list(first_work_page.get("/BleedBox", first_work_page.get("/CropBox", first_work_page.MediaBox)))
    first_trim = list(first_work_page.get("/TrimBox", first_work_page.get("/CropBox", first_work_page.MediaBox)))
    first_missing_bleed = not has_required_bleed(first_work_page, args.bleed_mm)
    dieline_resources = clone_resources(first_work_page.get("/Resources", pikepdf.Dictionary()))

    matrix_plate_instructions: list = []
    dieline_parts: dict[str, list] = {spec.name: [] for spec in dieline_specs}
    processed_pages: list[tuple[pikepdf.Page, list, set[str]]] = []
    warnings: list[str] = []

    for page_number, page_index in enumerate(range(work_start, work_end), start=1):
        page = pdf.pages[page_index]
        instructions = parse_content_stream(page)
        spaces_to_remove: set[str] = set()

        matrix_spaces = selected_colorspace_names(page, matrix_target)
        if matrix_target.spot and not matrix_spaces:
            warn_spot_not_found(page_number, page, matrix_target)
        matrix_split = split_for_plate(instructions, GraphicsState(), matrix_spaces, matrix_target, page_number)
        warnings.extend(matrix_split.warnings)
        spaces_to_remove.update(matrix_spaces)
        if args.plate_source == "all" or (args.plate_source == "first" and page_number == 1):
            matrix_plate_instructions.extend(matrix_split.plate)
        instructions = matrix_split.keep

        for spec in dieline_specs:
            selected_spaces = selected_colorspace_names(page, spec.target)
            if spec.target.spot and not selected_spaces and not spec.silent_missing:
                warn_spot_not_found(page_number, page, spec.target)
            if selected_spaces and not spec.spot_space_name:
                spec.spot_space_name = sorted(selected_spaces)[0]
            spaces_to_remove.update(selected_spaces)
            split = split_for_plate(
                instructions,
                GraphicsState(),
                selected_spaces,
                spec.target,
                page_number,
                paint_kinds={"stroke"},
            )
            warnings.extend(split.warnings)
            dieline_parts[spec.name].extend(split.plate)
            instructions = split.keep

        processed_pages.append((page, instructions, spaces_to_remove))

    if not matrix_plate_instructions:
        raise RuntimeError("No matching foil/plate objects were found.")
    if not any(dieline_parts.values()):
        raise RuntimeError("No matching dieline stroke objects were found.")

    dieline_bbox = None
    if getattr(args, "page_size", "source") == PAGE_SIZE_DIELINE_BOUNDS:
        dieline_bbox = combined_instructions_bbox(dieline_parts)
        if dieline_bbox is None:
            raise RuntimeError("Could not calculate dieline bounds.")
        if preserve_original:
            for index in range(original_count):
                original_page = pdf.pages[index]
                original_instructions = parse_content_stream(original_page)
                tx, ty, _width, _height = set_print_boxes(
                    original_page,
                    dieline_bbox,
                    args.bleed_mm,
                    PageSetup(),
                    source_is_trim=True,
                )
                wrap_translated_content(pdf, original_page, original_instructions, tx, ty)

    for page, instructions, spaces_to_remove in processed_pages:
        for space_name in spaces_to_remove:
            remove_selected_colorspaces(page, {space_name})
        if dieline_bbox:
            tx, ty, _width, _height = set_print_boxes(
                page,
                dieline_bbox,
                args.bleed_mm,
                PageSetup(),
                source_is_trim=True,
            )
            wrap_translated_content(pdf, page, instructions, tx, ty)
            continue

        missing_bleed = bleed_mode in {"mirror", "stretch"} and not has_required_bleed(page, args.bleed_mm)
        if missing_bleed:
            source_box = list(page.get("/TrimBox", page.get("/CropBox", page.MediaBox)))
            wrap_with_generated_bleed(pdf, page, instructions, source_box, args.bleed_mm, page_setup, bleed_mode)
        else:
            source_box = list(page.get("/BleedBox", page.get("/CropBox", page.MediaBox)))
            tx, ty, _width, _height = set_print_boxes(page, source_box, args.bleed_mm, page_setup)
            wrap_translated_content(pdf, page, instructions, tx, ty)

    pdf.pages.append(pdf.pages[work_start])
    matrix_page = pdf.pages[-1]
    plate_source_is_trim = bool(dieline_bbox) or (bleed_mode in {"mirror", "stretch"} and first_missing_bleed)
    plate_source_box = dieline_bbox if dieline_bbox else (first_trim if plate_source_is_trim else first_bleed)
    tx, ty, width, height = set_print_boxes(
        matrix_page,
        plate_source_box,
        args.bleed_mm,
        PageSetup() if dieline_bbox else page_setup,
        source_is_trim=plate_source_is_trim,
    )
    matrix_page.Contents = make_plate_stream(pdf, matrix_plate_instructions, tx, ty, width, height, plate_cmyk)
    if args.plate_resources == "empty":
        matrix_page.Resources = pikepdf.Dictionary()

    pdf.pages.append(pdf.pages[work_start])
    dieline_page = pdf.pages[-1]
    tx, ty, width, height = set_print_boxes(
        dieline_page,
        plate_source_box,
        args.bleed_mm,
        PageSetup() if dieline_bbox else page_setup,
        source_is_trim=plate_source_is_trim,
    )
    dieline_page.Resources = dieline_resources
    for spec in dieline_specs:
        if spec.output and spec.output.spot:
            spec.output_spot_space_name = ensure_spot_colorspace(
                pdf,
                dieline_page.Resources,
                spec.output.spot,
                f"CS_{spec.name}",
                alternate_cmyk=spec.output.cmyk or (0.0, 1.0, 0.0, 0.0),
            )
    dieline_page.Contents = make_dieline_stream(pdf, dieline_specs, dieline_parts, tx, ty, width, height)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def process_generated_shape_pdf(args: argparse.Namespace) -> None:
    if not bool(getattr(args, "shape_generator_enabled", False)):
        return
    try:
        import fitz
    except Exception as exc:
        raise RuntimeError("Generator kształtu wymaga biblioteki PyMuPDF / fitz.") from exc

    mode = getattr(args, "shape_generation_mode", "both")
    shape_type = getattr(args, "shape_type", "circle")
    include_cut = shape_mode_includes_cut(mode)
    include_bleed = shape_mode_includes_bleed(mode)
    if not include_cut and not include_bleed:
        raise RuntimeError("Wybierz tryb generowania kształtu.")

    input_pdf = Path(args.input)
    output_pdf = Path(args.output)
    stroke_width = float(getattr(args, "generated_cutline_stroke_pt", 0.5))
    spot_name = getattr(args, "generated_cutline_spot_name", "CutContour").strip() or "CutContour"

    src = fitz.open(input_pdf)
    if src.page_count < 1:
        raise RuntimeError("Input PDF has no pages.")

    out = fitz.open()
    edits: list[GeneratedPageEdit] = []

    for page_index, src_page in enumerate(src):
        if shape_type == "irregular":
            geometry = irregular_shape_geometry(src_page, args)
            clip = geometry.source_clip
            image_rect = fitz.Rect(0, 0, geometry.page_width_pt, geometry.page_height_pt)
        else:
            geometry = simple_shape_geometry(args)
            clip = None
            bleed_inset = float(getattr(args, "shape_bleed_mm", 3.0)) * PT_PER_MM if include_bleed else 0.0
            trim_rect = fitz.Rect(
                bleed_inset,
                bleed_inset,
                geometry.page_width_pt - bleed_inset,
                geometry.page_height_pt - bleed_inset,
            )
            full_rect = fitz.Rect(0, 0, geometry.page_width_pt, geometry.page_height_pt)
            image_rect = full_rect if include_bleed else trim_rect

        def add_art_page(use_bleed_clip: bool, add_cutline: bool) -> None:
            page = out.new_page(width=geometry.page_width_pt, height=geometry.page_height_pt)
            if clip is not None:
                shifted_source_rect = fitz.Rect(
                    -clip.x0,
                    -clip.y0,
                    src_page.rect.width - clip.x0,
                    src_page.rect.height - clip.y0,
                )
                page.show_pdf_page(shifted_source_rect, src, page_index)
            else:
                page.show_pdf_page(fit_rect_contain(src_page.rect.width, src_page.rect.height, image_rect), src, page_index)
            before = shape_clip_stream(geometry.bleed_path) if use_bleed_clip else None
            after = "Q\n" if use_bleed_clip else None
            contour = generated_cutline_stream(geometry.cut_path, stroke_width, spot=False) if add_cutline else None
            edits.append(
                GeneratedPageEdit(
                    page_index=page.number,
                    before=before,
                    after=after,
                    contour=contour,
                    contour_is_spot=False,
                    trim_box=geometry.trim_box,
                )
            )

        def add_line_page(spot: bool) -> None:
            page = out.new_page(width=geometry.page_width_pt, height=geometry.page_height_pt)
            edits.append(
                GeneratedPageEdit(
                    page_index=page.number,
                    contour=generated_cutline_stream(geometry.cut_path, stroke_width, spot=spot),
                    contour_is_spot=spot,
                    trim_box=geometry.trim_box,
                )
            )

        if include_cut and include_bleed:
            add_art_page(use_bleed_clip=False, add_cutline=True)
            add_art_page(use_bleed_clip=True, add_cutline=False)
            add_line_page(spot=False)
            add_line_page(spot=True)
        elif include_bleed:
            add_art_page(use_bleed_clip=True, add_cutline=False)
        elif include_cut:
            add_art_page(use_bleed_clip=False, add_cutline=True)
            add_line_page(spot=False)
            add_line_page(spot=True)

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
        temp_path = Path(handle.name)
    try:
        out.save(temp_path, garbage=4, deflate=True)
        apply_generated_page_edits(temp_path, output_pdf, edits, spot_name)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass
    logging.info("Saved %s", output_pdf)


def build_white_target(args: argparse.Namespace) -> TargetColor:
    mode = getattr(args, "white_target_mode", "cmyk")
    if mode == "spot":
        value = getattr(args, "white_input_spot", "")
    elif mode == "cmyk":
        value = getattr(args, "white_input_cmyk", "")
    elif mode == "rgb":
        value = getattr(args, "white_input_rgb", "")
    elif mode == "gray":
        value = getattr(args, "white_input_gray", "")
    else:
        raise RuntimeError("Podaj poprawny kolor wejściowy.")
    if not str(value).strip():
        raise RuntimeError("Podaj poprawny kolor wejściowy.")
    return target_from_values(mode, value, getattr(args, "tolerance", 0.05))


def make_white_stream(
    pdf: pikepdf.Pdf,
    instructions: Sequence,
    width: float,
    height: float,
    overprint: bool,
) -> pikepdf.Stream:
    lines = [
        "q",
        f"0 0 {pdf_number(width)} {pdf_number(height)} re",
        "W n",
    ]
    if overprint:
        lines.append("/GS_WHITE_OP gs")
    lines.extend(["/CS_WHITE_OUTPUT cs", "1 scn", "/CS_WHITE_OUTPUT CS", "1 SCN"])
    prefix = ("\n".join(lines) + "\n").encode("ascii")
    body = unparse_content_stream(instructions)
    return pdf.make_stream(prefix + body + b"\nQ\n")


def make_spot_plate_stream(
    pdf: pikepdf.Pdf,
    instructions: Sequence,
    width: float,
    height: float,
    color_space_name: str,
    extgstate_name: Optional[str] = None,
) -> pikepdf.Stream:
    lines = [
        "q",
        f"0 0 {pdf_number(width)} {pdf_number(height)} re",
        "W n",
    ]
    if extgstate_name:
        lines.append(f"{extgstate_name} gs")
    lines.extend(
        [
            f"{color_space_name} cs",
            "1 scn",
            f"{color_space_name} CS",
            "1 SCN",
        ]
    )
    prefix = ("\n".join(lines) + "\n").encode("ascii")
    body = unparse_content_stream(instructions)
    return pdf.make_stream(prefix + body + b"\nQ\n")


def ensure_white_resources(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    spot_name: str,
    alternate_cmyk: tuple[float, float, float, float],
    overprint: bool,
) -> None:
    resources = page_resources(page)
    ensure_spot_colorspace(pdf, resources, spot_name, "CS_WHITE_OUTPUT", alternate_cmyk=alternate_cmyk)
    if overprint:
        ensure_extgstate_overprint(pdf, resources, "GS_WHITE_OP")


def process_white_pdf(args: argparse.Namespace) -> None:
    if getattr(args, "white_source_type", "color") == "separate_page":
        process_white_from_separation_pdf(args)
        return

    target = build_white_target(args)
    spot_name = getattr(args, "white_output_spot_name", "White").strip()
    if not spot_name:
        raise RuntimeError("Podaj nazwę próbki WHITE.")
    alternate_cmyk = parse_percent_components(getattr(args, "white_output_cmyk", "100,0,0,0"), 4)
    overprint = bool(getattr(args, "white_overprint", False))
    output_type = getattr(args, "white_output_type", "graphic")
    preserve_original = bool(getattr(args, "white_preserve_original", True))
    include_graphic = output_type in {"graphic", "both"}
    include_separation = output_type in {"separation", "both"}
    if not include_graphic and not include_separation:
        raise RuntimeError("Wybierz sposób wyjścia WHITE.")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    original_count = len(pdf.pages)
    matches_found = False
    warnings: list[str] = []

    if include_graphic:
        if preserve_original:
            for index in range(original_count):
                pdf.pages.append(pdf.pages[index])
            graphic_range = range(original_count, original_count * 2)
        else:
            graphic_range = range(0, original_count)

        for page_number, page_index in enumerate(graphic_range, start=1):
            page = pdf.pages[page_index]
            selected_spaces = selected_colorspace_names(page, target)
            if target.spot and not selected_spaces:
                warn_spot_not_found(page_number, page, target)
            instructions = parse_content_stream(page)
            split = split_for_plate(instructions, GraphicsState(), selected_spaces, target, page_number)
            warnings.extend(split.warnings)
            if split.plate:
                matches_found = True
            ensure_white_resources(pdf, page, spot_name, alternate_cmyk, overprint)
            width = float(page.MediaBox[2]) - float(page.MediaBox[0])
            height = float(page.MediaBox[3]) - float(page.MediaBox[1])
            keep = unparse_content_stream(split.keep)
            white = make_white_stream(pdf, split.plate, width, height, overprint).read_bytes()
            page.Contents = pdf.make_stream(keep + b"\n" + white)

    if include_separation:
        for page_number in range(1, original_count + 1):
            source_page = pdf.pages[page_number - 1]
            pdf.pages.append(source_page)
            page = pdf.pages[-1]
            selected_spaces = selected_colorspace_names(page, target)
            if target.spot and not selected_spaces:
                warn_spot_not_found(page_number, page, target)
            instructions = parse_content_stream(page)
            split = split_for_plate(instructions, GraphicsState(), selected_spaces, target, page_number)
            warnings.extend(split.warnings)
            if split.plate:
                matches_found = True
            if not split.plate:
                logging.warning("Page %s: no WHITE objects matched.", page_number)
            ensure_white_resources(pdf, page, spot_name, alternate_cmyk, overprint)
            width = float(page.MediaBox[2]) - float(page.MediaBox[0])
            height = float(page.MediaBox[3]) - float(page.MediaBox[1])
            page.Contents = make_white_stream(pdf, split.plate, width, height, overprint)

    if not preserve_original and not include_graphic:
        for _ in range(original_count):
            del pdf.pages[0]

    if not matches_found:
        raise RuntimeError("Nie znaleziono obiektów pasujących do koloru wejściowego WHITE.")
    if overprint:
        logging.info("Ustawiono overprint dla elementów WHITE.")
    else:
        logging.info("Overprint WHITE jest wyłączony.")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def process_white_from_separation_pdf(args: argparse.Namespace) -> None:
    white_target = build_white_target(args)
    white_spot_name = getattr(args, "white_output_spot_name", "White").strip()
    if not white_spot_name:
        raise RuntimeError("Podaj nazwę próbki WHITE.")
    alternate_cmyk = parse_percent_components(getattr(args, "white_output_cmyk", "100,0,0,0"), 4)
    cut_spot_name = getattr(args, "white_cut_spot_name", "CUT").strip() or "CUT"
    overprint = bool(getattr(args, "white_overprint", True))

    pdf = pikepdf.open(args.input)
    if len(pdf.pages) < 2 or len(pdf.pages) % 2:
        raise RuntimeError("Tryb WHITE z osobnej strony separacji wymaga par stron: grafika+CUT oraz WHITE+CUT.")

    original_count = len(pdf.pages)
    warnings: list[str] = []
    matched_white = False
    matched_cut = False

    for pair_start in range(0, original_count, 2):
        art_source = pdf.pages[pair_start]
        white_source = pdf.pages[pair_start + 1]
        page_no = (pair_start // 2) + 1
        width = float(art_source.MediaBox[2]) - float(art_source.MediaBox[0])
        height = float(art_source.MediaBox[3]) - float(art_source.MediaBox[1])
        trim_box = tuple(float(v) for v in art_source.get("/TrimBox", art_source.get("/CropBox", art_source.MediaBox)))
        bleed_box = tuple(float(v) for v in art_source.get("/BleedBox", art_source.get("/CropBox", art_source.MediaBox)))

        cut_target = TargetColor(spot=cut_spot_name, tolerance=getattr(args, "tolerance", 0.05))

        art_cut_spaces = selected_colorspace_names(art_source, cut_target)
        art_split = split_for_plate(parse_content_stream(art_source), GraphicsState(), art_cut_spaces, cut_target, page_no)
        warnings.extend(art_split.warnings)

        white_cut_spaces = selected_colorspace_names(white_source, cut_target)
        white_without_cut = split_for_plate(
            parse_content_stream(white_source),
            GraphicsState(),
            white_cut_spaces,
            cut_target,
            page_no,
        )
        warnings.extend(white_without_cut.warnings)

        white_spaces = selected_colorspace_names(white_source, white_target)
        white_split = split_for_plate(
            white_without_cut.keep,
            GraphicsState(),
            white_spaces,
            white_target,
            page_no,
        )
        warnings.extend(white_split.warnings)

        cut_instructions = list(art_split.plate or white_without_cut.plate)
        if white_split.plate:
            matched_white = True
        if cut_instructions:
            matched_cut = True

        pdf.pages.append(art_source)
        preview_page = pdf.pages[-1]
        pdf.pages.append(art_source)
        print_page = pdf.pages[-1]
        pdf.pages.append(art_source)
        cut_page = pdf.pages[-1]

        for page in (preview_page, print_page, cut_page):
            set_pdf_boxes(page, width, height, trim_box)
            page.BleedBox = pikepdf.Array(list(bleed_box))

        ensure_white_resources(pdf, preview_page, white_spot_name, alternate_cmyk, overprint)
        ensure_white_resources(pdf, print_page, white_spot_name, alternate_cmyk, overprint)
        preview_cut_space = ensure_spot_colorspace(
            pdf,
            page_resources(preview_page),
            cut_spot_name,
            "CS_CUT_OUTPUT",
            alternate_cmyk=(0.0, 1.0, 0.0, 0.0),
        )
        cut_space = ensure_spot_colorspace(
            pdf,
            page_resources(cut_page),
            cut_spot_name,
            "CS_CUT_OUTPUT",
            alternate_cmyk=(0.0, 1.0, 0.0, 0.0),
        )
        preview_ext = ensure_extgstate_overprint(pdf, page_resources(preview_page), "GS_CUT_OP")
        cut_ext = ensure_extgstate_overprint(pdf, page_resources(cut_page), "GS_CUT_OP")

        white_stream_preview = make_white_stream(pdf, white_split.plate, width, height, overprint).read_bytes()
        cut_stream_preview = make_spot_plate_stream(
            pdf,
            cut_instructions,
            width,
            height,
            preview_cut_space,
            preview_ext,
        ).read_bytes()
        preview_page.Contents = pdf.make_stream(
            white_stream_preview + b"\n" + unparse_content_stream(art_split.keep) + b"\n" + cut_stream_preview
        )

        print_page.Contents = pdf.make_stream(
            make_white_stream(pdf, white_split.plate, width, height, overprint).read_bytes()
            + b"\n"
            + unparse_content_stream(art_split.keep)
        )

        cut_page.Resources = pikepdf.Dictionary()
        cut_space = ensure_spot_colorspace(
            pdf,
            page_resources(cut_page),
            cut_spot_name,
            "CS_CUT_OUTPUT",
            alternate_cmyk=(0.0, 1.0, 0.0, 0.0),
        )
        cut_ext = ensure_extgstate_overprint(pdf, page_resources(cut_page), "GS_CUT_OP")
        cut_page.Contents = make_spot_plate_stream(pdf, cut_instructions, width, height, cut_space, cut_ext)

    if not matched_white:
        raise RuntimeError("Nie znaleziono obiektów pasujących do koloru wejściowego WHITE.")
    if not matched_cut:
        logging.warning("Nie znaleziono linii CUT do osobnej strony wykrojnika.")
    if overprint:
        logging.info("Ustawiono overprint dla elementów WHITE.")

    for _ in range(original_count):
        del pdf.pages[0]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def process_remove_color_pdf(args: argparse.Namespace) -> None:
    specs = build_remove_specs(args)
    if not specs:
        raise RuntimeError("Podaj kolor lub linie do usuniecia.")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    total_removed = 0
    page_scope = getattr(args, "remove_page_scope", "all")
    for page_number, page in enumerate(pdf.pages, start=1):
        if page_scope == "first" and page_number > 1:
            continue
        total_removed += remove_matching_objects_from_page(pdf, page, specs, page_number)

    if total_removed == 0:
        raise RuntimeError("Nie znaleziono obiektow pasujacych do koloru/linii do usuniecia.")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    logging.info("Usunieto pasujace operacje PDF: %s", total_removed)
    logging.info("Saved %s", output)


def process_black_pdf(args: argparse.Namespace) -> None:
    output_cmyk = parse_percent_components(getattr(args, "black_output_cmyk", "0,0,0,100"), 4)
    min_k = float(getattr(args, "black_min_k", 95.0)) / 100.0
    max_cmy = float(getattr(args, "black_max_cmy", 35.0)) / 100.0
    max_rgb_gray = float(getattr(args, "black_max_rgb_gray", 5.0)) / 100.0
    if not 0 <= min_k <= 1 or not 0 <= max_cmy <= 1 or not 0 <= max_rgb_gray <= 1:
        raise RuntimeError("Parametry zmiany czarnego musza miescic sie w zakresie 0-100.")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    total_changed = 0
    skipped_xobjects = 0
    for page in pdf.pages:
        instructions = parse_content_stream(page)
        rewritten, changed = rewrite_black_instructions(instructions, output_cmyk, min_k, max_cmy, max_rgb_gray)
        total_changed += changed
        if changed:
            page.Contents = pdf.make_stream(unparse_content_stream(rewritten))

        resources = page.get("/Resources", pikepdf.Dictionary())
        xobjects = resources.get("/XObject", pikepdf.Dictionary()) if isinstance(resources, pikepdf.Dictionary) else {}
        if isinstance(xobjects, pikepdf.Dictionary):
            for xobject in xobjects.values():
                if isinstance(xobject, pikepdf.Stream) and xobject.get("/Subtype") == pikepdf.Name("/Form"):
                    try:
                        form_instructions = parse_content_stream(xobject)
                        form_rewritten, form_changed = rewrite_black_instructions(
                            form_instructions,
                            output_cmyk,
                            min_k,
                            max_cmy,
                            max_rgb_gray,
                        )
                        if form_changed:
                            xobject.write(unparse_content_stream(form_rewritten))
                            total_changed += form_changed
                    except Exception:
                        skipped_xobjects += 1

    if total_changed == 0:
        raise RuntimeError("Nie znaleziono kolorow bliskich czarnemu do zmiany.")
    if skipped_xobjects:
        logging.warning(
            "Pominieto %s zagniezdzonych obiektow PDF, ktorych nie udalo sie przepisac.",
            skipped_xobjects,
        )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    logging.info("Zmieniono operacje koloru czarnego: %s", total_changed)
    logging.info("Saved %s", output)


def process_pdf(args: argparse.Namespace) -> None:
    target = build_matrix_target(args)
    plate_cmyk = parse_percent_components(args.plate_cmyk, 4)
    page_setup = resolve_page_setup(args)
    bleed_mode = getattr(args, "bleed_mode", "mirror")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    plate_instructions: list = []
    first_bleed = list(pdf.pages[0].get("/BleedBox", pdf.pages[0].get("/CropBox", pdf.pages[0].MediaBox)))
    first_trim = list(pdf.pages[0].get("/TrimBox", pdf.pages[0].get("/CropBox", pdf.pages[0].MediaBox)))
    first_missing_bleed = not has_required_bleed(pdf.pages[0], args.bleed_mm)
    warnings: list[str] = []

    for page_index, page in enumerate(list(pdf.pages), start=1):
        selected_spaces = selected_colorspace_names(page, target)
        if target.spot and not selected_spaces:
            warn_spot_not_found(page_index, page, target)

        instructions = parse_content_stream(page)
        split = split_for_plate(instructions, GraphicsState(), selected_spaces, target, page_index)
        warnings.extend(split.warnings)

        remove_selected_colorspaces(page, selected_spaces)
        missing_bleed = bleed_mode in {"mirror", "stretch"} and not has_required_bleed(page, args.bleed_mm)
        if missing_bleed:
            source_box = list(page.get("/TrimBox", page.get("/CropBox", page.MediaBox)))
            wrap_with_generated_bleed(pdf, page, split.keep, source_box, args.bleed_mm, page_setup, bleed_mode)
        else:
            source_box = list(page.get("/BleedBox", page.get("/CropBox", page.MediaBox)))
            tx, ty, width, height = set_print_boxes(page, source_box, args.bleed_mm, page_setup)
            wrap_translated_content(pdf, page, split.keep, tx, ty)

        if args.plate_source == "all" or (args.plate_source == "first" and page_index == 1):
            plate_instructions.extend(split.plate)

    if not plate_instructions:
        raise RuntimeError("No matching foil/plate objects were found.")

    pdf.pages.append(pdf.pages[0])
    plate_page = pdf.pages[-1]
    selected_spaces = selected_colorspace_names(plate_page, target)
    plate_source_is_trim = bleed_mode in {"mirror", "stretch"} and first_missing_bleed
    plate_source_box = first_trim if plate_source_is_trim else first_bleed
    tx, ty, width, height = set_print_boxes(
        plate_page,
        plate_source_box,
        args.bleed_mm,
        page_setup,
        source_is_trim=plate_source_is_trim,
    )
    plate_page.Contents = make_plate_stream(pdf, plate_instructions, tx, ty, width, height, plate_cmyk)
    remove_selected_colorspaces(plate_page, selected_spaces)
    if args.plate_resources == "empty":
        plate_page.Resources = pikepdf.Dictionary()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def process_bleed_pdf(args: argparse.Namespace) -> None:
    if getattr(args, "page_size", "source") == PAGE_SIZE_DIELINE_BOUNDS:
        raise RuntimeError("Bleed-only mode cannot calculate page size from dieline bounds.")

    page_setup = resolve_page_setup(args)
    bleed_mode = getattr(args, "bleed_mode", "mirror")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    for page in list(pdf.pages):
        instructions = parse_content_stream(page)
        missing_bleed = bleed_mode in {"mirror", "stretch"} and not has_required_bleed(page, args.bleed_mm)
        if missing_bleed:
            source_box = list(page.get("/TrimBox", page.get("/CropBox", page.MediaBox)))
            wrap_with_generated_bleed(pdf, page, instructions, source_box, args.bleed_mm, page_setup, bleed_mode)
        else:
            source_box = list(page.get("/BleedBox", page.get("/CropBox", page.MediaBox)))
            if page_setup.scale_content:
                wrap_fitted_content(pdf, page, instructions, source_box, args.bleed_mm, page_setup, source_is_trim=False)
            else:
                tx, ty, _width, _height = set_print_boxes(page, source_box, args.bleed_mm, page_setup)
                wrap_translated_content(pdf, page, instructions, tx, ty)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)
    logging.info("Saved %s", output)


def process_format_by_dieline_pdf(args: argparse.Namespace) -> None:
    specs = build_dieline_specs(args)
    if not specs:
        raise RuntimeError("Format po linii ciecia wymaga ustawienia koloru linii ciecia.")

    pdf = pikepdf.open(args.input)
    if not pdf.pages:
        raise RuntimeError("Input PDF has no pages.")

    plate_parts: dict[str, list] = {spec.name: [] for spec in specs}
    page_instructions: list[tuple[pikepdf.Page, list]] = []
    warnings: list[str] = []

    for page_number, page in enumerate(pdf.pages, start=1):
        instructions = parse_content_stream(page)
        page_instructions.append((page, instructions))
        for spec in specs:
            selected_spaces = selected_colorspace_names(page, spec.target)
            if spec.target.spot and not selected_spaces and not spec.silent_missing:
                warn_spot_not_found(page_number, page, spec.target)
            split = split_for_plate(
                instructions,
                GraphicsState(),
                selected_spaces,
                spec.target,
                page_number,
                paint_kinds={"stroke"},
            )
            warnings.extend(split.warnings)
            plate_parts[spec.name].extend(split.plate)

    if not any(plate_parts.values()):
        raise RuntimeError("Nie znaleziono linii ciecia pasujacej do wskazanego koloru.")

    dieline_bbox = combined_instructions_bbox(plate_parts)
    if dieline_bbox is None:
        raise RuntimeError("Nie udalo sie obliczyc formatu po linii ciecia.")

    bleed_mm = float(getattr(args, "bleed_mm", 0.0) or 0.0)
    bleed_mode = getattr(args, "bleed_mode", "none")
    page_setup = resolve_page_setup(args)
    for page, instructions in page_instructions:
        if bleed_mm > 0 and bleed_mode in {"mirror", "stretch"}:
            wrap_with_generated_bleed(pdf, page, instructions, dieline_bbox, bleed_mm, page_setup, bleed_mode)
        elif page_setup.scale_content:
            wrap_fitted_content(pdf, page, instructions, dieline_bbox, bleed_mm, page_setup, source_is_trim=True)
        else:
            tx, ty, _width, _height = set_print_boxes(
                page,
                dieline_bbox,
                bleed_mm,
                page_setup,
                source_is_trim=True,
            )
            wrap_translated_content(pdf, page, instructions, tx, ty)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(output, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.preserve)

    for warning in warnings:
        logging.warning(warning)
    logging.info("Saved %s", output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move foil/spot-colored PDF objects to an added plate page and set 3 mm bleed boxes."
    )
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("matrix", "dieline", "both", "bleed", "white", "shape", "black"), default="matrix")
    selector = parser.add_mutually_exclusive_group(required=False)
    selector.add_argument("--spot", help="Spot/separation color name, for example HS.")
    selector.add_argument("--cmyk", help="Process CMYK to select, e.g. 0,100,0,0 or 0,1,0,0.")
    selector.add_argument("--rgb", help="RGB to select, e.g. 255,0,0 or 1,0,0.")
    selector.add_argument("--gray", help="Gray value to select, e.g. 100 or 1.")
    parser.add_argument("--plate-cmyk", default="0,0,0,100", help="CMYK for the added plate page.")
    parser.add_argument("--bleed-mm", type=float, default=3.0)
    parser.add_argument(
        "--bleed-mode",
        choices=("none", "mirror", "stretch"),
        default="mirror",
        help="How to create bleed if the PDF has no usable BleedBox.",
    )
    parser.add_argument(
        "--page-size",
        default="source",
        help="Trim page size: source, dieline_bounds, A5, A4, A3, SRA3, B4, B3, or custom.",
    )
    parser.add_argument("--page-width-mm", type=float, help="Custom trim width in mm.")
    parser.add_argument("--page-height-mm", type=float, help="Custom trim height in mm.")
    parser.add_argument("--scale-content", action="store_true", help="Scale page contents while changing the page format.")
    parser.add_argument("--scale-mode", choices=("actual", "fit", "fill", "nonprop_fill"), default="actual")
    parser.add_argument("--scale-percent", type=float, default=100.0)
    parser.add_argument(
        "--plate-source",
        choices=("first", "all"),
        default="first",
        help="Use matching objects from the first page only, or overlay matches from all pages on one plate page.",
    )
    parser.add_argument(
        "--plate-resources",
        choices=("empty", "copy"),
        default="empty",
        help="Use empty resources on the plate page when the selected artwork is self-contained vector paths.",
    )
    parser.add_argument("--preserve-original", action="store_true", help="Dieline mode: keep original pages first.")
    parser.add_argument("--cut-enabled", action="store_true", help="Dieline mode: extract cut lines.")
    parser.add_argument("--cut-mode", choices=("cmyk", "spot", "rgb", "gray"), default="cmyk")
    parser.add_argument("--cut-value", default="0,100,100,0")
    parser.add_argument("--cut-output-mode", choices=("same", "spot", "cmyk", "rgb", "gray"), default="same")
    parser.add_argument("--cut-output-value", default="")
    parser.add_argument("--cut-output-color", default="")
    parser.add_argument("--cut-output-name", default="")
    parser.add_argument("--crease-enabled", action="store_true", help="Dieline mode: extract crease lines.")
    parser.add_argument("--crease-mode", choices=("cmyk", "spot", "rgb", "gray"), default="cmyk")
    parser.add_argument("--crease-value", default="88,63,0,47")
    parser.add_argument("--crease-output-mode", choices=("same", "spot", "cmyk", "rgb", "gray"), default="same")
    parser.add_argument("--crease-output-value", default="")
    parser.add_argument("--crease-output-color", default="")
    parser.add_argument("--crease-output-name", default="")
    parser.add_argument("--perf-enabled", action="store_true", help="Dieline mode: extract perforation lines.")
    parser.add_argument("--perf-mode", choices=("cmyk", "spot", "rgb", "gray"), default="cmyk")
    parser.add_argument("--perf-value", default="0,100,0,0")
    parser.add_argument("--perf-output-mode", choices=("same", "spot", "cmyk", "rgb", "gray"), default="same")
    parser.add_argument("--perf-output-value", default="")
    parser.add_argument("--perf-output-color", default="")
    parser.add_argument("--perf-output-name", default="")
    parser.add_argument("--shape-generator-enabled", action="store_true")
    parser.add_argument("--shape-generation-mode", choices=("cut", "bleed", "both"), default="both")
    parser.add_argument("--shape-type", choices=("circle", "rectangle", "irregular"), default="circle")
    parser.add_argument("--circle-diameter-mm", type=float, default=50.0)
    parser.add_argument("--rect-width-mm", type=float, default=90.0)
    parser.add_argument("--rect-height-mm", type=float, default=50.0)
    parser.add_argument("--rect-radius-mm", type=float, default=3.0)
    parser.add_argument("--shape-bleed-mm", type=float, default=3.0)
    parser.add_argument("--irregular-offset-mm", type=float, default=3.0)
    parser.add_argument("--irregular-close-mm", type=float, default=10.0)
    parser.add_argument("--irregular-smooth-mm", type=float, default=2.0)
    parser.add_argument("--irregular-simplify-percent", type=float, default=15.0)
    parser.add_argument("--irregular-ignore-edge-mm", type=float, default=18.0)
    parser.add_argument("--irregular-white", type=int, default=248)
    parser.add_argument("--generated-cutline-stroke-pt", type=float, default=0.5)
    parser.add_argument("--generated-cutline-spot-name", default="CutContour")
    parser.add_argument("--white-target-mode", choices=("spot", "cmyk", "rgb", "gray"), default="cmyk")
    parser.add_argument("--white-input-spot", default="")
    parser.add_argument("--white-input-cmyk", default="0,0,0,0")
    parser.add_argument("--white-input-rgb", default="")
    parser.add_argument("--white-input-gray", default="")
    parser.add_argument("--white-output-spot-name", default="White")
    parser.add_argument("--white-output-cmyk", default="100,0,0,0")
    parser.add_argument("--white-overprint", action="store_true")
    parser.add_argument("--white-output-type", choices=("separation", "graphic", "both"), default="graphic")
    parser.add_argument("--white-preserve-original", action="store_true")
    parser.add_argument("--white-source-type", choices=("color", "separate_page"), default="color")
    parser.add_argument("--white-cut-spot-name", default="CUT")
    parser.add_argument("--remove-selected", action="store_true", help="Remove selected color/line instead of creating a plate.")
    parser.add_argument("--remove-source", choices=("matrix", "dieline"), default="matrix")
    parser.add_argument("--remove-scope", choices=("all", "stroke", "fill"), default="all")
    parser.add_argument("--black-output-cmyk", default="0,0,0,100")
    parser.add_argument("--black-min-k", type=float, default=95.0)
    parser.add_argument("--black-max-cmy", type=float, default=35.0)
    parser.add_argument("--black-max-rgb-gray", type=float, default=5.0)
    parser.add_argument("--tolerance", type=float, default=0.05)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    if getattr(args, "remove_selected", False):
        process_remove_color_pdf(args)
    elif getattr(args, "shape_generator_enabled", False) or args.mode == "shape":
        args.shape_generator_enabled = True
        process_generated_shape_pdf(args)
    elif args.mode == "dieline":
        process_dieline_pdf(args)
    elif args.mode == "both":
        process_combined_pdf(args)
    elif args.mode == "bleed":
        process_bleed_pdf(args)
    elif args.mode == "white":
        process_white_pdf(args)
    elif args.mode == "black":
        process_black_pdf(args)
    else:
        process_pdf(args)


if __name__ == "__main__":
    main()
