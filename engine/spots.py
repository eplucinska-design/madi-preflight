"""Print the spot colour usage report of a PDF as JSON (run as an isolated child process)."""
import json
import sys

import colors

if __name__ == '__main__':
    print(json.dumps({'spots': colors.report(sys.argv[1])}, ensure_ascii=True))
