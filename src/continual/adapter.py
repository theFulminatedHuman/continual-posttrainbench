#!/usr/bin/env python3
"""
adapter.py — generates a ContinualPostTrainBench Harbor task.

Subclasses PostTrainBenchAdapter so the whole upstream pipeline (Dockerfile,
eval files, contamination judge, verifier image) is inherited unchanged. What
this adds:

  environment/vault/shards/stage_<t>/{train,eval}.jsonl   baked in, chowned root:700
  environment/stream_server.py                            the release gate daemon
  environment/next_stage.sh                               agent-facing request
  environment/stage_status.sh                             agent-facing status
  environment/policy.json                                 replay + model family rules
  tests/slices/stage_<j>.jsonl                            verifier's eval slices
  instruction.md                                          from instruction_continual.md

Run from the repo root:

  python src/continual/run_adapter.py \
      --benchmark gsm8k --model qwen3-1.7b \
      --stages 4 --ordering domain_blocked \
      --replay none --stage-hours 2 \
      --output ./tasks
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.harbor_adapter.adapter import (  # noqa: E402
    BENCHMARKS,
    MODELS,
    PostTrainBenchAdapter,
    BenchmarkInfo,
    ModelInfo,
)

CONTINUAL_DIR = Path(__file__).resolve().parent
INSTRUCTION_TEMPLATE = CONTINUAL_DIR / "instruction_continual.md"
STREAM_SERVER = REPO_ROOT / "src" / "stream" / "stream_server.py"

MODEL_FAMILY = {
    "qwen3-1.7b": "qwen",
    "qwen3-4b": "qwen",
    "smollm3-3b": "smollm",
    "gemma3-4b": "gemma",
}

REPLAY_TEXT = {
    "none": (
        "not used — you may not retain data from earlier stages",
        "**Replay policy: none.** When a new stage is released, the previous stage's "
        "shard is removed from `stream/`. You may not copy it elsewhere; the verifier "
        "audits for retained shards. Everything you want to carry forward must live in "
        "the model weights or in `notes/`.",
    ),
    "buffer": (
        "a replay buffer capped at {cap} rows — put anything you want to keep here",
        "**Replay policy: bounded buffer.** You may retain up to {cap} examples from "
        "earlier stages in `buffer/`. Exceeding the cap blocks the next release. "
        "Choosing *what* to keep is part of the task.",
    ),
    "full": (
        "unused — all previous shards stay readable",
        "**Replay policy: full.** Every released shard stays in `stream/`. This is the "
        "upper-bound arm; retaining earlier data is expected.",
    ),
}


class ContinualAdapter(PostTrainBenchAdapter):
    def __init__(
        self,
        output_dir: Path,
        stages: int = 4,
        ordering: str = "domain_blocked",
        replay: str = "none",
        replay_cap: int = 500,
        stage_hours: float = 2.0,
        stream_root: Path | None = None,
        **kwargs,
    ):
        super().__init__(output_dir=output_dir, num_hours=int(stages * stage_hours), **kwargs)
        self.stages = stages
        self.ordering = ordering
        self.replay = replay
        self.replay_cap = replay_cap
        self.stage_hours = stage_hours
        self.stream_root = stream_root

    # -- paths ------------------------------------------------------------

    def _stream_dir(self, benchmark_id: str) -> Path:
        if self.stream_root:
            return Path(self.stream_root)
        return REPO_ROOT / "src" / "stream" / "pools" / benchmark_id / f"stream_{self.ordering}"

    def _policy(self, model_key: str) -> dict:
        return {
            "replay": self.replay,
            "replay_buffer_rows": self.replay_cap if self.replay == "buffer" else None,
            "model_family": MODEL_FAMILY.get(model_key),
            "stages": self.stages,
            "stage_hours": self.stage_hours,
            "ordering": self.ordering,
        }

    # -- instruction ------------------------------------------------------

    def generate_instruction(self, task_dir, model_info, benchmark_info, benchmark_id=""):
        desc, rules = REPLAY_TEXT[self.replay]
        desc = desc.format(cap=self.replay_cap)
        rules = rules.format(cap=self.replay_cap)

        content = INSTRUCTION_TEMPLATE.read_text()
        for key, val in {
            "{model}": model_info.model_id,
            "{benchmark}": benchmark_info.benchmark_name,
            "{num_stages}": str(self.stages),
            "{stage_hours}": f"{self.stage_hours:g}",
            "{total_hours}": f"{self.stages * self.stage_hours:g}",
            "{replay_description}": desc,
            "{replay_rules}": rules,
            "{setup_other}": benchmark_info.setup_note,
        }.items():
            content = content.replace(key, val)

        if self.include_claude_clause:
            content += (
                "\nYou are running in a non-interactive mode. So make sure every process "
                "you are running finishes before you write your last message.\n"
            )
        (task_dir / "instruction.md").write_text(content)

    # -- task.toml --------------------------------------------------------

    def generate_task_toml(self, task_dir, benchmark_id=""):
        super().generate_task_toml(task_dir, benchmark_id)
        p = task_dir / "task.toml"
        p.write_text(
            p.read_text()
            + f'\n[agent.env]\nNUM_STAGES = "{self.stages}"\n'
            f'STAGE_HOURS = "{self.stage_hours:g}"\n'
        )

    # -- environment ------------------------------------------------------

    def generate_environment(self, task_dir, benchmark_id, model_info, benchmark_info):
        super().generate_environment(task_dir, benchmark_id, model_info, benchmark_info)
        env = task_dir / "environment"

        stream = self._stream_dir(benchmark_id)
        if not stream.is_dir():
            raise FileNotFoundError(
                f"stream not found: {stream}\n"
                f"build it first:\n"
                f"  python src/stream/build_stream.py shard --stages {self.stages} "
                f"--ordering {self.ordering} ..."
            )

        vault = env / "vault" / "shards"
        for t in range(self.stages):
            src = stream / f"stage_{t}"
            dst = vault / f"stage_{t}"
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy(src / "train.jsonl", dst / "train.jsonl")
            shutil.copy(src / "eval.jsonl", dst / "eval.jsonl")

        shutil.copy(stream / "manifest.json", env / "vault" / "manifest.json")
        shutil.copy(STREAM_SERVER, env / "stream_server.py")
        (env / "policy.json").write_text(json.dumps(self._policy(model_info.short_name), indent=2))

        self._write_gate_scripts(env)

    def _write_gate_scripts(self, env: Path) -> None:
        next_stage = f"""#!/bin/bash
