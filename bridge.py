#!/usr/bin/env python3
"""
muse-free-tokens: an OpenAI-compatible local endpoint whose "model"
is your own Muse bot.

Your tools (Cursor, LibreChat, anything speaking the OpenAI chat API) send
prompts here. The bridge files each prompt in a spool directory; your Muse
agent picks it up, does the work, and drops the reply back. The bridge
streams the reply to the waiting client. Inference costs you nothing beyond
the Muse subscription you already pay for.

Single user, localhost-first, stdlib only. No dependencies to install.

Quickstart:
    python3 bridge.py              # listen; prints Cursor settings + Muse prompt
    python3 bridge.py --setup      # write client.json + prompt, then exit
    python3 bridge.py --doctor     # check token, prompt, /healthz, last poll

Spool layout (<base>/spool/):
    in/<id>.json        new prompts (worker claims by renaming to working/)
    working/<id>.json   claimed prompts
    out/<id>.json       replies (worker writes tmp file, then renames)
    progress/<id>.jsonl live status lines, streamed to the client as SSE comments
"""

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
try:
    from urllib.request import Request, urlopen
    from urllib.error import URLError
except ImportError:
    Request = None
    urlopen = None
    URLError = OSError

MODEL_ID = "muse-free-tokens"
DEFAULT_PORT = 3733            # F-R-E-E on a phone keypad
POLL_INTERVAL = 1.0            # how often the waiter checks the spool
KEEPALIVE_EVERY = 15           # SSE comment ping cadence (seconds)
DEFAULT_HOLD = 600             # max seconds to hold a client connection
DEFAULT_MAX_HOLDS = 8          # concurrent chat completions
PROGRESS_LINE_MAX = 200        # hard cap on worker progress lines
DIR_MODE = 0o700
FILE_MODE = 0o600


# ---------------------------------------------------------------- config

