# arbitrage-contest-task

Standalone spot–spot taker–taker competition project. Primary language: Python.
Prefer established libraries: Pydantic contracts, PyArrow Parquet,
standard-library Decimal/heapq/sqlite3/zipfile/subprocess. Avoid replacing
library facilities with custom implementations. Domain-specific replay/execution
rules are the only bespoke simulation code.
Publish only participant-facing source, examples and documentation. Training Parquet
files are distributed through CodaBench: never add datasets, credentials, server
configuration or run artifacts to this repository.

## Invariants

- Trusted judge never imports or executes participant modules in its own process.
- Closed test files, judge output directories and Docker socket never enter the strategy sandbox.
- `subprocess` runner is trusted local development only. Competition runner is Docker
  on a dedicated worker; stronger runtime isolation can be selected via `--runtime`.
- Spot only; no funding, short borrowing, leverage, passive orders or cross-venue transfers.
- Only Limit IOC; legs execute independently. No negative balances or repeated reuse of consumed depth.
- Decimal accounting; fee charged in quote asset under a published fixed-rate rule.
- Sampled 100 ms market frames become available at window close/publication, never at
  their embedded earlier exchange timestamp. Never claim millisecond-accurate exchange replay from these frames.
- Score relative to prefunded starting inventory, including terminal rebalancing costs.

## Checks

`.venv/bin/python -m unittest discover -s tests -v`

`.venv/bin/python -m spot_contest demo --output /tmp/spot-demo-NEW`

`.venv/bin/python -m spot_contest evaluate --config /tmp/spot-demo-NEW/config.json --data /tmp/spot-demo-NEW/events.jsonl --submission /tmp/spot-demo-NEW/baseline --output /tmp/spot-run-NEW --runner subprocess`

Keep synthetic fixtures clearly labelled. Do not download production data or use
exchange credentials unless the user explicitly requests it.