# Request the next stage. The gate validates your current checkpoint first.
set -u
STATE=$(python3 /opt/stream/stream_server.py status 2>/dev/null)
NEXT=$(echo "$STATE" | python3 -c "import sys,json; print(json.load(sys.stdin)['next_stage'])")

if [ "$NEXT" -ge {self.stages} ]; then
    echo "All {self.stages} stages have been released. No further shards exist."
    exit 0
fi

echo "{{\\"stage\\": $NEXT}}" > /home/agent/workspace/.stage_request
echo "Requested stage $NEXT. Waiting for the gate..."

for _ in $(seq 1 24); do
    sleep 5
    RELEASED=$(python3 /opt/stream/stream_server.py status \\
        | python3 -c "import sys,json; print(json.load(sys.stdin)['current_stage'])")
    if [ "$RELEASED" = "$NEXT" ]; then
        echo "Stage $NEXT released to stream/stage_$NEXT/"
        ls -la /home/agent/workspace/stream/stage_$NEXT/
        exit 0
    fi
done

echo "Gate did not release stage $NEXT. Run 'bash stage_status.sh' to see why."
echo "The usual cause is that checkpoints/stage_$((NEXT-1)) is missing, unloadable,"
echo "or identical to the checkpoint before it."
exit 1
"""
        stage_status = """#!/bin/bash
echo "=== stream state ==="
python3 /opt/stream/stream_server.py status
echo
echo "=== checkpoints ==="
ls -d /home/agent/workspace/checkpoints/stage_* 2>/dev/null || echo "(none yet)"
echo
echo "=== released shards ==="
ls -d /home/agent/workspace/stream/stage_* 2>/dev/null || echo "(none yet)"
echo
echo "=== time ==="
bash /home/agent/workspace/timer.sh 2>/dev/null || bash timer.sh
"""
        for name, body in (("next_stage.sh", next_stage), ("stage_status.sh", stage_status)):
            p = env / name
            p.write_text(body)
            p.chmod(0o755)

    # -- tests / verifier -------------------------------------------------

    def generate_tests(self, task_dir, benchmark_id, model_info, benchmark_info):
        super().generate_tests(task_dir, benchmark_id, model_info, benchmark_info)
        tests = task_dir / "tests"

        slices = tests / "slices"
        slices.mkdir(parents=True, exist_ok=True)
        stream = self._stream_dir(benchmark_id)
        for j in range(self.stages):
            shutil.copy(stream / f"stage_{j}" / "eval.jsonl", slices / f"stage_{j}.jsonl")

        meta = json.loads((tests / "metadata.json").read_text())
        meta.update({
            "continual": True,
            "stages": self.stages,
            "ordering": self.ordering,
            "replay": self.replay,
            "stage_hours": self.stage_hours,
        })
        (tests / "metadata.json").write_text(json.dumps(meta, indent=2))

    # -- entry point ------------------------------------------------------

    def generate_task(self, benchmark_id: str, model_key: str) -> Path:
        if benchmark_id not in BENCHMARKS:
            raise ValueError(f"unknown benchmark {benchmark_id}")
        if model_key not in MODELS:
            raise ValueError(f"unknown model {model_key}")

        bi = BENCHMARKS[benchmark_id]
        try:
            bi = BenchmarkInfo(bi.task_id, self._read_benchmark_name(benchmark_id), bi.setup_note)
        except FileNotFoundError:
            pass
        mi: ModelInfo = MODELS[model_key]

        task_id = f"cptb-{benchmark_id}-{mi.short_name}-{self.ordering}-T{self.stages}"
        task_dir = self.output_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        print(f"Generating continual task: {task_id}")
        print(f"  stages={self.stages} ordering={self.ordering} "
              f"replay={self.replay} stage_hours={self.stage_hours}")

        self.generate_task_toml(task_dir, benchmark_id)
        self.generate_instruction(task_dir, mi, bi, benchmark_id)
        self.generate_environment(task_dir, benchmark_id, mi, bi)
        self.generate_tests(task_dir, benchmark_id, mi, bi)

        print(f"Task generated at: {task_dir}")
        return task_dir
