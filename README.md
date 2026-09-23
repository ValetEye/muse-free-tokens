<p align="center">
  <img
    src="assets/banner.jpg"
    alt="Muse Free Tokens — a cute moose valet with a monocle holds a single gold key between a warm local HTTP box and a bright cloud window, under the ValetEye mark and the title Muse Free Tokens"
    width="100%"
  >
</p>

# Muse Free Tokens

The goal of this repo is to let **other apps** use **your Muse
subscription**. Cursor, LibreChat, curl, or anything that speaks the
OpenAI chat API can send prompts here. Muse does the inference. This
does **not** give you free tokens to spend *inside* Muse. It is the
opposite: Muse is the side you already pay for, and other apps ride it.

A **local, single-user** OpenAI-compatible HTTP bridge. The only model it
serves is `muse-free-tokens`. Behind that name is **your own** Muse agent,
on **your own** Muse account, answering **your own** prompts.

```
Cursor / LibreChat / curl                 Your Muse agent (Meta cloud VM)
        │                                          │
        │  POST /v1/chat/completions               │  GET  /v1/worker/next
        │  GET  /v1/models                         │  POST /v1/worker/progress
        ▼                                          │  POST /v1/worker/reply
   bridge.py  (127.0.0.1:3733 by default)          │
        │                                          │
        └── same bearer token, via a URL Muse ─────┘
            can reach (tunnel / Tailscale / …)
```

