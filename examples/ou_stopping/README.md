# OU optimal stopping

This strategy adapts the spread model and entry/exit timing problem in Tim Leung
and Xin Li, *Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss
Exit* (2015). [Paper](https://arxiv.org/abs/1411.5062)

The state is the Bybit midpoint minus the Binance midpoint, in USDT per SOL.
The hedge ratio is one to satisfy the contest's equal-quantity paired-order rule.
An AR(1) fit supplies a discrete OU transition. Gaussian quadrature and dynamic
programming produce entry values and liquidation boundaries. This implementation
uses a 300-second maximum holding period and undiscounted liquidation costs to
account for the contest's mandatory terminal inventory restoration. It is a
finite-horizon numerical adaptation of the paper.

The committed model uses the first 48 hours of public Train. Fitting clips the
0.1% tails, requires valid synchronized observations, and keeps later samples for
forecast evaluation. The one-way closing-cost estimate is the training 95th
percentile of both taker fees and quoted half-spreads. Entries also check current
depth-weighted execution costs. The stop-loss is four times this closing cost,
with a six-standard-deviation floor, and the maximum pair size is one SOL.
The stop-loss width was calibrated on a training episode before submission.

Run training from the repository root with NumPy and PyArrow installed.

```bash
python research/fit_spread_models.py --data /path/to/public/train \
  --cache /path/to/public/cache --output examples/ou_stopping --kind ou
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode/events.jsonl --submission examples/ou_stopping \
  --output /path/to/new/run --runner subprocess
```

Subprocess evaluation is for trusted local development. Submit `strategy.py`,
`execution.py`, `parameters.json`, and `model.json` at the ZIP root for Codabench.
Runtime inference uses the Python standard library. The executor reserves consumed
depth, waits for both reports, and immediately hedges a residual after one-leg or
partial execution. Snapshot availability and the configured callback queue govern
causality throughout.
