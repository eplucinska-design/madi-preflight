"""Whole-file preflight report: issues per page with level (error / warning / info) and location.

Checks: page boxes and bleed, fonts, effective image resolution, RGB objects and images, spot
colours, white objects with effective overprint, transparency. Findings are facts read from the
PDF; thresholds come from the job (bleed, DPI) and are passed in, not imposed.
Not checked: line weights (needs full CTM analysis), PDF/X conformance, ink coverage per area.
"""

from __future__ import annotations

import io
import json
import sys

import fitz
import pikepdf

import colors
from colors import FILL_OPS, STROKE_OPS, TEXT_SHOW, State, Walker

MM = 72 / 25.4
LIMITS = ("Raport nie sprawdza grubosci linii, zgodnosci PDF/X ani pokrycia farba. "
          "Wynik nie zastepuje kontroli produkcyjnej.")


def _white(kind: str, values: tuple) -> bool:
    if kind == "cmyk":
        return len(values) == 4 and all(abs(v) < 1e-6 for v in values)
    if kind == "gray":
        return len(values) == 1 and abs(values[0] - 1) < 1e-6
    if kind == "rgb":
        return len(values) == 3 and all(abs(v - 1) < 1e-6 for v in values)
    return False


def _content_checks(pdf: pikepdf.Pdf):
    """Per page: RGB painting count, white overprint count, transparency flags (content-stream level)."""
    result = []
    for page in pdf.pages:
        stats = {"rgb": 0, "white_op": 0, "transparency": set()}

        def handler(op, operands, state: State, resources):
            fills = op in FILL_OPS or (op in TEXT_SHOW and state.render in (0, 2, 4, 6))
            strokes = op in STROKE_OPS or (op in TEXT_SHOW and state.render in (1, 2, 5, 6))
            if fills and state.fill_kind == "rgb" or strokes and state.stroke_kind == "rgb":
                stats["rgb"] += 1
            if fills and state.op_fill and _white(state.fill_kind, state.fill_vals) or \
               strokes and state.op_stroke and _white(state.stroke_kind, state.stroke_vals):
                stats["white_op"] += 1
            if op == "gs" and resources is not None:
                gs = resources.get("/ExtGState", pikepdf.Dictionary()).get(operands[0])
                if gs is not None:
                    if float(gs.get("/CA", 1)) < 1 or float(gs.get("/ca", 1)) < 1:
                        stats["transparency"].add("krycie < 100%")
                    if "/SMask" in gs and str(gs["/SMask"]) != "/None":
                        stats["transparency"].add("maska miekka")
                    if "/BM" in gs and str(gs["/BM"]) not in ("/Normal", "/Compatible"):
                        stats["transparency"].add("tryb mieszania " + str(gs["/BM"])[1:])
            return None

        Walker(handler).walk(pikepdf.parse_content_stream(page), page.obj.get("/Resources"), State())
        result.append(stats)
    return result


def report(data: bytes, bleed_mm: float = 3.0, dpi_error: int = 150, dpi_warning: int = 250) -> dict:
    issues = []

    def add(level, page, code, message, bbox=None):
        issues.append({"level": level, "page": page, "code": code, "message": message,
                       "bbox_mm": [round(v / MM, 2) for v in bbox] if bbox else None})

    with pikepdf.open(io.BytesIO(data)) as pdf:
        content = _content_checks(pdf)
        usage = colors.spot_usage(pdf)
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("PDF jest chroniony haslem.")
        seen_fonts = {}
        for index, page in enumerate(doc):
            number = index + 1
            rot = page.rotation_matrix
            crop = page.cropbox
            trim, bleed = page.trimbox, page.bleedbox
            if trim == crop and page.mediabox == crop:
                add("warning", number, "no_trimbox", "Brak TrimBox: format netto nieokreslony, spadu nie da sie sprawdzic.")
            else:
                margins = (trim.x0 - bleed.x0, trim.y0 - bleed.y0, bleed.x1 - trim.x1, bleed.y1 - trim.y1)
                smallest = min(margins) / MM
                if smallest + 0.05 < bleed_mm:
                    level = "error" if smallest < 0.05 else "warning"
                    add(level, number, "bleed", f"Spad {smallest:.1f} mm, wymagany {bleed_mm:g} mm (BleedBox wzgledem TrimBox).")
            for font in page.get_fonts(full=True):
                xref, _, kind, name = font[:4]
                key = xref or ("direct", name)
                if key in seen_fonts:
                    continue
                # A font dictionary stored inline (no xref) cannot carry an embedded font program here.
                embedded = bool(doc.extract_font(xref)[3]) if xref else False
                seen_fonts[key] = number
                clean = name.split("+")[-1] or "bez nazwy"
                if not embedded:
                    add("error", number, "font_not_embedded", f"Font nieosadzony: {clean} ({kind}).")
                elif kind == "Type3":
                    add("info", number, "font_type3", f"Font Type 3: {clean} (bitmapowy lub wektorowy, sprawdz jakosc).")
            for image in page.get_image_info():
                a, b, c, d, _, _ = image["transform"]
                width, height = (a * a + b * b) ** .5, (c * c + d * d) ** .5
                if not width or not height or image["width"] < 2:
                    continue
                dpi = min(image["width"] * 72 / width, image["height"] * 72 / height)
                box = fitz.Rect(image["bbox"]) * rot
                size = f"{width / MM:.0f} x {height / MM:.0f} mm"
                if dpi < dpi_error:
                    add("error", number, "image_dpi", f"Obraz {dpi:.0f} dpi ({size}), minimum {dpi_error} dpi.", box)
                elif dpi < dpi_warning:
                    add("warning", number, "image_dpi", f"Obraz {dpi:.0f} dpi ({size}), zalecane {dpi_warning} dpi.", box)
                cs = str(image.get("cs-name", ""))
                if "RGB" in cs.upper():
                    add("warning", number, "image_rgb", f"Obraz w RGB ({size}): wynik zalezy od konwersji w RIP.", box)
            stats = content[index]
            if stats["rgb"]:
                add("warning", number, "vector_rgb", f"Obiekty wektorowe lub tekst w RGB: {stats['rgb']}.")
            if stats["white_op"]:
                add("error", number, "white_overprint", f"Biale obiekty z nadrukiem: {stats['white_op']} (znikna w druku).")
            if stats["transparency"]:
                add("info", number, "transparency", "Przezroczystosc: " + ", ".join(sorted(stats["transparency"])) + ".")
        spots = sorted(name for name, item in usage.items() if item.fill or item.stroke or item.images)
        unused = sorted(name for name, item in usage.items() if not (item.fill or item.stroke or item.images))
        if spots:
            add("info", None, "spots", "Kolory spot w uzyciu: " + ", ".join(spots) + ".")
        if unused:
            add("info", None, "spots_unused", "Zdefiniowane, nieuzywane kolory spot: " + ", ".join(unused) + ".")
        pages = len(doc)
    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: (order[item["level"]], item["page"] or 0))
    counts = {level: sum(1 for item in issues if item["level"] == level) for level in order}
    return {"pages": pages, "issues": issues, "counts": counts,
            "settings": {"bleed_mm": bleed_mm, "dpi_error": dpi_error, "dpi_warning": dpi_warning},
            "limitations": LIMITS}


if __name__ == "__main__":
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    try:
        with open(sys.argv[1], "rb") as handle:
            result = report(handle.read(), float(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
        print(json.dumps(result, ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=True), file=sys.stderr)
        sys.exit(1)
