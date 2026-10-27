"""Colour operations on explicit command: spot alternate values, spot <-> process, white overprint fix.

Content streams are rewritten at operator level (pikepdf); vectors, text and fonts stay as they are.
Images and shadings are not recoloured; they are counted and reported when they use the colour.
"""

from __future__ import annotations

import pikepdf
from pikepdf import Name, Operator

import colors
from colors import FILL_OPS, STROKE_OPS, TEXT_SHOW, _all_spaces, _gs_name, _rewrite_with_paths, spot_usage


def _cmyk(text: str) -> list[float]:
    parts = [float(v.strip().replace(",", ".")) for v in str(text).replace(";", ",").split(",") if v.strip()]
    if len(parts) != 4 or not all(0 <= v <= 100 for v in parts):
        raise ValueError("Podaj CMYK jako cztery wartosci 0-100, np. 0,30,100,0.")
    return [round(v / 100, 4) for v in parts]


def _separations(pdf, spot):
    return [s for s in _all_spaces(pdf) if str(s[0]) == "/Separation" and str(s[1])[1:] == spot]


def set_spot_alternate(src, out, spot: str, cmyk_text: str) -> int:
    """Change the alternate CMYK of a spot (screen and proof appearance); the separation itself is unchanged."""
    values = _cmyk(cmyk_text)
    with pikepdf.open(src) as pdf:
        spaces = _separations(pdf, spot)
        if not spaces:
            raise ValueError(f"Nie znaleziono koloru spot {spot}.")
        for space in spaces:
            space[2] = Name.DeviceCMYK
            space[3] = pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=values, N=1)
        pdf.save(out)
    return len(spaces)


def _tint_to_cmyk(space):
    """CMYK for a tint of a Separation with a DeviceCMYK alternate and an exponential (type 2) function."""
    function = space[3]
    if str(space[2]) != "/DeviceCMYK" or not isinstance(function, pikepdf.Dictionary) or int(function.get("/FunctionType", 0)) != 2:
        raise ValueError("Zamiana na CMYK wymaga koloru spot z alternatywa DeviceCMYK i funkcja typu 2. "
                         "Najpierw ustaw skladowe CMYK tego koloru.")
    c0 = [float(v) for v in function.get("/C0", [0, 0, 0, 0])]
    c1 = [float(v) for v in function.get("/C1", [1, 1, 1, 1])]
    exponent = float(function.get("/N", 1))
    return lambda t: [round(a + (b - a) * (max(0.0, min(1.0, t)) ** exponent), 4) for a, b in zip(c0, c1)]


def spot_to_cmyk(src, out, spot: str) -> int:
    """Paint objects in a spot with its CMYK equivalent (explicit colour conversion of that spot only)."""
    with pikepdf.open(src) as pdf:
        spaces = _separations(pdf, spot)
        if not spaces:
            raise ValueError(f"Nie znaleziono koloru spot {spot}.")
        convert = _tint_to_cmyk(spaces[0])
        count = 0

        def handler(page, op, operands, state, resources):
            nonlocal count
            fill = op in ("cs", "sc", "scn") and state.fill == (spot,)
            stroke = op in ("CS", "SC", "SCN") and state.stroke == (spot,)
            if not (fill or stroke):
                return None
            count += 1
            values = state.fill_vals if fill else state.stroke_vals
            tint = values[0] if values else 1.0
            # cs selects the space with tint 1.0; sc/scn set the tint.
            return [(convert(1.0 if op in ("cs", "CS") else tint), Operator("k" if fill else "K"))]

        _rewrite_with_paths(pdf, handler)
        if not count:
            raise ValueError(f"Kolor {spot} nie jest uzyty jako kolor wypelnienia ani obrysu (obrazy i cieniowania nie sa przeliczane).")
        pdf.save(out)
    return count


def cmyk_to_spot(src, out, cmyk_text: str, spot: str, tolerance: float = 0.5, target: str = "both") -> int:
    """Paint objects of a given CMYK value (within tolerance, in %) with a new or existing spot at 100 %."""
    values = _cmyk(cmyk_text)
    spot = spot.strip()
    if not spot or spot in colors.NOT_SPOT:
        raise ValueError("Podaj nazwe koloru dodatkowego.")
    if target not in ("fill", "stroke", "both"):
        raise ValueError("Nieprawidlowy zakres.")
    limit = max(float(tolerance), 0.0) / 100 + 1e-6
    with pikepdf.open(src) as pdf:
        existing = _separations(pdf, spot)
        space = existing[0] if existing else pdf.make_indirect(pikepdf.Array([
            Name.Separation, Name("/" + spot), Name.DeviceCMYK,
            pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[0, 0, 0, 0], C1=values, N=1)]))
        key = Name("/MADIspot_" + "".join(ch if ch.isalnum() else "_" for ch in spot))
        count = 0

        def handler(page, op, operands, state, resources):
            nonlocal count
            if op not in ("k", "K") or resources is None:
                return None
            if (op == "k" and target == "stroke") or (op == "K" and target == "fill"):
                return None
            current = [float(v) for v in operands]
            if len(current) != 4 or any(abs(a - b) > limit for a, b in zip(current, values)):
                return None
            if "/ColorSpace" not in resources:
                resources.ColorSpace = pikepdf.Dictionary()
            resources.ColorSpace[key] = space
            count += 1
            return [([key], Operator("cs" if op == "k" else "CS")), ([1], Operator("scn" if op == "k" else "SCN"))]

        _rewrite_with_paths(pdf, handler)
        if not count:
            raise ValueError("Nie znaleziono obiektow w podanym kolorze CMYK (sprawdz tolerancje).")
        pdf.save(out)
    return count


def remove_white_overprint(src, out) -> int:
    """Switch overprint off for white fills and strokes that have it on (they would disappear in print)."""
    count = 0
    with pikepdf.open(src) as pdf:
        from preflight_report import _white

        def handler(page, op, operands, state, resources):
            nonlocal count
            fills = op in FILL_OPS or (op in TEXT_SHOW and state.render in (0, 2, 4, 6))
            strokes = op in STROKE_OPS or (op in TEXT_SHOW and state.render in (1, 2, 5, 6))
            bad_fill = fills and state.op_fill and _white(state.fill_kind, state.fill_vals)
            bad_stroke = strokes and state.op_stroke and _white(state.stroke_kind, state.stroke_vals)
            if not (bad_fill or bad_stroke) or resources is None:
                return None
            count += 1
            off = _gs_name(pdf, resources, state.op_fill and not bad_fill, state.op_stroke and not bad_stroke)
            back = _gs_name(pdf, resources, state.op_fill, state.op_stroke)
            if op in TEXT_SHOW:
                return [([off], Operator("gs")), (operands, Operator(op)), ([back], Operator("gs"))]
            return [("__path__", off, back), (operands, Operator(op))]

        _rewrite_with_paths(pdf, handler)
        if not count:
            raise ValueError("Brak bialych obiektow z nadrukiem.")
        pdf.save(out)
    return count
