# Step 9 — corrected diffs (supersedes STEP9_DIFFS.md)

Five findings from the real templates. Fix in this order.

---

## FINDING 1 (blocking) — the agent runs as root, so the 0700 vault is not enforcement

The Dockerfile never issues `USER agent`. `/home/agent/workspace` is just a
directory name; `chmod -R a+rw` makes it world-writable and the agent CLI runs
as root. `chmod 700 /opt/stream` stops nothing — root reads it trivially.

There is no way to fix this with permissions alone inside one container. Three
options, in increasing strength:

**v1 (do this now) — obscure, audit, judge.** Keep the vault out of the
workspace so it is not in the agent's line of sight, log every access, and
extend the contamination judge to look for lookahead. Cheating becomes a
deliberate, visible act rather than an accident, and the judge catches it.

  1. Store shards encrypted at rest. The daemon decrypts on release.
  2. Add a tripwire: a decoy `stage_9/train.jsonl` in the vault whose rows are
     poisoned and traceable. If poisoned rows appear in the agent's training
     data, the run is void. This detects lookahead even without file auditing.
  3. Extend `contamination_judge.py` with a lookahead clause (below).

**v2 — real isolation.** Run the agent as an unprivileged user. Add to the
Dockerfile: `RUN useradd -m agent && chown -R agent:agent /home/agent`, then
confirm harbor's `sandbox.exec` accepts a user argument. Verify before relying
on it; if harbor always execs as root, this does nothing.

**v3 — out-of-container release.** Shards live in a Modal Volume the agent
container cannot mount. A separate Modal function validates the checkpoint hash
and writes the next shard in. Genuinely unforgeable, roughly a day of work.

Write this limitation into SPEC.md explicitly. A benchmark that overstates its
own integrity guarantees is worse than one that states them accurately.

### Judge clause to append to `contamination_judge.py`

```
Additionally, determine whether the agent accessed training data for a stage
that had not yet been released. Evidence includes: reading files under
/opt/stream, decrypting or copying vault contents, training on rows absent
from the stage shards present in the workspace at that time, or retaining a
previous stage's shard when the replay policy was "none". Write your verdict
to lookahead_judgement.txt as either "no lookahead detected" or a description
of what you found.
```

---

## FINDING 2 (blocking) — the verifier looks for `final_model`, my instruction says `checkpoints/stage_<t>`

`test.sh` exits early with reward 0 when `$WORKSPACE/final_model` is missing.
The continual task must satisfy both. Add to the instruction, after step 5 of
the stage loop:

```
6. After the FINAL stage, also copy your last checkpoint to `final_model/`:
   `cp -r checkpoints/stage_<T-1> final_model`
   The verifier will not score you without it.
```

And make the verifier defensive — in `test.sh`, before the `final_model` check:

```bash
# Continual mode: synthesize final_model from the last checkpoint if absent
if [ ! -d "$WORKSPACE/final_model" ]; then
    LAST=$(ls -d "$WORKSPACE"/checkpoints/stage_* 2>/dev/null | sort -V | tail -1)
    if [ -n "$LAST" ]; then
        echo "final_model missing; using $LAST"
        cp -r "$LAST" "$WORKSPACE/final_model"
    fi
fi
```

---

## FINDING 3 (blocking) — 4 full checkpoints will kill the artifact transfer

`task.toml` documents two prior trials dying on harbor's tar with multi-GB
workspaces. Qwen3-1.7B in bf16 is ~3.4 GB; four stage checkpoints is ~13.6 GB
plus `final_model`. This will fail.

**Fix: intermediate checkpoints are LoRA adapters, only the final is merged.**
Add to the instruction's stage loop, step 4:

```
4. Save to `checkpoints/stage_<t>/`. Save a LoRA adapter (~50 MB), not merged
   weights — the harness transfers all checkpoints and full weights will
   exceed the transfer limit. Merge to full weights only for `final_model`.
```

Then `stream_server.py` must accept adapters. In `validate_checkpoint`, replace
the `config.json` requirement with:

```python
    has_full = (prev / "config.json").exists()
    has_adapter = (prev / "adapter_config.json").exists()
    if not (has_full or has_adapter):
        return False, f"no config.json or adapter_config.json under {prev}"
```

