#!/usr/bin/env python3
"""
metrics.py — continual-learning metrics from the A[i][j] score matrix.

A[i][j] = score of the checkpoint after stage i, evaluated on eval slice j.

The driver writes one file per cell:
    <run>/scores/A_{i}_{j}.json      # {"accuracy": 0.41, ...}  (evaluate.py --json-output-file)
and a baseline row for the untrained base model:
    <run>/scores/A_base_{j}.json

Metrics produced:
  acc_final       mean_j A[T-1][j]                  headline: what the deployed model knows
  bwt             mean_{j<T-1} (A[T-1][j] - A[j][j])   negative = forgetting
  forgetting      mean_j (max_i A[i][j] - A[T-1][j])   peak-to-final drop, always >= 0
  fwt             mean_{j>i} (A[i][j] - A_base[j])     zero-shot transfer to unseen stages
  plasticity      mean_i A[i][i]                     how well each new stage is learned
  cost_per_point  spend / (acc_final - acc_base)      the number that has to beat joint_retrain

`compare` puts several runs side by side and writes the stability-plasticity
Pareto data. A single scalar cannot express continual learning: an agent that
maxes the final stage while forgetting stages 0..T-2 must look worse than a
balanced one, and only the (acc_final, bwt) pair shows that.

Usage:
  python src/continual/metrics.py compute --run results/continual/run001 --stages 5
  python src/continual/metrics.py compare --runs results/continual/{run001,ctrl_naive,ctrl_joint}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

METRIC_KEY_CANDIDATES = ("accuracy", "mean", "pass_at_1", "score")


def read_score(path: Path) -> float | None:
    """evaluate.py writes {metric_name: value}; pick the accuracy-like key."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    for k in METRIC_KEY_CANDIDATES:
        if k in data:
            return float(data[k])
    # fall back to the single numeric value, if unambiguous
    nums = [v for v in data.values() if isinstance(v, (int, float))]
    return float(nums[0]) if len(nums) == 1 else None


def load_matrix(run: Path, stages: int) -> tuple[list[list[float | None]], list[float | None]]:
    scores = run / "scores"
    A = [[read_score(scores / f"A_{i}_{j}.json") for j in range(stages)] for i in range(stages)]
    base = [read_score(scores / f"A_base_{j}.json") for j in range(stages)]
    return A, base


def mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def compute(A: list[list[float | None]], base: list[float | None], spend: float | None = None) -> dict:
    T = len(A)
    last = T - 1

    acc_final = mean([A[last][j] for j in range(T)])
    acc_base = mean([b for b in base])

    bwt = mean([
        A[last][j] - A[j][j]
        for j in range(last)
        if A[last][j] is not None and A[j][j] is not None
    ])

    forgetting = mean([
        max(v for v in (A[i][j] for i in range(T)) if v is not None) - A[last][j]
        for j in range(T)
        if A[last][j] is not None and any(A[i][j] is not None for i in range(T))
    ])

    fwt = mean([
        A[i][j] - base[j]
        for i in range(T) for j in range(i + 1, T)
        if A[i][j] is not None and base[j] is not None
    ])

    plasticity = mean([A[i][i] for i in range(T)])

    out = {
        "stages": T,
        "acc_final": acc_final,
        "acc_base": acc_base,
        "delta_over_base": (acc_final - acc_base) if (acc_final is not None and acc_base is not None) else None,
        "bwt": bwt,
        "forgetting": forgetting,
        "fwt": fwt,
        "plasticity": plasticity,
        "matrix": A,
        "base_row": base,
    }

    if spend is not None and out["delta_over_base"]:
        out["spend"] = spend
        out["cost_per_point"] = spend / (out["delta_over_base"] * 100)

    return out


def render_matrix(A: list[list[float | None]], base: list[float | None]) -> str:
    T = len(A)
    w = 8
    lines = ["", "A[i][j]  rows = checkpoint after stage i, cols = eval slice j", ""]
    lines.append("        " + "".join(f"{'j=' + str(j):>{w}}" for j in range(T)))
    lines.append("  base  " + "".join(f"{(f'{b:.3f}' if b is not None else '—'):>{w}}" for b in base))
    for i, row in enumerate(A):
        cells = []
        for j, v in enumerate(row):
            s = f"{v:.3f}" if v is not None else "—"
            if j > i:
                s = f"({s})"  # parenthesised = not yet trained on this stage
            cells.append(f"{s:>{w}}")
        lines.append(f"  i={i}   " + "".join(cells))
    lines.append("")
    lines.append("  values in parentheses are forward-transfer cells (stage not yet seen)")
    return "\n".join(lines)


def cmd_compute(args: argparse.Namespace) -> None:
    run = Path(args.run)
    A, base = load_matrix(run, args.stages)

    spend = None
    cost_file = run / "cost.json"
    if cost_file.exists():
        spend = json.loads(cost_file.read_text()).get("usd_total")

    res = compute(A, base, spend)
    (run / "metrics.json").write_text(json.dumps(res, indent=2))

    print(render_matrix(A, base))
    for k in ("acc_base", "acc_final", "delta_over_base", "bwt", "forgetting", "fwt", "plasticity", "cost_per_point"):
        v = res.get(k)
        print(f"  {k:<18} {v:.4f}" if isinstance(v, float) else f"  {k:<18} —")


def cmd_compare(args: argparse.Namespace) -> None:
    rows = []
    for r in args.runs:
        run = Path(r)
        m = json.loads((run / "metrics.json").read_text())
        rows.append({"run": run.name, **{k: m.get(k) for k in
                     ("acc_final", "bwt", "forgetting", "fwt", "plasticity", "cost_per_point")}})

    hdr = ["run", "acc_final", "bwt", "forgetting", "fwt", "plasticity", "cost_per_point"]
    print("| " + " | ".join(hdr) + " |")
    print("|" + "|".join("---" for _ in hdr) + "|")
    for r in rows:
        print("| " + " | ".join(
            (f"{r[h]:.4f}" if isinstance(r.get(h), float) else str(r.get(h, "—"))) if h != "run" else r["run"]
            for h in hdr
        ) + " |")

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.out}")

    joint = next((r for r in rows if "joint" in r["run"]), None)
    agent = next((r for r in rows if "joint" not in r["run"] and "naive" not in r["run"]), None)
    if joint and agent and joint.get("cost_per_point") and agent.get("cost_per_point"):
        ratio = joint["cost_per_point"] / agent["cost_per_point"]
        print(f"\ncontinual is {ratio:.2f}x cheaper per accuracy point than joint retraining"
              if ratio > 1 else
              f"\ncontinual is {1/ratio:.2f}x MORE expensive per point than joint retraining")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compute")
    c.add_argument("--run", required=True)
    c.add_argument("--stages", type=int, required=True)
    c.set_defaults(func=cmd_compute)

    k = sub.add_parser("compare")
    k.add_argument("--runs", nargs="+", required=True)
    k.add_argument("--out", default=None)
    k.set_defaults(func=cmd_compare)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