This is **not** [Meta Model API](https://ai.developer.meta.com/)
(`https://api.meta.ai/v1`, Muse Spark, metered tokens). It does not call
Muse over any official inference API. Muse has no documented consumer API
for this, so the bridge is a mailbox: it holds the client connection and
waits for *you* to have Muse poll in and write a reply.

One Python file, stdlib only. Default bind is loopback. MIT.

**Read [TOS-NOTE.md](TOS-NOTE.md) and the live
[Muse Supplemental Terms](https://muse.ai/terms) before using.** This
project is not affiliated with or endorsed by Meta.

## What this is not

- Not a public or shared “free Muse API.” Each person runs their own
  bridge against their own account. Sharing the token or the tunnel URL
  lets whoever has them spend that account.
- Not a VPS product. A VPS works if you want one; it is not required.
- Not installed Muse. Muse runs in Meta’s cloud VM. Nothing named Muse
  is started by `bridge.py`.
- Not a local-folder integration for Muse. Muse cannot see
  `~/.muse-free-tokens/spool/` on your laptop. The HTTP worker API is
  the supported path.

## How a turn works

1. A client `POST`s `/v1/chat/completions` with `model` exactly
   `muse-free-tokens` and a non-empty `messages` array. Optional `tools`
   (OpenAI function-definition shape) are stored with the job.
2. The bridge writes a job file under its spool (implementation detail)
   and, if `stream` is true, immediately sends an SSE role chunk
   (`assistant`). It does **not** put status text into `content`.
3. Muse — or any worker that has the bearer token — `GET`s
   `/v1/worker/next`. **204** means empty inbox; **200** is a claimed
   job (`id`, `created`, `model`, `messages`, optional `tools`). The
   oldest waiting `prm_*.json` is claimed.
4. Optional `POST /v1/worker/progress` lines are streamed as SSE
   comments (`: line`). They do not join the assistant message.
   Lines longer than 200 characters are truncated.
5. `POST /v1/worker/reply` with `{id, content}` and optional
   `tool_calls` finishes the turn. `arguments` may be an object or a
   JSON string; the bridge normalizes them to a string for the client.
6. Streaming ends with a finish chunk, then `data: [DONE]`, then the
   connection closes (`Connection: close`). Non-streaming returns one
   `chat.completion` JSON object.
7. If nothing replies within `--hold-seconds` (default **600**), the
   client gets a plain-English timeout that tells it to check
   `/v1/worker/next` and `~/.muse-free-tokens/muse-prompt.txt`. The
   connection does not hang forever.

While waiting, streaming connections also get an SSE comment
`: ping` every 15 seconds so intermediaries do not drop the socket.

`usage` on the response is a rough estimate (`len(text) // 4`), not
Muse’s real token count.

## Requirements

- Python **3.9+** (`python3`). No pip packages.
- A Muse account and agent **you** control.
- A URL Muse can open. `127.0.0.1` is not that URL. Typical options:
  - `cloudflared tunnel --url http://127.0.0.1:3733` (quick tunnel;
    hostname changes every time you restart `cloudflared`)
  - a named Cloudflare Tunnel, Tailscale Serve, or another tunnel you
    already use
  - `--host 0.0.0.0` on a machine Muse can route to (LAN/VPS). This
    prints a warning: anyone with the token can use your subscription.

`cloudflared` is **not** bundled. Install it yourself if you use it.

## Install

```bash
git clone https://github.com/ValetEye/muse-free-tokens.git
cd muse-free-tokens
python3 bridge.py                 # leave this running
# other terminal, after you have a public URL:
python3 bridge.py --setup --public-url https://YOUR-TUNNEL
python3 bridge.py --doctor        # ready only after Muse has polled once
```

`--setup` writes files and prints Cursor click-path, then **exits**.
`--doctor` exits `0` if ready, `1` if any check is `error`. Neither
listens on the port. `--doctor` and `--revert` never create a token.

The listening process creates `~/.muse-free-tokens/` (or `--base-dir`)
at mode `0700`:

| Path | Mode | What |
|------|------|------|
| (base dir) | `0700` | Token, prompt, spool. chmod'd on listen/`--setup`. |
| `token` | `0600` | Bearer token, `mft_` + `secrets.token_urlsafe(32)`. Created once, reused. Empty file is an error. |
| `muse-prompt.txt` | `0600` | Standing instructions for Muse, including the token. |
| `client.json` | `0600` | Written by `--setup`: `base_url`, `model`, `api_key_file`. No raw key. |
| `setup-journal.json` | `0600` | Paths `--revert` will delete (`client.json` only). |
| `spool/{in,working,out,progress,bad}/` | `0700` | Job mailbox. Files are `0600`. Do not point Muse at these. |

The **listening** process deletes spool files older than **one hour**.
`--setup` and `--print-setup` do not sweep the spool.

A listener start **does not overwrite** `muse-prompt.txt` unless the
file is missing or you passed `--public-url`. `--setup` and
`--print-setup` always rewrite it.

### 1. Give Muse a reachable URL

In another terminal (example using a quick Cloudflare tunnel):

```bash
cloudflared tunnel --url http://127.0.0.1:3733
```

Copy the `https://….trycloudflare.com` URL. Refresh the prompt **without**
stopping the bridge:

```bash
python3 bridge.py --print-setup --public-url https://YOUR-TUNNEL.trycloudflare.com
```

`--print-setup` writes `muse-prompt.txt` and prints Cursor settings, then
**exits**. It does not take over port 3733.

You can also pass `--public-url` on the process that listens:

```bash
python3 bridge.py --public-url https://YOUR-TUNNEL.trycloudflare.com
```

If you omit `--public-url`, the prompt file contains the placeholder
`YOUR_PUBLIC_URL` and tells Muse to ask you for a tunnel.

### 2. Install the worker prompt in Muse

Paste `~/.muse-free-tokens/muse-prompt.txt` into Muse as standing or
scheduled instructions, using whatever mechanism your account provides.
The generated prompt tells Muse to poll `GET <public>/v1/worker/next`
about every 20 seconds with the bearer token, post progress, then post
exactly one reply.

A quick tunnel hostname **changes when you restart `cloudflared`**.
Re-run `--print-setup --public-url …` and update Muse.

The “~40 seconds per tool round” line in the prompt is advice to Muse
(agent latency), not a limit the bridge enforces.

### 3. Point the client at loopback, not the tunnel

Clients on this machine should use the local URL. Muse uses the public
URL. Same token for both.

**Cursor** — `--setup` cannot write these into Cursor. Override OpenAI
Base URL lives in the app UI, not a supported `settings.json` key.

- Cursor Settings → Models
- Enable **Override OpenAI Base URL** → `http://127.0.0.1:3733/v1`
- OpenAI API key → contents of `~/.muse-free-tokens/token`
- Add custom model exactly `muse-free-tokens`

While the override is on, Cursor sends OpenAI-family models through this
bridge, including built-in picker models. Turn the override **off** to
use Cursor-hosted models again. If a local URL fails TLS/HTTP2: Settings
→ Network → HTTP Compatibility Mode → HTTP/1.1.

Menu labels move; the three values above do not. `client.json` is a
copy of those values for you or LibreChat.

**LibreChat** — custom OpenAI-compatible endpoint, same three values.

**curl**

```bash
TOKEN=$(cat ~/.muse-free-tokens/token)
curl -N http://127.0.0.1:3733/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"muse-free-tokens","stream":true,
       "messages":[{"role":"user","content":"hello"}]}'
```

Until something claims `/v1/worker/next` and posts a reply, a streamed
turn stays open (role chunk, then `: ping` keepalives) and then times
out after `--hold-seconds`.

## Command-line flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--host` | `127.0.0.1` | Bind address. `0.0.0.0` / non-loopback prints a stderr warning. `localhost` and `::1` count as loopback for that warning. |
| `--port` | `3733` | Listen port 1–65535 (F-R-E-E on a phone keypad). |
| `--base-dir` | `~/.muse-free-tokens` | Token, prompt file, and spool. |
| `--hold-seconds` | `600` | Max seconds to wait for a worker reply. Must be >= 1. |
| `--max-holds` | `8` | Max concurrent `/v1/chat/completions` waits. Extra requests get `429`. |
| `--worker-stale-seconds` | `90` | `--doctor`: last authorized worker poll older than this is an error. |
| `--public-url` | empty | URL written into `muse-prompt.txt` for Muse. Does not change the Cursor URL. |
| `--print-setup` | off | Write the prompt file, print settings, exit. |
| `--setup` | off | Write `client.json` + prompt, print Cursor steps, exit. |
| `--revert` | off | Delete `client.json` and `setup-journal.json`. Does not delete the token or prompt. Does not change Cursor. |
| `--doctor` | off | Check token/prompt/dir modes, `YOUR_PUBLIC_URL`, authorized `/healthz`, and last worker poll. Exit `1` on any error. Does not create a token. |

## HTTP surface

Unauthorized requests (missing or wrong `Authorization: Bearer …`)
return `401` with `error.type` = `authentication_error`, except `GET /`
and `GET /healthz`. A wrong-length token is `401`, not `500`.

| Method | Path | Auth | Result |
|--------|------|------|--------|
| `GET` | `/` | no | `ok` only. Does not name the product or its routes. |
| `GET` | `/healthz` | no | `{service, status, version, accepting}` only. **Never includes the token, queue, or claim id.** |
| `GET` | `/healthz` | yes | Also `model`, `last_worker_seen` (Unix time of the last authorized `GET /v1/worker/next` in **this process**, or `null`), `last_worker_seen_seconds_ago`, `last_worker_claim_id`, `queue`. |
| `GET` | `/v1/models` | yes | `{object: list, data: [{id: muse-free-tokens, …}]}` |
| `POST` | `/v1/chat/completions` | yes | Completion or SSE stream. |
| `GET` | `/v1/worker/next` | yes | `204` empty, or `200` claimed job. |
| `POST` | `/v1/worker/progress` | yes | `{id, line}` — `404` if `id` is not in `working/`. |
| `POST` | `/v1/worker/reply` | yes | `{id, content, tool_calls?}` — same `404` rule. |

`POST /v1/chat/completions` also:

- `400` if the body is missing, not JSON, larger than 10 MiB, or
  `messages` is missing / not a non-empty array.
- `404` `model_not_found` if `model` is not exactly `muse-free-tokens`.
- `429` `rate_limit_error` if `--max-holds` in-flight completions are
  already waiting.
- `404` for any other path.

Worker `POST /v1/worker/reply` returns `400` if `tool_calls` is present
and is not an array.

Worker `id` values must match `prm_` + hex. Full request and reply
shapes: [worker/WORKER.md](worker/WORKER.md).

The server is `ThreadingHTTPServer` (`muse-free-tokens/1.0`). It accepts
concurrent client and worker requests.

## Filesystem mailbox (optional)

The HTTP worker API is what Muse should use. The spool is how the
process stores jobs on disk. A **local** script (not Muse) may instead:

1. `rename` `spool/in/<id>.json` → `spool/working/<id>.json`
2. Append lines to `spool/progress/<id>.jsonl`
3. Write `spool/out/<id>.json` via a temp file + rename

[worker/poll-example.sh](worker/poll-example.sh) only does step 1.

## Security

- The base directory and spool directories are `0700`. Token, prompt,
  `client.json`, and job files are `0600`. The prompt file **contains
  the token**. Do not commit, paste into public chats, or sync that
  folder to a shared drive.
- `client.json` stores `base_url`, `model`, and `api_key_file` only —
  never the raw key.
- The loopback server is reachable by other processes as the same OS
  user.
- A tunnel or `--host 0.0.0.0` makes the same API reachable remotely.
  The bearer token is the only gate on worker and chat routes.
  Unauthenticated `/healthz` is a liveness stub only.
- The bridge does not log the token after the first-run line (and on
  first run it prints the new token once).

## Terms

The author’s reading of the Muse Supplemental Terms of Service
(https://muse.ai/terms, page text “Last updated: September 8, 2026”) is
in [TOS-NOTE.md](TOS-NOTE.md). **That note is not legal advice.**

The live page names headings (“Restrictions”, “Using Connected
Services”, “User Responsibility”, “Enforcement, Termination and
Suspension” / Section 17, “Changes to These Terms”). It does **not**
print decimal Restriction cites such as `§4.6`. Treat those numbers in
TOS-NOTE.md as the author’s count of Restriction bullets, not labels
Meta printed. The Restriction that forbids distillation is worded
“Distill, or attempt to distill, Muse”, not “extract weights.”

Meta also sells a separate, official Model API. Using this bridge as a
stand-in for that API is a risk you take yourself. If the terms change,
or you are unsure, stop. Do not share or resell access. Section 17 of
the Supplemental Terms allows Meta to suspend or terminate access
without prior notice.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Stream opens, then a timeout note | Nothing is claiming `/v1/worker/next` in time. Run `--doctor`. Prompt not installed, Muse still on `YOUR_PUBLIC_URL` / `127.0.0.1`, or the tunnel hostname changed. |
| Muse still has `YOUR_PUBLIC_URL` after `--setup` | You restarted the listener without `--public-url` on an **older** build that rewrote the prompt. This build keeps the file. Re-run `--print-setup --public-url …` and paste it again. |
| `--doctor` says Muse has not polled | No authorized `GET /v1/worker/next` since this process started, or last poll older than `--worker-stale-seconds` (default 90). |
| `429` | `--max-holds` (default 8) in-flight completions are already waiting. |
| `--doctor` says bridge is not reachable | The listen process is not running on that `--port`. |
| Cursor built-in models fail after setup | Override OpenAI Base URL is global. Turn it off for Cursor-hosted models. |
| `401` | Wrong or missing bearer token. `cat ~/.muse-free-tokens/token`. |
| `404 model_not_found` | `model` must be exactly `muse-free-tokens`. |
| Address already in use | Something (often another `bridge.py`) is on 3733. Use `--port` or stop the other process. |
| Muse cannot connect | Tunnel process died, or it is polling the Cursor loopback URL. |
| `--print-setup` and the server both fail to bind | You omitted `--print-setup` on the second command, so it tried to listen too. |
| `curl -N` hangs after `[DONE]` | You are on a build from before the stream used `Connection: close`. Update `bridge.py`. |
| Slow first answer | Muse agent startup on that side; later polls can be faster. Not a bridge warmup. |

`GET /healthz` and `python3 bridge.py --doctor` are the supported
liveness checks. There is no MCP connector, outbound tunnel client, or
automatic Cursor settings writer in this tree.

## Repository layout

- `assets/banner.jpg` — README / GitHub social banner.
- `bridge.py` — the server and setup printer.
- `tests/run_tests.py` — isolated end-to-end suite (`python3 tests/run_tests.py`).
- `.github/workflows/test.yml` — CI: Python 3.9 and 3.12 run that suite.
- `worker/WORKER.md` — worker contract.
- `worker/poll-example.sh` — optional local claim skeleton.
- `TOS-NOTE.md` — author’s terms reading (see caveats above).
- `LICENSE` — MIT, Copyright (c) 2026 ValetEye.

## Disclaimer

Independent project, not affiliated with or endorsed by Meta. You are
responsible for your own compliance and for whatever your agent does
with a job.
