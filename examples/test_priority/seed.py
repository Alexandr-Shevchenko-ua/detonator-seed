"""Baseline test prioritization policy for DS-001."""


def prioritize(change: dict, tests: list[dict]) -> list[str]:
    """Return every test id exactly once, in execution order.

    Prefer tests that cover changed symbols, then cheaper tests, then id.
    """
    changed = set(change.get("changed_symbols") or [])

    def sort_key(test: dict) -> tuple:
        covers = set(test.get("covers") or [])
        covers_change = 0 if covers & changed else 1
        return (covers_change, int(test["cost_units"]), str(test["id"]))

    ordered = sorted(tests, key=sort_key)
    return [t["id"] for t in ordered]
