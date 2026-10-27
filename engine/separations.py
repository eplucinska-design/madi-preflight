"""Separation sampler: ink percentages per plate at a point, with overprint and knockout simulated.

Each plate (process CMYK, then every spot used on the page) is rendered from a rewritten copy of
the page, only in a small clip around the point:
- process plate: spot-coloured objects knock out (paint white) unless overprinted, in which case
  they are skipped; process objects render natively and MuPDF converts them to CMYK;
- spot plate S: objects in S paint gray = 1 - tint; every other object knocks out (white) unless
  overprinted, in which case it is skipped.
Known limits (reported to the user): spot shadings and images in spot colour spaces are not
sampled on their own plate; process images and shadings do not knock spot plates out; RGB values
are converted with MuPDF default profiles, not the job's output profile; OPM is not simulated.
"""

from __future__ import annotations

import io
from dataclasses import replace

import fitz
import pikepdf
from pikepdf import Name, Operator

import colors
from colors import FILL_OPS, PATH_PAINT, PATH_START, STROKE_OPS, TEXT_SHOW, State, Walker, _names, _xobject_spaces

LIMITS = ("Nie probkowane na wlasnej plycie: cieniowania i obrazy w przestrzeni spot. "
          "Obrazy i cieniowania procesowe nie wybijaja plyt spot. RGB przeliczane domyslnym profilem MuPDF, "
          "bez profilu wyjsciowego zlecenia. OPM nie jest symulowany.")


def _decide(plate: str, kind: str, names: tuple, values: tuple, overprint: bool):
    """Return ('keep'|'drop'|'white'|('gray', value)) for one paint part on a plate."""
    if plate == "process":
        if kind != "spot":
            return "keep"
        return "drop" if overprint else "white"
    if kind == "spot" and plate in names:
        index = names.index(plate)
        tint = values[index] if index < len(values) else 1.0
        return ("gray", 1.0 - max(0.0, min(1.0, tint)))
    return "drop" if overprint else "white"


def _color_ops(plate, part, action):
    """Operators setting the fill (part='f') or stroke ('s') colour for a decision."""
    if action == "white":
        if plate == "process":
            return [([0, 0, 0, 0], Operator("k" if part == "f" else "K"))]
        return [([1], Operator("g" if part == "f" else "G"))]
    if isinstance(action, tuple):
        return [([round(action[1], 4)], Operator("g" if part == "f" else "G"))]
    return []


def _plate_handler(plate):
    def handler(page, op, operands, state: State, resources):
        fill = _decide(plate, state.fill_kind, state.fill, state.fill_vals, state.op_fill)
        stroke = _decide(plate, state.stroke_kind, state.stroke, state.stroke_vals, state.op_stroke)
        if op in PATH_PAINT and op != "n":
            paints_fill, paints_stroke = op in FILL_OPS, op in STROKE_OPS
            fill_on = paints_fill and fill != "drop"
            stroke_on = paints_stroke and stroke != "drop"
            if not fill_on and not stroke_on:
                return [([], Operator("n"))]
            if fill_on and stroke_on:
                new_op = op
            elif fill_on:
                new_op = "f*" if "*" in op else "f"
            else:
                new_op = "s" if op in ("b", "b*", "s") else "S"
            pre = (_color_ops(plate, "f", fill) if fill_on else []) + (_color_ops(plate, "s", stroke) if stroke_on else [])
            if not pre and new_op == op:
                return None
            if not pre:
                return [(operands, Operator(new_op))]
            return [("__wrap__", [([], Operator("q"))] + pre, [([], Operator("Q"))]), (operands, Operator(new_op))]
        if op in TEXT_SHOW:
            uses_fill = state.render in (0, 2, 4, 6)
            uses_stroke = state.render in (1, 2, 5, 6)
            fill_on = uses_fill and fill != "drop"
            stroke_on = uses_stroke and stroke != "drop"
            if not fill_on and not stroke_on:
                return [([3], Operator("Tr")), (operands, Operator(op)), ([state.render], Operator("Tr"))]
            pre = (_color_ops(plate, "f", fill) if fill_on else []) + (_color_ops(plate, "s", stroke) if stroke_on else [])
            render = state.render
            if fill_on != uses_fill or stroke_on != uses_stroke:
                render = 0 if fill_on else 1
            restore = list(state.fill_raw) + list(state.stroke_raw)
            return ([([render], Operator("Tr"))] if render != state.render else []) + pre + [(operands, Operator(op))] + \
                (restore if pre else []) + ([([state.render], Operator("Tr"))] if render != state.render else [])
        if op == "Do" and resources is not None:
            xobj = resources.get("/XObject", pikepdf.Dictionary()).get(operands[0])
            if xobj is None or xobj.get("/Subtype") != "/Image":
                return None
            if xobj.get("/ImageMask"):
                if fill == "drop":
                    return []
                pre = _color_ops(plate, "f", fill)
                return [([], Operator("q"))] + pre + [(operands, Operator("Do")), ([], Operator("Q"))] if pre else None
            spot_image = bool(_xobject_spaces(xobj))
            if plate == "process":
                if not spot_image:
                    return None
                action = "drop" if state.op_fill else "white"
            else:
                action = "drop" if state.op_fill or spot_image else "white"
            if action == "drop":
                return []
            return [([], Operator("q"))] + _color_ops(plate, "f", "white") + \
                [([0, 0, 1, 1], Operator("re")), ([], Operator("f")), ([], Operator("Q"))]
        if op == "sh" and resources is not None:
            shading = resources.get("/Shading", pikepdf.Dictionary()).get(operands[0])
            spot = shading is not None and bool(_names(shading.get("/ColorSpace")))
            return None if plate == "process" and not spot else []
        if op == "BI":
            return None if plate == "process" else []
        return None
    return handler


