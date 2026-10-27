"""Spot colour operations on PDF content streams (pikepdf), without rasterising or converting colours.

Colours are tracked per graphics state while walking content streams, so every
painting operator is attributed to the fill/stroke colour space actually active
for it. Form XObjects are processed recursively; shared forms are edited once.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pikepdf
from pikepdf import Name, Operator

FILL_OPS = {"f", "F", "f*", "B", "B*", "b", "b*"}
STROKE_OPS = {"S", "s", "B", "B*", "b", "b*"}
PATH_PAINT = FILL_OPS | STROKE_OPS | {"n"}
PATH_START = {"m", "re"}
TEXT_SHOW = {"Tj", "TJ", "'", '"'}


def _names(space) -> list[str]:
    """Colourant names of a Separation or DeviceN colour space, else []."""
    try:
        if isinstance(space, pikepdf.Array) and len(space) >= 2:
            kind = str(space[0])
            if kind == "/Separation":
                return [str(space[1])[1:]]
            if kind == "/DeviceN":
                return [str(n)[1:] for n in space[1]]
            if kind in ("/Indexed", "/Pattern") and len(space) > 1:
                return _names(space[1])
    except (TypeError, ValueError):
        pass
    return []


def _resolve_space(resources, value) -> list[str]:
    if isinstance(value, Name):
        spaces = resources.get("/ColorSpace") if resources is not None else None
        if spaces is not None and value in spaces:
            return _names(spaces[value])
        return []
    return _names(value)


def _numbers(operands) -> tuple:
    values = []
    for value in operands:
        if isinstance(value, Name):
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            pass
    return tuple(values)


def _kind(resources, value) -> str:
    """Colour space family of a cs/CS operand."""
    names = {"/DeviceCMYK": "cmyk", "/DeviceRGB": "rgb", "/DeviceGray": "gray", "/Pattern": "other"}
    if str(value) in names:
        return names[str(value)]
    space = None
    spaces = resources.get("/ColorSpace") if resources is not None else None
    if spaces is not None and value in spaces:
        space = spaces[value]
    if space is None:
        return "other"
    if _names(space):
        return "spot"
    if isinstance(space, Name):
        return names.get(str(space), "other")
    if isinstance(space, pikepdf.Array) and str(space[0]) == "/ICCBased":
        return {4: "cmyk", 3: "rgb", 1: "gray"}.get(int(space[1].get("/N", 0)), "other")
    return "other"


@dataclass
class State:
    fill: tuple[str, ...] = ()
    stroke: tuple[str, ...] = ()
    op_fill: bool = False
    op_stroke: bool = False
    render: int = 0
    # Colour values and kind ("cmyk", "rgb", "gray", "spot", "other") plus the raw
    # operators that set them, used by the separation sampler to restore colours.
    fill_kind: str = "gray"
    stroke_kind: str = "gray"
    fill_vals: tuple = (0.0,)
    stroke_vals: tuple = (0.0,)
    fill_raw: tuple = ()
    stroke_raw: tuple = ()


@dataclass
class Usage:
    pages: set[int] = field(default_factory=set)
    fill: int = 0
    stroke: int = 0
    text: int = 0
    images: int = 0
    overprint_fill: int = 0
    overprint_stroke: int = 0
    alternate: str = ""


def _gs_overprint(resources, name, state: State) -> State:
    states = resources.get("/ExtGState") if resources is not None else None
    if states is None or name not in states:
        return state
    gs = states[name]
    op_stroke = bool(gs["/OP"]) if "/OP" in gs else state.op_stroke
    if "/op" in gs:
        op_fill = bool(gs["/op"])
    elif "/OP" in gs:
        op_fill = bool(gs["/OP"])
    else:
        op_fill = state.op_fill
    return replace(state, op_fill=op_fill, op_stroke=op_stroke)


def _xobject_spaces(xobj) -> list[str]:
    if xobj.get("/ImageMask"):
        return []
    return _names(xobj.get("/ColorSpace"))


class Walker:
    """Walks a content stream, calling handler(op, operands, state, resources) per operator."""

    def __init__(self, handler, visit_forms=True):
        self.handler = handler
        self.visit_forms = visit_forms
        self.seen: set = set()

    def walk(self, instructions, resources, state: State):
        stack: list[State] = []
        out = []
        for operands, operator in instructions:
            op = str(operator)
            if op == "q":
                stack.append(state)
            elif op == "Q" and stack:
                state = stack.pop()
            elif op == "cs":
                state = replace(state, fill=tuple(_resolve_space(resources, operands[0])), fill_kind=_kind(resources, operands[0]),
                                fill_raw=((list(operands), operator),), fill_vals=(1.0,))
            elif op == "CS":
                state = replace(state, stroke=tuple(_resolve_space(resources, operands[0])), stroke_kind=_kind(resources, operands[0]),
                                stroke_raw=((list(operands), operator),), stroke_vals=(1.0,))
            elif op in ("sc", "scn"):
                state = replace(state, fill_vals=_numbers(operands), fill_raw=state.fill_raw[:1] + ((list(operands), operator),))
            elif op in ("SC", "SCN"):
                state = replace(state, stroke_vals=_numbers(operands), stroke_raw=state.stroke_raw[:1] + ((list(operands), operator),))
            elif op in ("g", "rg", "k"):
                state = replace(state, fill=(), fill_kind={"g": "gray", "rg": "rgb", "k": "cmyk"}[op],
                                fill_vals=_numbers(operands), fill_raw=((list(operands), operator),))
            elif op in ("G", "RG", "K"):
                state = replace(state, stroke=(), stroke_kind={"G": "gray", "RG": "rgb", "K": "cmyk"}[op],
                                stroke_vals=_numbers(operands), stroke_raw=((list(operands), operator),))
            elif op == "gs":
                state = _gs_overprint(resources, operands[0], state)
            elif op == "Tr":
                state = replace(state, render=int(operands[0]))
            result = self.handler(op, list(operands), state, resources)
            if op == "Do" and self.visit_forms and resources is not None:
                xobj = resources.get("/XObject", pikepdf.Dictionary()).get(operands[0])
                if xobj is not None and xobj.get("/Subtype") == "/Form" and xobj.objgen not in self.seen:
                    self.seen.add(xobj.objgen)
                    self.form(xobj, state)
            out.append(result if result is not None else [(operands, operator)])
        return [item for group in out for item in group]

    def form(self, xobj, state):
        resources = xobj.get("/Resources")
        instructions = pikepdf.parse_content_stream(xobj)
        result = self.walk(instructions, resources, state)
        if result is not None and getattr(self, "rewrite", False):
            xobj.write(pikepdf.unparse_content_stream(result))


def _pages_walk(pdf, handler, rewrite=False):
    for index, page in enumerate(pdf.pages):
        walker = Walker(lambda op, operands, state, res, i=index: handler(i, op, operands, state, res))
        walker.rewrite = rewrite
        resources = page.obj.get("/Resources")
        result = walker.walk(pikepdf.parse_content_stream(page), resources, State())
        if rewrite:
            page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(result))


def spot_usage(pdf: pikepdf.Pdf) -> dict[str, Usage]:
    """Count painting operations per spot colourant, with their effective overprint."""
    usage: dict[str, Usage] = {}

    def add(name, page) -> Usage:
        item = usage.setdefault(name, Usage())
        item.pages.add(page + 1)
        return item

    def handler(page, op, operands, state, resources):
        if op in FILL_OPS or (op in TEXT_SHOW and state.render in (0, 2, 4, 6)):
            for name in state.fill:
                item = add(name, page)
                item.fill += 1
                item.text += op in TEXT_SHOW
                item.overprint_fill += state.op_fill
        if op in STROKE_OPS or (op in TEXT_SHOW and state.render in (1, 2, 5, 6)):
            for name in state.stroke:
                item = add(name, page)
                item.stroke += 1
                item.overprint_stroke += state.op_stroke
        if op == "Do" and resources is not None:
            xobj = resources.get("/XObject", pikepdf.Dictionary()).get(operands[0])
            if xobj is not None and xobj.get("/Subtype") == "/Image":
                names = list(state.fill) if xobj.get("/ImageMask") else _xobject_spaces(xobj)
                for name in names:
                    add(name, page).images += 1
        if op == "sh" and resources is not None:
            shading = resources.get("/Shading", pikepdf.Dictionary()).get(operands[0])
            for name in _names(shading.get("/ColorSpace")) if shading is not None else []:
                add(name, page).fill += 1
        return None

    _pages_walk(pdf, handler)
    for space in _all_spaces(pdf):
        if str(space[0]) == "/Separation":
            name = str(space[1])[1:]
            usage.setdefault(name, Usage()).alternate = str(space[2])[1:] if isinstance(space[2], Name) else "inna"
    return usage


def _all_spaces(pdf):
    """Every Separation/DeviceN array reachable from resource dictionaries."""
    found, seen = [], set()

    def key(obj):
        return obj.objgen if obj.is_indirect else None

    def space(value):
        if isinstance(value, pikepdf.Array) and len(value) >= 2:
            kind = str(value[0])
            if kind in ("/Separation", "/DeviceN") and len(value) >= 3:
                k = key(value)
                if k is None or k not in seen:
                    if k:
                        seen.add(k)
                    found.append(value)
            elif kind in ("/Indexed", "/Pattern"):
                space(value[1])

    def resources(res):
        if not isinstance(res, pikepdf.Dictionary):
            return
        k = key(res)
        if k:
            if ("r", k) in seen:
                return
            seen.add(("r", k))
        for value in res.get("/ColorSpace", pikepdf.Dictionary()).values():
            space(value)
        for group in ("/Shading", "/Pattern", "/XObject"):
            for obj in res.get(group, pikepdf.Dictionary()).values():
                if "/ColorSpace" in obj:
                    space(obj["/ColorSpace"])
                if "/Shading" in obj and "/ColorSpace" in obj["/Shading"]:
                    space(obj["/Shading"]["/ColorSpace"])
                if "/Resources" in obj:
                    resources(obj["/Resources"])

    for page in pdf.pages:
        resources(page.obj.get("/Resources"))
    return found


def report(path: str | Path) -> list[dict]:
    with pikepdf.open(path) as pdf:
        usage = spot_usage(pdf)
    return [
        {"name": name, "pages": sorted(item.pages), "fill": item.fill, "stroke": item.stroke, "text": item.text,
         "images": item.images, "overprint_fill": item.overprint_fill, "overprint_stroke": item.overprint_stroke,
         "alternate": item.alternate}
        for name, item in sorted(usage.items())
    ]


def rename_spot(src, out, old: str, new: str) -> int:
    """Rename a colourant. If `new` already exists, its alternate space and tint transform are reused,
    so the replaced colour previews and separates exactly like the existing one."""
    old, new = old.strip(), new.strip()
    if not old or not new:
        raise ValueError("Podaj nazwe obecnego i nowego koloru spot.")
    changed = 0
    with pikepdf.open(src) as pdf:
        spaces = _all_spaces(pdf)
        target = next((s for s in spaces if str(s[0]) == "/Separation" and str(s[1])[1:] == new), None)
        for space in spaces:
            if str(space[0]) == "/Separation" and str(space[1])[1:] == old:
                space[1] = Name("/" + new)
                if target is not None and target is not space:
                    space[2], space[3] = target[2], target[3]
                changed += 1
            elif str(space[0]) == "/DeviceN":
                names = [str(n)[1:] for n in space[1]]
                if old in names:
                    if new in names:
                        raise ValueError(f"Kolor {new} juz wystepuje w tym samym DeviceN; zamiana wymaga laczenia kanalow.")
                    space[1] = pikepdf.Array([Name("/" + (new if n == old else n)) for n in names])
                    changed += 1
        if not changed:
            raise ValueError(f"Nie znaleziono koloru spot {old}.")
        pdf.save(out)
    return changed


def _gs_name(pdf, resources, op_fill: bool, op_stroke: bool) -> Name:
    if "/ExtGState" not in resources:
        resources.ExtGState = pikepdf.Dictionary()
    name = Name(f"/MADIop{int(op_fill)}{int(op_stroke)}")
    if name not in resources.ExtGState:
        resources.ExtGState[name] = pdf.make_indirect(pikepdf.Dictionary(Type=Name.ExtGState, op=op_fill, OP=op_stroke, OPM=1))
    return name


def set_overprint(src, out, spot: str, target: str = "fill", value: bool = True) -> int:
    """Set fill and/or stroke overprint for objects painted in `spot`; other objects keep their state."""
    if target not in ("fill", "stroke", "both"):
        raise ValueError("Nieprawidlowy zakres nadruku.")
    count = 0
    with pikepdf.open(src) as pdf:
        def handler(page, op, operands, state, resources):
            nonlocal count
            hit_fill = target in ("fill", "both") and spot in state.fill and (op in FILL_OPS or (op in TEXT_SHOW and state.render in (0, 2, 4, 6)))
            hit_stroke = target in ("stroke", "both") and spot in state.stroke and (op in STROKE_OPS or (op in TEXT_SHOW and state.render in (1, 2, 5, 6)))
            if not (hit_fill or hit_stroke) or resources is None:
                return None
            new_fill = value if hit_fill else state.op_fill
            new_stroke = value if hit_stroke else state.op_stroke
            if (new_fill, new_stroke) == (state.op_fill, state.op_stroke):
                return None
            count += 1
            on = _gs_name(pdf, resources, new_fill, new_stroke)
            back = _gs_name(pdf, resources, state.op_fill, state.op_stroke)
            # gs is legal immediately before a painting operator only for text; for paths the
            # state is applied at path start, handled by marking and fixing up below.
            return [([on], Operator("gs")), (operands, Operator(op)), ([back], Operator("gs"))] if op in TEXT_SHOW else [("__path__", on, back), (operands, Operator(op))]

        _rewrite_with_paths(pdf, handler)
        pdf.save(out)
    if not count:
        raise ValueError(f"Brak obiektow w kolorze {spot} do zmiany nadruku (lub stan juz ustawiony).")
    return count


def _fix_paths(instructions):
    """Move overprint gs markers in front of their path construction (gs is not allowed inside a path)."""
    result, path_start, back = [], None, None
    for item in instructions:
        if isinstance(item[0], str):
            _, on, back = item
            result.insert(path_start if path_start is not None else len(result), ([on], Operator("gs")))
            continue
        operands, operator = item
        op = str(operator)
        if op in PATH_START and path_start is None:
            path_start = len(result)
        result.append((operands, operator))
        if op in PATH_PAINT:
            if back is not None:
                result.append(([back], Operator("gs")))
                back = None
            path_start = None
    return result


def _rewrite_with_paths(pdf, handler):
    for index, page in enumerate(pdf.pages):
        walker = Walker(lambda op, operands, state, res, i=index: handler(i, op, operands, state, res))
        resources = page.obj.get("/Resources")

        def form(xobj, state, walker=walker):
            res = xobj.get("/Resources")
            xobj.write(pikepdf.unparse_content_stream(_fix_paths(walker.walk(pikepdf.parse_content_stream(xobj), res, state))))
        walker.form = form
        result = walker.walk(pikepdf.parse_content_stream(page), resources, State())
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(_fix_paths(result)))


def extract_spot(src, out, spot: str, placement: str = "after") -> int:
    """Add, after each page (or as the only pages), a copy that paints only objects in `spot`.

    Other paths are ended without painting, other text is set invisible (Tr 3) so text
    positions stay exact, other images and shadings are skipped. Vectors, fonts and the
    spot colour space itself are kept unchanged.
    """
    if placement not in ("after", "only"):
        raise ValueError("Nieprawidlowe umieszczenie wydzielonego koloru.")
    with pikepdf.open(src) as pdf:
        usage = spot_usage(pdf)
        if spot not in usage or not usage[spot].pages:
            raise ValueError(f"Kolor spot {spot} nie jest uzywany w dokumencie.")
        count = len(pdf.pages)
        added = 0
        for index in reversed(range(count)):
            if index + 1 not in usage[spot].pages and placement == "after":
                continue
            pdf.pages.insert(index + 1, pdf.pages[index])
            plate = pdf.pages[index + 1]
            _filter_page(pdf, plate, spot)
            added += 1
        if placement == "only":
            for index in reversed(range(0, len(pdf.pages), 2)):
                del pdf.pages[index]
        pdf.save(out)
    return added


def _filter_page(pdf, page, spot):
    forms: dict = {}

    def handler(op, operands, state, resources):
        fill_hit = spot in state.fill
        stroke_hit = spot in state.stroke
        if op in PATH_PAINT and op != "n":
            fill = op in FILL_OPS and fill_hit
            stroke = op in STROKE_OPS and stroke_hit
            if fill and stroke:
                return None
            if fill:
                return [(operands, Operator("f*" if "*" in op else "f"))] if op not in ("f", "F", "f*") else None
            if stroke:
                return [(operands, Operator("s" if op in ("b", "b*", "s") else "S"))] if op not in ("S", "s") else None
            return [([], Operator("n"))]
        if op in TEXT_SHOW:
            visible = (fill_hit and state.render in (0, 2, 4, 6)) or (stroke_hit and state.render in (1, 2, 5, 6))
            if visible:
                return None
            return [([3], Operator("Tr")), (operands, Operator(op)), ([state.render], Operator("Tr"))]
        if op == "sh":
            shading = resources.get("/Shading", pikepdf.Dictionary()).get(operands[0]) if resources is not None else None
            return None if shading is not None and spot in _names(shading.get("/ColorSpace")) else []
        if op == "BI":
            return []
        if op == "Do" and resources is not None:
            xobj = resources.get("/XObject", pikepdf.Dictionary()).get(operands[0])
            if xobj is None:
                return []
            if xobj.get("/Subtype") == "/Image":
                spaces = list(state.fill) if xobj.get("/ImageMask") else _xobject_spaces(xobj)
                return None if spot in spaces else []
            if xobj.get("/Subtype") == "/Form":
                key = xobj.objgen
                if key not in forms:
                    clone = pdf.make_indirect(pikepdf.Stream(pdf, xobj.read_bytes()))
                    for k, v in xobj.items():
                        if k not in ("/Length", "/Filter", "/DecodeParms"):
                            clone[k] = v
                    clone.write(pikepdf.unparse_content_stream(walker.walk(pikepdf.parse_content_stream(xobj), xobj.get("/Resources"), state)))
                    forms[key] = clone
                name = Name(f"/MADIplate{len(forms)}_{key[0]}")
                resources.XObject[name] = forms[key]
                return [([name], Operator("Do"))]
        return None

    walker = Walker(handler, visit_forms=False)
    # The plate shares resources with the original page; give it its own copies to edit.
    page.obj.Resources = pikepdf.Dictionary({k: (pikepdf.Dictionary(v) if isinstance(v, pikepdf.Dictionary) else v) for k, v in page.obj.get("/Resources", pikepdf.Dictionary()).items()})
    if "/XObject" not in page.obj.Resources:
        page.obj.Resources.XObject = pikepdf.Dictionary()
    result = walker.walk(pikepdf.parse_content_stream(page), page.obj.Resources, State())
    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(result))
