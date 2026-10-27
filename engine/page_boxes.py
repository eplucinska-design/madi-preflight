"""Set PDF page boxes (as Acrobat "Set Page Boxes"): one box from a reference box and margins.

Only box entries change; page content, resources and colours are untouched. Margins are given for
the page as displayed (top/bottom/left/right after /Rotate) in millimetres; negative values extend
the box. Boxes are kept inside the MediaBox, as the PDF specification requires.
"""

from __future__ import annotations

import pikepdf

MM = 72 / 25.4
BOXES = {"trim": "/TrimBox", "bleed": "/BleedBox", "crop": "/CropBox", "media": "/MediaBox", "art": "/ArtBox"}


def _box(page, key):
    """Effective box with PDF inheritance defaults (Crop -> Media, Trim/Bleed/Art -> Crop)."""
    media = [float(v) for v in page.obj.get("/MediaBox", page.mediabox)]
    if key == "/MediaBox":
        return media
    crop = [float(v) for v in page.obj.get("/CropBox", media)]
    if key == "/CropBox":
        return crop
    return [float(v) for v in page.obj.get(key, crop)]


def _pages(spec: str, count: int) -> list[int]:
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("all", "wszystkie"):
        return list(range(count))
    result = []
    for part in spec.split(","):
        bounds = [int(v) for v in part.strip().split("-")]
        if len(bounds) == 1:
            bounds *= 2
        start, end = bounds
        if not 1 <= start <= count or not 1 <= end <= count or start > end:
            raise ValueError("Zakres stron wykracza poza dokument.")
        result.extend(range(start - 1, end))
    return sorted(set(result))


def set_box(src, out, target: str, reference: str, top: float, bottom: float, left: float, right: float,
            pages: str = "") -> int:
    if target not in BOXES or reference not in BOXES:
        raise ValueError("Nieznane pole strony.")
    with pikepdf.open(src) as pdf:
        indices = _pages(pages, len(pdf.pages))
        for index in indices:
            page = pdf.pages[index]
            llx, lly, urx, ury = _box(page, BOXES[reference])
            llx, urx = min(llx, urx), max(llx, urx)
            lly, ury = min(lly, ury), max(lly, ury)
            # Displayed sides -> sides of the unrotated PDF box.
            rotate = int(page.obj.get("/Rotate", 0)) % 360
            t, b, l, r = (v * MM for v in (top, bottom, left, right))
            side = {0: (t, b, l, r), 90: (r, l, t, b), 180: (b, t, r, l), 270: (l, r, b, t)}[rotate]
            pdf_top, pdf_bottom, pdf_left, pdf_right = side
            box = [llx + pdf_left, lly + pdf_bottom, urx - pdf_right, ury - pdf_top]
            if target != "media":
                media = _box(page, "/MediaBox")
                box = [max(box[0], min(media[0], media[2])), max(box[1], min(media[1], media[3])),
                       min(box[2], max(media[0], media[2])), min(box[3], max(media[1], media[3]))]
            if box[2] - box[0] < 1 or box[3] - box[1] < 1:
                raise ValueError(f"Marginesy sa wieksze niz pole odniesienia na stronie {index + 1}.")
            page.obj[BOXES[target]] = pikepdf.Array([round(v, 4) for v in box])
        pdf.save(out)
    return len(indices)
