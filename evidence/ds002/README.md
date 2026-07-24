# DS-002 evidence — **DOMAIN REJECTED**

Preflight headroom gates failed (`headroom_gates`); the domain was rejected before search.

**Commit 3 / live evolution was not run** — there is no `runs/ds002-live` directory.

## Artifacts

- [`result.json`](result.json) — machine-readable published verdict (re-exported from preflight)
- [`report.md`](report.md) — human-readable summary
- [`07_preflight_evidence.md`](07_preflight_evidence.md) — Commit 2 preflight evidence

## Verify

```bash
uv run detonator publish examples/real_mutations/mission.json \
  --preflight runs/ds002-preflight/preflight.json \
  --output evidence/ds002
uv run python -c "
import json
r=json.load(open('evidence/ds002/result.json'))
p=json.load(open('runs/ds002-preflight/preflight.json'))
assert r['verdict']=='DOMAIN REJECTED' and r['provider_calls']==0
assert r['formula_fingerprint']==p['formula_fingerprint']
print('OK')
"
uv run detonator order examples/real_mutations/mission.json --corpus runs/ds002-corpus
uv run pytest -q tests/test_ds002.py
```
