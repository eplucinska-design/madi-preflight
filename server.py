"""Web interface for MADI Preflight: loopback by default, public only with MADI_PUBLIC=1 and resource limits."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import base64
import collections
import hmac
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import fitz
from inspector import inspect_page

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 40 * 1024 * 1024
PDF_LOCK = threading.Lock()


def env_int(name, default):
    try:
        return max(int(os.environ.get(name, default)), 1)
    except ValueError:
        return default


# Engine jobs run one child process each; this bounds parallel CPU/memory use on small hosts.
JOBS = threading.BoundedSemaphore(env_int('MADI_MAX_JOBS', 2))
RATE_PER_MINUTE = env_int('MADI_RATE_PER_MINUTE', 60)
CHILD_MEMORY_MB = env_int('MADI_CHILD_MEMORY_MB', 1536)
MAX_PAGES = env_int('MADI_MAX_PAGES', 500)
TRUST_PROXY = os.environ.get('MADI_TRUST_PROXY') == '1'
RATE_LOCK = threading.Lock()
RATE_HITS: dict[str, collections.deque] = {}
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'no-referrer',
    'Content-Security-Policy': "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
}


def limit_child():
    """Applied in engine child processes on Linux: memory, CPU time, files and no core dumps."""
    import resource
    memory = CHILD_MEMORY_MB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def engine_command(script, *args):
    """Command for an engine script; in the desktop EXE the executable itself runs the script."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--engine', script, *args]
    return [sys.executable, str(ROOT / 'engine' / script), *args]


def run_engine(command, data=None, timeout=90):
    """Run an engine script in a separate, resource-limited process; refuse when all job slots are busy."""
    if not JOBS.acquire(timeout=5):
        raise BusyError()
    try:
        return subprocess.run(command, input=data, capture_output=True, timeout=timeout,
                              preexec_fn=limit_child if os.name == 'posix' else None)
    finally:
        JOBS.release()


class BusyError(Exception):
    pass