def _fix(instructions):
    """Place '__wrap__' prefixes at the start of their path and suffixes after its painting operator."""
    result, start, post = [], None, None
    for item in instructions:
        if isinstance(item[0], str):
            _, pre, post = item
            at = start if start is not None else len(result)
            result[at:at] = pre
            continue
        operands, operator = item
        name = str(operator)
        if name in PATH_START and start is None:
            start = len(result)
        result.append((operands, operator))
        if name in PATH_PAINT:
            if post:
                result.extend(post)
                post = None
            start = None
    return result


def _plate_pdf(data: bytes, page_index: int, plate: str) -> bytes:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        keep = pdf.pages[page_index]
        for index in reversed(range(len(pdf.pages))):
            if index != page_index:
                del pdf.pages[index]
        handler = _plate_handler(plate)
        walker = Walker(lambda op, operands, state, res: handler(0, op, operands, state, res))

        def form(xobj, state):
            xobj.write(pikepdf.unparse_content_stream(_fix(walker.walk(pikepdf.parse_content_stream(xobj), xobj.get("/Resources"), state))))
        walker.form = form
        page = pdf.pages[0]
        result = walker.walk(pikepdf.parse_content_stream(page), page.obj.get("/Resources"), State())
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(_fix(result)))
        out = io.BytesIO()
        pdf.save(out)
        del keep
        return out.getvalue()


def sample(data: bytes, page_number: int, x_mm: float, y_mm: float, radius_mm: float = 0.25) -> dict:
    """Ink per plate (0-100 %) at a point given in mm from the top-left of the displayed page."""
    with fitz.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_number <= len(doc):
            raise ValueError("Nieprawidlowy numer strony.")
        rect = doc[page_number - 1].rect
    pt = 72 / 25.4
    x, y, r = x_mm * pt, y_mm * pt, max(radius_mm * pt, 0.3)
    if not (0 <= x <= rect.width and 0 <= y <= rect.height):
        raise ValueError("Punkt poza strona.")
    clip = fitz.Rect(x - r, y - r, x + r, y + r) & rect
    with pikepdf.open(io.BytesIO(data)) as pdf:
        usage = colors.spot_usage(pdf)
    spots = sorted(name for name, item in usage.items() if page_number in item.pages and name not in ("All", "None"))
    plates = {}

    def average(pix, channel):
        total = count = 0
        for yy in range(pix.height):
            for xx in range(pix.width):
                total += pix.pixel(xx, yy)[channel]
                count += 1
        return total / count if count else 0

    with fitz.open(stream=_plate_pdf(data, page_number - 1, "process"), filetype="pdf") as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip, colorspace=fitz.csCMYK, alpha=False)
        for index, name in enumerate(("C", "M", "Y", "K")):
            plates[name] = round(average(pix, index) / 255 * 100, 1)
    for name in spots:
        with fitz.open(stream=_plate_pdf(data, page_number - 1, name), filetype="pdf") as doc:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip, colorspace=fitz.csGRAY, alpha=False)
            plates[name] = round((255 - average(pix, 0)) / 255 * 100, 1)
    return {"x_mm": round(x_mm, 2), "y_mm": round(y_mm, 2), "plates": plates,
            "spots": spots, "total": round(sum(plates.values()), 1), "limitations": LIMITS}
