#!/usr/bin/env python3
"""
stream_server.py — the release gate. Runs INSIDE the task container, as root.

Why a daemon and not a plain script: the agent must not be able to read stage
t+1 before it has committed stage t. Prompt-level rules do not hold (see the
PostTrainBench v1.1 integrity findings). So:

  /opt/stream/            root:root 0700   the vault — agent cannot read
  /workspace/stream/      agent-writable   only the current stage lives here
  /opt/stream/audit.log   root:root 0600   append-only release record

The agent interacts through a request file. `next_stage.sh` (agent-runnable)
writes /workspace/.stage_request; this daemon polls for it, validates the
previous stage's checkpoint, and copies the next shard into place.

Modes:
  daemon    poll for requests and service them        (started by entrypoint.sh, as root)
  release   service exactly one request and exit      (for local testing)
  status    print stage state as JSON                 (agent-safe, read-only)

Validation before releasing stage t (t > 0):
  1. checkpoints/stage_{t-1}/ exists and holds weights + config.json
  2. the checkpoint is not byte-identical to checkpoints/stage_{t-2}
     (blocks the no-op "submit the previous checkpoint" strategy)
  3. config architecture matches the assigned base model family
  4. replay policy is satisfied (buffer size under cap, if capped)

Anything that fails is refused, logged, and left for the verifier to grade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

VAULT = Path(os.environ.get("STREAM_VAULT", "/opt/stream"))
WORKSPACE = Path(os.environ.get("STREAM_WORKSPACE", "/home/agent/workspace"))
REQUEST = WORKSPACE / ".stage_request"
CURRENT = WORKSPACE / "stream"
CKPT_DIR = WORKSPACE / "checkpoints"
BUFFER = WORKSPACE / "buffer"
STATE = VAULT / "state.json"
AUDIT = VAULT / "audit.log"

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt")


# --------------------------------------------------------------------------


def log(event: str, **fields) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event}
    rec.update(fields)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"released": [], "refused": 0, "policy": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def dir_digest(path: Path) -> str:
    """Stable digest over weight files — cheap identity check."""
    h = hashlib.sha1()
    for p in sorted(path.rglob("*")):
        if p.is_file() and p.suffix in WEIGHT_SUFFIXES:
            h.update(p.name.encode())
            h.update(str(p.stat().st_size).encode())
            with open(p, "rb") as f:
                h.update(f.read(1 << 20))  # first 1MB is enough to distinguish
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------


def validate_checkpoint(t: int, policy: dict) -> tuple[bool, str]:
    """Can we release stage t? Returns (ok, reason)."""
    if t == 0:
        return True, "first stage"

    prev = CKPT_DIR / f"stage_{t - 1}"
    if not prev.is_dir():
        return False, f"checkpoint {prev} does not exist"

    weights = [p for p in prev.rglob("*") if p.suffix in WEIGHT_SUFFIXES]
    if not weights:
        return False, f"no weight files under {prev}"
    has_full = (prev / "config.json").exists()
    has_adapter = (prev / "adapter_config.json").exists()
    if not (has_full or has_adapter):
        return False, f"no config.json or adapter_config.json under {prev}"

    # no-op detection: identical to the stage before it
    if t >= 2:
        prev2 = CKPT_DIR / f"stage_{t - 2}"
        if prev2.is_dir() and dir_digest(prev) == dir_digest(prev2):
            return False, f"stage_{t-1} is byte-identical to stage_{t-2} (no training occurred)"

    # architecture family check
    expected = policy.get("model_family")
    if expected:
        try:
            if has_full:
                arch = json.loads((prev / "config.json").read_text())["architectures"][0].lower()
            else:
                arch = json.loads(
                    (prev / "adapter_config.json").read_text()
                )["base_model_name_or_path"].lower()
        except Exception as e:  # noqa: BLE001
            return False, f"unreadable checkpoint config: {e}"
        if expected.lower() not in arch:
            return False, f"architecture {arch!r} does not match assigned family {expected!r}"

    # replay policy
    cap = policy.get("replay_buffer_rows")
    if cap is not None and BUFFER.is_dir():
        rows = sum(
            sum(1 for line in open(p, encoding="utf-8") if line.strip())
            for p in BUFFER.rglob("*.jsonl")
        )
        if rows > cap:
            return False, f"replay buffer holds {rows} rows, cap is {cap}"

    return True, "ok"


def release(t: int, stream_dir: Path, policy: dict, force: bool = False) -> bool:
    state = load_state()

    if t in state["released"]:
        log("release_duplicate", stage=t)
        return True

    if state["released"] and t != max(state["released"]) + 1:
        state["refused"] += 1
        save_state(state)
        log("release_refused", stage=t, reason="out of order",
            last_released=max(state["released"]))
        print(f"REFUSED: stage {t} requested but last released was {max(state['released'])}")
        return False

    ok, reason = (True, "forced") if force else validate_checkpoint(t, policy)
    if not ok:
        state["refused"] += 1
        save_state(state)
        log("release_refused", stage=t, reason=reason)
        print(f"REFUSED: {reason}")
        return False

    src = stream_dir / f"stage_{t}" / "train.jsonl"
    if not src.exists():
        log("release_error", stage=t, reason=f"missing {src}")
        print(f"ERROR: no shard at {src}")
        return False

    # Replace, never accumulate: with replay=none the agent sees only stage t.
    if policy.get("replay") == "none" and CURRENT.exists():
        shutil.rmtree(CURRENT)
    dst = CURRENT / f"stage_{t}"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst / "train.jsonl")
    os.chmod(dst / "train.jsonl", 0o444)

    ev = stream_dir / f"stage_{t}" / "eval.jsonl"
    if ev.exists():
        shutil.copy(ev, dst / "eval.jsonl")
        os.chmod(dst / "eval.jsonl", 0o444)

    state["released"].append(t)
    state["policy"] = policy
    save_state(state)
    log("released", stage=t, rows=sum(1 for _ in open(src, encoding="utf-8")),
        dest=str(dst), replay=policy.get("replay"))
    print(f"Released stage {t} -> {dst}")
    return True


# --------------------------------------------------------------------------


def cmd_daemon(args: argparse.Namespace) -> None:
    policy = json.loads(Path(args.policy).read_text())
    stream_dir = VAULT / "shards"
    print(f"[stream_server] watching {REQUEST}, vault={stream_dir}, policy={policy}")
    log("daemon_start", policy=policy)

    release(0, stream_dir, policy)  # stage 0 is available immediately

    while True:
        if REQUEST.exists():
            try:
                req = json.loads(REQUEST.read_text() or "{}")
            except json.JSONDecodeError:
                req = {}
            REQUEST.unlink(missing_ok=True)
            t = int(req.get("stage", len(load_state()["released"])))
            release(t, stream_dir, policy)
        if load_state()["released"] and max(load_state()["released"]) >= args.stages - 1:
            # keep serving status but nothing left to release
            pass
        time.sleep(args.poll)


def cmd_release(args: argparse.Namespace) -> None:
    policy = json.loads(Path(args.policy).read_text())
    ok = release(args.stage, Path(args.stream), policy, force=args.force)
    raise SystemExit(0 if ok else 1)


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    released = state["released"]
    print(json.dumps({
        "stages_released": released,
        "current_stage": max(released) if released else None,
        "next_stage": (max(released) + 1) if released else 0,
        "refused_requests": state["refused"],
        "replay_policy": state.get("policy", {}).get("replay"),
    }, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon")
    d.add_argument("--policy", required=True)
    d.add_argument("--stages", type=int, required=True)
    d.add_argument("--poll", type=float, default=5.0)
    d.set_defaults(func=cmd_daemon)

    r = sub.add_parser("release")
    r.add_argument("--stage", type=int, required=True)
    r.add_argument("--stream", required=True)
    r.add_argument("--policy", required=True)
    r.add_argument("--force", action="store_true")
    r.set_defaults(func=cmd_release)

    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