def cleanup_stale_workdirs(max_age=3600):
    """Remove work directories left behind by a crash or kill; normal jobs delete their own."""
    base = Path(tempfile.gettempdir())
    for path in base.glob('madi-web-*'):
        try:
            if time.time() - path.stat().st_mtime > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


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
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def client_ip(self):
        if TRUST_PROXY:
            forwarded = self.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            if forwarded:
                return forwarded
        return self.client_address[0]

    def rate_limited(self):
        now, ip = time.monotonic(), self.client_ip()
        with RATE_LOCK:
            hits = RATE_HITS.setdefault(ip, collections.deque())
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= RATE_PER_MINUTE:
                return True
            hits.append(now)
            if len(RATE_HITS) > 10000:
                RATE_HITS.clear()
        return False

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
                 '/preflight.js': ('preflight.js', 'text/javascript; charset=utf-8'),
                 '/panels.js': ('panels.js', 'text/javascript; charset=utf-8'),
                 '/menus.js': ('menus.js', 'text/javascript; charset=utf-8'),
                 '/sampler.js': ('sampler.js', 'text/javascript; charset=utf-8'),
                 '/montax.js': ('montax.js', 'text/javascript; charset=utf-8'),
                 '/shell.js': ('shell.js', 'text/javascript; charset=utf-8'),
                 '/shell.css': ('shell.css', 'text/css; charset=utf-8'),
                 '/ux.js': ('ux.js', 'text/javascript; charset=utf-8'),
                 '/colors.js': ('colors.js', 'text/javascript; charset=utf-8'),
                 '/measure.js': ('measure.js', 'text/javascript; charset=utf-8'),
                 '/clickup.css': ('clickup.css', 'text/css; charset=utf-8'),
                 '/vendor/lucide.min.js': ('vendor/lucide.min.js', 'text/javascript; charset=utf-8'),
                 '/vendor/lucide-LICENSE': ('vendor/lucide-LICENSE', 'text/plain; charset=utf-8')}
        path = urllib.parse.urlsplit(self.path).path
        if path == '/healthz':
            return self.reply(200, {'status': 'ok'})
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
            if self.rate_limited():
                return self.reply(429, {'error': 'Zbyt wiele zapytan. Sprobuj za minute.'})
            if self.path not in {'/api/analyze', '/api/process', '/api/inspect', '/api/spots', '/api/sample', '/api/preflight'}:
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
                    completed = run_engine(engine_command('spots.py', str(src)), timeout=60)
                if completed.returncode:
                    raise ValueError('Nie udalo sie odczytac kolorow spot.')
                return self.reply(200, json.loads(completed.stdout.decode('utf-8', 'replace').strip().splitlines()[-1]))
            if self.path == '/api/sample':
                numbers = [float(fields.get(key, 'nan')) for key in ('x', 'y')]
                if not all(0 <= value <= 20000 for value in numbers):
                    raise ValueError('Nieprawidlowy punkt probkowania.')
                with tempfile.TemporaryDirectory(prefix='madi-web-') as work:
                    src = Path(work) / 'input.pdf'
                    src.write_bytes(pdf)
                    completed = run_engine(engine_command('sample.py', str(src),
                        str(int(fields.get('page', '1'))), str(numbers[0]), str(numbers[1])), timeout=60)
                if completed.returncode:
                    try:
                        error = json.loads(completed.stderr.decode('utf-8', 'replace').splitlines()[-1])['error']
                    except (ValueError, KeyError, IndexError):
                        error = 'Nie udalo sie odczytac separacji.'
                    raise ValueError(error)
                return self.reply(200, json.loads(completed.stdout.decode('utf-8', 'replace').strip().splitlines()[-1]))
            if self.path == '/api/preflight':
                bleed = float(fields.get('bleed', '3'))
                dpi_error, dpi_warning = int(fields.get('dpi_error', '150')), int(fields.get('dpi_warning', '250'))
                if not (0 <= bleed <= 50 and 10 <= dpi_error <= dpi_warning <= 2400):
                    raise ValueError('Nieprawidlowe progi kontroli.')
                with tempfile.TemporaryDirectory(prefix='madi-web-') as work:
                    src = Path(work) / 'input.pdf'
                    src.write_bytes(pdf)
                    completed = run_engine(engine_command('preflight_report.py', str(src), str(bleed), str(dpi_error),
                        str(dpi_warning)), timeout=90)
                if completed.returncode:
                    try:
                        error = json.loads(completed.stderr.decode('utf-8', 'replace').splitlines()[-1])['error']
                    except (ValueError, KeyError, IndexError):
                        error = 'Nie udalo sie wykonac kontroli pliku.'
                    raise ValueError(error)
                return self.reply(200, json.loads(completed.stdout.decode('utf-8', 'replace').strip().splitlines()[-1]))
            if self.path in {'/api/analyze', '/api/inspect'}:
                with PDF_LOCK, fitz.open(stream=pdf, filetype='pdf') as doc:
                    if doc.needs_pass:
                        raise ValueError('PDF jest chroniony haslem.')
                    if len(doc) > MAX_PAGES:
                        raise ValueError(f'Limit wynosi {MAX_PAGES} stron.')
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
                command = engine_command('workflow.py', str(src), str(out))
                completed = run_engine(command, json.dumps(steps).encode(), timeout=90)
                if completed.returncode or not out.exists():
                    lines = completed.stderr.decode('utf-8', errors='replace').splitlines()
                    try:
                        error = json.loads(lines[-1])['error']
                    except (ValueError, KeyError, IndexError):
                        error = 'Silnik nie przetworzyl pliku. Sprawdz parametry operacji.'
                    raise ValueError(error)
                result = out.read_bytes()
            self.reply(200, result, 'application/pdf')
        except BusyError:
            self.reply(503, {'error': 'Serwer jest zajety innymi zadaniami. Sprobuj za chwile.'})
        except subprocess.TimeoutExpired:
            self.reply(422, {'error': 'Przekroczono czas kolejki (90 s).'})
        except (ValueError, RuntimeError, OverflowError) as error:
            self.reply(422, {'error': str(error)})
        except Exception:
            self.reply(500, {'error': 'Nie udalo sie odczytac lub przetworzyc PDF.'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32


if __name__ == '__main__':
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get('PORT', 8765))
    bind = os.environ.get('MADI_BIND', '127.0.0.1')
    public = os.environ.get('MADI_PUBLIC') == '1'
    # Network access needs either a password or an explicit public mode with the limits above.
    if bind not in {'127.0.0.1', 'localhost', '::1'} and not os.environ.get('MADI_PASSWORD') and not public:
        raise RuntimeError('Set MADI_PASSWORD, or MADI_PUBLIC=1 for open access with resource limits.')
    cleanup_stale_workdirs()
    Handler.timeout = 60  # drop clients that stall while sending a request
    server = Server((bind, port), Handler)
    print(f'MADI Preflight: http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
