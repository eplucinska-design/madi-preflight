"""Local browser interface for the preserved MADI PDF engine."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import base64
import json
import subprocess
import sys
import tempfile
import threading
import urllib.parse

import fitz

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 40 * 1024 * 1024
PDF_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
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
        path = urllib.parse.urlsplit(self.path).path
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if path not in files:
            return self.reply(404, {'error': 'Nie znaleziono.'})
        name, kind = files[path]
        self.reply(200, (ROOT / 'static' / name).read_bytes(), kind)

    def do_POST(self):
        try:
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self.reply(403, {'error': 'Niedozwolone zrodlo zapytania.'})
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= MAX_UPLOAD:
                return self.reply(413, {'error': 'Limit pliku wynosi 40 MB.'})
            content_type = self.headers.get('Content-Type', '')
            if not content_type.startswith('multipart/form-data;'):
                raise ValueError('Wymagany formularz z plikiem PDF.')
            message = BytesParser(policy=default).parsebytes(
                ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode()
                + self.rfile.read(size))
            fields = {}
            pdf = None
            for part in message.iter_parts():
                name = part.get_param('name', header='content-disposition')
                data = part.get_payload(decode=True)
                if name == 'file':
                    pdf = data
                elif name:
                    fields[name] = data.decode('utf-8')
            if not pdf or not pdf.startswith(b'%PDF-'):
                raise ValueError('Wybierz poprawny plik PDF.')
            if self.path == '/api/analyze':
                with PDF_LOCK, fitz.open(stream=pdf, filetype='pdf') as doc:
                    if doc.needs_pass:
                        raise ValueError('PDF jest chroniony haslem.')
                    if not len(doc):
                        raise ValueError('PDF nie zawiera stron.')
                    page_number = int(fields.get('page', '1'))
                    if not 1 <= page_number <= len(doc):
                        raise ValueError('Nieprawidlowy numer strony.')
                    page = doc[page_number - 1]
                    scale = min(1.5, 1200 / max(page.rect.width, page.rect.height))
                    preview = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).tobytes('png')
                    result = {'pages': len(doc), 'width': round(page.rect.width * 25.4 / 72, 2),
                              'height': round(page.rect.height * 25.4 / 72, 2),
                              'preview': base64.b64encode(preview).decode('ascii')}
                return self.reply(200, result)
            if self.path != '/api/process':
                return self.reply(404, {'error': 'Nie znaleziono.'})
            mode = fields.get('mode', 'bleed')
            if mode not in {'bleed', 'black', 'white', 'matrix', 'shape', 'dieline', 'remove', 'imposer'}:
                raise ValueError('Nieznana operacja.')
            bleed = float(fields.get('bleed', '3').replace(',', '.'))
            if not 0 <= bleed <= 20:
                raise ValueError('Spad musi miescic sie w zakresie 0-20 mm.')
            spot = fields.get('spot', 'HS').strip()
            if mode == 'matrix' and not spot:
                raise ValueError('Podaj nazwe koloru dodatkowego.')
            with tempfile.TemporaryDirectory(prefix='madi-web-') as work:
                src, out = Path(work) / 'input.pdf', Path(work) / 'output.pdf'
                src.write_bytes(pdf)
                args = [sys.executable, str(ROOT / 'engine' / 'foil_preflight.py'), str(src), str(out),
                        '--mode', 'matrix' if mode == 'remove' else 'bleed' if mode == 'imposer' else mode,
                        '--bleed-mm', str(bleed), '--bleed-mode', fields.get('bleed_mode', 'mirror')]
                if mode in {'matrix', 'remove'}:
                    args += ['--spot', spot]
                if mode == 'remove':
                    args += ['--remove-selected', '--remove-scope', fields.get('remove_scope', 'all')]
                allowed = {'page_size', 'page_width_mm', 'page_height_mm', 'scale_mode', 'shape_type',
                           'rect_width_mm', 'rect_height_mm', 'rect_radius_mm', 'circle_diameter_mm',
                           'generated_cutline_spot_name', 'white_output_spot_name', 'white_output_type',
                           'white_target_mode', 'white_input_spot', 'white_input_cmyk', 'black_output_cmyk',
                           'cut_mode', 'cut_value', 'crease_mode', 'crease_value'}
                for key in allowed:
                    if fields.get(key):
                        args += ['--' + key.replace('_', '-'), fields[key]]
                for key in {'cut_enabled', 'crease_enabled', 'preserve_original', 'white_overprint', 'scale_content'}:
                    if fields.get(key) == 'true':
                        args += ['--' + key.replace('_', '-')]
                if mode == 'imposer':
                    settings = {'imposer_sheet': fields.get('imposer_sheet', 'SRA3')}
                    for key, lo, hi, fallback in [('imposer_rows', 1, 20, 2), ('imposer_columns', 1, 20, 2),
                            ('imposer_repeat_count', 1, 1000, 4), ('imposer_gap_x_mm', 0, 100, 3),
                            ('imposer_gap_y_mm', 0, 100, 3)]:
                        value = float(fields.get(key, fallback))
                        if not lo <= value <= hi:
                            raise ValueError('Nieprawidlowe ustawienia impozycji.')
                        settings[key] = int(value) if 'mm' not in key else value
                    settings['imposer_duplex'] = fields.get('imposer_duplex') == 'true'
                    args = [sys.executable, str(ROOT / 'engine' / 'run_imposer.py'), str(src), str(out), json.dumps(settings)]
                completed = subprocess.run(args, capture_output=True, timeout=90)
                if completed.returncode or not out.exists():
                    if b'RuntimeError:' in completed.stderr:
                        raise ValueError('Nie znaleziono obiektow pasujacych do operacji. Sprawdz kolor wejsciowy lub nazwe separacji.')
                    raise ValueError('Silnik nie przetworzyl pliku. Sprawdz ustawienia i strukture PDF.')
                result = out.read_bytes()
            self.reply(200, result, 'application/pdf')
        except subprocess.TimeoutExpired:
            self.reply(422, {'error': 'Przekroczono czas operacji (90 s).'})
        except (ValueError, RuntimeError, OverflowError) as error:
            self.reply(422, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': 'Nie udalo sie odczytac lub przetworzyc PDF.'})


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'MADI Preflight: http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
