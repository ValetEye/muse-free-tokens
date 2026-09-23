# Worker contract

Muse runs in a cloud VM. It cannot see `~/.muse-free-tokens/spool/` on the
user's laptop. **Use the HTTP worker API.** The spool directory is an
implementation detail (and an optional local-filesystem fallback).

Default bridge: `http://127.0.0.1:3733` (Cursor). Muse needs a public URL
in front of that — `cloudflared tunnel --url http://127.0.0.1:3733` or
Tailscale — plus the same bearer token.

A filled-in standing prompt is written to `~/.muse-free-tokens/muse-prompt.txt`
on every start, and by `python3 bridge.py --print-setup --public-url URL`.

## HTTP API (preferred)

All routes require `Authorization: Bearer <token>`.

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/worker/next` | claim the oldest waiting prompt |
| `POST` | `/v1/worker/progress` | live status line for the waiting client |
| `POST` | `/v1/worker/reply` | final text or tool_calls |

### `GET /v1/worker/next`

- **204** — inbox empty. Sleep until the next poll (~20s).
- **200** — you now own this job. Shape:

```json
{
  "id": "prm_9f3c…",
  "created": 1758570000,
  "model": "muse-free-tokens",
  "messages": [
    {"role": "system", "content": "…"},
    {"role": "user", "content": "refactor the login handler"}
  ],
  "tools": [
    {"type": "function", "function": {
      "name": "Read",
      "description": "Read a file",
      "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
    }}
  ]
}
```

`tools` is whatever the client declared. It may be absent.

### `POST /v1/worker/progress`

```json
{"id": "prm_9f3c…", "line": "Reading the login handler"}
```

Keep lines short (under ~80 chars), a handful per turn. The bridge
truncates at 200 characters. Each line is sent to the waiting client as
an SSE comment (`: line`), not as assistant `content`.

### `POST /v1/worker/reply`

Exactly one of:

**A. Final text answer**
```json
{"id": "prm_9f3c…", "content": "Done. The login handler now…"}
```

**B. Tool calls** (the client executes them and sends the results back as a
new prompt — the classic agent loop)
```json
{
  "id": "prm_9f3c…",
  "content": "Reading the two files to compare implementations.",
  "tool_calls": [
    {"id": "call_1", "name": "Read", "arguments": {"path": "/abs/path/a.py"}},
    {"id": "call_2", "name": "Read", "arguments": {"path": "/abs/path/b.py"}}
  ]
}
```

- `name` must be one of the tools declared in the prompt's `tools` array.
- `arguments` may be an object or a JSON string; the bridge normalizes it.
- `content` is a one-line status shown while the client runs your calls.

The bridge holds the client's HTTP connection open (up to `--hold-seconds`,
default 600s) with SSE keepalives. If you never reply, the client gets a
friendly timeout note — not a hang.

## Standing prompt

`bridge.py` generates this for you. The important constraints, if you write
your own:

> You are the inference worker behind `muse-free-tokens`. Poll
> `GET <public>/v1/worker/next` with the bearer token every ~20 seconds.
> 204 means stop until the next poll. 200 is a job: read `messages` and
> `tools`, POST progress lines, then POST exactly one reply (tool calls or a
> final answer). Batch independent tool calls — every round costs the user
> ~40 seconds. Never emit a lone exploratory call. Never delete data, drop
> databases, rewrite git history, or exfiltrate secrets. Never print the
> bearer token. Stay silent on routine polls.

## Filesystem mailbox (optional)

Only useful if something on the *same machine* as the bridge is the worker
(a local script, not Muse itself). Layout under `--base-dir`/spool:

| Dir         | Who writes | Contents                              |
|-------------|------------|---------------------------------------|
| `in/`       | bridge     | new prompts as `<id>.json`            |
| `working/`  | worker     | prompts claimed (rename from `in/`)   |
| `out/`      | worker     | replies as `<id>.json`                |
| `progress/` | worker     | optional live status lines `<id>.jsonl` |

Claim with an atomic rename `in/<id>.json` → `working/<id>.json`. Write the
reply to a temp file in `out/`, then rename. `poll-example.sh` is a skeleton
for that path.
