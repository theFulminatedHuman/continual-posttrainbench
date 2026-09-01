# Step 9 — template diffs

Four edits. Apply in order; each has a check.

---

## 1. `src/stream/stream_server.py` — release eval slices too

The agent needs the eval slice for stages it has completed, so it can measure
retention itself. Same gate, same ordering guarantee.

In `release()`, immediately after the `shutil.copy(src, dst / "train.jsonl")` line:

```python
    ev = stream_dir / f"stage_{t}" / "eval.jsonl"
    if ev.exists():
        shutil.copy(ev, dst / "eval.jsonl")
        os.chmod(dst / "eval.jsonl", 0o444)
```

Check:
```bash
export STREAM_VAULT=/tmp/gate/vault STREAM_WORKSPACE=/tmp/gate/ws
rm -rf /tmp/gate && mkdir -p /tmp/gate/vault /tmp/gate/ws
cp -r src/stream/pools/gsm8k/stream_domain_blocked /tmp/gate/vault/shards
echo '{"replay":"none","model_family":"qwen","replay_buffer_rows":null}' > /tmp/gate/policy.json
python src/stream/stream_server.py release --stage 0 \
    --stream /tmp/gate/vault/shards --policy /tmp/gate/policy.json
ls /tmp/gate/ws/stream/stage_0/    # expect train.jsonl AND eval.jsonl
```

---

## 2. `src/harbor_adapter/template/environment/Dockerfile`

Append at the end, after the existing `COPY .`:

```dockerfile
# --- continual stream gate ---------------------------------------------
# The vault holds every stage's shard. It is root-owned and mode 700 so the
# agent (running unprivileged) cannot read ahead. Only the daemon, running as
# root, moves a shard into the workspace once the gate conditions are met.
COPY vault /opt/stream/shards
COPY stream_server.py /opt/stream/stream_server.py
COPY policy.json /opt/stream/policy.json
RUN chown -R root:root /opt/stream && chmod -R 700 /opt/stream
```

`next_stage.sh` and `stage_status.sh` arrive via the existing `COPY .` and are
already mode 755 from the adapter.

Check after building:
```bash
# as the agent user, inside the container
ls /opt/stream/          # must be: Permission denied
bash next_stage.sh       # must work
```
That asymmetry is the whole enforcement mechanism. If `ls /opt/stream/` succeeds,
the gate is decorative and the experiment is invalid — verify it explicitly.

---

## 3. `src/harbor_adapter/template/environment/entrypoint.sh`

Before the agent process launches (same place `system_monitor.sh` is
backgrounded), add:

```bash
# --- continual stream gate ---------------------------------------------
if [ -f /opt/stream/stream_server.py ]; then
    mkdir -p /workspace/checkpoints /workspace/notes /workspace/buffer
    chown -R agent:agent /workspace/checkpoints /workspace/notes /workspace/buffer
    python3 /opt/stream/stream_server.py daemon \
        --policy /opt/stream/policy.json \
        --stages "${NUM_STAGES:-4}" \
        --poll 5 &
    echo "[entrypoint] stream gate started (NUM_STAGES=${NUM_STAGES:-4})"
    sleep 3   # let stage 0 land before the agent starts reading
fi
```

The daemon must start as root — put this before any `su`/`gosu` drop to the
agent user. Check the agent's first `ls stream/` shows `stage_0` only.

---

## 4. `src/harbor_adapter/template/tests/test.sh`

Replace the single-eval block with the T x T loop. Keep the 3-phase retry
wrapper you already have; just call it per cell.

