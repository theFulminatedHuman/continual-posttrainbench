# ContinualPostTrainBench

Fork of PostTrainBench reframed for continual learning: training data arrives
in T ordered stages, each resuming from the previous stage's weights, scored on
retention and cost rather than a single final accuracy.

See SPEC.md for the benchmark definition, notes/design-memo.md for further
tweaks and other benchmarks, notes/evidence.md for tested behaviour.

## Built and tested
- src/stream/build_stream.py — decontamination + 3 ordering regimes (verified on GSM8K)
- src/stream/stream_server.py — release gate: ordering, checkpoint lineage, replay policy (unit-tested)
- src/continual/metrics.py — ACC/BWT/FWT/forgetting/cost (validated on synthetic matrix)
- src/continual/adapter.py — Harbor task generator (generates at T=2 and T=4)
- src/eval/tasks/gsm8k/evaluate.py — added --eval-slice (additive; upstream path unchanged)

## Specified, not applied
src/continual/STEP9_DIFFS_V2.md documents five blockers found by reading the
Harbor templates, with fixes. The most consequential: the existing
`COPY . /home/agent/workspace/` copies the build context into the agent's
workspace, which would hand it every future shard and silently produce
clean-looking, meaningless numbers.

## Not done
No runs executed — no GPU access during the assignment window.

## Reproduce the data layer
    python src/stream/build_stream.py fetch-gsm8k --out src/stream/pools/gsm8k
    python src/stream/build_stream.py decontaminate --pool src/stream/pools/gsm8k/pool_raw.jsonl \
        --test src/stream/pools/gsm8k/test.jsonl --ngram 13 --out src/stream/pools/gsm8k/pool_clean.jsonl
    python src/stream/build_stream.py shard --pool src/stream/pools/gsm8k/pool_clean.jsonl \
        --test src/stream/pools/gsm8k/test.jsonl --stages 4 --ordering domain_blocked \
        --seed 0 --out src/stream/pools/gsm8k/stream_domain_blocked
    python src/continual/run_adapter.py --benchmark gsm8k --model qwen3-1.7b --stages 4 \
        --ordering domain_blocked --replay none --stage-hours 2 --output ./tasks
