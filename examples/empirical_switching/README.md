# Empirical switching

The strategy adapts the three-regime switching formulation of Minh-Man Ngo and
Huyen Pham. The regimes hold zero, positive, or negative relative inventory.
Selling uses prefunded spot inventory and each entry submits equal quantities
on Binance and Bybit. The formulation comes from *Optimal switching for pairs
trading rule — a viscosity solutions approach*.
[Publication](https://arxiv.org/abs/1412.7649)

Training estimates one-second spread transitions from the first 48 hours of
public Train. Quantile states retain the observed tails and a fixed outer grid
covers spreads from -2 to +2 USDT/SOL. Sparse transition rows shrink toward a
mean-reverting AR(1) prior with empirical, untrimmed innovations and strength 20.
The prior drift reuses the existing robust public-data AR(1) estimator. This
regularizes rare states while retaining non-Gaussian innovations. A 300-second dynamic
program includes entry costs, closing costs, terminal liquidation, and future
opportunities after closing. Runtime compares waiting with entering or closing
using current execution costs. Entry planning restarts at 300 seconds when flat,
and the closing horizon decreases with position age. This is a discrete,
empirical adaptation of the switching framework. Rare tail states have very few
observed transitions, so their estimated dynamics remain uncertain.

The current pair size is at most one SOL. Entries require an additional
0.005 USDT/SOL advantage over waiting. Costs are rechecked against executable
depth before sending orders. The execution layer is copied from the existing
Kalman example and retains its book freshness, synchronization, cash, depth,
turnover, residual hedging, cooldown, and maximum holding rules. The switching
model approximates decisions at one-second intervals. Outside training support,
continuation values extend with the cash value of held inventory.

Train with NumPy using the public BBO cache produced by
`research/fit_spread_models.py` or the equivalent richer public cache.

```bash
python research/fit_empirical_switching.py --cache /path/to/public/cache \
  --output examples/empirical_switching
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode.jsonl --submission examples/empirical_switching \
  --output /path/to/new/run --runner subprocess
```

Package `strategy.py`, `execution.py`, `model.json`, and `parameters.json` at the
ZIP root. Runtime uses the Python standard library. Training and local replay
use public data exclusively. Subprocess execution is for trusted local work.