```bash
STAGES=$(python3 -c "import json;print(json.load(open('/tests/metadata.json'))['stages'])")
mkdir -p /logs/verifier/scores

# base-model row: the denominator for forward transfer
for j in $(seq 0 $((STAGES-1))); do
    run_evaluation_with_retry 2 "" \
        --model-path "$BASE_MODEL_ID" \
        --eval-slice "/tests/slices/stage_${j}.jsonl" \
        --json-output-file "/logs/verifier/scores/A_base_${j}.json"
done

# A[i][j]: checkpoint after stage i, scored on slice j
for i in $(seq 0 $((STAGES-1))); do
    CKPT="/workspace/checkpoints/stage_${i}"
    if [ ! -d "$CKPT" ]; then
        echo "MISSING checkpoint stage_${i} — scoring as previous stage"
        CKPT="/workspace/checkpoints/stage_$((i-1))"
        [ -d "$CKPT" ] || CKPT="$BASE_MODEL_ID"
    fi
    for j in $(seq 0 $((STAGES-1))); do
        run_evaluation_with_retry 2 "" \
            --model-path "$CKPT" \
            --eval-slice "/tests/slices/stage_${j}.jsonl" \
            --json-output-file "/logs/verifier/scores/A_${i}_${j}.json"
    done
done

# integrity artifacts for grading
cp /opt/stream/audit.log     /logs/verifier/ 2>/dev/null || true
cp /opt/stream/state.json    /logs/verifier/ 2>/dev/null || true
cp -r /workspace/notes       /logs/verifier/ 2>/dev/null || true

# replay violation check (replay=none only)
REPLAY=$(python3 -c "import json;print(json.load(open('/tests/metadata.json'))['replay'])")
if [ "$REPLAY" = "none" ]; then
    find /workspace -name "train.jsonl" -not -path "/workspace/stream/*" \
        > /logs/verifier/retained_shards.txt 2>/dev/null || true
fi

# headline reward stays the final checkpoint's mean across all slices
python3 - <<'PY' > /logs/verifier/reward.txt
import json, glob, re
T = json.load(open('/tests/metadata.json'))['stages']
vals = []
for j in range(T):
    try:
        d = json.load(open(f'/logs/verifier/scores/A_{T-1}_{j}.json'))
        vals.append(next(v for k, v in d.items() if isinstance(v, (int, float))))
    except Exception:
        pass
print(sum(vals)/len(vals) if vals else 0.0)
PY
```

`run_evaluation_with_retry` currently hardcodes its `evaluate.py` arguments —
change it to accept and forward `"$@"` after its two positional parameters.

**Cost note.** T=4 means 16 cells plus a 4-cell base row = 20 vLLM runs. At
~230-436 items each that is well over the 3h verifier timeout in `task.toml`.
Two fixes, apply both:

- raise `verifier.timeout_sec` in `template/task.toml` to 6-8 hours
- add `--limit 150` to every eval call and record it in SPEC.md as a stated
  deviation; relative metrics (BWT, FWT) survive subsampling, absolute scores
  will not match the public leaderboard

---

## Generate and inspect

```bash
python src/continual/run_adapter.py \
    --benchmark gsm8k --model qwen3-1.7b \
    --stages 4 --ordering domain_blocked --replay none --stage-hours 2 \
    --output ./tasks

find tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked -maxdepth 2 -type d
cat tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked/instruction.md
cat tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked/environment/policy.json
ls tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked/environment/vault/shards/
ls tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked/tests/slices/
```

Read `instruction.md` end to end before running anything. It is the contract;
every placeholder must be substituted and every path in it must exist in the
generated task.

---

## Smoke test

```bash
python src/continual/run_adapter.py --benchmark gsm8k --model qwen3-1.7b \
    --stages 2 --ordering domain_blocked --stage-hours 0.25 \
    --output ./tasks-smoke

harbor run --path ./tasks-smoke/cptb-gsm8k-qwen3-1.7b-domain_blocked-T2 \
    --agent claude-code --model anthropic/claude-sonnet-5 --env modal
```

Three things must be true in the logs:

1. the agent called `next_stage.sh` and received stage 1
2. `audit.log` shows two releases and no unexplained refusals
3. `scores/` contains all four `A_{i}_{j}.json` plus two `A_base_{j}.json`

Only then spend money on Opus 5 at full length.
