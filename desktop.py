"""Desktop launcher (MADI Preflight.exe): local server on a free loopback port shown in its own window.

Built without a console and with the default asInvoker manifest, so it needs no administrator rights.
Closing the window ends the program. If the WebView2 window cannot start, the default browser is used.
The same executable also runs engine scripts in isolated child processes:
`MADI Preflight.exe --engine workflow.py <args>` (used by server.engine_command when frozen).
"""
import runpy
import socket
import sys
import threading
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
    import server  # imported here so engine child processes stay light
    # A windowed build has no console: request logging would write to a missing stderr.
    server.Handler.log_message = lambda *args: None
    port = free_port()
    server.cleanup_stale_workdirs()
    server.Handler.timeout = 60
    httpd = server.Server(('127.0.0.1', port), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{port}/'
    try:
        import webview
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.create_window('MADI Preflight', url, width=1440, height=900, min_size=(1000, 650))
        webview.start(private_mode=False)
    except Exception:
        import webbrowser
        import time
        webbrowser.open(url)
        while True:  # no window to close: keep serving until the process is ended
            time.sleep(3600)
    httpd.shutdown()


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--engine':
        run_engine_script()
    else:
        main()
