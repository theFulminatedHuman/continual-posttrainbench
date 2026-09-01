# Evidence
## Generated task structure
```
tasks
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/contamination_judge.py
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/Dockerfile
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/.dockerignore
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/entrypoint.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/evaluate.py
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/metadata.json
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/next_stage.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/policy.json
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/requirements-direct.txt
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/stage_status.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/stream_server.py
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/system_monitor.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/templates
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/timer.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/environment/vault
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/instruction.md
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/task.toml
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/contamination_judge.py
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/Dockerfile
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/entrypoint.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/evaluate.py
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/metadata.json
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/requirements-direct.txt
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/slices
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/system_monitor.sh
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/templates
tasks/cptb-gsm8k-qwen3-1.7b-domain_blocked-T4/tests/test.sh
```
## Gate behaviour (unit test)
```
$ release --stage 0
Released stage 0 -> /tmp/gate/ws/stream/stage_0
$ release --stage 2 (out of order)
REFUSED: stage 2 requested but last released was 0
$ release --stage 1 (no checkpoint)
REFUSED: checkpoint /tmp/gate/ws/checkpoints/stage_0 does not exist
$ status
{
  "stages_released": [
    0
  ],
  "current_stage": 0,
  "next_stage": 1,
  "refused_requests": 2,
  "replay_policy": "none"
}
```
## Stream manifests
```
--- iid ---
  0 1866 330 ['money'] 3.27
  1 1866 330 ['money'] 3.27
  2 1865 330 ['money'] 3.23
  3 1865 329 ['money'] 3.17
--- domain_blocked ---
  0 2418 436 ['money'] 3.42
  1 2064 362 ['rate_time'] 3.23
  2 1602 288 ['counting'] 3.03
  3 1378 233 ['proportion'] 3.16
--- difficulty_curriculum ---
  0 1865 329 ['rate_time'] 1.78
  1 1865 329 ['money'] 2.62
  2 1865 329 ['money'] 3.46
  3 1867 332 ['money'] 5.07
```
