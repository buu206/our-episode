"""Single-instance runtime: private API + web, shared persistent volume.

Exit if either process fails so the container supervisor restarts both.
No credentials or environment contents are printed.
"""
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


def validate(environment):
    url = urlsplit(environment.get('APP_ORIGIN', ''))
    if (url.scheme != 'https' or not url.hostname or url.username or url.password
            or url.path not in ('', '/') or url.query or url.fragment):
        raise ValueError('APP_ORIGIN 必须是正式 HTTPS 入口，不能包含路径或凭据。')
    if environment.get('LOCAL_DEMO_ENTRY', '0') != '0':
        raise ValueError('公网部署必须关闭 LOCAL_DEMO_ENTRY。')
    if not environment.get('APP_INVITES_FILE'):
        raise ValueError('公网部署需要 APP_INVITES_FILE，为不同用户配置不同归属。')
    if environment.get('BACKEND_MODE', 'normal') != 'normal':
        raise ValueError('公网制作服务不能使用演示后端。')


def main():
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    validate(environment)
    sys.path.insert(0, str(root / 'backend'))
    from app.config import configured_invites
    configured_invites()  # Fail before serving traffic on invalid account configuration.
    environment.update(BACKEND_URL='http://127.0.0.1:8788', BACKEND_MODE='normal', LOCAL_DEMO_ENTRY='0')
    processes = []
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    result = 0
    try:
        processes.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', '--factory', 'app.main:production_app',
            '--host', '127.0.0.1', '--port', '8788', '--workers', '1', '--no-access-log'], cwd=root / 'backend', env=environment))
        processes.append(subprocess.Popen(['node', 'server.js'], cwd=root / 'web',
            env={**environment, 'HOSTNAME': '0.0.0.0', 'PORT': '3000', 'NODE_ENV': 'production'}))
        while not stopping:
            if any(process.poll() is not None for process in processes):
                result = 1
                break
            time.sleep(0.25)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 20
        for process in processes:
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return result


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
