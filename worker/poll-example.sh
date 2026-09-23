#!/usr/bin/env bash
# poll-example.sh — optional local-filesystem skeleton.
#
# Muse itself should use GET /v1/worker/next (see WORKER.md), not this.
# This script is only for a worker that already runs on the same machine
# as the bridge and can see the spool directory.
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
