# Worker contract

The bridge never talks to your Muse bot directly — there is no API for that.
Instead it uses a **spool directory as a mailbox**. Your job is to run a Muse
agent that watches the mailbox, answers prompts, and drops replies back.

Default spool location: `~/.muse-free-tokens/spool/` (overridable with
`bridge.py --base-dir`).

## Mailbox layout

| Dir         | Who writes | Contents                              |
|-------------|------------|---------------------------------------|
| `in/`       | bridge     | new prompts as `<id>.json`            |
| `working/`  | your agent | prompts you claimed (rename from `in/`) |
| `out/`      | your agent | replies as `<id>.json`                |
| `progress/` | your agent | optional live status lines `<id>.jsonl` |

## The prompt file (`in/<id>.json`)

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

`tools` is whatever the client declared (OpenAI function-definition shape).
It may be absent — then this is a plain chat turn.

## Claiming a prompt

**Atomic rename** `in/<id>.json` → `working/<id>.json`. If the source is
already gone, another worker claimed it first — move on. (The reference
design is single-worker; the rename makes multi-worker safe anyway.)

## The reply file (`out/<id>.json`)

Write to a temp file in the same directory, then rename — the bridge treats
an observable file as complete. Exactly one of:

**A. Final text answer**
```json
{"content": "Done. The login handler now…"}
```

**B. Tool calls** (the client executes them and sends the results back as a
new prompt — the classic agent loop)
```json
{
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

## Live progress (optional, recommended)

While you work, append plain-text lines to `progress/<id>.jsonl`:

```
Reading the login handler
Found the bug — editing now
Running the test suite
```

Each line streams to the client as it arrives, so a minutes-long turn looks
alive instead of stalled. Keep lines short (under ~80 chars), a handful per
turn. The bridge deletes the file when the turn completes.

## The agent prompt (adapt to your setup)

Your Muse agent needs standing instructions roughly like this. Adapt names
and mechanics to whatever scheduled-agent / hook system your Muse runs on:

> You are the inference worker behind the `muse-free-tokens` endpoint.
> Watch `<spool>/in/` for new `<id>.json` files. For each: claim it with an
> atomic rename to `<spool>/working/`, read `messages` (the conversation)
> and `tools` (function definitions the client offers).
>
> Decide the turn's move and reply with exactly one `out/<id>.json`:
> (a) tool calls, or (b) a final text answer, in the shapes from WORKER.md.
> Batch independent tool calls into one reply — every round costs the user
> ~40 seconds. Never emit a lone exploratory call when you could include its
> likely follow-ups. When the task is done, send the final answer immediately;
> no "just to check" extra rounds. Post progress lines to
> `progress/<id>.jsonl` at natural beats. Never emit calls that delete data,
> drop databases, rewrite git history, or exfiltrate secrets.
>
> Stay silent on routine turns. Speak up only if the bridge itself is broken.

## Wiring it up

How the agent gets woken depends on your Muse runtime:

- **Scheduled / polling agent** (recommended): run your agent on a short
  interval; each run it scans `in/`, claims everything pending, processes
  each, and exits. `poll-example.sh` in this folder is a skeleton showing
  the scan-and-claim logic — wire its "new prompt" branch into your agent's
  wake mechanism.
- **Filesystem watcher**: `inotifywait`/`fswatch` on `in/` for instant pickup
  instead of polling.

The bridge holds the client's HTTP connection open (up to `--hold-seconds`,
default 600s) with SSE keepalives, so slow agent turns are fine. If the
agent never answers, the client gets a friendly timeout note — not a hang.
