# Further tweaks and other benchmarks

## Tweaks worth making, ranked

1. **Trace-driven streams.** Stage t trains on stage t-1's own production
   rollouts, verifier-filtered, rather than a static pool. This is what
   continual learning looks like in deployment and no current benchmark does
   it. The SDK's sdft algorithm already supports the shape. Highest value.

2. **Non-stationary objectives.** Don't only add data — change the target. New
   BFCL tool signatures, a shifted HealthBench rubric, a new output format.
   Tests adaptation rather than accumulation.

3. **Unlabeled / partially-verifiable stages.** Production traces have no gold
   answers. A stage where only a fraction is verifiable is far more realistic
   and much harder.

4. **Anytime evaluation.** Score continuously, not at stage boundaries.
   Regression risk is what actually blocks continual learning in production.

5. **Inference-config discipline.** PTB found decoding settings swinging GSM8K
   42.7 -> 78 and BFCL 17 -> 91. Either fix generation_config.json or report
   training-attributable gains separately, or CL metrics are polluted by a flag.

## Other benchmarks

- **RSIBench** — recursive self-improvement is the natural sibling framing.
  Rank first for a follow-up; needs assessment of whether its task structure
  admits a streaming split at all.
- **Temporal-shift QA** (timestamped question sets) — genuinely continual,
  cheap to stream, directly measures forgetting of superseded facts. Best
  cost/insight ratio after CPTB.
- **BFCL by tool family** — already in PTB, and its natural domain structure
  supports larger T than GSM8K allows. Obvious second benchmark for CPTB.
- **MLE-bench / KernelBench / InferenceBench** — same lineage, but they measure
  agentic ML engineering, not continual learning. Wrong target here.
