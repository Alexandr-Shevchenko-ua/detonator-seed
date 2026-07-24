# DS-002 Commit 2 — preflight evidence

## Acceptance command (exit 0)

```bash
uv run detonator preflight \
  examples/real_mutations/mission.json \
  --corpus runs/ds002-corpus \
  --output runs/ds002-preflight
```

## `preflight.json` excerpts

- `verdict`: `DOMAIN REJECTED`
- `provider_calls`: `0`
- `formula_fingerprint`: `01c8e4a02e12bf86784dbf8a94e44b01e19fa9ecb29667eb9f4647130190ed0e`
- `baseline_to_beat`: `strong-human`
- `headroom.passed`: `false`
- `testmon.passed`: `true`

## Reproduction

Corpus verify (unchanged from Commit 1):

```bash
uv run detonator corpus verify runs/ds002-corpus
```

Tests:

```bash
uv run pytest -q tests/test_ds002.py
```

## Hashes

| Path | SHA-256 |
|---|---|
| `runs/ds002-preflight/preflight.json` | `1018a9f8d29114184713a2981f2aafa4213d6c381eabd3009092d4816cd14125` |
| `src/detonator/real_mutations.py` | `3e8ec3f6b354fba44d43b2a629065e101023018f1e60c055e7c5fd010d8e3853` |
| `examples/real_mutations/seed.py` | `0cb4633efb67d3fc4ce7f7fd06163b800194891764429bd5240160c3bdda3779` |
| `runs/ds002-corpus/split.json` | `1621ff48a571b88cc29994f53e6316afc389328e9503f2cf8d7c5a92f24826cd` |
