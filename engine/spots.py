"""Print the spot colour usage report of a PDF as JSON (run as an isolated child process)."""
import json
import sys

import colors

if __name__ == '__main__':
    # MuPDF may print warnings to stdout; the JSON result must be the last line.
    import fitz
    fitz.TOOLS.mupdf_display_errors(False)
    fitz.TOOLS.mupdf_display_warnings(False)
    print(json.dumps({'spots': colors.report(sys.argv[1])}, ensure_ascii=True))
