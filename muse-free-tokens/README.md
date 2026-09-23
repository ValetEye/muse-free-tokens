# muse-free-tokens

An OpenAI-compatible local endpoint whose "model" is **your own Muse bot**.
Point Cursor, LibreChat, or anything speaking the OpenAI chat API at it, and
your bot answers — inference with no per-token API bill, beyond the Muse
subscription you already pay for.

```
Cursor / LibreChat / curl
        │  POST /v1/chat/completions (OpenAI shape, SSE streaming)
        ▼
   bridge.py  ── files prompt in spool/in/
        │  holds your client's connection open with keepalives
        ▼
   your Muse agent ── claims it, does the work, drops the reply
        │  in spool/out/ (+ live progress lines)
        ▼
   bridge streams the reply back ── text or tool_calls, then [DONE]
```

Single user. Localhost by default. One Python file, **zero dependencies**
(stdlib only). Your prompts never leave your machine except to reach your
own bot.

## Install (about 5 minutes)

**1. Requirements:** Python 3.9+ and a Muse account/bot you control. That's it.

```bash
git clone https://github.com/ValetEye/muse-free-tokens.git
cd muse-free-tokens
python3 bridge.py
```

First run prints your endpoint URL, model name, and a generated bearer token
(saved to `~/.muse-free-tokens/token` — keep it secret). Leave it running.

**2. Connect your Muse agent** as the worker — this is the important step.
Read [`worker/WORKER.md`](worker/WORKER.md): it specifies the spool-dir
mailbox protocol (claim prompt → write reply → optional live progress) and
gives you a standing agent prompt to install in whatever scheduled-agent /
hook system your Muse runs on. `worker/poll-example.sh` is a scan-and-claim
skeleton to wire into it.

**3. Point a client at it.**

*Cursor* — Settings → Models → Override OpenAI Base URL:
`http://127.0.0.1:3733/v1`, add custom model `muse-free-tokens`, paste your
bearer token as the API key.

*LibreChat* — add a custom OpenAI-compatible endpoint with base URL
`http://127.0.0.1:3733/v1`, model `muse-free-tokens`, and your token.

*curl* —
```bash
TOKEN=$(cat ~/.muse-free-tokens/token)
curl -N http://127.0.0.1:3733/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"model":"muse-free-tokens","stream":true,
       "messages":[{"role":"user","content":"hello"}]}'
```

## How a turn feels

The stream opens instantly (`Thinking…`), then — if your agent posts them —
live progress lines (`Reading the login handler`, `Running tests`) arrive as
real deltas while the work happens. Text answers stream word-by-word;
`tool_calls` stream back in OpenAI shape for the client to execute (the
results come back as a new prompt — the normal agent loop). The bridge holds
the connection up to 10 minutes per turn (`--hold-seconds`); if your agent
never answers, the client gets a plain-English timeout note instead of a hang.

## Going beyond localhost (optional)

`bridge.py --host 0.0.0.0` listens on all interfaces — it prints a loud
warning when you do, because anyone with the token can spend your
subscription. Prefer a tunnel instead: `cloudflared tunnel --url
http://127.0.0.1:3733` gives you a public HTTPS URL with the server still
bound to localhost.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Stream opens, then timeout note | your worker agent isn't picking up prompts — check it's watching the spool dir |
| `401` | wrong/missing bearer token (`cat ~/.muse-free-tokens/token`) |
| `404 model_not_found` | model id must be exactly `muse-free-tokens` |
| Address already in use | another bridge (or something else) on 3733 — `--port` to change |
| Slow first answer | the first turn includes agent startup; later turns reuse the warm loop |

## Files

- `bridge.py` — the whole server. Read it; it's ~450 lines and commented.
- `worker/WORKER.md` — the agent contract (the important doc).
- `worker/poll-example.sh` — scan-and-claim skeleton.
- `TOS-NOTE.md` — why the author believes this complies with the Muse Terms
  of Service **as of 2026-09-22**, and your obligation to re-verify. **Read it
  before using.**
- `LICENSE` — MIT.

## Disclaimer

This is an independent project, not affiliated with or endorsed by Meta.
You are responsible for your own compliance — see `TOS-NOTE.md`.
