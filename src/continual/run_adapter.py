#!/usr/bin/env python3
"""
run_adapter.py — CLI for generating ContinualPostTrainBench Harbor tasks.

  # single task
  python src/continual/run_adapter.py --benchmark gsm8k --model qwen3-1.7b \
      --stages 4 --ordering domain_blocked --replay none --stage-hours 2 \
      --output ./tasks

  # cheap smoke test before spending real money
  python src/continual/run_adapter.py --benchmark gsm8k --model qwen3-1.7b \
      --stages 2 --ordering domain_blocked --stage-hours 0.25 \
      --output ./tasks-smoke

  # all three ordering regimes at once
  python src/continual/run_adapter.py --benchmark gsm8k --model qwen3-1.7b \
      --all-orderings --output ./tasks
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.continual.adapter import ContinualAdapter  # noqa: E402
from src.harbor_adapter.adapter import BENCHMARKS, MODELS  # noqa: E402

ORDERINGS = ["iid", "domain_blocked", "difficulty_curriculum"]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--benchmark", default="gsm8k", choices=sorted(BENCHMARKS))
    p.add_argument("--model", default="qwen3-1.7b", choices=sorted(MODELS))
    p.add_argument("--stages", type=int, default=4)
    p.add_argument("--ordering", default="domain_blocked", choices=ORDERINGS)
    p.add_argument("--all-orderings", action="store_true",
                   help="generate one task per ordering regime")
    p.add_argument("--replay", default="none", choices=["none", "buffer", "full"])
    p.add_argument("--replay-cap", type=int, default=500)
    p.add_argument("--stage-hours", type=float, default=2.0)
    p.add_argument("--stream-root", default=None,
                   help="override the stream directory (defaults to "
                        "src/stream/pools/<benchmark>/stream_<ordering>)")
    p.add_argument("--output", default="./tasks")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()

    if args.list:
        print("benchmarks:", ", ".join(sorted(BENCHMARKS)))
        print("models:    ", ", ".join(sorted(MODELS)))
        print("orderings: ", ", ".join(ORDERINGS))
        return

    orderings = ORDERINGS if args.all_orderings else [args.ordering]
    for ordering in orderings:
        adapter = ContinualAdapter(
            output_dir=Path(args.output),
            stages=args.stages,
            ordering=ordering,
            replay=args.replay,
            replay_cap=args.replay_cap,
            stage_hours=args.stage_hours,
            stream_root=args.stream_root,
        )
        adapter.generate_task(args.benchmark, args.model)


if __name__ == "__main__":
    main()
