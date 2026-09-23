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
    python3 bridge.py              # prints your bearer token and URL
    # wire your Muse agent to the spool dir (see worker/WORKER.md)
    # point your client at http://127.0.0.1:3733/v1, model muse-free-tokens

Spool layout (<base>/spool/):
    in/<id>.json        new prompts (worker claims by renaming to working/)
    working/<id>.json   claimed prompts
    out/<id>.json       replies (worker writes tmp file, then renames)
    progress/<id>.jsonl live status lines, streamed to the client as deltas
"""

import argparse
import json
import os
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "muse-free-tokens"
DEFAULT_PORT = 3733            # F-R-E-E on a phone keypad
POLL_INTERVAL = 1.0            # how often the waiter checks the spool
KEEPALIVE_EVERY = 15           # SSE comment ping cadence (seconds)
DEFAULT_HOLD = 600             # max seconds to hold a client connection


# ---------------------------------------------------------------- config

def parse_args():
    p = argparse.ArgumentParser(description="muse-free-tokens bridge")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default localhost; use 0.0.0.0 only "
                        "behind your own auth/TLS)")
    p.add_argument("--base-dir", default=os.path.join(os.path.expanduser("~"),
                                                      ".muse-free-tokens"),
                   help="state dir (token + spool)")
    p.add_argument("--hold-seconds", type=int, default=DEFAULT_HOLD,
                   help="max seconds to hold a client connection open")
    return p.parse_args()


ARGS = parse_args()
SPOOL = os.path.join(ARGS.base_dir, "spool")
TOKEN_FILE = os.path.join(ARGS.base_dir, "token")


def ensure_dirs():
    for sub in ("in", "working", "out", "progress"):
        os.makedirs(os.path.join(SPOOL, sub), exist_ok=True)
    # drop stale spool files left by dead runs (older than an hour)
    cutoff = time.time() - 3600
    for sub in ("in", "working", "out", "progress"):
        d = os.path.join(SPOOL, sub)
        for name in os.listdir(d):
            path = os.path.join(d, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass


def load_or_create_token():
    os.makedirs(ARGS.base_dir, exist_ok=True)
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as fh:
            return fh.read().strip(), False
    token = "mft_" + secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return token, True


BEARER, TOKEN_IS_NEW = load_or_create_token()


# ---------------------------------------------------------------- helpers

def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg), flush=True)


def sse_send(wfile, payload):
    wfile.write(("data: %s\n\n" % payload).encode())
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

    def _error(self, code, message, err_type="invalid_request_error"):
        self._send_json(code, {"error": {"message": message, "type": err_type}})

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            self._error(401, "missing bearer token", "authentication_error")
            return False
        if not secrets.compare_digest(auth[len("Bearer "):].strip(), BEARER):
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
        if path == "/":
            body = (b"muse-free-tokens bridge is running. "
                    b"See /v1/models and POST /v1/chat/completions.")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._error(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._error(404, "not found")
            return
        if not self._authorized():
            return
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
        tmp = os.path.join(SPOOL, "in", prompt_id + ".tmp")
        final = os.path.join(SPOOL, "in", prompt_id + ".json")
        with open(tmp, "w") as fh:
            json.dump(spool_obj, fh)
        os.rename(tmp, final)  # atomic publish
        log("queued %s (%d messages, %d tools, stream=%s)"
            % (prompt_id, len(messages),
               len(tools) if isinstance(tools, list) else 0, stream))

        completion_id = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        try:
            if stream:
                self._serve_stream(prompt_id, completion_id, created,
                                   prompt_text)
            else:
                self._serve_once(prompt_id, completion_id, created,
                                 prompt_text)
        finally:
            cleanup_spool(prompt_id)

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
        last_ping = 0
        streaming = on_progress is not None
        while time.time() < deadline:
            if os.path.exists(out_path):
                try:
                    with open(out_path) as fh:
                        reply = json.load(fh)
                    if isinstance(reply, dict):
                        return reply
                except (ValueError, OSError):
                    pass  # worker mid-write; rename is atomic so retry
            if streaming and os.path.exists(prog_path):
                try:
                    with open(prog_path) as fh:
                        fh.seek(prog_offset)
                        for line in fh:
                            line = line.strip()
                            if line:
                                on_progress(line)
                        prog_offset = fh.tell()
                except OSError:
                    pass
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
                       "is your worker agent running? see worker/WORKER.md)")
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
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(delta=None, finish_reason=None, usage=None):
            sse_send(self.wfile,
                     sse_chunk(completion_id, created, delta=delta,
                               finish_reason=finish_reason, usage=usage))

        def on_progress(text):
            emit(delta={"content": "\n" + text})

        try:
            # open the stream immediately so the client knows we're alive
            emit(delta={"role": "assistant"})
            emit(delta={"content": "Thinking…"})

            reply = self._wait_for_reply(prompt_id, on_progress=on_progress)
            if reply is None:
                log("client disconnected for %s" % prompt_id)
                return
            if reply == "timeout":
                self._stream_text(emit, completion_id, created,
                                  "(the request timed out waiting for your "
                                  "Muse bot - is your worker agent running? "
                                  "see worker/WORKER.md)", prompt_text)
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


# ---------------------------------------------------------------- main

def main():
    if ARGS.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to %s exposes your bot to the network. "
              "Anyone with the bearer token can spend your Muse subscription. "
              "Prefer localhost or put this behind your own TLS/auth."
              % ARGS.host, file=sys.stderr)
    ensure_dirs()
    server = ThreadingHTTPServer((ARGS.host, ARGS.port), Handler)
    print("muse-free-tokens bridge listening on %s:%d"
          % (ARGS.host, ARGS.port))
    print("endpoint : http://%s:%d/v1" % (ARGS.host, ARGS.port))
    print("model    : %s" % MODEL_ID)
    print("spool    : %s" % SPOOL)
    if TOKEN_IS_NEW:
        print("token    : %s  (new - saved to %s, keep it secret)"
              % (BEARER, TOKEN_FILE))
    else:
        print("token    : (saved in %s)" % TOKEN_FILE)
    print("Next: connect your Muse agent to the spool dir - see "
          "worker/WORKER.md")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")


if __name__ == "__main__":
    main()