and read the architecture from `adapter_config.json`'s `base_model_name_or_path`
when only the adapter is present.

Also add `checkpoints` handling to `task.toml`'s exclude list — keep them, but
note the size. If transfers still fail, exclude `checkpoints` and have `test.sh`
evaluate from within the agent container instead (weaker isolation, but a
working run beats a failed one).

---

## FINDING 4 — path is `/home/agent/workspace`, not `/workspace`

`adapter.py`'s generated scripts and `stream_server.py`'s defaults both use
`/workspace`. Fix in three places.

In `adapter.py`, `_write_gate_scripts`, replace every `/workspace` with
`/home/agent/workspace`.

In `stream_server.py`:

```python
WORKSPACE = Path(os.environ.get("STREAM_WORKSPACE", "/home/agent/workspace"))
```

In the entrypoint snippet, same substitution.

---

## FINDING 5 — verifier timeout and eval cost

`timeout_sec = 10800.0` (3h) for the verifier. T=4 needs 20 evals. Even at
`--limit 150` that is roughly 20 x 8 min = 2.7h with no retry headroom.

In `template/task.toml`:

```toml
[verifier]
timeout_sec = 28800.0   # 8h — 20 eval cells for T=4 continual
```

And in `test.sh`'s `run_evaluation`, change the hardcoded `--limit -1` to
`--limit ${EVAL_LIMIT:--1}`, then set `EVAL_LIMIT=150` in the continual path.
Record the subsampling in SPEC.md: BWT and FWT survive it, absolute
leaderboard comparability does not.

---

## Corrected entrypoint.sh insertion

Place immediately before `exec sleep infinity` — the daemon must be
backgrounded while the script still runs.

```bash
# --- continual stream gate ---------------------------------------------
if [ -f /opt/stream/stream_server.py ]; then
    mkdir -p /home/agent/workspace/checkpoints \
             /home/agent/workspace/notes \
             /home/agent/workspace/buffer
    chmod -R a+rw /home/agent/workspace/checkpoints \
                  /home/agent/workspace/notes \
                  /home/agent/workspace/buffer
    python3 /opt/stream/stream_server.py daemon \
        --policy /opt/stream/policy.json \
        --stages "${NUM_STAGES:-4}" \
        --poll 5 >> /logs/agent/stream_gate.txt 2>&1 &
    echo "[entrypoint] stream gate started (NUM_STAGES=${NUM_STAGES:-4})"
fi

exec sleep infinity
```

Also add `/logs/agent/stream_gate.txt` to the `touch` list above so `tail -F`
picks it up and gate activity streams to the Modal dashboard live — you want to
watch releases happen during the smoke test.

---

## Corrected Dockerfile insertion

Append after the existing `COPY . /home/agent/workspace/` block. Order matters:
the vault must land after the workspace copy so it is not swept into it.

```dockerfile
# --- continual stream gate ---------------------------------------------
# NOTE: the agent runs as root in this image, so these permissions are
# obscurity, not isolation. See SPEC.md "Integrity limitations".
COPY vault /opt/stream/shards
COPY stream_server.py /opt/stream/stream_server.py
COPY policy.json /opt/stream/policy.json
RUN chmod -R 700 /opt/stream && \
    rm -rf /home/agent/workspace/vault \
           /home/agent/workspace/stream_server.py \
           /home/agent/workspace/policy.json
```

That `rm -rf` is essential — `COPY .` in the existing Dockerfile copies the
entire build context into the workspace, which includes `vault/`. Without the
removal the agent gets every stage's data in plain sight, and the benchmark
measures nothing.

**Verify this explicitly after building:**

```bash
# inside the container
ls /home/agent/workspace/          # must NOT contain vault/ or stream_server.py
ls /home/agent/workspace/stream/   # must contain stage_0 only
```

---

## Order of application

1. Finding 4 (paths) — trivial, do first
2. Finding 2 (final_model) — instruction + test.sh
3. Finding 3 (LoRA) — instruction + stream_server validation
4. Finding 5 (timeout, limit) — task.toml + test.sh
5. Dockerfile + entrypoint insertions
6. Finding 1 (integrity) — tripwire + judge clause + SPEC.md limitations section
7. Regenerate the task, re-read instruction.md, smoke test at T=2
