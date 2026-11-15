# Active EWMA relative-value strategy

This small-position variant prioritizes observable trading activity. It tracks
the causal Bybit-minus-Binance midpoint spread using a 60-second EWMA mean and
variance. Entries require a deviation of at least one basis point and 0.75
standard deviations. It buys the relatively cheap venue and sells prefunded
spot inventory on the relatively expensive venue in equal quantities.

The entry rule permits forecast movements smaller than roundtrip transaction
costs. Each fill still pays the configured fee in the Judge. At the published
10 bps taker fee per venue, frequent ordinary deviations can produce a negative
score. Actual depth-weighted entry costs must stay below 25 bps per pair.

Each pair uses at most 0.1 SOL, with one relative position at a time and at most
12 entry attempts per process. A 30-second warmup and 60-second entry cooldown
limit activity. Positions close on a mean crossing after five seconds, a
0.05 USDT/SOL adverse spread change, or 120 seconds. Failed and partial fills
use the existing executor's report handling and residual hedging. All cash,
depth, freshness, synchronization, and turnover checks remain in force.

The example adapts online spread normalization commonly used in statistical
arbitrage. Its purpose is to provide an active baseline for further experiments.
[Review of pairs trading methods](https://doi.org/10.1111/joes.12153)

```bash
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode.jsonl --submission examples/active_ewma \
  --output /path/to/new/run --runner subprocess
```

Package `strategy.py`, `execution.py`, `parameters.json`, and `model.json` at the
ZIP root. The empty model file satisfies the reused executor's loading contract.
The signal learns online from delivered observations and uses standard-library
Python only. Subprocess evaluation is intended for trusted local development.
