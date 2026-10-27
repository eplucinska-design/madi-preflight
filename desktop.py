"""Desktop launcher (MADI Preflight.exe): local server on a free port, opened in the default browser.

The same executable also runs engine scripts in isolated child processes:
`MADI Preflight.exe --engine workflow.py <args>` (used by server.engine_command when frozen).
"""
import runpy
import socket
import sys
import threading
import webbrowser
from pathlib import Path

BASE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))

def run_engine_script():
    name = sys.argv[2]
    engine = BASE / 'engine'
    script = engine / name
    if script.parent != engine or not script.is_file():
        sys.exit('Nieznany skrypt silnika.')
    sys.path.insert(0, str(engine))
    sys.argv = [str(script)] + sys.argv[3:]
    runpy.run_path(str(script), run_name='__main__')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    import server  # noqa: imported here so engine child processes stay light
    port = free_port()
    server.cleanup_stale_workdirs()
    server.Handler.timeout = 60
    httpd = server.Server(('127.0.0.1', port), server.Handler)
    url = f'http://127.0.0.1:{port}/'
    print('MADI Preflight dziala lokalnie na tym komputerze:', url, flush=True)
    print('Pliki PDF nie opuszczaja komputera. Zamknij to okno, aby wylaczyc program.', flush=True)
    threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--engine':
        run_engine_script()
    else:
        main()
