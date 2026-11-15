# Twelve active relative-value signals

All variants trade the delivered Bybit-minus-Binance midpoint spread with equal
spot quantities and prefunded inventory. Positive signals buy Bybit and sell
Binance. The default parameters select `median_mad`. The catalog in
`variants.json` selects twelve distinct online signal rules.

| Mode | Entry rule | Threshold unit |
| --- | --- | --- |
| `median_mad` | Revert a deviation from the rolling median scaled by MAD | Robust standardized deviation |
| `quantile_band` | Revert a spread in the outer rolling empirical quantiles | Signed rank in [-1, 1] |
| `ema_momentum` | Follow the difference between 5-second and 60-second spread EMAs | Basis points |
| `channel_breakout` | Follow a break outside the previous 30-second spread channel | Basis points beyond the channel |
| `rsi_reversal` | Revert an imbalance between positive and negative spread increments | Signed oscillator in [-1, 1] |
| `lead_lag` | Trade toward relative price convergence after unequal 5-second venue returns | Basis points |
| `queue_pressure` | Follow relative five-level bid-ask depth imbalance | Difference of normalized imbalances |
| `microprice` | Follow relative imbalance-weighted midpoint adjustments | Basis points |
| `trade_flow` | Follow relative signed aggressive trade volume | Difference of signed volume ratios |
| `book_ofi` | Follow relative decayed top-of-book order-flow imbalance | Depth-normalized sampled OFI |
| `cusum_reversal` | Fade a decayed cumulative spread shock after drift adjustment | Basis points |
| `regime_adaptive` | Follow momentum when return autocorrelation exceeds 0.15, otherwise revert to the median | Basis points |

Signals use delivered observations only. The rolling history samples eligible
synchronized books at intervals of at least one second. The median, rank, RSI,
and correlation calculations use up to 60 seconds of this history. The channel
uses 30 seconds. Trade flow and sampled OFI decay over ten seconds. A trade event
updates its flow state and returns zero orders. Decisions remain on book callbacks
under the published 2 ms book and 0.1 ms trade compute profile.

Each entry is at most 0.1 SOL per venue, with one relative position, a 60-second
warmup, a 60-second entry cooldown, and a maximum of twelve entry attempts per
process. Positions close when the raw signal loses its direction after five
seconds, at a 120-second holding limit, or after a 0.05 USDT/SOL adverse spread
change. Entry costs, including depth-weighted slippage and fees, must be below
25 basis points. The signal threshold deliberately permits forecast movements
smaller than full roundtrip fees. The Judge continues to charge all configured
fees. These variants provide active experiments whose profitability requires
measurement.

The executor reuses the active EWMA example's cash, turnover, freshness,
synchronization, pending-report, consumed-depth, and residual-hedging logic.
The minimum signal strength and risk limits are fixed in the catalog before
Codabench evaluation. The rules estimate their state online and require no
offline fitting or closed data.

The microstructure variants adapt the queue imbalance, order-flow imbalance,
and weighted-midpoint ideas in the publications below. The weighted midpoint is
a simplified microprice proxy. Sampled 100 ms snapshots provide an OFI proxy
with aggregate changes. The regime rule uses a simple autocorrelation heuristic.
These are methodological adaptations to this contest's data and execution rules.

- Gould and Bonart, *Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book*. [Publication](https://arxiv.org/abs/1512.03492)
- Cont, Kukanov, and Stoikov, *The Price Impact of Order Book Events*. [Publication](https://arxiv.org/abs/1011.6402)
- Stoikov, *The micro-price — a high-frequency estimator of future prices*. [Publication](https://doi.org/10.1080/14697688.2018.1489139)
- Page, *Continuous Inspection Schemes*. The CUSUM variant applies a decayed two-sided detector to spread increments. [Publication](https://doi.org/10.1093/biomet/41.1-2.100)
- Krauss, *Statistical Arbitrage Pairs Trading Strategies — Review and Outlook*, provides context for the rolling spread rules. [Publication](https://doi.org/10.1111/joes.12153)

Package variants to a directory outside the source repository. The command emits
twelve standalone folders, twelve ZIP archives, and a hash manifest. Runtime
uses Python's standard library.

```bash
python research/package_active_suite.py --output /path/to/experiment
python -m spot_contest evaluate --config examples/real-test-config.json \
  --data /path/to/public/episode.jsonl \
  --submission /path/to/experiment/submissions/median_mad \
  --output /path/to/new/run --runner subprocess
```

Each archive has `strategy.py`, `execution.py`, `parameters.json`, and an empty
`model.json` at its root. Subprocess evaluation is for trusted local development.
