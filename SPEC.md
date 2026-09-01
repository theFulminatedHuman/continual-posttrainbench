# ContinualPostTrainBench — Specification

## 1. Motivation

PostTrainBench gives an agent a base model, an eval script, 10h on an H100, and
unrestricted internet, then scores one number: final benchmark accuracy. That
measures post-training as a one-shot event.

Continual learning is not one-shot. Data arrives over time, the deployed model
must improve without losing what it already does, and the whole proposition is
that incremental updates cost less than retraining. None of those three things
are observable in PostTrainBench's setup.

CPTB reframes the task: data arrives in T ordered stages, each stage resumes
from the previous stage's weights, and scoring covers retention and cost, not
just final accuracy.

## 2. Task definition

- T = 4 stages. Stage t releases only after stage t-1 produces a valid checkpoint.
- Per-stage budget: 2h (8h total), not bankable.
- Stage t must resume from checkpoints/stage_{t-1}.
- Intermediate checkpoints are LoRA adapters; only final_model is merged.
- Ordering regimes: iid (control), domain_blocked, difficulty_curriculum.
- Replay policies: none (strict), buffer:N (bounded), full (upper bound).

The most consequential change from upstream: **the agent no longer sources its
own training data**. PTB's agents scavenge the open internet, which makes
"release data over time" meaningless. CPTB ships a fixed decontaminated pool
and releases it in shards, so the task is about recipe and scheduling rather
than dataset acquisition.

## 3. Data construction (GSM8K)

Pool = GSM8K train (7,473). Test = GSM8K test (1,319).

Decontamination: 13-gram overlap filter, pool against test. 11 rows dropped
(0.15%). The splits are nominally disjoint, so a non-zero drop is itself a
finding — e.g. the "Bella bought stamps" problem appears in near-duplicate form
across both. Justifies running the filter even on datasets assumed clean.

Domain buckets (keyword-scored, greedy max):
  money(2418/436) rate_time(2064/362) counting(1602/288) proportion(1378/233)
  [train/test rows]

Eval slices are a partition of the TEST set under the same ordering, so
A[i][j] means "how much of the skill introduced at stage j survives in the
checkpoint after stage i".

## 4. Why T=4, not T=5

At T=5 the domain-blocked stages were 2297/2254/1821/866/224 train rows, with
the smallest eval slice at **26 items**. One item moves that score 3.8 points,
making per-stage BWT pure noise — and BWT is the headline metric.

Splitting money into spend/earn to rebalance made it worse (23 test items):
"$" appears in nearly every money problem, so the greedy max-scoring bucket
absorbs earning problems before the earn bucket can score them.

Settled on 4 merged buckets, smallest eval slice 233. All orderings use T=4 so
metrics are comparable within a regime. Side benefit: 16 eval cells per run
instead of 25.

This constraint is a property of GSM8K, not of the design. Benchmarks with more
natural domains (BFCL, by tool family) support larger T.

## 5. Metrics

A[i][j] = score of checkpoint after stage i on eval slice j.

  acc_final    mean_j A[T-1][j]
  bwt          mean_{j<T-1} (A[T-1][j] - A[j][j])       negative = forgetting
  forgetting   mean_j (max_i A[i][j] - A[T-1][j])       peak-to-final drop
  fwt          mean_{j>i} (A[i][j] - A_base[j])         zero-shot transfer
  plasticity   mean_i A[i][i]                           per-stage learning
  cost/point   spend / (acc_final - acc_base)

Validated against a synthetic matrix with known forgetting: BWT recovered as
-0.15, plasticity 0.62, correctly separating "learns each stage" from
"retains it".

The leaderboard is a Pareto plot over (acc_final, bwt), not a scalar. An agent
that maxes the final stage while forgetting stages 0..T-2 must rank below a
balanced one, and a single number cannot express that.

## 6. Control arms

  base           zero-shot base model                 floor
  naive_seq      plain SFT per stage, no strategy     does the agent add value?
  joint_retrain  full retrain on shards 0..t          THE number to beat on cost
  agent          the CLI agent under test

joint_retrain is what makes "cheaper than retraining from scratch" falsifiable.
Without it the claim is unmeasurable.

## 7. Integrity — and its limits

Inherited from PTB v1.1: decontamination (applied once at pool build, so the
pool is certifiably clean), no external teachers, no instruct-weight
substitution, contamination judge.

Added: no lookahead to unreleased shards; no retained shards under replay=none;
checkpoint lineage validation (missing, unloadable, or byte-identical-to-
previous is refused, blocking the no-op "resubmit last checkpoint" strategy).

**Limitation, stated plainly: the agent runs as root.** The PTB Dockerfile never
issues `USER agent`; /home/agent/workspace is a directory name, not a privilege
boundary. The root-owned 0700 vault is therefore obscurity, not isolation. This
is not a flaw introduced here — upstream's integrity model is fundamentally
post-hoc (detect violations with an agent-as-judge, void the run), and CPTB
inherits it.

Mitigations at this level: vault outside the workspace, release audit log,
tripwire shard with traceable poisoned rows, extended judge clause for
lookahead. These make cheating deliberate and detectable rather than accidental.

Genuine enforcement needs one of:
  v2  unprivileged agent user (requires harbor's exec to honour it — unverified)
  v3  out-of-container release: shards on a volume the agent cannot mount,
      validated and released by a separate process. Unforgeable, ~1 day.

## 8. Limitations

- No runs executed; no GPU access during the assignment window.
- Domain buckets are keyword-based and crude. A clustering approach over
  problem embeddings would be more defensible.
- Per-stage budget is advisory in the single-container design; enforced by
  timestamp audit rather than a hard kill.
- Eval subsampling to --limit 150 is planned for tractability (20 cells at T=4
  against an 8h verifier timeout). Relative metrics survive it; absolute
  comparability to the public leaderboard does not.
- Few-shot exemplars in the inspect_evals GSM8K task are drawn from the train
  split, i.e. the same pool being streamed. Not test contamination, and
  upstream does the same, but worth noting.

## 9. Status

Built and tested: stream builder, release gate, CL metrics, Harbor task
generator, --eval-slice patch.
Specified but not applied: entrypoint daemon hook, test.sh T x T eval loop,
tripwire, judge clause. See src/continual/STEP9_DIFFS_V2.md.
