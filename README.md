# detonator-seed

Bounded open-ended mutation kernel for **test prioritization** (DS-001).

A mission starts from a seed prioritization policy, executes real tests against
faulty revisions, retains behavioral diversity in a fixed 2×2 archive, then
scores frozen finalists on a procedural holdout.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.12+

No LLM API key and no network calls are required for the default offline run.

## First run (< 5 minutes)

```bash
git clone https://github.com/Alexandr-Shevchenko-ua/detonator-seed
cd detonator-seed
uv sync --locked
uv run detonator evolve examples/test_priority/mission.json
```

You should see:

- the seed executing and scoring on 16 search faults
- 24 unique descendant attempts (valid / invalid / crash / timeout)
- four archive cells with retained winners
- holdout scores for seed ∪ frozen winners
- an honest `improved over seed` or `no holdout improvement found`
- a path to the run directory under `runs/`

Inspect and replay retained artifacts:

```bash
uv run detonator inspect runs/<run-id> --verify --replay-retained
```

`--replay-retained` re-executes stored source artifacts and compares evaluation
results. It does not regenerate proposals or replay mutations.

## Candidate contract

```python
def prioritize(change: dict, tests: list[dict]) -> list[str]:
    """Return every test id exactly once, in execution order."""
```

Public inputs only: `changed_symbols`, test `id`, `cost_units`, `covers`,
`historical_failure_rate`. Candidates never see fault IDs, implementations,
outcomes, or holdout data.

## External variation (optional)

Any command that speaks the JSON stdin/stdout protocol can propose sources:

```bash
uv run detonator evolve examples/test_priority/mission.json \
  --variation-command "uv run python examples/providers/sample_provider.py" \
  --provider-timeout-seconds 120
```

Live Cursor Agent provider (requires `agent` login; credentials from the CLI/env):

```bash
uv run detonator evolve examples/test_priority/mission.json \
  --budget 6 \
  --variation-command "uv run python examples/providers/live_agent_provider.py" \
  --provider-timeout-seconds 180
```

Default provider timeout is `provider_timeout_seconds` from the mission (120s)
and is independent of `candidate_timeout_seconds`. Override with
`--provider-timeout-seconds`.

The kernel assigns IDs, hashes, parents, and evaluation. Provider score claims
are ignored. Holdout paths and results are never sent to the provider. The raw
variation command is not persisted in run artifacts — only a sanitized provider
descriptor (kind/name/timeout) is stored. Credentials must come from the
environment or provider process, never from committed examples.

## Trust boundary

Temporary cwd and subprocess timeouts protect against accidental failures only.
They are not an adversarial sandbox.

Holdout discipline is procedural: the file is not opened until the archive is
frozen. The holdout file remains in the public repository, so this is not a
secrecy boundary against a malicious provider process.

## Development

```bash
uv run pytest -q
```