def port_number(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer")
    if n < 1 or n > 65535:
        raise argparse.ArgumentTypeError("port must be 1-65535")
    return n


def positive_int(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer")
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def parse_args():
    p = argparse.ArgumentParser(description="muse-free-tokens bridge")
    p.add_argument("--port", type=port_number, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default localhost; use 0.0.0.0 only "
                        "behind your own auth/TLS)")
    p.add_argument("--base-dir", default=os.path.join(os.path.expanduser("~"),
                                                      ".muse-free-tokens"),
                   help="state dir (token + spool)")
    p.add_argument("--hold-seconds", type=positive_int, default=DEFAULT_HOLD,
                   help="max seconds to hold a client connection open")
    p.add_argument("--max-holds", type=positive_int, default=DEFAULT_MAX_HOLDS,
                   help="max concurrent /v1/chat/completions waits")
    p.add_argument("--worker-stale-seconds", type=positive_int, default=90,
                   help="doctor: treat last worker poll older than this as error")
    p.add_argument("--public-url", default="",
                   help="URL Muse should poll (cloudflared/Tailscale). "
                        "Written into the paste-ready prompt. Cursor still "
                        "uses http://127.0.0.1:<port>/v1")
    p.add_argument("--print-setup", action="store_true",
                   help="print the Muse prompt and Cursor settings, then exit")
    p.add_argument("--setup", action="store_true",
                   help="write client.json + Muse prompt, print Cursor steps, exit")
    p.add_argument("--revert", action="store_true",
                   help="remove files written by --setup (not the token), then exit")
    p.add_argument("--doctor", action="store_true",
                   help="check token, prompt, /healthz, and last worker poll, then exit")
    return p.parse_args()


ARGS = parse_args()
SPOOL = os.path.join(ARGS.base_dir, "spool")
TOKEN_FILE = os.path.join(ARGS.base_dir, "token")
CLIENT_FILE = os.path.join(ARGS.base_dir, "client.json")
JOURNAL_FILE = os.path.join(ARGS.base_dir, "setup-journal.json")
PROMPT_FILE = os.path.join(ARGS.base_dir, "muse-prompt.txt")
WORKER_STALE_SECONDS = ARGS.worker_stale_seconds
SERVER_VERSION = "1.0"
BEARER = None
TOKEN_IS_NEW = False
HOLD_SEMA = threading.BoundedSemaphore(ARGS.max_holds)


def chmod_private(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def ensure_private_dir(path):
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    chmod_private(path, DIR_MODE)
    return path


def write_private_text(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    chmod_private(path, FILE_MODE)
    return path


def write_private_json(path, obj):
    return write_private_text(path, json.dumps(obj, indent=2) + "\n")


def write_json_atomic(final, obj):
    tmp = final + ".tmp"
    write_private_text(tmp, json.dumps(obj))
    os.rename(tmp, final)
    chmod_private(final, FILE_MODE)
    return final


def append_private_line(path, line):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)
    chmod_private(path, FILE_MODE)


def ensure_dirs(sweep=False):
    ensure_private_dir(ARGS.base_dir)
    ensure_private_dir(SPOOL)
    for sub in ("in", "working", "out", "progress", "bad"):
        ensure_private_dir(os.path.join(SPOOL, sub))
    if not sweep:
        return
    cutoff = time.time() - 3600
    for sub in ("in", "working", "out", "progress", "bad"):
        d = os.path.join(SPOOL, sub)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            path = os.path.join(d, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def read_token_file():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def load_or_create_token():
    """Return (token, created). Exit if the file stays empty."""
    ensure_private_dir(ARGS.base_dir)
    deadline = time.time() + 2
    while True:
        token = read_token_file()
        if token:
            chmod_private(TOKEN_FILE, FILE_MODE)
            return token, False
        try:
            token = "mft_" + secrets.token_urlsafe(32)
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
            chmod_private(TOKEN_FILE, FILE_MODE)
            return token, True
        except FileExistsError:
            if time.time() >= deadline:
                print("token file is empty: %s" % TOKEN_FILE, file=sys.stderr)
                sys.exit(1)
            time.sleep(0.02)


def use_token(create):
    """Set BEARER. create=False never writes a token file."""
    global BEARER, TOKEN_IS_NEW
    if create:
        BEARER, TOKEN_IS_NEW = load_or_create_token()
        return BEARER
    token = read_token_file()
    BEARER = token or None
    TOKEN_IS_NEW = False
    return BEARER


# ---------------------------------------------------------------- helpers

def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg), flush=True)


def sse_send(wfile, payload):
    wfile.write(("data: %s\n\n" % payload).encode())
    wfile.flush()


def sse_comment(wfile, text):
    line = text.replace("\n", " ").replace("\r", " ").strip()
    wfile.write((": %s\n\n" % line).encode())
    wfile.flush()


def sse_chunk(completion_id, created, delta=None, finish_reason=None,
              usage=None):
    choice = {"index": 0, "delta": delta or {}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    else:
        choice["finish_reason"] = None
    obj = {"id": completion_id, "object": "chat.completion.chunk",
           "created": created, "model": MODEL_ID, "choices": [choice]}
    if usage is not None:
        obj["usage"] = usage
    return json.dumps(obj)


def estimate_usage(prompt_text, completion_text):
    # rough token estimates; clients expect the field, exact counts are
    # unknowable here since inference happens in the user's own bot
    pt = max(1, len(prompt_text) // 4)
    ct = max(1, len(completion_text) // 4)
    return {"prompt_tokens": pt, "completion_tokens": ct,
            "total_tokens": pt + ct}


def cleanup_spool(prompt_id):
    for sub in ("in", "working", "out"):
        try:
            os.remove(os.path.join(SPOOL, sub, prompt_id + ".json"))
        except OSError:
            pass
    try:
        os.remove(os.path.join(SPOOL, "progress", prompt_id + ".jsonl"))
    except OSError:
        pass


PROMPT_ID_RE = re.compile(r"^prm_[0-9a-f]+$")


def valid_prompt_id(pid):
    return isinstance(pid, str) and bool(PROMPT_ID_RE.match(pid))


def public_base():
    url = (ARGS.public_url or "").strip().rstrip("/")
    return url or "YOUR_PUBLIC_URL"


def muse_prompt_text():
    base = public_base()
    return """You are the inference worker behind muse-free-tokens.

Muse runs in a cloud VM and cannot see the user's laptop. Talk to the
bridge over HTTP only. Do not look for a local spool directory.

Base URL: {base}
Authorization: Bearer {token}

Loop, every 20 seconds or whenever you are idle:
  GET {base}/v1/worker/next
  Header: Authorization: Bearer {token}

- HTTP 204: nothing waiting. Stop until the next poll.
- HTTP 200: a job JSON with id, messages, and maybe tools.

While you work:
  POST {base}/v1/worker/progress
  {{"id": "<id>", "line": "short status under 80 chars"}}

When the turn is done:
  POST {base}/v1/worker/reply
  {{"id": "<id>", "content": "final answer"}}
or, to call client tools:
  {{"id": "<id>", "content": "one-line status",
    "tool_calls": [{{"id": "call_1", "name": "Read", "arguments": {{"path": "..."}}}}]}}

Rules:
- Batch independent tool calls. Do not emit a lone exploratory call.
- Never delete data, drop databases, rewrite git history, or exfiltrate secrets.
- Never print the bearer token.
- Stay silent on routine polls. Speak up only if the bridge is broken.

If the base URL is still YOUR_PUBLIC_URL, ask the user to run
`cloudflared tunnel --url http://127.0.0.1:{port}` (or Tailscale) and
give you the https URL, then use that as the base.
""".format(base=base, token=BEARER, port=ARGS.port)


def write_muse_prompt():
    ensure_private_dir(ARGS.base_dir)
    return write_private_text(PROMPT_FILE, muse_prompt_text())


def claim_next():
    inbox = os.path.join(SPOOL, "in")
    try:
        names = sorted(os.listdir(inbox))
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json"):
            continue
        pid = name[:-5]
        if not valid_prompt_id(pid):
            continue
        src = os.path.join(inbox, name)
        dest = os.path.join(SPOOL, "working", name)
        try:
            os.rename(src, dest)
        except OSError:
            continue
        try:
            with open(dest, encoding="utf-8") as fh:
                job = json.load(fh)
        except (ValueError, OSError):
            _quarantine_job(dest, name)
            continue
        if not isinstance(job, dict):
            _quarantine_job(dest, name)
            continue
        job["id"] = pid
        return job
    return None


def _quarantine_job(path, name):
    bad = os.path.join(SPOOL, "bad", name)
    try:
        ensure_private_dir(os.path.join(SPOOL, "bad"))
        os.rename(path, bad)
        chmod_private(bad, FILE_MODE)
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def working_path(prompt_id):
    return os.path.join(SPOOL, "working", prompt_id + ".json")


def append_progress(prompt_id, line):
    path = os.path.join(SPOOL, "progress", prompt_id + ".jsonl")
    append_private_line(path, line.replace("\n", " ").strip() + "\n")


def write_reply(prompt_id, payload):
    final = os.path.join(SPOOL, "out", prompt_id + ".json")
    write_json_atomic(final, payload)


# last successful authorized GET /v1/worker/next (204 or 200), this process only
_state_lock = threading.Lock()
LAST_WORKER_SEEN = None
LAST_WORKER_CLAIM_ID = None
LAST_WORKER_CLAIM_AT = None


def note_worker_poll(claimed_id=None):
    global LAST_WORKER_SEEN, LAST_WORKER_CLAIM_ID, LAST_WORKER_CLAIM_AT
    now = time.time()
    with _state_lock:
        LAST_WORKER_SEEN = now
        if claimed_id:
            LAST_WORKER_CLAIM_ID = claimed_id
            LAST_WORKER_CLAIM_AT = now


def spool_counts():
    counts = {}
    for sub in ("in", "working", "out", "progress"):
        d = os.path.join(SPOOL, sub)
        n = 0
        try:
            for name in os.listdir(d):
                if name.endswith(".tmp"):
                    continue
                n += 1
        except OSError:
            n = 0
        counts[sub] = n
    return counts


def health_payload(detailed=False):
    payload = {
        "service": "muse-free-tokens",
        "status": "ok",
        "version": SERVER_VERSION,
        "accepting": True,
    }
    if not detailed:
        return payload
    with _state_lock:
        seen = LAST_WORKER_SEEN
        claim_id = LAST_WORKER_CLAIM_ID
        claim_at = LAST_WORKER_CLAIM_AT
    now = time.time()
    ago = None if seen is None else int(now - seen)
    payload.update({
        "model": MODEL_ID,
        "last_worker_seen": None if seen is None else int(seen),
        "last_worker_seen_seconds_ago": ago,
        "last_worker_claim_id": claim_id,
        "last_worker_claim_at": None if claim_at is None else int(claim_at),
        "queue": spool_counts(),
    })
    return payload


def file_mode(path):
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def local_base_url():
    host = ARGS.host if ARGS.host not in ("0.0.0.0", "::") else "127.0.0.1"
    if ":" in host and not host.startswith("["):
        return "http://[%s]:%d/v1" % (host, ARGS.port)
    return "http://%s:%d/v1" % (host, ARGS.port)


def write_client_file():
    return write_private_json(CLIENT_FILE, {
        "base_url": local_base_url(),
        "model": MODEL_ID,
        "api_key_file": TOKEN_FILE,
    })


def run_setup():
    use_token(create=True)
    ensure_dirs(sweep=False)
    prompt_path = write_muse_prompt()
    client_path = write_client_file()
    write_private_json(JOURNAL_FILE, {
        "written": [client_path],
        "created": int(time.time()),
    })
    print_setup(write_prompt=False)
    print()
    print("Wrote %s (base URL + model; key stays in the token file)."
          % client_path)
    print("Prompt: %s" % prompt_path)
    print("Cursor stores Override OpenAI Base URL in the app, not settings.json.")
    print("  Cursor Settings → Models")
    print("  → enable Override OpenAI Base URL → %s" % local_base_url())
    print("  → OpenAI API key = contents of %s" % TOKEN_FILE)
    print("  → Add custom model exactly: %s" % MODEL_ID)
    print("While the override is on, Cursor sends OpenAI-family models through")
    print("this bridge. Turn the override off to use Cursor-hosted models again.")
    print("If a local endpoint fails TLS/HTTP2: Settings → Network → HTTP/1.1.")


def run_revert():
    removed = []
    for path in (CLIENT_FILE, JOURNAL_FILE):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    print("revert: removed %s" % (", ".join(removed) if removed else "nothing"))
    print("Token and muse-prompt.txt were left in place.")
    print("In Cursor, turn off Override OpenAI Base URL yourself.")


def _check(status, message, detail=None):
    return {"status": status, "message": message, "detail": detail}


def doctor_health_url():
    host = ARGS.host
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    elif host == "localhost":
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        netloc = "[%s]:%d" % (host, ARGS.port)
    else:
        netloc = "%s:%d" % (host, ARGS.port)
    return "http://%s/healthz" % netloc


def run_doctor():
    checks = []
    token = use_token(create=False)

    mode = file_mode(TOKEN_FILE)
    if mode is None:
        checks.append(_check("error", "token file is missing: %s" % TOKEN_FILE))
    elif not token:
        checks.append(_check("error", "token file is empty: %s" % TOKEN_FILE))
    elif mode & 0o077:
        checks.append(_check("error",
                             "token file is readable by others (%03o): %s"
                             % (mode, TOKEN_FILE)))
    else:
        checks.append(_check("ok", "token file is mode %03o" % mode))

    dmode = file_mode(ARGS.base_dir)
    if dmode is None:
        checks.append(_check("error", "base dir is missing: %s" % ARGS.base_dir))
    elif dmode & 0o077:
        checks.append(_check("error",
                             "base dir is accessible by others (%03o): %s"
                             % (dmode, ARGS.base_dir)))
    else:
        checks.append(_check("ok", "base dir is mode %03o" % dmode))

    pmode = file_mode(PROMPT_FILE)
    if pmode is None:
        checks.append(_check("error",
                             "muse-prompt.txt is missing; run --setup or start the bridge"))
    elif pmode & 0o077:
        checks.append(_check("error",
                             "muse-prompt.txt is readable by others (%03o)" % pmode))
    else:
        checks.append(_check("ok", "muse-prompt.txt is mode %03o" % pmode))
        try:
            with open(PROMPT_FILE, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            text = ""
        if "YOUR_PUBLIC_URL" in text:
            checks.append(_check("warning",
                                 "muse-prompt.txt still has YOUR_PUBLIC_URL — "
                                 "Muse cannot reach 127.0.0.1"))
        elif token and token in text:
            checks.append(_check("ok", "muse-prompt.txt contains this machine's token"))
        else:
            checks.append(_check("warning",
                                 "muse-prompt.txt does not contain the current token"))

    if os.path.exists(CLIENT_FILE):
        checks.append(_check("ok", "client.json is present"))
    else:
        checks.append(_check("warning",
                             "client.json missing; run --setup to write Cursor values"))

    health_url = doctor_health_url()
    if urlopen is None or Request is None:
        checks.append(_check("error", "urllib is unavailable; cannot probe /healthz"))
        health = None
    else:
        try:
            headers = {}
            if token:
                headers["Authorization"] = "Bearer %s" % token
            raw = urlopen(Request(health_url, headers=headers), timeout=2).read()
            health = json.loads(raw.decode("utf-8"))
        except (URLError, OSError, ValueError, UnicodeDecodeError) as exc:
            checks.append(_check("error",
                                 "bridge is not reachable at %s" % health_url,
                                 str(exc)))
            health = None
        else:
            if health.get("service") != "muse-free-tokens":
                checks.append(_check("error",
                                     "port %d is another service" % ARGS.port,
                                     json.dumps(health)))
            else:
                checks.append(_check("ok",
                                     "bridge /healthz ok on %s" % health_url
                                     .replace("http://", "").replace("/healthz", "")))
    if health and health.get("service") == "muse-free-tokens":
        if "last_worker_seen_seconds_ago" not in health:
            checks.append(_check("error",
                                 "authorized /healthz did not return worker status "
                                 "(is the token valid?)"))
        else:
            ago = health.get("last_worker_seen_seconds_ago")
            if ago is None:
                checks.append(_check("error",
                                     "Muse has not polled GET /v1/worker/next "
                                     "since this process started"))
            elif ago >= WORKER_STALE_SECONDS:
                checks.append(_check("error",
                                     "Muse has not polled in %ss "
                                     "(stale after %ss)"
                                     % (ago, WORKER_STALE_SECONDS)))
            else:
                checks.append(_check("ok", "last worker poll %ss ago" % ago))
            q = health.get("queue") or {}
            checks.append(_check("ok",
                                 "queue in=%s working=%s out=%s"
                                 % (q.get("in"), q.get("working"), q.get("out"))))

    icons = {"ok": "ok ", "warning": " ! ", "error": "err"}
    for c in checks:
        line = "%s %s" % (icons[c["status"]], c["message"])
        print(line)
        if c.get("detail"):
            print("     %s" % c["detail"])
    ok = not any(c["status"] == "error" for c in checks)
    print("Doctor result: %s" % ("ready" if ok else "not ready"))
    return ok


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "muse-free-tokens/1.0"

    def log_message(self, *a):
        pass  # keep stdout for our own log lines

    # -- plumbing -------------------------------------------------
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _error(self, code, message, err_type="invalid_request_error"):
        self._send_json(code, {"error": {"message": message, "type": err_type}})

    def _bearer_ok(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        presented = auth[len("Bearer "):].strip()
        if not BEARER or not presented:
            return False
        try:
            return secrets.compare_digest(presented, BEARER)
        except (ValueError, TypeError):
            return False

    def _authorized(self):
        if not self._bearer_ok():
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._error(401, "missing bearer token", "authentication_error")
            else:
                self._error(401, "invalid bearer token", "authentication_error")
            return False
        return True

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length <= 0 or length > 10 * 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # -- routes ---------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/models":
            if not self._authorized():
                return
            self._send_json(200, {
                "object": "list",
                "data": [{"id": MODEL_ID, "object": "model",
                          "created": int(time.time()),
                          "owned_by": "local"}],
            })
            return
        if path == "/v1/worker/next":
            self._worker_next()
            return
        if path == "/healthz":
            self._send_json(200, health_payload(detailed=self._bearer_ok()))
            return
        if path == "/":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._error(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/v1/worker/progress":
            self._worker_progress()
            return
        if path == "/v1/worker/reply":
            self._worker_reply()
            return
        if path != "/v1/chat/completions":
            self._error(404, "not found")
            return
        if not self._authorized():
            return
        if not HOLD_SEMA.acquire(blocking=False):
            self._error(429,
                         "too many in-flight requests (max %d)"
                         % ARGS.max_holds,
                         "rate_limit_error")
            return
        prompt_id = None
        try:
            body = self._read_json()
            if body is None:
                self._error(400, "invalid or missing JSON body")
                return
            model = body.get("model", "")
            if model != MODEL_ID:
                self._error(404,
                             "model '%s' not found - this endpoint serves only "
                             "'%s'" % (model, MODEL_ID),
                             "model_not_found")
                return
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                self._error(400, "'messages' must be a non-empty array")
                return
            stream = bool(body.get("stream", False))
            tools = body.get("tools")

            prompt_id = "prm_" + uuid.uuid4().hex[:16]
            prompt_text = "\n".join(
                str(m.get("content", "")) for m in messages
                if isinstance(m, dict))
            spool_obj = {"id": prompt_id, "created": int(time.time()),
                         "model": MODEL_ID, "messages": messages}
            if tools is not None:
                spool_obj["tools"] = tools
            final = os.path.join(SPOOL, "in", prompt_id + ".json")
            write_json_atomic(final, spool_obj)
            log("queued %s (%d messages, %d tools, stream=%s)"
                % (prompt_id, len(messages),
                   len(tools) if isinstance(tools, list) else 0, stream))

            completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]
            created = int(time.time())
            if stream:
                self._serve_stream(prompt_id, completion_id, created,
                                   prompt_text)
            else:
                self._serve_once(prompt_id, completion_id, created,
                                 prompt_text)
        finally:
            if prompt_id:
                cleanup_spool(prompt_id)
            HOLD_SEMA.release()

    # -- waiting --------------------------------------------------
    def _wait_for_reply(self, prompt_id, on_progress=None):
        """Poll the spool until the worker drops a reply.

        Returns the parsed reply dict, the string "timeout", or None if the
        client disconnected. Calls on_progress(text) for each new progress
        line (streaming mode only).
        """
        deadline = time.time() + ARGS.hold_seconds
        out_path = os.path.join(SPOOL, "out", prompt_id + ".json")
        prog_path = os.path.join(SPOOL, "progress", prompt_id + ".jsonl")
        prog_offset = 0
        last_ping = time.time()
        streaming = on_progress is not None
        while time.time() < deadline:
            # Drain progress before treating a reply as final, so a fast
            # worker that writes both in the same poll window still streams
            # its status lines.
            if streaming and os.path.exists(prog_path):
                try:
                    with open(prog_path, encoding="utf-8") as fh:
                        fh.seek(prog_offset)
                        for line in fh:
                            line = line.strip()
                            if line:
                                on_progress(line)
                        prog_offset = fh.tell()
                except OSError:
                    pass
            if os.path.exists(out_path):
                try:
                    with open(out_path, encoding="utf-8") as fh:
                        reply = json.load(fh)
                    if isinstance(reply, dict):
                        return reply
                except (ValueError, OSError):
                    pass  # worker mid-write; rename is atomic so retry
            if streaming and time.time() - last_ping >= KEEPALIVE_EVERY:
                try:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return None
                last_ping = time.time()
            time.sleep(POLL_INTERVAL)
        return "timeout"

    # -- non-streaming --------------------------------------------
    def _serve_once(self, prompt_id, completion_id, created, prompt_text):
        reply = self._wait_for_reply(prompt_id)
        if reply is None or reply == "timeout":
            content = ("(the request timed out waiting for your Muse bot - "
                       "is it polling GET /v1/worker/next? see the prompt in "
                       "~/.muse-free-tokens/muse-prompt.txt)")
            finish, tool_calls = "stop", None
        else:
            content, finish, tool_calls = self._interpret_reply(reply)
        message = {"role": "assistant", "content": content or ""}
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = estimate_usage(prompt_text, content or "")
        self._send_json(200, {
            "id": completion_id, "object": "chat.completion",
            "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "message": message,
                         "finish_reason": finish}],
            "usage": usage,
        })
        log("delivered %s (%d chars)" % (prompt_id, len(content or "")))

    # -- streaming ------------------------------------------------
    def _serve_stream(self, prompt_id, completion_id, created, prompt_text):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # Close after [DONE]. keep-alive leaves curl/urllib hanging on an
        # open socket with no Content-Length (the documented client path).
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(delta=None, finish_reason=None, usage=None):
            sse_send(self.wfile,
                     sse_chunk(completion_id, created, delta=delta,
                               finish_reason=finish_reason, usage=usage))

        def on_progress(text):
            sse_comment(self.wfile, text)

        try:
            emit(delta={"role": "assistant"})

            reply = self._wait_for_reply(prompt_id, on_progress=on_progress)
            if reply is None:
                log("client disconnected for %s" % prompt_id)
                return
            if reply == "timeout":
                self._stream_text(emit, completion_id, created,
                                  "(the request timed out waiting for your "
                                  "Muse bot - is it polling GET "
                                  "/v1/worker/next? see the prompt in "
                                  "~/.muse-free-tokens/muse-prompt.txt)",
                                  prompt_text)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                log("timeout guard fired for %s" % prompt_id)
                return
            content, finish, tool_calls = self._interpret_reply(reply)
            if tool_calls:
                if content:
                    self._stream_text(emit, completion_id, created, content,
                                      prompt_text, final=False)
                for i, call in enumerate(tool_calls):
                    emit(delta={"tool_calls": [{
                        "index": i, "id": call.get("id", "call_%d" % i),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", "{}"),
                        }}]})
                usage = estimate_usage(prompt_text, content or "")
                emit(finish_reason="tool_calls", usage=usage)
                log("delivered %d tool_calls for %s"
                    % (len(tool_calls), prompt_id))
            else:
                self._stream_text(emit, completion_id, created,
                                  content or "", prompt_text)
                log("delivered reply for %s (%d chars)"
                    % (prompt_id, len(content or "")))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("client disconnected for %s" % prompt_id)

    @staticmethod
    def _stream_text(emit, completion_id, created, text, prompt_text,
                     final=True):
        for i in range(0, len(text), 120):
            emit(delta={"content": text[i:i + 120]})
        if final:
            usage = estimate_usage(prompt_text, text)
            emit(finish_reason="stop", usage=usage)

    @staticmethod
    def _interpret_reply(reply):
        """Normalize the worker's reply file.

        Returns (content, finish_reason, tool_calls|None). Accepts:
          {"content": "..."}                                  -> text answer
          {"content": "...", "tool_calls": [{id,name,arguments}]} -> tool calls
        arguments may be an object or a JSON string.
        """
        content = reply.get("content") or ""
        raw_calls = reply.get("tool_calls")
        if not raw_calls:
            return content, "stop", None
        calls = []
        for n, call in enumerate(raw_calls):
            if not isinstance(call, dict):
                continue
            args = call.get("arguments", {})
            if not isinstance(args, str):
                args = json.dumps(args)
            calls.append({
                "id": call.get("id") or "call_%d" % n,
                "name": call.get("name", ""),
                "arguments": args,
            })
        if not calls:
            return content, "stop", None
        return content, "tool_calls", calls

    # -- muse worker (HTTP mailbox; Muse cannot see the local spool) --
    def _worker_next(self):
        if not self._authorized():
            return
        job = claim_next()
        if job is None:
            note_worker_poll()
            self._send_empty(204)
            return
        note_worker_poll(job.get("id"))
        log("claimed %s via HTTP worker" % job.get("id", "?"))
        self._send_json(200, job)

    def _worker_progress(self):
        if not self._authorized():
            return
        body = self._read_json()
        if not isinstance(body, dict):
            self._error(400, "invalid or missing JSON body")
            return
        pid = body.get("id")
        line = body.get("line")
        if not valid_prompt_id(pid) or not isinstance(line, str) or not line.strip():
            self._error(400, "need {id, line}")
            return
        if not os.path.exists(working_path(pid)):
            self._error(404, "unknown or unclaimed prompt id")
            return
        line = line.replace("\n", " ").replace("\r", " ").strip()
        if len(line) > PROGRESS_LINE_MAX:
            line = line[:PROGRESS_LINE_MAX]
        append_progress(pid, line)
        self._send_json(200, {"ok": True})

    def _worker_reply(self):
        if not self._authorized():
            return
        body = self._read_json()
        if not isinstance(body, dict):
            self._error(400, "invalid or missing JSON body")
            return
        pid = body.get("id")
        if not valid_prompt_id(pid):
            self._error(400, "need a prompt id")
            return
        if not os.path.exists(working_path(pid)):
            self._error(404, "unknown or unclaimed prompt id")
            return
        payload = {"content": body.get("content") or ""}
        raw_calls = body.get("tool_calls")
        if raw_calls:
            if not isinstance(raw_calls, list):
                self._error(400, "tool_calls must be an array")
                return
            payload["tool_calls"] = raw_calls
        write_reply(pid, payload)
        log("reply via HTTP worker for %s" % pid)
        self._send_json(200, {"ok": True})


# ---------------------------------------------------------------- main

def print_setup(write_prompt=True):
    if write_prompt:
        prompt_path = write_muse_prompt()
    else:
        prompt_path = PROMPT_FILE
    print("muse-free-tokens")
    print("  Cursor / LibreChat base URL : %s" % local_base_url())
    print("  model                       : %s" % MODEL_ID)
    if TOKEN_IS_NEW:
        print("  token (new, keep secret)    : %s" % BEARER)
        print("                                saved to %s" % TOKEN_FILE)
    else:
        print("  token                       : (saved in %s)" % TOKEN_FILE)
    print("  Muse prompt                 : %s" % prompt_path)
    print()
    print("Setup")
    print("  1. Leave the bridge running on this machine.")
    print("  2. Muse cannot see localhost. Give it a URL it can reach:")
    print("       cloudflared tunnel --url http://127.0.0.1:%d" % ARGS.port)
    if public_base() == "YOUR_PUBLIC_URL":
        print("     Then re-print the prompt with that URL filled in:")
        print("       python3 bridge.py --print-setup --public-url https://YOUR-TUNNEL")
    else:
        print("     Public URL in the prompt: %s" % public_base())
    print("     Paste the prompt file into Muse as standing / scheduled")
    print("     instructions. Muse polls GET /v1/worker/next — no VPS,")
    print("     no spool directory.")
    print("  3. Point Cursor at the local URL (or run: python3 bridge.py --setup).")
    print("  4. python3 bridge.py --doctor   # after Muse has polled once")
    sys.stdout.flush()


def main():
    if ARGS.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to %s exposes your bot to the network. "
              "Anyone with the bearer token can spend your Muse subscription. "
              "Prefer localhost or put this behind your own TLS/auth."
              % ARGS.host, file=sys.stderr)
    if ARGS.revert:
        run_revert()
        return
    if ARGS.doctor:
        sys.exit(0 if run_doctor() else 1)
    if ARGS.setup:
        run_setup()
        return
    use_token(create=True)
    ensure_dirs(sweep=not ARGS.print_setup)
    # Rewrite the prompt only when missing, or when the user passed a URL.
    # A bare listener restart must not clobber --setup --public-url.
    should_write = (bool((ARGS.public_url or "").strip())
                    or not os.path.exists(PROMPT_FILE)
                    or ARGS.print_setup)
    print_setup(write_prompt=should_write)
    if ARGS.print_setup:
        return
    server = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    server.daemon_threads = True
    print()
    print("listening on %s:%d" % (ARGS.host, ARGS.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
    finally:
        try:
            server.server_close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
