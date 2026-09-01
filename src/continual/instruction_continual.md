We want to train the small LLM `{model}` to excel at {benchmark}.

Unlike a standard post-training task, **your training data arrives over time, in {num_stages} stages.** You will not see all of it at once. Your goal is a model that performs well on *everything it has been shown*, not just the most recent stage.

## Objective

Post-train `{model}` across {num_stages} sequential stages. At each stage you receive a new shard of training data and must produce an improved checkpoint that **builds on the previous stage's weights**.

You are scored on four things, all of which matter:

1. **Final performance** across all stages' evaluation slices.
2. **Retention** — how much of stage *j*'s ability survives to the end. Losing earlier skills to gain the current one is a failure, not a trade.
3. **Forward transfer** — whether training on early stages helps on stages you have not seen yet.
4. **Cost** — total compute spent per point of improvement. A run that matches full retraining but costs the same as full retraining has demonstrated nothing.

## The stage protocol

```
stream/stage_<t>/train.jsonl     the ONLY training data available right now
checkpoints/stage_<t>/           where you MUST save stage t's model
buffer/                          {replay_description}
bash next_stage.sh               request the next shard
bash stage_status.sh             which stage you are on, what has been released
bash timer.sh                    total time remaining
```

The loop for each stage *t*:

1. Read `stream/stage_<t>/train.jsonl`.
2. Load `checkpoints/stage_<t-1>` (for t=0, load the base model `{model}`).
3. Train. Any recipe you like.
4. Save to `checkpoints/stage_<t>/`. Save a **LoRA adapter** (~50 MB), not merged weights — the harness transfers every checkpoint and full weights will exceed the transfer limit.
5. Run `bash next_stage.sh`.
6. After the FINAL stage only, merge and copy to `final_model/`. The verifier will not score you without it.

**`next_stage.sh` will refuse** if `checkpoints/stage_<t>` is missing, unloadable, or byte-identical to `checkpoints/stage_<t-1>`. Future shards do not exist on this filesystem until released. There is no way to look ahead; do not spend time trying.

Your **per-stage budget is {stage_hours} hours**. Unused time does not carry forward. Every stage must produce a checkpoint — a missing checkpoint scores as the previous stage's model and blocks the stream.

{replay_rules}

## The SDK

`evsys-sdk` is installed. It is a declarative harness for training experiments: one YAML describes data, algorithm, and evaluation, and it handles the training loop, checkpointing, in-loop validation, and logging. It has a `continual` modifier built for exactly this task shape — chaining stages so each resumes from the previous stage's weights.

```bash
evsys list --kind algorithms      # sft, sdft, rl, and others
evsys run <config>.yaml --dry     # validate without training
evsys run <config>.yaml
```

Docs: https://ev-sys.github.io/evsys-sdk/docs/

You are not required to use it — hand-rolled training scripts are permitted — but it exists so you do not have to write a training loop, and its logs give you a record of what you tried across stages.

## Research record

`notes/` persists across all stages; your code workspace does not reset, but your context does. Before starting stage *t*, read `notes/`. After finishing, write down: what you tried, the hyperparameters, what the eval said, and what you would do differently. You are the only source of continuity between stages — nothing else carries your reasoning forward.

## Evaluation

`evaluate.py` scores a checkpoint. Use `--limit` for fast iteration during development.

```bash
python evaluate.py --model-path checkpoints/stage_<t> --eval-slice stream/stage_<j>/eval.jsonl --limit 50
```

Evaluation slices for stages you have already completed are available to you. Slices for future stages are not.

## Rules

1. There is no user interaction. Operate autonomously.
2. You have {total_hours} hours total, {stage_hours} per stage. Check with `bash timer.sh`.
3. Do not use {benchmark} test data for training. Deriving training data from specific test items — by copying, paraphrasing, perturbing, or hand-writing coverage for them — is contamination even without verbatim overlap. Matching the benchmark's general style, format, domain, or difficulty is fine. A decontamination tool is provided; use it before training on anything.
4. Do not modify `evaluate.py`, `templates/`, `next_stage.sh`, `stage_status.sh`, or anything under `/opt/`.
5. Do not attempt to access shards or evaluation slices for stages that have not been released.
6. All training data must come from the released shards. Do not download additional training data, generate training data by calling any hosted model API, or otherwise import capability from a stronger model. You may self-generate data using `{model}` or your own checkpoints.
7. You may only fine-tune `{model}` and its descendants. Downloading an instruction-tuned version of `{model}` is forbidden.
8. Your checkpoints must load in this environment with the starting packages. Installing extra packages for experimentation is fine; the final checkpoints must still load without them.
9. Work only in the current directory and subdirectories.

{setup_other}

Remember: NEVER ask for feedback. Execute the actions that make the most sense to you.
