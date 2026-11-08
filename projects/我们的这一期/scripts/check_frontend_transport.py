#!/usr/bin/env python3
"""Real local Next → FastAPI transport acceptance in disposable processes/data.

Run with backend/.venv/bin/python after the production frontend has been built.
No normal/demo server or data directory is used. The backend fixture adds only
an ASGI request counter; all authentication, uploads, media and render endpoints
are the production implementation. No model service is enabled or contacted.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx


PRODUCT = Path(__file__).resolve().parents[1]
BACKEND, FRONTEND = PRODUCT / "backend", PRODUCT / "frontend"
TEST_CODES = ("transport-a", "transport-b")
API = "/api/v1"

FIXTURE = '''import json
import os
from pathlib import Path
from app.config import Settings
from app.main import create_app

settings = Settings(data_dir=Path(os.environ["TRANSPORT_DATA_DIR"]),
    invites={"transport-a": "transport-owner-a", "transport-b": "transport-owner-b"},
    worker_enabled=True, cloud={"CLOUD_ENABLED": False, "CLOUD_REGION_VERIFIED": False,
        "CLOUD_RETENTION_VERIFIED": False, "MODEL_API_KEY": ""})
real_app = create_app(settings)
metrics = {"uploads_started": 0, "uploads_finished": 0, "active_uploads": 0,
           "request_bytes": 0, "disconnects": 0}
destination = Path(os.environ["TRANSPORT_METRICS"])
def save():
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(metrics), encoding="utf-8")
    temporary.replace(destination)
save()
async def app(scope, receive, send):
    observed = scope["type"] == "http" and scope["method"] == "POST" and scope["path"].endswith("/assets")
    if not observed:
        return await real_app(scope, receive, send)
    metrics["uploads_started"] += 1
    metrics["active_uploads"] += 1
    save()
    async def observed_receive():
        message = await receive()
        if message["type"] == "http.request":
            metrics["request_bytes"] += len(message.get("body", b""))
        elif message["type"] == "http.disconnect":
            metrics["disconnects"] += 1
        save()
        return message
    try:
        return await real_app(scope, observed_receive, send)
    finally:
        metrics["active_uploads"] -= 1
        metrics["uploads_finished"] += 1
        save()
'''


class CheckFailed(Exception):
    pass


def require(condition, label):
    if not condition:
        raise CheckFailed(label)


def checked(response, status, label):
    require(response.status_code == status, f"{label}: HTTP {response.status_code}, expected {status}")
    return response


def denied(response, status, label, code=None):
    checked(response, status, label)
    require(response.headers.get("cache-control") == "no-store", label + ": no-store missing")
    if response.request.method == "HEAD":
        require(response.content == b"", label + ": HEAD body not empty")
        return
    data = response.json()
    require(set(data) == {"error"} and set(data["error"]) == {"code", "message"}, label + ": error shape")
    if code:
        require(data["error"]["code"] == code, label + ": error code")


def wait_for(predicate, label, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise CheckFailed(label)


def metrics_at(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def ready(base, process, seconds=40):
    deadline = time.monotonic() + seconds
    with httpx.Client(base_url=base, trust_env=False, timeout=2) as client:
        while time.monotonic() < deadline and process.poll() is None:
            try:
                response = client.get(API + "/status")
                if response.status_code == 200:
                    require(response.json()["cloud_enabled"] is False, "isolated cloud guard")
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
    raise CheckFailed("temporary service did not become ready")


def group_alive(process):
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop(process):
    # Each recorded Popen owns a new process group. Its FFmpeg children may
    # outlive the parent, so cleanup must not stop at Popen.poll().
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process.poll()
        if not group_alive(process):
            return
        time.sleep(.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


def port_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    return listener


class RssSampler:
    def __init__(self, pid):
        self.pid, self.values, self.done = pid, [], threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.done.is_set():
            result = subprocess.run(["ps", "-p", str(self.pid), "-o", "rss="],
                capture_output=True, text=True, check=False, timeout=2)
            if result.returncode == 0 and result.stdout.strip().isdigit():
                self.values.append(int(result.stdout.strip()) * 1024)
            self.done.wait(.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.done.set()
        self.thread.join(timeout=3)


def generate_video(directory):
    binary = shutil.which("ffmpeg")
    require(binary is not None, "FFmpeg is required")
    source = directory / "synthetic-large.mov"
    # Uncompressed RGB video makes a genuinely decodable 13+ MiB input without
    # padding a tiny file, downloading media or touching any real person's video.
    result = subprocess.run([binary, "-nostdin", "-v", "error", "-n", "-filter_threads", "1",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=20",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "1",
        "-c:v", "rawvideo", "-pix_fmt", "rgb24", "-threads", "2", "-c:a", "pcm_s16le",
        "-f", "mov", str(source)], capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, "synthetic media creation failed")
    require(source.stat().st_size > 10 * 1024 * 1024, "fixture must exceed 10 MiB")
    return source


def file_digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def check_media(client, url, source, label):
    size = source.stat().st_size
    with source.open("rb") as file:
        first = file.read(128)
        file.seek(-128, os.SEEK_END)
        last = file.read()
    partial = checked(client.get(url, headers={"Range": "bytes=0-127"}), 206, label + " Range")
    require(partial.content == first, label + ": first Range bytes")
    require(partial.headers.get("content-range") == f"bytes 0-127/{size}", label + ": Content-Range")
    # Next's actual Node response uses HTTP/1.1 chunked framing for streamed GET
    # bodies. Both legal framings must still deliver exactly the requested bytes.
    length = partial.headers.get("content-length")
    require(length == "128" or (length is None and partial.headers.get("transfer-encoding") == "chunked"),
            label + ": invalid partial response framing")
    require(partial.headers.get("cache-control") == "no-store", label + ": no-store")
    suffix = checked(client.get(url, headers={"Range": "bytes=-128"}), 206, label + " suffix")
    require(suffix.content == last, label + ": suffix bytes")
    head = checked(client.head(url), 200, label + " HEAD")
    require(not head.content, label + ": HEAD body not empty")
    # Next 16.4.5 send-response.js deliberately excludes Content-Length while
    # copying Route Handler headers. RFC 9110 §8.6 permits HEAD without it.
    head_length = head.headers.get("content-length")
    require(head_length is None or head_length == str(size), label + ": incorrect HEAD Content-Length")
    require(head.headers.get("content-type", "").startswith("video/"), label + ": HEAD media type")
    require(head.headers.get("accept-ranges") == "bytes", label + ": HEAD range support")
    require(head.headers.get("cache-control") == "no-store", label + ": HEAD cache policy")
    head_range = checked(client.head(url, headers={"Range": "bytes=0-127"}), 206, label + " HEAD Range")
    require(not head_range.content, label + ": HEAD Range body")
    require(head_range.headers.get("content-length") in (None, "128"), label + ": HEAD Range length")
    require(head_range.headers.get("content-range") == f"bytes 0-127/{size}", label + ": HEAD Content-Range")
    checksum, received = hashlib.sha256(), 0
    with client.stream("GET", url, timeout=30) as stream:
        checked(stream, 200, label + " full download")
        for chunk in stream.iter_bytes(65536):
            checksum.update(chunk)
            received += len(chunk)
    require(received == size and checksum.hexdigest() == file_digest(source), label + ": full byte/hash match")
    return {"partial_content_length": length, "partial_transfer_encoding": partial.headers.get("transfer-encoding"),
            "head_content_length": int(head_length) if head_length is not None else None,
            "head_range_content_range": head_range.headers["content-range"]}


def streamed_form(source, metrics_path, started_before):
    bytes_before = metrics_at(metrics_path).get("request_bytes", 0)
    yield b'--transport-boundary\r\nContent-Disposition: form-data; name="file"; filename="synthetic-large.mov"\r\nContent-Type: video/quicktime\r\n\r\n'
    with source.open("rb") as file:
        first = file.read(65536)
        yield first
        # The upstream must receive bytes before we produce the remaining file;
        # this detects request-wide buffering in the real Next execution path.
        wait_for(lambda: metrics_at(metrics_path).get("uploads_started", 0) > started_before
                 and metrics_at(metrics_path).get("request_bytes", 0) > bytes_before,
                 "Next buffered the upload before reaching FastAPI")
        for chunk in iter(lambda: file.read(65536), b""):
            yield chunk
            time.sleep(.001)
    yield b"\r\n--transport-boundary--\r\n"


def cancel_upload(client, base, episode_id, metrics_path):
    before = metrics_at(metrics_path)
    parsed = httpx.URL(base)
    cookie = "; ".join(f"{item.name}={item.value}" for item in client.cookies.jar)
    headers = (f"POST {API}/episodes/{episode_id}/assets HTTP/1.1\r\nHost: {parsed.host}:{parsed.port}\r\n"
        f"Origin: {base}\r\nCookie: {cookie}\r\nX-CSRF-Token: {client.headers['X-CSRF-Token']}\r\n"
        "Content-Type: multipart/form-data; boundary=transport-cancel\r\nTransfer-Encoding: chunked\r\n\r\n")
    initial = (b'--transport-cancel\r\nContent-Disposition: form-data; name="file"; filename="cancelled.mov"\r\n'
               b'Content-Type: video/quicktime\r\n\r\n' + b"x" * 131072)
    with socket.create_connection((parsed.host, parsed.port), timeout=5) as connection:
        connection.sendall(headers.encode())
        connection.sendall(f"{len(initial):x}\r\n".encode() + initial + b"\r\n")
        wait_for(lambda: metrics_at(metrics_path).get("uploads_started", 0) > before["uploads_started"],
                 "cancelled request never reached upstream")
        # Deliberately close before the final multipart boundary or zero chunk.
    wait_for(lambda: metrics_at(metrics_path).get("active_uploads") == 0,
             "client disconnect did not release upstream upload", seconds=15)
    after = metrics_at(metrics_path)
    require(after["uploads_started"] == before["uploads_started"] + 1, "disconnect caused an automatic upload retry")
    require(after["disconnects"] > before["disconnects"], "backend did not observe the cancelled connection")


def run_checks(base, source, metrics_path, next_process, directory, secrets_seen):
    with ExitStack() as stack:
        clients = [stack.enter_context(httpx.Client(base_url=base, timeout=45, trust_env=False,
                    follow_redirects=False, headers={"Origin": base})) for _ in range(3)]
        one, two, visitor = clients
        denied(visitor.get(API + "/episodes"), 401, "anonymous access", "AUTH_REQUIRED")
        for client, code in zip((one, two), TEST_CODES):
            response = checked(client.post(API + "/auth/login", json={"invite_code": code}), 200, "login")
            identity = response.json()
            client.headers["X-CSRF-Token"] = identity["csrf_token"]
            secrets_seen.append(identity["csrf_token"])
            secrets_seen.extend(cookie.value for cookie in client.cookies.jar)
            require("httponly" in response.headers.get("set-cookie", "").lower(), "HttpOnly cookie not forwarded")
            require("samesite=strict" in response.headers.get("set-cookie", "").lower(), "SameSite cookie not forwarded")
        first = checked(one.post(API + "/episodes", json={"title": "隔离传输验收A"}), 201, "create first episode").json()
        second = checked(two.post(API + "/episodes", json={"title": "隔离传输验收B"}), 201, "create second episode").json()
        denied(two.get(API + f"/episodes/{first['id']}"), 404, "other owner episode")
        denied(one.get(API + f"/episodes/{second['id']}"), 404, "reverse owner isolation")
        denied(one.post(API + "/episodes", headers={"X-CSRF-Token": ""}, json={"title": "forbidden"}),
               403, "missing CSRF", "CSRF_FAILED")
        denied(two.post(API + "/episodes", headers={"X-CSRF-Token": one.headers["X-CSRF-Token"]},
               json={"title": "forbidden"}), 403, "other session CSRF", "CSRF_FAILED")
        denied(one.post(API + "/episodes", headers={"Origin": "http://untrusted.invalid"}, json={"title": "forbidden"}),
               403, "foreign Origin", "INVALID_ORIGIN")
        denied(one.get(API + "/episodes", headers={"Sec-Fetch-Site": "cross-site"}),
               403, "cross-site read", "INVALID_ORIGIN")
        token = next(iter(one.cookies.jar)).value
        for name in ("episode_demo_session", "episode_session_participant", "episode_session_viewer"):
            denied(visitor.get(API + "/episodes", headers={"Cookie": name + "=" + token}),
                   401, "wrong role/environment cookie", "AUTH_REQUIRED")
        url = API + f"/episodes/{first['id']}/assets"
        with RssSampler(next_process.pid) as memory:
            with source.open("rb") as file:
                asset = checked(one.post(url, files={"file": (source.name, file, "video/quicktime")}),
                                201, "declared multipart upload above 10 MiB").json()
            before = metrics_at(metrics_path)["uploads_started"]
            streamed = checked(one.post(url, content=streamed_form(source, metrics_path, before),
                headers={"Content-Type": "multipart/form-data; boundary=transport-boundary"}),
                201, "chunked streaming upload above 10 MiB").json()
        require(asset["size"] == streamed["size"] == source.stat().st_size, "uploaded file size mismatch")
        require(asset["id"] != streamed["id"], "distinct explicit uploads did not create distinct sources")
        source_framing = check_media(one, asset["media_url"], source, "uploaded source")
        denied(visitor.get(asset["media_url"], headers={"Range": "bytes=0-127"}), 401, "anonymous Range")
        denied(visitor.head(asset["media_url"]), 401, "anonymous HEAD")
        denied(two.get(asset["media_url"], headers={"Range": "bytes=0-127"}), 404, "other owner Range")
        denied(two.head(asset["media_url"]), 404, "other owner HEAD")
        cancel_upload(one, base, first["id"], metrics_path)
        saved = checked(one.get(API + f"/episodes/{first['id']}"), 200, "refresh after cancellation").json()
        require(len(saved["assets"]) == 2 and saved["quota"]["original_count"] == 2, "cancelled upload created an asset")
        require(not list((directory / "data" / "incoming").iterdir()), "cancelled upload left application temp files")
        version = checked(one.post(API + f"/episodes/{first['id']}/versions", json={
            "title": "代理真实渲染验证", "packaging": "original", "narration_text": "",
            "segments": [{"asset_id": asset["id"], "start": 0, "end": .7, "caption": ""}]}), 201, "create render version").json()
        task = checked(one.post(API + f"/versions/{version['id']}/render", json={}), 200, "start local render").json()
        def finished():
            row = checked(one.get(API + f"/jobs/{task['id']}"), 200, "render polling").json()
            require(row["state"] != "failed", "real local render failed")
            return row["state"] == "succeeded"
        wait_for(finished, "real local render timed out", 45)
        checked(one.post(API + f"/versions/{version['id']}/confirm", json={}), 200, "confirm real output")
        destination = directory / "downloaded-output.mp4"
        export = API + f"/versions/{version['id']}/export"
        with one.stream("GET", export) as response:
            checked(response, 200, "confirmed export")
            require("attachment" in response.headers.get("content-disposition", ""), "export attachment header")
            with destination.open("wb") as file:
                for chunk in response.iter_bytes(65536):
                    file.write(chunk)
        probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(destination)], capture_output=True, timeout=15, check=False)
        require(probe.returncode == 0 and float(json.loads(probe.stdout)["format"]["duration"]) > 0,
                "exported bytes are not decodable media")
        check_media(one, API + f"/versions/{version['id']}/media", destination, "rendered version")
        denied(two.get(export), 404, "other owner export")
        denied(two.head(export), 404, "other owner export HEAD")
        exported_head = checked(one.head(export), 200, "confirmed export HEAD")
        require(not exported_head.content and "attachment" in exported_head.headers.get("content-disposition", ""),
                "confirmed export HEAD attachment metadata")
        # A durable deletion must revoke old media even with a cached validator.
        checked(one.delete(API + f"/assets/{asset['id']}?confirm=true"), 200, "delete application source copy")
        denied(one.get(API + f"/versions/{version['id']}/media", headers={"Range": "bytes=0-127", "If-None-Match": "*"}),
               409, "revoked media Range")
        denied(one.head(export), 409, "revoked export HEAD")
        checked(one.post(API + "/auth/logout"), 200, "empty-body logout")
        denied(one.get(API + "/episodes"), 401, "expired logout cookie")
        require(checked(two.get(API + "/episodes"), 200, "unaffected second session").json()["items"][0]["id"] == second["id"],
                "second owner's data changed")
        return {"uploaded_bytes_per_file": source.stat().st_size, "declared_and_chunked_uploads": 2,
            "stream_reached_backend_before_remaining_file_was_produced": True,
            "cancelled_upload_released_upstream_without_retry": True,
            "media_range_head_and_full_sha256": True, "real_render_confirm_export": True,
            "source_response_framing": source_framing,
            "anonymous_csrf_origin_role_and_owner_isolation": True, "revoked_cached_media_denied": True,
            "next_rss_first_sample_bytes": memory.values[0] if memory.values else None,
            "next_rss_peak_sample_bytes": max(memory.values) if memory.values else None}


def main(checks=run_checks, report_heading=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Write a sanitized Markdown result, never logs or credentials")
    args = parser.parse_args()
    started = time.monotonic()
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "status": "failed", "cloud_calls": 0}
    processes = []
    try:
        python = BACKEND / ".venv/bin/python"
        node = shutil.which("node")
        build_id = FRONTEND / ".next/BUILD_ID"
        standalone = FRONTEND / ".next/standalone/server.js"
        require(python.is_file() and node, "local Python/Node environment unavailable")
        require(build_id.is_file() and standalone.is_file(), "production standalone frontend build is not ready")
        newest = max(path.stat().st_mtime for path in [FRONTEND / "next.config.ts", FRONTEND / "package.json", *FRONTEND.joinpath("src").rglob("*.ts"), *FRONTEND.joinpath("src").rglob("*.tsx")])
        require(build_id.stat().st_mtime >= newest, "frontend source is newer than the production build")
        with tempfile.TemporaryDirectory(prefix="episode-frontend-transport-") as temporary:
            directory = Path(temporary).resolve()
            directory.chmod(0o700)
            metrics = directory / "metrics.json"
            (directory / "transport_fixture.py").write_text(FIXTURE, encoding="utf-8")
            secrets_seen = list(TEST_CODES)
            env = os.environ.copy()
            env.update({"PYTHONPATH": str(BACKEND), "PYTHON_DOTENV_DISABLED": "1", "NEXT_TELEMETRY_DISABLED": "1",
                "TRANSPORT_DATA_DIR": str(directory / "data"), "TRANSPORT_METRICS": str(metrics),
                "CLOUD_ENABLED": "false", "CLOUD_REGION_VERIFIED": "false", "CLOUD_RETENTION_VERIFIED": "false",
                "MODEL_API_KEY": "", "NODE_ENV": "production"})
            with ExitStack() as stack:
                backend_log = stack.enter_context((directory / "backend.log").open("wb"))
                next_log = stack.enter_context((directory / "frontend.log").open("wb"))
                with port_socket() as listener:
                    backend_port = listener.getsockname()[1]
                    backend = subprocess.Popen([str(python), "-m", "uvicorn", "transport_fixture:app",
                        "--app-dir", str(directory), "--host", "127.0.0.1", "--port", str(backend_port),
                        "--fd", str(listener.fileno()), "--workers", "1", "--no-access-log", "--log-level", "warning"],
                        cwd=BACKEND, env=env, stdout=backend_log, stderr=backend_log, stdin=subprocess.DEVNULL,
                        start_new_session=True, pass_fds=(listener.fileno(),))
                    processes.append(backend)
                    stack.callback(stop, backend)
                with port_socket() as listener:
                    frontend_port = listener.getsockname()[1]
                base = f"http://127.0.0.1:{frontend_port}"
                env.update({"BACKEND_URL": f"http://127.0.0.1:{backend_port}", "BACKEND_MODE": "normal",
                            "APP_ORIGIN": base, "HOSTNAME": "127.0.0.1", "PORT": str(frontend_port)})
                frontend = subprocess.Popen([node, str(standalone)], cwd=FRONTEND, env=env,
                    stdout=next_log, stderr=next_log, stdin=subprocess.DEVNULL, start_new_session=True)
                processes.append(frontend)
                stack.callback(stop, frontend)
                try:
                    ready(env["BACKEND_URL"], backend)
                    ready(base, frontend)
                    source = generate_video(directory)
                    report.update(checks(base, source, metrics, frontend, directory, secrets_seen))
                    for log in (directory / "backend.log", directory / "frontend.log"):
                        content = log.read_text(encoding="utf-8", errors="replace")
                        require(not any(value and value in content for value in secrets_seen), "credential appeared in process logs")
                    report["credentials_absent_from_logs"] = True
                    report["status"] = "passed"
                finally:
                    for process in reversed(processes):
                        stop(process)
        report["temporary_data_removed"] = True
    except CheckFailed as error:
        report["failed_check"] = str(error)
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError, subprocess.SubprocessError) as error:
        # Do not print exception text: it can contain request/cookie details.
        report["failed_check"] = "transport execution interrupted: " + type(error).__name__
    finally:
        for process in reversed(processes):
            stop(process)
        report["temporary_processes_stopped"] = all(process.poll() is not None and not group_alive(process) for process in processes)
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    if args.report:
        # Other acceptance suites reuse these isolated production processes;
        # they keep their own factual report instead of claiming transport checks.
        if report_heading is not None:
            text = f"# {report_heading}\n\n由独立本机验收脚本通过真实HTTP执行。使用临时FastAPI、SQLite/媒体目录和Next production standalone；不调用模型，不使用正常/演示用户数据。\n\n"
            text += "```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n"
            text += "\n仅代表本机接口与媒体链路，不代表真实AI、手机真机、1GiB负载或生产部署验收。\n"
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(text, encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "passed" else 1
        text = "# 阶段3-1 本机正式代理传输验收\n\n由 `scripts/check_frontend_transport.py` 实际执行生成。使用临时独立 FastAPI、SQLite/文件目录和 Next production standalone 进程；全部通过真实 HTTP，不调用模型。\n\n"
        text += "```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n\n"
        text += "范围：合成视频的实际大于10 MiB传输、提前到达上游的分块流、取消、权限及本机渲染导出。RSS为Next进程采样观察，不代表1 GiB或高并发负载验证；未测试真实AI或手机浏览器。正常和演示服务与数据未参与。\n"
        text += "\n传输头差异：本机 Next 16.4.5 的 `frontend/node_modules/next/dist/server/send-response.js` 第18–21、49–50行主动排除 Content-Length/Content-Encoding/Transfer-Encoding；第74–77行对HEAD结束空响应。实际GET使用chunked，HEAD没有Content-Length。依据 [RFC 9110 §8.6](https://www.rfc-editor.org/rfc/rfc9110.html#section-8.6)，HEAD可省略Content-Length；有值时仍须准确。验收严格检查206、Content-Range、Range字节、HEAD无body/媒体类型/no-store/Accept-Ranges，以及完整GET字节数与SHA-256，不以省略长度头代替完整性检查。\n"
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
