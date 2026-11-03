#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""smoke_fixture_server.py - smoketest-only fixture HTTP server.

Serves a JSON file on 127.0.0.1:<port> for the CI smoke_e2e pipeline.
Response shape is the 4-fast-field + body contract that srcradar pdtm +
algo_diff expect to see. Two routes:

  GET /          Return the current active file. Initially file.json;
                  after /change has been called, returns file_changed.json.
                  The active file is read from disk on every request, so the
                  CI "swap fixture file in place" affordance works without
                  restarting the server.

  GET /change    Permanently switch to file_changed.json. There is NO
                  /revert or /restore - once switched, the server stays on
                  the changed file for the lifetime of the process.

Templates in body (strict whitelist, no injection):
  {{ timestamp }}                      ISO8601 with +08:00 timezone
  {{ random:N }}                         N digits (0-9), default 8
  {{ random:N,hex }}                      N hex chars (0-9 + a-f), default 8

Anything else matching braces is rejected at startup so a typo or a
malicious key never reaches the renderer. The renderer never uses
f-string or eval - it only walks the regex match list and substitutes
the two known values.

CLI (default background):
  python3 -m modules.main.lib.smoke_fixture_server <port> <file.json>
                  --log PATH         log file path (default ./smoke_fixture_server.log)
                  --pid-file PATH    PID file path (default <log>.pid)
                  --foreground       run in foreground (debug only)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import socket
import string
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

TEMPLATE_RE = re.compile(
    r"\{\{\s*(?:timestamp|random(?::(\d+))?(?:,(hex))?)\s*\}\}"
)
ANY_BRACES_RE = re.compile(r"\{\{[^}]*\}\}")

TIMESTAMP_FMT = "%Y-%m-%dT%H:%M:%S%z"
CN_TZ = timezone(timedelta(hours=8))

MAX_BODY_BYTES = 1 * 1024 * 1024
DEFAULT_LOG = Path("./smoke_fixture_server.log")
DEFAULT_PID_SUFFIX = ".pid"
HEALTH_TIMEOUT_S = 10.0


def now_timestamp() -> str:
    """ISO8601 with +08:00 timezone."""
    return datetime.now(CN_TZ).strftime(TIMESTAMP_FMT)


def random_hex_chars(n: int) -> str:
    """N hex characters (0-9 + a-f). N must be >= 1."""
    if n < 1:
        raise ValueError(f"random length must be >= 1, got {n}")
    return "".join(random.choices(string.hexdigits.lower(), k=n))


def random_digits(n: int) -> str:
    """N digits (0-9). N must be >= 1."""
    if n < 1:
        raise ValueError(f"random length must be >= 1, got {n}")
    return "".join(random.choices(string.digits, k=n))


def render(body: str) -> str:
    """Render templates in body. No f-string, no eval.

    Only known keys are replaced; everything else is left as-is. The
    whitelist check at startup guarantees nothing else can be present.
    """
    out: list[str] = []
    last_end = 0
    for m in TEMPLATE_RE.finditer(body):
        out.append(body[last_end : m.start()])
        candidate = m.group(0)
        if "timestamp" in candidate:
            out.append(now_timestamp())
        else:
            n_str = m.group(1)
            n = int(n_str) if n_str else 8
            if m.group(2) == "hex":
                out.append(random_hex_chars(n))
            else:
                out.append(random_digits(n))
        last_end = m.end()
    out.append(body[last_end:])
    return "".join(out)


def strict_check(body: str) -> None:
    """Reject any brace pair not on the whitelist. Raises ValueError."""
    for m in ANY_BRACES_RE.finditer(body):
        if not TEMPLATE_RE.fullmatch(m.group(0)):
            raise ValueError(
                f"unauthorized template key {m.group(0)!r}; "
                "only {{ timestamp }} / {{ random:N }} / {{ random:N,hex }} are accepted"
            )


def load(path: Path) -> dict[str, Any]:
    """Load JSON file at path. Raises with a clear message on failure."""
    if not path.exists():
        raise FileNotFoundError(f"fixture file not found: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError(
            f"fixture file too large: {len(raw)} bytes > {MAX_BODY_BYTES}"
        )
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"fixture file must be a JSON object, got {type(data).__name__}"
        )
    for key in ("title", "status_code", "redirect_to", "technologies", "body"):
        if key not in data:
            raise ValueError(f"fixture file missing required key: {key!r}")
    strict_check(str(data["body"]))
    return data


def changed_path(file: Path) -> Path:
    """Locate the swapped-in fixture file alongside the initial one.

    srcradar convention: initial.json + changed.json live in the same
    directory. The HTTP server is started with the initial file; GET
    /change flips it to "changed.<ext>" in the SAME directory. We do not
    rewrite the stem (the old "_changed" convention assumed a single
    file and renamed it, which breaks the ct2 fixture layout).
    """
    return file.with_name("changed" + file.suffix)


