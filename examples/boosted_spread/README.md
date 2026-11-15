# Boosted spread forecast

The strategy predicts the Bybit-minus-Binance midpoint spread 60 seconds ahead
with 80 shallow gradient-boosted regression trees. Features contain the current
spread, top-level and five-level depth imbalance at each venue, and each venue's
bid-ask spread. Entry requires the forecast change to cover current entry costs,
estimated closing costs, a calibrated error margin, and 0.005 USDT/SOL.

The use of gradient boosting for relative-price signals follows Christopher
Krauss, Xuan Anh Do, and Nicolas Huck, *Deep neural networks, gradient-boosted
trees, random forests — Statistical arbitrage on the S&P 500* (2017). This example
adapts the forecasting method to a single spot pair, equal quantities, and a
60-second horizon. Its market, features, target, and execution rules differ from
the publication's daily equity ranking experiment.
[Publication](https://doi.org/10.1016/j.ejor.2016.10.031)

The first 48 hours of public Train fit the trees. The next 12 hours calibrate
the 95th percentile of absolute forecast error. Training and calibration target
windows end inside their respective chronological partitions. Later public
observations evaluate the forecast. The committed model records these cutoffs,
cache hashes, sample counts, and held-out forecast errors. A forecast improvement
alone does not establish profitability after fees.

The execution layer is copied from the existing Kalman example. It checks
available depth, cash, turnover, freshness, and synchronization, waits for pending
reports, and immediately hedges residual exposure. Positions close when the
spread crosses the forecast level, on a cost-scaled stop,
or after 300 seconds. Each entry uses at most one SOL per venue and sells
prefunded inventory. Inference uses exported JSON trees and standard-library
float32 conversion to match sklearn's branch comparisons.

Train with NumPy, PyArrow, and scikit-learn. The optional `--data` argument builds
a public depth cache containing `bq`, `aq`, and `imb5`. Omit it to reuse an
existing compatible cache, including the earlier microprice experiment's cache.

```bash
python research/fit_boosted_spread.py --data /path/to/public/train \
  --cache /path/to/public/cache \
  --output examples/boosted_spread
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode.jsonl --submission examples/boosted_spread \
  --output /path/to/new/run --runner subprocess
```

Package `strategy.py`, `execution.py`, `model.json`, and `parameters.json` at the
ZIP root. Runtime uses the Python standard library. Training exports are checked
against sklearn predictions. Subprocess execution is for trusted local work.
