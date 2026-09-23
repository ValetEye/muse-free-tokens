#!/usr/bin/env python3
"""End-to-end tests for muse-free-tokens bridge + worker contract."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "bridge.py"
POLL = ROOT / "worker" / "poll-example.sh"
PYTHON = sys.executable

passed = 0
failed = 0


def record(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def redact(text: str) -> str:
    return re.sub(r"mft_[A-Za-z0-9_-]+", "mft_[redacted]", text)


class Bridge:
    def __init__(self, hold_seconds: int = 30, host: str = "127.0.0.1",
                 public_url: str = "", extra_args: list[str] | None = None):
        self.base = Path(tempfile.mkdtemp(prefix="mft-test-"))
        self.port = free_port()
        self.host = host
        self.hold_seconds = hold_seconds
        self.public_url = public_url
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.token = ""
        self.url = f"http://127.0.0.1:{self.port}"

    @property
    def spool(self) -> Path:
        return self.base / "spool"

    def start(self) -> str:
        cmd = [
            PYTHON, str(BRIDGE),
            "--host", self.host,
            "--port", str(self.port),
            "--base-dir", str(self.base),
            "--hold-seconds", str(self.hold_seconds),
        ]
        if self.public_url:
            cmd.extend(["--public-url", self.public_url])
        cmd.extend(self.extra_args)
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        if not wait_port("127.0.0.1", self.port, 8):
            out, err = self.proc.communicate(timeout=2)
            raise RuntimeError(f"bridge failed to start\nstdout={out}\nstderr={err}")
        token_file = self.base / "token"
        deadline = time.time() + 3
        while time.time() < deadline and not token_file.exists():
            time.sleep(0.05)
        self.token = token_file.read_text(encoding="utf-8").strip()
        return self.token

    def stop(self, wipe: bool = True) -> tuple[str, str]:
        out = err = ""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                out, err = self.proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                out, err = self.proc.communicate(timeout=2)
        if wipe:
            shutil.rmtree(self.base, ignore_errors=True)
        return out, err


def request(url: str, method: str = "GET", token: str | None = None,
            body: dict | bytes | None = None, timeout: float = 15,
            extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        else:
            data = body
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = {}
            if raw:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {}
            return resp.status, parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return e.code, parsed, raw


def stream_request(url: str, token: str, body: dict, timeout: float = 20) -> tuple[str, float, int]:
    t0 = time.time()
    r = subprocess.run(
        [
            "curl", "-sS", "-N", "--max-time", str(int(timeout)),
            url,
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(body),
        ],
        capture_output=True, text=True, timeout=timeout + 5,
    )
    elapsed = time.time() - t0
    return r.stdout, elapsed, r.returncode


class MockWorker(threading.Thread):
    def __init__(self, spool: Path, mode: str = "text", delay: float = 0.3,
                 progress: list[str] | None = None, reply: dict | None = None):
        super().__init__(daemon=True)
        self.spool = spool
        self.mode = mode
        self.delay = delay
        self.progress = progress or []
        self.reply = reply
        self.claimed: list[str] = []
        self.stop_flag = threading.Event()

    def run(self) -> None:
        deadline = time.time() + 20
        while time.time() < deadline and not self.stop_flag.is_set():
            inbox = self.spool / "in"
            for path in inbox.glob("prm_*.json"):
                dest = self.spool / "working" / path.name
                try:
                    os.rename(path, dest)
                except OSError:
                    continue
                pid = path.stem
                self.claimed.append(pid)
                if self.progress:
                    prog = self.spool / "progress" / f"{pid}.jsonl"
                    with open(prog, "a", encoding="utf-8") as fh:
                        for line in self.progress:
                            fh.write(line + "\n")
                            fh.flush()
                            time.sleep(0.15)
                time.sleep(self.delay)
                if self.reply is not None:
                    payload = self.reply
                elif self.mode == "tools_obj":
                    payload = {
                        "content": "Reading files.",
                        "tool_calls": [
                            {"id": "call_1", "name": "Read",
                             "arguments": {"path": "/tmp/a.py"}},
                            {"id": "call_2", "name": "Read",
                             "arguments": {"path": "/tmp/b.py"}},
                        ],
                    }
                elif self.mode == "tools_str":
                    payload = {
                        "content": "",
                        "tool_calls": [
                            {"id": "call_x", "name": "Read",
                             "arguments": json.dumps({"path": "/tmp/c.py"})},
                        ],
                    }
                else:
                    payload = {"content": f"hello from worker for {pid}"}
                tmp = self.spool / "out" / f"{pid}.tmp"
                final = self.spool / "out" / f"{pid}.json"
                tmp.write_text(json.dumps(payload), encoding="utf-8")
                os.rename(tmp, final)
                return
            time.sleep(0.05)


def expect_error(code: int, parsed: dict, want_code: int, name: str) -> None:
    ok = code == want_code and isinstance(parsed.get("error"), dict)
    record(name, ok, f"status={code} body={parsed}")


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def run_bridge_cmd(base: Path, *flags: str, timeout: float = 8) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(BRIDGE), "--base-dir", str(base), *flags],
        capture_output=True, text=True, timeout=timeout,
    )


def main() -> int:
    b = Bridge(hold_seconds=20)
    token = b.start()
    try:
        record("first-run token created",
               token.startswith("mft_") and len(token) > 20,
               f"len={len(token)} mode={oct(mode_of(b.base / 'token'))}")
        record("token file is 0600", mode_of(b.base / "token") == 0o600)
        record("base dir is 0700", mode_of(b.base) == 0o700)
        record("spool dir is 0700", mode_of(b.spool) == 0o700)
        for sub in ("in", "working", "out", "progress", "bad"):
            record(f"spool/{sub} exists and is 0700",
                   (b.spool / sub).is_dir() and mode_of(b.spool / sub) == 0o700)

        code, _, raw = request(b.url + "/")
        record("GET / is a generic ok",
               code == 200 and raw.strip() == b"ok"
               and b"muse-free-tokens" not in raw
               and b"/v1/" not in raw,
               f"status={code} body={raw!r}")

        code, parsed, raw = request(b.url + "/healthz")
        public = raw.decode("utf-8", errors="replace")
        record("GET /healthz no auth is a liveness stub",
               code == 200 and parsed.get("service") == "muse-free-tokens"
               and parsed.get("status") == "ok"
               and "last_worker_seen" not in parsed
               and "queue" not in parsed
               and "last_worker_claim_id" not in parsed
               and token not in public,
               f"status={code} body={parsed}")

        code, parsed, _ = request(b.url + "/healthz", token=token)
        record("GET /healthz with auth includes worker fields",
               code == 200 and parsed.get("last_worker_seen") is None
               and "queue" in parsed
               and parsed.get("model") == "muse-free-tokens",
               f"body={parsed}")

        code, _, _ = request(b.url + "/v1/worker/next", token=token)
        code, parsed, _ = request(b.url + "/healthz", token=token)
        record("healthz last_worker_seen after 204 poll",
               code == 200 and parsed.get("last_worker_seen") is not None
               and parsed.get("last_worker_seen_seconds_ago") is not None
               and parsed.get("last_worker_seen_seconds_ago") < 5,
               f"body={parsed}")

        doc = run_bridge_cmd(b.base, "--doctor", "--port", str(b.port))
        record("doctor ready after a worker poll",
               doc.returncode == 0 and "Doctor result: ready" in doc.stdout,
               f"rc={doc.returncode} out={doc.stdout!r}")

        setup = run_bridge_cmd(
            b.base, "--setup", "--port", str(b.port),
            "--public-url", "https://example.trycloudflare.com",
        )
        client = json.loads((b.base / "client.json").read_text(encoding="utf-8"))
        journal = json.loads((b.base / "setup-journal.json").read_text(encoding="utf-8"))
        record("--setup writes client.json and exits",
               setup.returncode == 0
               and client.get("model") == "muse-free-tokens"
               and client.get("base_url").endswith("/v1")
               and "api_key" not in client
               and "Override OpenAI Base URL" in setup.stdout,
               f"client={client} rc={setup.returncode}")
        record("setup journal lists only revert targets",
               journal.get("written") == [str(b.base / "client.json")],
               f"journal={journal}")

        rev = run_bridge_cmd(b.base, "--revert")
        record("--revert removes client.json, keeps token",
               rev.returncode == 0 and not (b.base / "client.json").exists()
               and (b.base / "token").exists(),
               f"rc={rev.returncode} out={rev.stdout!r}")

        code, parsed, _ = request(b.url + "/nope")
        expect_error(code, parsed, 404, "GET unknown path → 404")

        code, parsed, _ = request(b.url + "/v1/models")
        expect_error(code, parsed, 401, "GET /v1/models missing token → 401")

        code, parsed, _ = request(b.url + "/v1/models", token="wrong")
        expect_error(code, parsed, 401, "GET /v1/models short/wrong token → 401")

        code, parsed, _ = request(b.url + "/v1/models", token=token)
        ok = (code == 200 and parsed.get("object") == "list"
              and parsed.get("data", [{}])[0].get("id") == "muse-free-tokens")
        record("GET /v1/models authorized", ok, f"status={code} body={parsed}")

        code, parsed, _ = request(b.url + "/v1/other", method="POST", token=token,
                                 body={"model": "muse-free-tokens"})
        expect_error(code, parsed, 404, "POST unknown path → 404")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST",
            body={"model": "muse-free-tokens",
                  "messages": [{"role": "user", "content": "hi"}]})
        expect_error(code, parsed, 401, "POST completions missing token → 401")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token,
            body={"model": "gpt-4",
                  "messages": [{"role": "user", "content": "hi"}]})
        ok = code == 404 and parsed.get("error", {}).get("type") == "model_not_found"
        record("wrong model → 404 model_not_found", ok, f"status={code} {parsed}")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token,
            body={"model": "muse-free-tokens", "messages": []})
        expect_error(code, parsed, 400, "empty messages → 400")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token,
            body=b"{not json", extra_headers={"Content-Type": "application/json"})
        expect_error(code, parsed, 400, "invalid JSON → 400")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token,
            extra_headers={"Content-Type": "application/json"})
        expect_error(code, parsed, 400, "missing Content-Length → 400")

        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token,
            body=b"{}", extra_headers={
                "Content-Type": "application/json",
                "Content-Length": str(10 * 1024 * 1024 + 1),
            })
        expect_error(code, parsed, 400, "body over 10 MiB → 400")

        code, parsed, _ = request(
            b.url + "/v1/worker/progress", method="POST", token=token,
            body={"line": "no id"})
        expect_error(code, parsed, 400, "progress missing id → 400")

        code, parsed, _ = request(
            b.url + "/v1/worker/reply", method="POST", token=token,
            body={"id": "prm_deadbeefdeadbeef", "content": "nope"})
        expect_error(code, parsed, 404, "reply unknown id → 404")

        bad_calls_id = "prm_" + "ee" * 8
        (b.spool / "working" / f"{bad_calls_id}.json").write_text(
            json.dumps({"id": bad_calls_id, "messages": []}), encoding="utf-8")
        code, parsed, _ = request(
            b.url + "/v1/worker/reply", method="POST", token=token,
            body={"id": bad_calls_id, "content": "x",
                  "tool_calls": {"nope": True}})
        expect_error(code, parsed, 400, "reply tool_calls not an array → 400")
        (b.spool / "working" / f"{bad_calls_id}.json").unlink(missing_ok=True)

        bad_id = "prm_" + "ab" * 8
        (b.spool / "in" / f"{bad_id}.json").write_text("not-json", encoding="utf-8")
        code, _, _ = request(b.url + "/v1/worker/next", token=token)
        record("corrupt inbox JSON is quarantined (204, not orphaned in working/)",
               code == 204
               and (b.spool / "bad" / f"{bad_id}.json").is_file()
               and not (b.spool / "working" / f"{bad_id}.json").exists()
               and not (b.spool / "in" / f"{bad_id}.json").exists(),
               f"status={code} bad={list((b.spool / 'bad').iterdir())}")

        # --- non-streaming text reply ---
        w = MockWorker(b.spool, mode="text", delay=0.2)
        w.start()
        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token, timeout=15,
            body={"model": "muse-free-tokens", "stream": False,
                  "messages": [{"role": "user", "content": "hello"}]})
        w.join(timeout=5)
        content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
        finish = parsed.get("choices", [{}])[0].get("finish_reason")
        usage = parsed.get("usage", {})
        record("non-stream text completion",
               code == 200 and parsed.get("object") == "chat.completion"
               and parsed.get("model") == "muse-free-tokens"
               and content.startswith("hello from worker")
               and finish == "stop"
               and usage.get("total_tokens", 0) >= 2,
               f"status={code} content={content!r} finish={finish} usage={usage}")
        leftover = list((b.spool / "in").glob("*")) + list((b.spool / "out").glob("*"))
        record("spool cleaned after non-stream", leftover == [], f"left={leftover}")

        # --- HTTP worker API ---
        code, parsed, _ = request(b.url + "/v1/worker/next", token=token)
        record("GET /v1/worker/next empty → 204",
               code == 204, f"status={code} body={parsed}")

        code, parsed, _ = request(b.url + "/v1/worker/next")
        expect_error(code, parsed, 401, "GET /v1/worker/next missing token → 401")

        claimed_modes: list[int] = []

        def http_worker_once(progress_line, reply_obj, delay=0.2):
            def run():
                deadline = time.time() + 10
                job = None
                while time.time() < deadline:
                    c, p, _ = request(b.url + "/v1/worker/next", token=token)
                    if c == 200 and isinstance(p, dict) and p.get("id"):
                        job = p
                        break
                    time.sleep(0.05)
                if not job:
                    return
                work = b.spool / "working" / f"{job['id']}.json"
                if work.is_file():
                    claimed_modes.append(mode_of(work))
                time.sleep(delay)
                request(b.url + "/v1/worker/progress", method="POST", token=token,
                        body={"id": job["id"], "line": progress_line})
                request(b.url + "/v1/worker/reply", method="POST", token=token,
                        body={"id": job["id"], **reply_obj})
            t = threading.Thread(target=run, daemon=True)
            t.start()
            return t

        hw = http_worker_once("http worker progress",
                              {"content": "hello via HTTP worker"})
        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token, timeout=15,
            body={"model": "muse-free-tokens", "stream": False,
                  "messages": [{"role": "user", "content": "via http"}]})
        hw.join(timeout=5)
        content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
        record("HTTP worker next+reply completes a turn",
               code == 200 and content == "hello via HTTP worker",
               f"status={code} content={content!r}")
        record("claimed job file is 0600",
               claimed_modes and claimed_modes[0] == 0o600,
               f"modes={claimed_modes}")

        code, parsed, _ = request(b.url + "/healthz", token=token)
        record("healthz last_worker_claim_id after HTTP 200 claim",
               code == 200 and isinstance(parsed.get("last_worker_claim_id"), str)
               and str(parsed.get("last_worker_claim_id")).startswith("prm_"),
               f"body={parsed}")

        hw = http_worker_once("tunnel-safe progress",
                              {"content": "streamed via HTTP"})
        sse, elapsed, curl_rc = stream_request(
            b.url + "/v1/chat/completions", token,
            {"model": "muse-free-tokens", "stream": True,
             "messages": [{"role": "user", "content": "http stream"}]})
        hw.join(timeout=5)
        record("HTTP worker progress streams as SSE comment, not content",
               ": tunnel-safe progress" in sse
               and "streamed via HTTP" in sse
               and '"content": "\\n tunnel-safe progress"' not in sse
               and curl_rc == 0,
               f"elapsed={elapsed:.2f}s")

        prompt_file = b.base / "muse-prompt.txt"
        record("startup wrote muse-prompt.txt",
               prompt_file.is_file()
               and "/v1/worker/next" in prompt_file.read_text(encoding="utf-8")
               and token in prompt_file.read_text(encoding="utf-8"))

        w = MockWorker(b.spool, mode="text", delay=0.25,
                       progress=["Reading the login handler", "Running tests"])
        w.start()
        sse, elapsed, curl_rc = stream_request(
            b.url + "/v1/chat/completions", token,
            {"model": "muse-free-tokens", "stream": True,
             "messages": [{"role": "user", "content": "stream me"}]})
        w.join(timeout=5)
        record("stream does not inject Thinking… into content",
               "Thinking" not in sse)
        record("stream includes progress as SSE comments",
               ": Reading the login handler" in sse and ": Running tests" in sse)
        record("stream includes worker text", "hello from worker" in sse)
        record("stream ends with [DONE]", "data: [DONE]" in sse)
        record("stream chunks are chat.completion.chunk",
               "chat.completion.chunk" in sse)
        record("stream connection closes after [DONE] (no curl hang)",
               curl_rc == 0 and elapsed < 8,
               f"curl_rc={curl_rc} elapsed={elapsed:.2f}s")

        w = MockWorker(b.spool, mode="tools_obj", delay=0.2)
        w.start()
        code, parsed, _ = request(
            b.url + "/v1/chat/completions", method="POST", token=token, timeout=15,
            body={"model": "muse-free-tokens", "stream": False,
                  "messages": [{"role": "user", "content": "compare files"}],
                  "tools": [{"type": "function", "function": {
                      "name": "Read", "parameters": {"type": "object"}}}]})
        w.join(timeout=5)
        msg = parsed.get("choices", [{}])[0].get("message", {})
        finish = parsed.get("choices", [{}])[0].get("finish_reason")
        calls = msg.get("tool_calls") or []
        args0 = calls[0]["arguments"] if calls else ""
        record("non-stream tool_calls (object args normalized to string)",
               code == 200 and finish == "tool_calls" and len(calls) == 2
               and calls[0].get("name") == "Read"
               and isinstance(args0, str) and "/tmp/a.py" in args0,
               f"finish={finish} calls={calls}")

        w = MockWorker(b.spool, mode="tools_str", delay=0.2)
        w.start()
        sse, _, _ = stream_request(
            b.url + "/v1/chat/completions", token,
            {"model": "muse-free-tokens", "stream": True,
             "messages": [{"role": "user", "content": "read c"}],
             "tools": [{"type": "function",
                        "function": {"name": "Read"}}]})
        w.join(timeout=5)
        record("stream tool_calls finish_reason",
               '"finish_reason": "tool_calls"' in sse or '"finish_reason":"tool_calls"' in sse,
               "present" if "tool_calls" in sse else sse[:400])
        record("stream tool_calls keep string arguments",
               "/tmp/c.py" in sse)

        pid = "prm_polltest1234"
        prompt = {
            "id": pid, "created": int(time.time()),
            "model": "muse-free-tokens",
            "messages": [{"role": "user", "content": "from poll"}],
        }
        (b.spool / "in" / f"{pid}.json").write_text(json.dumps(prompt), encoding="utf-8")
        r = subprocess.run(
            ["bash", str(POLL)],
            env={**os.environ, "SPOOL": str(b.spool)},
            capture_output=True, text=True, timeout=5,
        )
        claimed = (b.spool / "working" / f"{pid}.json").exists()
        gone = not (b.spool / "in" / f"{pid}.json").exists()
        record("poll-example.sh claims in/ → working/",
               r.returncode == 0 and claimed and gone and f"claimed {pid}" in r.stdout,
               f"rc={r.returncode} stdout={r.stdout!r} err={r.stderr!r}")
        r2 = subprocess.run(
            ["bash", str(POLL)],
            env={**os.environ, "SPOOL": str(b.spool)},
            capture_output=True, text=True, timeout=5,
        )
        record("poll-example.sh empty inbox is quiet",
               r2.returncode == 0 and r2.stdout.strip() == "",
               f"rc={r2.returncode} stdout={r2.stdout!r}")
        (b.spool / "working" / f"{pid}.json").unlink(missing_ok=True)

        long_line = "x" * 300
        job_id = "prm_" + "cd" * 8
        (b.spool / "working" / f"{job_id}.json").write_text(
            json.dumps({"id": job_id, "messages": []}), encoding="utf-8")
        code, parsed, _ = request(
            b.url + "/v1/worker/progress", method="POST", token=token,
            body={"id": job_id, "line": long_line})
        prog = (b.spool / "progress" / f"{job_id}.jsonl").read_text(encoding="utf-8").strip()
        record("progress line longer than 200 is truncated",
               code == 200 and parsed.get("ok") is True and len(prog) == 200,
               f"status={code} len={len(prog)}")
        (b.spool / "working" / f"{job_id}.json").unlink(missing_ok=True)
        (b.spool / "progress" / f"{job_id}.jsonl").unlink(missing_ok=True)

    finally:
        b.stop()

    t = Bridge(hold_seconds=3)
    token = t.start()
    try:
        t0 = time.time()
        code, parsed, _ = request(
            t.url + "/v1/chat/completions", method="POST", token=token, timeout=15,
            body={"model": "muse-free-tokens", "stream": False,
                  "messages": [{"role": "user", "content": "nobody home"}]})
        elapsed = time.time() - t0
        content = parsed.get("choices", [{}])[0].get("message", {}).get("content", "")
        record("non-stream timeout note when no worker",
               code == 200 and "timed out" in content
               and "/v1/worker/next" in content
               and 2.5 <= elapsed <= 8,
               f"elapsed={elapsed:.1f}s content={content[:80]!r}")

        t0 = time.time()
        sse, elapsed, curl_rc = stream_request(
            t.url + "/v1/chat/completions", token,
            {"model": "muse-free-tokens", "stream": True,
             "messages": [{"role": "user", "content": "still nobody"}]},
            timeout=15)
        record("stream timeout note when no worker",
               curl_rc == 0 and "timed out" in sse and "data: [DONE]" in sse
               and 2.5 <= elapsed <= 8,
               f"elapsed={elapsed:.1f}s curl_rc={curl_rc}")
    finally:
        t.stop()

    wport = free_port()
    wbase = Path(tempfile.mkdtemp(prefix="mft-warn-"))
    try:
        proc = subprocess.Popen(
            [PYTHON, str(BRIDGE), "--host", "0.0.0.0", "--port", str(wport),
             "--base-dir", str(wbase), "--hold-seconds", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        wait_port("127.0.0.1", wport, 5)
        time.sleep(0.2)
        out, err = "", ""
        proc.terminate()
        try:
            out, err = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate(timeout=2)
        record("0.0.0.0 bind prints warning",
               "WARNING" in err and "0.0.0.0" in err,
               f"stderr={err.strip()[:200]!r}")
        url_block = out.split("Cursor / LibreChat base URL")[-1][:80] if out else ""
        record("0.0.0.0 bind prints loopback Cursor URL",
               "http://127.0.0.1:%d/v1" % wport in out
               and "http://0.0.0.0:" not in url_block,
               "loopback URL present" if "http://127.0.0.1:%d/v1" % wport in out else "missing URL")
        token1 = (wbase / "token").read_text(encoding="utf-8").strip()
        port2 = free_port()
        proc2 = subprocess.Popen(
            [PYTHON, str(BRIDGE), "--port", str(port2),
             "--base-dir", str(wbase), "--hold-seconds", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        wait_port("127.0.0.1", port2, 5)
        time.sleep(0.15)
        proc2.terminate()
        try:
            proc2.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.communicate(timeout=2)
        token2 = (wbase / "token").read_text(encoding="utf-8").strip()
        record("second start reuses existing token",
               token1 == token2 and token1.startswith("mft_"),
               f"same={token1 == token2} len={len(token2)}")
    finally:
        shutil.rmtree(wbase, ignore_errors=True)

    # --print-setup does not bind
    live = Bridge(hold_seconds=10)
    live.start()
    try:
        ps = run_bridge_cmd(
            live.base, "--print-setup", "--port", str(live.port),
            "--public-url", "https://print-setup.example",
        )
        record("--print-setup does not bind (same port as listener)",
               ps.returncode == 0 and not ps.stderr
               and "https://print-setup.example" in ps.stdout,
               f"rc={ps.returncode} err={ps.stderr!r}")
    finally:
        live.stop()

    # listen without --public-url must not clobber a filled-in prompt
    keep = Bridge(hold_seconds=8, public_url="https://keep.example")
    keep.start()
    keep.stop(wipe=False)
    try:
        text1 = (keep.base / "muse-prompt.txt").read_text(encoding="utf-8")
        keep2 = Bridge(hold_seconds=8)
        keep2.base = keep.base
        keep2.port = free_port()
        keep2.url = f"http://127.0.0.1:{keep2.port}"
        keep2.start()
        text2 = (keep.base / "muse-prompt.txt").read_text(encoding="utf-8")
        keep2.stop(wipe=False)
        record("listener restart without --public-url keeps filled-in prompt",
               "https://keep.example" in text1 and text1 == text2,
               "rewritten" if text1 != text2 else "kept")
    finally:
        shutil.rmtree(keep.base, ignore_errors=True)

    # --doctor / --revert must not create a token
    empty = Path(tempfile.mkdtemp(prefix="mft-empty-"))
    try:
        doc = run_bridge_cmd(empty, "--doctor", "--port", str(free_port()))
        record("--doctor on empty dir does not create a token",
               doc.returncode == 1 and not (empty / "token").exists(),
               f"rc={doc.returncode} files={list(empty.iterdir())}")
        rev = run_bridge_cmd(empty, "--revert")
        record("--revert on empty dir does not create a token",
               rev.returncode == 0 and not (empty / "token").exists(),
               f"rc={rev.returncode} files={list(empty.iterdir())}")
    finally:
        shutil.rmtree(empty, ignore_errors=True)

    # empty token file is an error
    blank = Path(tempfile.mkdtemp(prefix="mft-blank-"))
    try:
        (blank / "token").write_text("   \n", encoding="utf-8")
        r = run_bridge_cmd(blank, "--port", str(free_port()), "--hold-seconds", "2",
                           timeout=5)
        record("empty token file refuses to listen",
               r.returncode == 1 and "empty" in (r.stderr + r.stdout).lower(),
               f"rc={r.returncode} err={r.stderr!r} out={r.stdout[:200]!r}")
    finally:
        shutil.rmtree(blank, ignore_errors=True)

    # concurrent first-run token
    race_dir = Path(tempfile.mkdtemp(prefix="mft-race-"))
    p1, p2 = free_port(), free_port()
    try:
        a = subprocess.Popen(
            [PYTHON, str(BRIDGE), "--port", str(p1), "--base-dir", str(race_dir),
             "--hold-seconds", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        bproc = subprocess.Popen(
            [PYTHON, str(BRIDGE), "--port", str(p2), "--base-dir", str(race_dir),
             "--hold-seconds", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        up1 = wait_port("127.0.0.1", p1, 8)
        up2 = wait_port("127.0.0.1", p2, 8)
        token_race = (race_dir / "token").read_text(encoding="utf-8").strip() if (race_dir / "token").exists() else ""
        for proc in (a, bproc):
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=2)
        record("concurrent first-run shares one token and both listen",
               up1 and up2 and token_race.startswith("mft_"),
               f"up1={up1} up2={up2} token_len={len(token_race)}")
    finally:
        shutil.rmtree(race_dir, ignore_errors=True)

    # doctor against 0.0.0.0 listener uses 127.0.0.1
    bound = Bridge(hold_seconds=10, host="0.0.0.0")
    bound.start()
    try:
        request(bound.url + "/v1/worker/next", token=bound.token)
        doc = run_bridge_cmd(
            bound.base, "--doctor", "--host", "0.0.0.0", "--port", str(bound.port),
        )
        record("doctor on --host 0.0.0.0 still reaches loopback /healthz",
               doc.returncode == 0 and "Doctor result: ready" in doc.stdout,
               f"rc={doc.returncode} out={redact(doc.stdout)!r}")
    finally:
        bound.stop()

    port_dir = Path(tempfile.mkdtemp(prefix="mft-port-"))
    try:
        r0 = run_bridge_cmd(port_dir, "--port", "0", "--print-setup")
        rhi = run_bridge_cmd(port_dir, "--port", "70000", "--print-setup")
        record("port 0 is rejected",
               r0.returncode == 2 and "1-65535" in r0.stderr,
               f"rc={r0.returncode}")
        record("port 70000 is rejected",
               rhi.returncode == 2 and "1-65535" in rhi.stderr,
               f"rc={rhi.returncode}")
    finally:
        shutil.rmtree(port_dir, ignore_errors=True)

    limited = Bridge(hold_seconds=4, extra_args=["--max-holds", "1"])
    limited_token = limited.start()
    try:
        def _hold() -> None:
            request(
                limited.url + "/v1/chat/completions", method="POST",
                token=limited_token, timeout=12,
                body={"model": "muse-free-tokens", "stream": False,
                      "messages": [{"role": "user", "content": "hold"}]})
        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()
        time.sleep(0.4)
        code, parsed, _ = request(
            limited.url + "/v1/chat/completions", method="POST",
            token=limited_token, timeout=5,
            body={"model": "muse-free-tokens", "stream": False,
                  "messages": [{"role": "user", "content": "second"}]})
        record("max-holds 1 rejects a second wait with 429",
               code == 429
               and parsed.get("error", {}).get("type") == "rate_limit_error",
               f"status={code} body={parsed}")
        holder.join(timeout=8)
    finally:
        limited.stop()

    stale = Bridge(hold_seconds=8, extra_args=["--worker-stale-seconds", "1"])
    stale.start()
    try:
        request(stale.url + "/v1/worker/next", token=stale.token)
        time.sleep(1.5)
        doc = run_bridge_cmd(
            stale.base, "--doctor", "--port", str(stale.port),
            "--worker-stale-seconds", "1",
        )
        record("doctor stale window is an error",
               doc.returncode == 1 and "stale" in doc.stdout,
               f"rc={doc.returncode} out={redact(doc.stdout)!r}")
    finally:
        stale.stop()

    print()
    print(f"{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