def bind_check(port: int) -> None:
    """Verify port is bindable on 127.0.0.1; raises OSError if taken."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()


def health_probe(port: int, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    """Poll GET /health until OK or timeout."""
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionRefusedError, OSError):
            time.sleep(0.2)
    return False


def make_handler(initial_file: Path, changed_path_: Path):
    class Handler(BaseHTTPRequestHandler):
        active_file = initial_file
        # changed_path_ is what GET /change flips active_file to. Stash it
        # on the class so do_GET can find it without an instance attribute
        # (BaseHTTPRequestHandler is instantiated per request by the server
        # loop). The parameter is renamed from `changed_file` to avoid
        # shadowing the class attribute we set right after.
        changed_file = changed_path_

        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_json({"status": "ok"})
                return

            if self.path == "/change":
                # Permanently switch the served fixture from initial to
                # changed. After this, every "/" request reads
                # file_changed.json (the same fixture the CI workflow uses
                # to swap in for round 2). The switch is one-way and
                # process-lifetime: there is no "/unchange" - if a test
                # needs to reset, restart the server. This avoids the
                # process-restart race that the swap step in CI workflow
                # otherwise has to guard against.
                Handler.active_file = self.changed_file
                self.send_json({"status": "ok", "active": str(Handler.active_file)})
                return

            if self.path == "/stop":
                # Hard-stop this server. Returns metadata then drops the
                # listening socket and exits the process. Caller polls
                # /health and expects the pid to disappear from lsof.
                # We use os._exit to avoid going through Python's
                # atexit/shutdown hooks which can hang the socket close.
                self.send_json({"status": "ok", "action": "stopped"})
                self.wfile.flush()
                try:
                    self.server.shutdown()
                except (RuntimeError, OSError, AttributeError):
                    pass
                import threading as _threading
                _threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)), daemon=True).start()
                return

            if self.path == "/":
                # Read fixture file from disk each request. The JSON's
                # "status_code" field decides the real HTTP status code so
                # the server can return 403/404/500 from the same fixture.
                data = load(Handler.active_file)
                rendered_body = render(str(data["body"]))
                status = int(data.get("status_code", 200))
                payload = {
                    "title": data["title"],
                    "status_code": status,
                    "redirect_to": data["redirect_to"],
                    "technologies": data["technologies"],
                    "body": rendered_body,
                }
                self.send_json(payload, status=status)
                return

            self.send_error(404, f"unknown path: {self.path}")

    return Handler


def run_foreground(port: int, file: Path, log_file: Path) -> int:
    """Run server in foreground (foreground mode for debugging)."""
    load(file)  # validate fixture file at startup, fail fast

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fp = log_file.open("a", encoding="utf-8")

    class DualLog:
        def write(self, msg: str) -> int:
            sys.stdout.write(msg)
            log_fp.write(msg)
            return len(msg)

        def flush(self) -> None:
            sys.stdout.flush()
            log_fp.flush()

    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(file, changed_path(file)))
    sys.stderr.write(
        f"[smoke_fixture_server] listening on 127.0.0.1:{port} file={file}\n"
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


def run_background(
    port: int, file: Path, log_file: Path, pid_file: Path
) -> int:
    """Fork into background via double-fork + setsid. Parent returns 0 on success.

    Child writes its PID to pid_file, redirects stdio to log_file, and runs
    the HTTP server until SIGTERM. Caller polls /health for up to 10s; if
    health fails, caller kills the child and returns non-zero.
    """
    if pid_file.exists():
        try:
            old = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            old = 0
        if old and os.path.exists(f"/proc/{old}"):
            sys.stderr.write(
                f"[smoke_fixture_server] already running PID {old} "
                f"(pid_file={pid_file})\n"
            )
            return 0
        pid_file.unlink(missing_ok=True)

    try:
        bind_check(port)
    except OSError as exc:
        sys.stderr.write(
            f"[smoke_fixture_server] port {port} not bindable: {exc}\n"
        )
        return 2

    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_file, "ab", buffering=0)

    pid_first = os.fork()
    if pid_first > 0:
        os.waitpid(pid_first, 0)

    pid_second = os.fork()
    if pid_second > 0:
        os._exit(0)

    os.setsid()
    os.dup2(log_fp.fileno(), sys.stdout.fileno())
    os.dup2(log_fp.fileno(), sys.stderr.fileno())
    os.dup2(log_fp.fileno(), sys.stdin.fileno())
    log_fp.close()

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

    try:
        run_foreground(port, file, log_file)
    finally:
        pid_file.unlink(missing_ok=True)
    return 0


def parse_argv(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="smoke_fixture_server",
        description="smoketest fixture HTTP server (4 fast fields + body JSON)",
    )
    p.add_argument("port", type=int, help="port to bind on 127.0.0.1")
    p.add_argument("file", type=Path, help="initial fixture JSON file")
    p.add_argument("--log", type=Path, default=None, help="log file path")
    p.add_argument("--pid-file", type=Path, default=None, help="PID file path")
    p.add_argument(
        "--foreground",
        action="store_true",
        help="run in foreground (default: background)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_argv(argv if argv is not None else sys.argv[1:])
    if args.port < 1 or args.port > 65535:
        sys.stderr.write(f"invalid port: {args.port}\n")
        return 2
    if not args.file.exists():
        sys.stderr.write(f"fixture file not found: {args.file}\n")
        return 2

    log = args.log or DEFAULT_LOG
    pid = args.pid_file or log.with_name(log.name + DEFAULT_PID_SUFFIX)

    if args.foreground:
        return run_foreground(args.port, args.file, log)

    rc = run_background(args.port, args.file, log, pid)
    if rc != 0:
        return rc

    if not health_probe(args.port):
        sys.stderr.write(
            f"[smoke_fixture_server] health probe failed after "
            f"{HEALTH_TIMEOUT_S}s; check {log}\n"
        )
        return 3

    if pid.exists():
        try:
            actual_pid = int(pid.read_text().strip())
            sys.stdout.write(f"PID={actual_pid}\n")
        except (ValueError, OSError):
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())