# DS-002 Commit 2 — preflight implementation notes

## CLI

```bash
uv run detonator preflight \
  examples/real_mutations/mission.json \
  --corpus runs/ds002-corpus \
  --output runs/ds002-preflight
```

## Testmon (documented pytest-testmon CLI only)

Pristine DB at target SHA `bd17a50d22ccbabd40f1e230868e9dbb7b19c8ff`:

```bash
# detached worktree at target SHA, orchestrator venv pytest
python -m pytest --testmon -q
```

Per search mutant (matrix worktree + corpus materialize):

```bash
python -m pytest --testmon-forceselect --collect-only -q
```

Resume cache: `runs/ds002-preflight/testmon_masks.json` (rewritten after each mutant).

Collection failures fall back to full-suite mask (recorded in `collection_fallbacks`).

## Seed

`examples/real_mutations/seed.py` implements `baseline_to_beat` from preflight (`strong-human`).

## Artifact SHA-256

| Artifact | SHA-256 |
|---|---|
| `runs/ds002-preflight/preflight.json` | `1018a9f8d29114184713a2981f2aafa4213d6c381eabd3009092d4816cd14125` |
| `src/detonator/real_mutations.py` | `3e8ec3f6b354fba44d43b2a629065e101023018f1e60c055e7c5fd010d8e3853` |
| `examples/real_mutations/seed.py` | `0cb4633efb67d3fc4ce7f7fd06163b800194891764429bd5240160c3bdda3779` |
| `runs/ds002-corpus/split.json` | `1621ff48a571b88cc29994f53e6316afc389328e9503f2cf8d7c5a92f24826cd` |

## Verdict

Preflight completed with `DOMAIN REJECTED` (headroom gates failed; testmon usefulness gates passed). `provider_calls: 0`.
