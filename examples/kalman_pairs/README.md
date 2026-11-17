# Kalman spread prediction

## Run the committed model

The directory includes a trained `model.json` and runtime parameters. Copy the
complete directory when starting your own strategy. Runtime uses Python's
standard library. The executor accounts for consumed depth, waits for both
reports, and hedges partial or one-leg execution.

From the repository root, evaluate the ready public quickstart after unpacking
Train into `data/train/`.

```bash
.venv/bin/python -m spot_contest evaluate --config examples/real-test-config.json \
  --data data/train/quickstart/events.jsonl --submission examples/kalman_pairs \
  --output runs/kalman-001 --runner subprocess
```

Subprocess evaluation is for trusted local development. Docker evaluation and
environment setup are described in the [main README](../../README.md).

Package all four runtime files at the ZIP root for CodaBench.

```bash
cd examples/kalman_pairs
../../.venv/bin/python -m zipfile -c ../../submission.zip \
  strategy.py execution.py parameters.json model.json
cd ../..
.venv/bin/python -m zipfile -l submission.zip
```

## Model

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

## Retraining

The training script is maintained in the participant repository. Run the commands
below from that repository's root with NumPy and PyArrow installed.
[Training script](https://github.com/proooooogiba/arbitrage-contest-task/blob/main/research/fit_spread_models.py)

```bash
.venv/bin/python research/fit_spread_models.py --data /path/to/public/train \
  --cache /path/to/public/cache --output examples/kalman_pairs --kind kalman
```
