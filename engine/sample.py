"""Print ink per separation at a point as JSON (run as an isolated child process)."""
import json
import sys
from pathlib import Path

import separations

if __name__ == '__main__':
    try:
        path, page, x, y = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
        print(json.dumps(separations.sample(Path(path).read_bytes(), page, x, y), ensure_ascii=True))
    except Exception as error:
        print(json.dumps({'error': str(error)[:300]}, ensure_ascii=True), file=sys.stderr)
        sys.exit(1)
