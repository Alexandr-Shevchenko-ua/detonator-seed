# Holdout content intentionally withheld from this evidence publication

`holdout-matrix.jsonl` (the frozen holdout split) is **not** included in this
evidence branch, on purpose.

- sha256: `ff84c7363a20f2ac8566dc54144b799c86d319aba548baeac5369ffaee9b0821`
- line count: `830`
- Rationale: DS-002 headroom/preflight (Commit 2) never opened this file on the
  search/preflight code path (verified via `rg -n 'holdout-matrix\.jsonl' src/detonator/`
  during Commit 2 acceptance). The scientific verdict (`DOMAIN REJECTED`) was
  computed entirely on search-only data. Withholding holdout content here
  preserves blind-holdout integrity in case this exact corpus/split is ever
  reused for a future live-search attempt on the same mission. The hash above
  lets an auditor verify file identity/provenance without exposing content.
- If a future decision explicitly authorizes reusing this corpus for live
  search, this file's content can be shared separately at that time.
