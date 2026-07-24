# DS-002 published verdict

## Verdict

`DOMAIN REJECTED` — live evolution was **not** run (no `runs/ds002-live`).

## Target

- `target_sha`: `bd17a50d22ccbabd40f1e230868e9dbb7b19c8ff`
- `formula_fingerprint`: `01c8e4a02e12bf86784dbf8a94e44b01e19fa9ecb29667eb9f4647130190ed0e`

## Baselines

| policy | composite | detection_rate | median_kill_cost |
| --- | --- | --- | --- |
| dependency-first | 0.6102892934185957 | 0.7093023255813954 | 0.7906340000000001 |
| historical-kill | 0.6110294160116427 | 0.6976744186046512 | 0.9256145 |
| risk-per-cost | 0.6112041903417511 | 0.7093023255813954 | 0.4431605 |
| shortest | 0.5954421587282397 | 0.7093023255813954 | 0.8654075000000001 |
| strong-human | 0.6195343345985056 | 0.7093023255813954 | 0.4431605 |

## Headroom

- `passed`: `false`
- failed checks: `baseline_composite_spread`, `oracle_advantage`
- Oracle relative composite advantage: 0.002667969414569358; oracle median kill cost improvement: -1.0886665214972904; baseline composite spread: 0.02409217587026591.

## How to verify

```bash
uv run detonator publish examples/real_mutations/mission.json \
  --preflight runs/ds002-preflight/preflight.json \
  --output evidence/ds002
test -f evidence/ds002/result.json && test -f evidence/ds002/report.md
uv run python -c "
import json
r=json.load(open('evidence/ds002/result.json'))
p=json.load(open('runs/ds002-preflight/preflight.json'))
assert r['verdict']=='DOMAIN REJECTED' and r['provider_calls']==0
assert r['formula_fingerprint']==p['formula_fingerprint']
assert r['target_sha']==p['target_sha']
print('S3_OK')
"
uv run detonator order --help
uv run detonator order examples/real_mutations/mission.json --corpus runs/ds002-corpus
uv run pytest -q tests/test_ds002.py
```

## Provenance

- Preflight source: `/home/shevchenkool/project/detonator-seed/runs/ds002-preflight/preflight.json`
- `provider_calls`: `0`
