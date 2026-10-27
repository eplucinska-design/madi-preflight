"""Imposition check as JSON (isolated child process): argv[1] = PDF path, stdin = imposition step (JSON)."""
import json
import sys
from types import SimpleNamespace

import fitz

from imposer import check_imposition, settings_from_args

if __name__ == "__main__":
    fitz.TOOLS.mupdf_display_errors(False)
    try:
        step = json.load(sys.stdin)
        args = SimpleNamespace(**{k: v for k, v in step.items() if k.startswith("imposer_")})
        with fitz.open(sys.argv[1]) as src:
            print(json.dumps(check_imposition(src, settings_from_args(args)), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=True), file=sys.stderr)
        sys.exit(1)