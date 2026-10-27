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


def relaunch_locally_if_on_network():
    """When started from a network drive, run a local copy so the shared folder is never held open.

    The copy lives in %LOCALAPPDATA%\\MADI Preflight\\app\\<build id>; a new build on the share gets a new
    folder automatically, older local copies are removed. Returns True if a local copy was started.
    """
    import ctypes
    import os
    import shutil
    import subprocess
    if not getattr(sys, 'frozen', False):
        return False
    exe = Path(sys.executable).resolve()
    root = exe.anchor
    remote = str(exe).startswith('\\\\') or ctypes.windll.kernel32.GetDriveTypeW(root) == 4  # DRIVE_REMOTE
    if not remote:
        return False
    build = (BASE / 'build_id.txt').read_text(encoding='utf-8').strip() if (BASE / 'build_id.txt').exists() else 'dev'
    base = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MADI Preflight' / 'app'
    target = base / build
    if not (target / exe.name).exists():
        staging = base / (build + '.tmp')
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(exe.parent, staging)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    for old in base.iterdir():
        if old.is_dir() and old.name != build:
            shutil.rmtree(old, ignore_errors=True)  # skipped silently while an old copy is still running
    subprocess.Popen([str(target / exe.name), *sys.argv[1:]], cwd=str(target), close_fds=True)
    return True


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
        # WebView2 profile per user on the local disk: never next to the EXE on a shared network drive,
        # where several users (or a second instance) would lock the same profile files.
        import os
        storage = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MADI Preflight' / 'webview'
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(private_mode=False, storage_path=str(storage))
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
    elif not relaunch_locally_if_on_network():
        main()
