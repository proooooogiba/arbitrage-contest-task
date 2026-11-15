# Kalman spread prediction

This strategy adapts the noisy mean-reverting spread model in Robert J. Elliott,
John van der Hoek and William P. Malcolm, *Pairs trading* (2005).
[Publication](https://doi.org/10.1080/14697680500149370)

The latent spread is a stationary Gaussian AR(1) process. Observed Bybit-minus-
Binance midpoints include measurement noise. Training estimates persistence and
the process/observation variance split from lagged covariance moments. A scalar
Kalman filter updates the latent spread using only delivered synchronized books.
This is a model-based adaptation with moment estimation and a fixed unit hedge
ratio to satisfy equal-quantity paired entries.

An entry requires its 60-second forecast to cover current depth-weighted entry
costs, the training closing-cost estimate, a one-standard-deviation uncertainty
margin, and an additional 0.005 USDT per SOL. Positions close on mean crossing,
an adverse forecast, a cost-scaled stop-loss, or the 300-second holding limit.
The maximum pair size is one SOL. The committed model uses only the first 48
hours of public Train. The stop-loss width was calibrated on a training episode.
Later observations are reserved for forecast evaluation and public replay.

Run training from the repository root with NumPy and PyArrow installed.

```bash
python research/fit_spread_models.py --data /path/to/public/train \
  --cache /path/to/public/cache --output examples/kalman_pairs --kind kalman
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode/events.jsonl --submission examples/kalman_pairs \
  --output /path/to/new/run --runner subprocess
```

Subprocess evaluation is for trusted local development. Submit `strategy.py`,
`execution.py`, `parameters.json`, and `model.json` at the ZIP root for Codabench.
Runtime inference uses the Python standard library. The executor accounts for
consumed depth, waits for both reports, and hedges partial or one-leg execution.
