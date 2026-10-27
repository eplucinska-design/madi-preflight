"""Loopback-only web interface for MADI Preflight."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import base64
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse

import fitz
from inspector import inspect_page

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 40 * 1024 * 1024
PDF_LOCK = threading.Lock()


def page_boxes(page):
    """Page boxes in mm, in displayed (rotated) orientation, relative to the visible CropBox."""
    matrix = page.rotation_matrix
    origin = page.cropbox * matrix
    boxes = {}
    for name in ('mediabox', 'cropbox', 'bleedbox', 'trimbox'):
        rect = getattr(page, name) * matrix
        boxes[name] = [round((value - offset) * 25.4 / 72, 3) for value, offset in
                       zip(rect, (origin.x0, origin.y0, origin.x0, origin.y0))]
    return boxes


class Handler(BaseHTTPRequestHandler):
    def authorized(self):
        password = os.environ.get('MADI_PASSWORD')
        if not password:
            return True
        expected = 'Basic ' + base64.b64encode(('madi:' + password).encode()).decode()
        if hmac.compare_digest(self.headers.get('Authorization', ''), expected):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="MADI Preflight", charset="UTF-8"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def reply(self, status, data, kind='application/json'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authorized():
            return
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/favicon.ico': ('favicon.ico', 'image/x-icon'),
                 '/madi-preflight.png': ('madi-preflight.png', 'image/png'),
                 '/tools.js': ('tools.js', 'text/javascript; charset=utf-8'),
                 '/workspace.js': ('workspace.js', 'text/javascript; charset=utf-8'),
                 '/workspace.css': ('workspace.css', 'text/css; charset=utf-8'),
                 '/ergonomics.css': ('ergonomics.css', 'text/css; charset=utf-8'),
                 '/usability.js': ('usability.js', 'text/javascript; charset=utf-8'),
                 '/layout.js': ('layout.js', 'text/javascript; charset=utf-8'),
                 '/ux.js': ('ux.js', 'text/javascript; charset=utf-8'),
                 '/colors.js': ('colors.js', 'text/javascript; charset=utf-8'),
                 '/measure.js': ('measure.js', 'text/javascript; charset=utf-8'),
                 '/clickup.css': ('clickup.css', 'text/css; charset=utf-8'),
                 '/fonts/jost-latin-600-normal.woff2': ('fonts/jost-latin-600-normal.woff2', 'font/woff2'),
                 '/fonts/jost-latin-ext-600-normal.woff2': ('fonts/jost-latin-ext-600-normal.woff2', 'font/woff2'),
                 '/fonts/Jost-OFL.txt': ('fonts/Jost-OFL.txt', 'text/plain; charset=utf-8'),
                 '/vendor/lucide.min.js': ('vendor/lucide.min.js', 'text/javascript; charset=utf-8'),
                 '/vendor/lucide-LICENSE': ('vendor/lucide-LICENSE', 'text/plain; charset=utf-8')}
        path = urllib.parse.urlsplit(self.path).path
        if path not in files:
            return self.reply(404, {'error': 'Nie znaleziono.'})
        name, kind = files[path]
        self.reply(200, (ROOT / 'static' / name).read_bytes(), kind)

    def do_POST(self):
        if not self.authorized():
            return
        try:
            origin = self.headers.get('Origin')
            parsed_origin = urllib.parse.urlsplit(origin or '')
            if origin and (parsed_origin.scheme not in {'http', 'https'} or parsed_origin.netloc != self.headers.get('Host', '')):
                return self.reply(403, {'error': 'Niedozwolone zrodlo zapytania.'})
            if self.path not in {'/api/analyze', '/api/process', '/api/inspect', '/api/spots'}:
                return self.reply(404, {'error': 'Nie znaleziono.'})
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= MAX_UPLOAD:
                return self.reply(413, {'error': 'Limit zadania wynosi 40 MB.'})
            content_type = self.headers.get('Content-Type', '')
            if not content_type.startswith('multipart/form-data;'):
                raise ValueError('Wymagany formularz z plikiem PDF.')
            message = BytesParser(policy=default).parsebytes(
                ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + self.rfile.read(size))
            fields, pdf = {}, None
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                data = part.get_payload(decode=True)
                if name == 'file':
                    pdf = data
                elif name and data is not None:
                    fields[name] = data.decode('utf-8')
            if not pdf or not pdf.startswith(b'%PDF-'):
                raise ValueError('Wybierz poprawny plik PDF.')
            if self.path == '/api/spots':
                # Content-stream walk runs in the isolated, time-limited child like other operations.
                with tempfile.TemporaryDirectory(prefix='madi-web-') as work:
                    src = Path(work) / 'input.pdf'
                    src.write_bytes(pdf)
                    completed = subprocess.run([sys.executable, str(ROOT / 'engine' / 'spots.py'), str(src)],
                        capture_output=True, timeout=60)
                if completed.returncode:
                    raise ValueError('Nie udalo sie odczytac kolorow spot.')
                return self.reply(200, json.loads(completed.stdout))
            if self.path in {'/api/analyze', '/api/inspect'}:
                with PDF_LOCK, fitz.open(stream=pdf, filetype='pdf') as doc:
                    if doc.needs_pass:
                        raise ValueError('PDF jest chroniony haslem.')
                    page_number = int(fields.get('page', '1'))
                    if not 1 <= page_number <= len(doc):
                        raise ValueError('Nieprawidlowy numer strony.')
                    if self.path == '/api/inspect':
                        return self.reply(200, inspect_page(pdf, doc, page_number))
                    page = doc[page_number - 1]
                    max_pixels = 180 if fields.get('thumbnail') == 'true' else 1600
                    scale = max_pixels / max(page.rect.width, page.rect.height)
                    image = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
                    result = {'pages': len(doc), 'width': round(page.rect.width * 25.4 / 72, 2),
                        'height': round(page.rect.height * 25.4 / 72, 2), 'preview': base64.b64encode(image).decode('ascii'),
                        'boxes': page_boxes(page)}
                return self.reply(200, result)
            steps = json.loads(fields['workflow']) if 'workflow' in fields else [fields]
            if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
                raise ValueError('Kolejka musi zawierac od 1 do 12 operacji.')
            with tempfile.TemporaryDirectory(prefix='madi-web-') as work:
                src, out = Path(work) / 'input.pdf', Path(work) / 'output.pdf'
                src.write_bytes(pdf)
                command = [sys.executable, str(ROOT / 'engine' / 'workflow.py'), str(src), str(out)]
                completed = subprocess.run(command, input=json.dumps(steps).encode(), capture_output=True, timeout=90)
                if completed.returncode or not out.exists():
                    lines = completed.stderr.decode('utf-8', errors='replace').splitlines()
                    try:
                        error = json.loads(lines[-1])['error']
                    except (ValueError, KeyError, IndexError):
                        error = 'Silnik nie przetworzyl pliku. Sprawdz parametry operacji.'
                    raise ValueError(error)
                result = out.read_bytes()
            self.reply(200, result, 'application/pdf')
        except subprocess.TimeoutExpired:
            self.reply(422, {'error': 'Przekroczono czas kolejki (90 s).'})
        except (ValueError, RuntimeError, OverflowError) as error:
            self.reply(422, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': 'Nie udalo sie odczytac lub przetworzyc PDF.'})


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    bind = os.environ.get('MADI_BIND', '127.0.0.1')
    if bind not in {'127.0.0.1', 'localhost', '::1'} and not os.environ.get('MADI_PASSWORD'):
        raise RuntimeError('MADI_PASSWORD is required for network access.')
    server = ThreadingHTTPServer((bind, port), Handler)
    print(f'MADI Preflight: http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
