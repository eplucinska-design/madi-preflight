"""Page operations on whole PDFs (pikepdf): delete pages, insert blank pages, merge, split.

Pages are copied as objects, so content, fonts, spot colours, overprint and page boxes are unchanged.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pikepdf

MM = 72 / 25.4
MAX_PAGES = 2000


def parse_range(spec: str, count: int) -> list[int]:
    """'1,3-5' -> zero-based indices in order given; empty -> all pages."""
    spec = (spec or "").strip()
    if not spec:
        return list(range(count))
    result = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        bounds = [int(v) for v in part.split("-")]
        if len(bounds) == 1:
            bounds *= 2
        start, end = bounds
        if not (1 <= start <= count and 1 <= end <= count):
            raise ValueError(f"Strony {part} wykraczaja poza dokument ({count} str.).")
        step = 1 if end >= start else -1
        result.extend(range(start - 1, end - 1 + step, step))
    if len(result) > MAX_PAGES:
        raise ValueError(f"Limit wynosi {MAX_PAGES} stron.")
    return result


def delete_pages(src, out, spec: str) -> int:
    with pikepdf.open(src) as pdf:
        remove = set(parse_range(spec, len(pdf.pages))) if spec.strip() else set()
        if not remove:
            raise ValueError("Podaj strony do usuniecia, np. 2,5-7.")
        if len(remove) >= len(pdf.pages):
            raise ValueError("Nie mozna usunac wszystkich stron dokumentu.")
        for index in sorted(remove, reverse=True):
            del pdf.pages[index]
        pdf.save(out)
    return len(remove)


def insert_blank(src, out, position: str, page: int, count: int, width_mm: float = 0, height_mm: float = 0) -> int:
    """Insert `count` blank pages before/after page `page` (1-based), sized like that page unless a size is given."""
    if not 1 <= count <= 500:
        raise ValueError("Liczba pustych stron: 1-500.")
    if position not in ("before", "after"):
        raise ValueError("Nieprawidlowe miejsce wstawienia.")
    with pikepdf.open(src) as pdf:
        total = len(pdf.pages)
        if not 1 <= page <= total:
            raise ValueError(f"Strona {page} nie istnieje ({total} str.).")
        reference = pdf.pages[page - 1]
        if width_mm > 0 and height_mm > 0:
            media = [0, 0, round(width_mm * MM, 4), round(height_mm * MM, 4)]
            boxes = {}
        else:
            media = list(reference.obj.get("/MediaBox", reference.mediabox))
            boxes = {key: reference.obj[key] for key in ("/CropBox", "/BleedBox", "/TrimBox", "/ArtBox") if key in reference.obj}
        at = page - 1 if position == "before" else page
        for _ in range(count):
            blank = pikepdf.Dictionary(Type=pikepdf.Name.Page, MediaBox=pikepdf.Array(media), Resources=pikepdf.Dictionary(),
                                       Contents=pdf.make_stream(b""))
            for key, value in boxes.items():
                blank[key] = value
            if "/Rotate" in reference.obj and not (width_mm > 0 and height_mm > 0):
                blank.Rotate = reference.obj.Rotate
            pdf.pages.insert(at, pikepdf.Page(blank))
            at += 1
        pdf.save(out)
    return count


def merge(out, inputs: list[str]) -> int:
    if len(inputs) < 2:
        raise ValueError("Do laczenia potrzebne sa co najmniej dwa dokumenty.")
    with pikepdf.new() as result:
        for path in inputs:
            with pikepdf.open(path) as part:
                if len(result.pages) + len(part.pages) > MAX_PAGES:
                    raise ValueError(f"Limit wynosi {MAX_PAGES} stron.")
                result.pages.extend(part.pages)
        result.save(out)
        return len(result.pages)


def split(src, out_zip, mode: str, value: str, stem: str) -> int:
    """Split into parts every N pages ('every') or by ranges ('ranges', e.g. '1-2;3-6'); writes a ZIP."""
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (stem or "dokument"))[:80] or "dokument"
    with pikepdf.open(src) as pdf:
        total = len(pdf.pages)
        if mode == "every":
            size = int(value or 0)
            if not 1 <= size <= total:
                raise ValueError(f"Podaj liczbe stron czesci od 1 do {total}.")
            groups = [list(range(start, min(start + size, total))) for start in range(0, total, size)]
        elif mode == "ranges":
            groups = [parse_range(part, total) for part in str(value).split(";") if part.strip()]
            if not groups:
                raise ValueError("Podaj zakresy czesci, np. 1-2;3-6.")
        else:
            raise ValueError("Nieznany sposob dzielenia.")
        if len(groups) > 500:
            raise ValueError("Limit wynosi 500 czesci.")
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for number, indices in enumerate(groups, 1):
                with pikepdf.new() as part:
                    for index in indices:
                        part.pages.append(pdf.pages[index])
                    part_path = Path(out_zip).with_name(f"part-{number}.pdf")
                    part.save(part_path)
                label = f"{indices[0] + 1}-{indices[-1] + 1}" if len(indices) > 1 else f"{indices[0] + 1}"
                archive.write(part_path, f"{stem}_{number:02d}_str{label}.pdf")
                part_path.unlink()
    return len(groups)


if __name__ == "__main__":
    try:
        command = sys.argv[1]
        if command == "merge":
            print(json.dumps({"pages": merge(sys.argv[2], sys.argv[3:])}))
        elif command == "split":
            print(json.dumps({"parts": split(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])}))
        else:
            raise ValueError("Nieznane polecenie.")
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=True), file=sys.stderr)
        sys.exit(1)
