#!/usr/bin/env bash
# poll-example.sh — skeleton: scan the spool for new prompts.
#
# This is NOT a complete worker. It shows the scan-and-claim mechanics;
# you must wire the "new prompt claimed" branch into whatever wakes your
# Muse agent (scheduled agent, hook system, cron that launches an agent
# run, etc.). See worker/WORKER.md for the full contract.
#
# Usage: SPOOL=~/.muse-free-tokens/spool ./poll-example.sh
set -euo pipefail

SPOOL="${SPOOL:-$HOME/.muse-free-tokens/spool}"

for prompt in "$SPOOL"/in/*.json; do
    [ -e "$prompt" ] || continue            # empty dir: glob didn't match
    id="$(basename "$prompt" .json)"

    # atomic claim: if the rename fails, someone else took it
    if mv "$prompt" "$SPOOL/working/$id.json" 2>/dev/null; then
        echo "claimed $id"
        # >>> YOUR AGENT WAKE GOES HERE <<<
        # e.g. launch your Muse agent run with:
        #   prompt file: "$SPOOL/working/$id.json"
        #   reply file : "$SPOOL/out/$id.json"   (write tmp, then rename)
        #   progress   : "$SPOOL/progress/$id.jsonl" (append lines)
        #
        # The agent must follow worker/WORKER.md for reply shapes.
    fi
done
