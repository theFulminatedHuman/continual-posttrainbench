#!/usr/bin/env python3
"""
build_stream.py — turn a training pool + test set into an ordered stream of stages.

Two subcommands:

  decontaminate   n-gram overlap filter of pool against test set (run ONCE, up front)
  shard           partition the clean pool into T stages under a given ordering regime,
                  and partition the test set into T matched eval slices

Output layout:

  <out>/
    manifest.json
    stage_0/train.jsonl
    stage_0/eval.jsonl
    ...
    stage_{T-1}/...

The eval slices are a partition of the TEST set that matches each stage's
distribution. That is what makes A[i][j] meaningful: "how well does the
checkpoint after stage i still do on the skill introduced at stage j".

Usage:
  python src/stream/build_stream.py fetch-gsm8k --out src/stream/pools/gsm8k
  python src/stream/build_stream.py decontaminate \
      --pool src/stream/pools/gsm8k/pool_raw.jsonl \
      --test src/stream/pools/gsm8k/test.jsonl \
      --ngram 13 --out src/stream/pools/gsm8k/pool_clean.jsonl
  python src/stream/build_stream.py shard \
      --pool src/stream/pools/gsm8k/pool_clean.jsonl \
      --test src/stream/pools/gsm8k/test.jsonl \
      --stages 5 --ordering domain_blocked --seed 0 \
      --out src/stream/pools/gsm8k/stream_domain_blocked
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path

# --------------------------------------------------------------------------
# io helpers
# --------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def row_text(row: dict) -> str:
    """Concatenate the fields that could carry contamination."""
    return " ".join(
        str(row.get(k, "")) for k in ("question", "answer", "problem", "solution", "prompt", "completion")
    )


def row_id(row: dict) -> str:
    return hashlib.sha1(row_text(row).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------


def cmd_fetch_gsm8k(args: argparse.Namespace) -> None:
    from datasets import load_dataset  # lazy import

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    train = load_dataset("openai/gsm8k", "main", split="train")
    test = load_dataset("openai/gsm8k", "main", split="test")

    write_jsonl([dict(r) for r in train], out / "pool_raw.jsonl")
    write_jsonl([dict(r) for r in test], out / "test.jsonl")
    print(f"pool_raw.jsonl: {len(train)} rows")
    print(f"test.jsonl:     {len(test)} rows")


# --------------------------------------------------------------------------
# decontaminate
# --------------------------------------------------------------------------


def normalize(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()


def ngrams(tokens: list[str], n: int) -> set[str]:
    return {" ".join(tokens[i : i + n]) for i in range(max(0, len(tokens) - n + 1))}


def cmd_decontaminate(args: argparse.Namespace) -> None:
    pool = read_jsonl(Path(args.pool))
    test = read_jsonl(Path(args.test))

    test_ngrams: set[str] = set()
    for r in test:
        test_ngrams |= ngrams(normalize(row_text(r)), args.ngram)

    clean, dropped = [], []
    for r in pool:
        hits = ngrams(normalize(row_text(r)), args.ngram) & test_ngrams
        (dropped if hits else clean).append(r)

    write_jsonl(clean, Path(args.out))

    report = {
        "pool_in": len(pool),
        "test_rows": len(test),
        "ngram": args.ngram,
        "kept": len(clean),
        "dropped": len(dropped),
        "drop_rate": round(len(dropped) / max(1, len(pool)), 5),
    }
    Path(args.out).with_suffix(".decontamination.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    if dropped:
        print("\nsample dropped row:")
        print(json.dumps(dropped[0], indent=2)[:600])


# --------------------------------------------------------------------------
# ordering regimes
# --------------------------------------------------------------------------

# Cheap, defensible domain buckets for grade-school math word problems.
# Keyword-based, deliberately simple: the point is that stages are *coherent
# and distinct*, not that the taxonomy is perfect. Document this in SPEC.md.
DOMAIN_KEYWORDS = {
    "money":      ["$", "cost", "price", "pay", "paid", "spend", "spent", "buy", "bought",
                   "cent", "earn", "profit", "sell", "sold", "salary", "wage", "income"],
    "rate_time":  ["hour", "minute", "day", "week", "month", "speed", "mile", "km", "travel", "per"],
    "proportion": ["percent", "%", "half", "third", "quarter", "fraction", "ratio",
                   "twice", "times as", "area", "length", "width", "square", "weight"],
    "counting":   ["how many", "each", "boxes", "bags", "pieces", "total number"],
}


def domain_of(row: dict) -> str:
    text = row_text(row).lower()
    scores = {d: sum(text.count(k) for k in kws) for d, kws in DOMAIN_KEYWORDS.items()}
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else "counting"


def difficulty_of(row: dict) -> int:
    """Proxy for difficulty: number of calculator annotations in the gold solution."""
    ans = str(row.get("answer", "") or row.get("solution", ""))
    n_calc = ans.count("<<")
    return n_calc if n_calc else len([ln for ln in ans.splitlines() if ln.strip()])


def partition(rows: list[dict], stages: int, ordering: str, seed: int) -> list[list[dict]]:
    rng = random.Random(seed)
    rows = list(rows)

    if ordering == "iid":
        rng.shuffle(rows)
        return [rows[i::stages] for i in range(stages)]

    if ordering == "difficulty_curriculum":
        rows.sort(key=lambda r: (difficulty_of(r), row_id(r)))
        size = len(rows) // stages
        return [rows[i * size : (i + 1) * size if i < stages - 1 else len(rows)] for i in range(stages)]

    if ordering == "domain_blocked":
        by_domain: dict[str, list[dict]] = {}
        for r in rows:
            by_domain.setdefault(domain_of(r), []).append(r)
        # Largest domains first so every stage has usable volume.
        domains = sorted(by_domain, key=lambda d: -len(by_domain[d]))
        if len(domains) < stages:
            raise SystemExit(
                f"only {len(domains)} domains found but {stages} stages requested; "
                f"add keyword buckets or reduce --stages"
            )
        buckets: list[list[dict]] = [[] for _ in range(stages)]
        for i, d in enumerate(domains):
            rng.shuffle(by_domain[d])
            buckets[i % stages].extend(by_domain[d])
        return buckets

    raise SystemExit(f"unknown ordering: {ordering}")


def partition_test_like(
    test: list[dict], stages: int, ordering: str, seed: int
) -> list[list[dict]]:
    """Partition the test set the same way, so eval slice j matches stage j."""
    return partition(test, stages, ordering, seed + 9973)


# --------------------------------------------------------------------------
# shard
# --------------------------------------------------------------------------


def cmd_shard(args: argparse.Namespace) -> None:
    pool = read_jsonl(Path(args.pool))
    test = read_jsonl(Path(args.test))
    out = Path(args.out)

    train_stages = partition(pool, args.stages, args.ordering, args.seed)
    eval_stages = partition_test_like(test, args.stages, args.ordering, args.seed)

    manifest = {
        "ordering": args.ordering,
        "seed": args.seed,
        "stages": args.stages,
        "pool_rows": len(pool),
        "test_rows": len(test),
        "stage_summary": [],
    }

    for i, (tr, ev) in enumerate(zip(train_stages, eval_stages)):
        write_jsonl(tr, out / f"stage_{i}" / "train.jsonl")
        write_jsonl(ev, out / f"stage_{i}" / "eval.jsonl")
        manifest["stage_summary"].append(
            {
                "stage": i,
                "train_rows": len(tr),
                "eval_rows": len(ev),
                "domains": dict(Counter(domain_of(r) for r in tr).most_common(3)),
                "mean_difficulty": round(sum(difficulty_of(r) for r in tr) / max(1, len(tr)), 2),
                "train_checksum": hashlib.sha1(
                    "".join(sorted(row_id(r) for r in tr)).encode()
                ).hexdigest()[:16],
            }
        )

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


# --------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch-gsm8k")
    f.add_argument("--out", required=True)
    f.set_defaults(func=cmd_fetch_gsm8k)

    d = sub.add_parser("decontaminate")
    d.add_argument("--pool", required=True)
    d.add_argument("--test", required=True)
    d.add_argument("--ngram", type=int, default=13)
    d.add_argument("--out", required=True)
    d.set_defaults(func=cmd_decontaminate)

    s = sub.add_parser("shard")
    s.add_argument("--pool", required=True)
    s.add_argument("--test", required=True)
    s.add_argument("--stages", type=int, default=5)
    s.add_argument("--ordering", default="iid",
                   choices=["iid", "domain_blocked", "difficulty_curriculum"])
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_shard)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
