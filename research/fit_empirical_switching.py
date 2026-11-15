"""Fit a finite-state switching policy using chronological public spread transitions."""

import argparse
import json
from pathlib import Path

import numpy as np
from fit_spread_models import fit_models, public_samples


def fit_policy(timestamps, spread, cost, good, cutoff):
    training = good & (timestamps < cutoff)
    pairs = training[:-1] & training[1:]
    previous, following = spread[:-1][pairs], spread[1:][pairs]
    # Quantile knots resolve the center and a fixed outer grid supports deviations
    # beyond the tiny negative tail in this particular training interval.
    grid = np.unique(
        np.r_[np.linspace(-2, 2, 201), np.quantile(spread[training], np.linspace(0, 1, 81))]
    )
    centers = (grid[:-1] + grid[1:]) / 2
    states = np.searchsorted(centers, previous)
    prior, _ = fit_models(timestamps, spread, cost, good, 48)
    residuals = following - prior["mean"] - prior["rho"] * (previous - prior["mean"])
    transition = np.zeros((len(grid), len(grid)))
    for state in range(len(grid)):
        observed = following[states == state]
        predicted = prior["mean"] + prior["rho"] * (grid[state] - prior["mean"]) + residuals
        local_weight = len(observed) / (len(observed) + 20)
        for draws, mass in ((observed, local_weight), (predicted, 1 - local_weight)):
            if not len(draws):
                continue
            right = np.clip(np.searchsorted(grid, draws), 1, len(grid) - 1)
            weight = np.clip((draws - grid[right - 1]) / (grid[right] - grid[right - 1]), 0, 1)
            np.add.at(transition[state], right - 1, mass * (1 - weight) / len(draws))
            np.add.at(transition[state], right, mass * weight / len(draws))
    exit_cost = float(np.quantile(cost[training], 0.95))
    positions = np.asarray([-1, 0, 1])
    value = positions[:, None] * grid - np.abs(positions[:, None]) * exit_cost
    continuation = [value.tolist()]
    holding_seconds = 300
    for _ in range(holding_seconds):
        waiting = value @ transition.T
        continuation.append(waiting.tolist())
        value = waiting.copy()
        # A switch consumes one decision interval. Reverse positions pass through
        # flat, so four legs can never be returned as an entry pair.
        value[1] = np.maximum.reduce(
            [waiting[1], -grid - exit_cost + waiting[2], grid - exit_cost + waiting[0]]
        )
        value[0] = np.maximum(waiting[0], -grid - exit_cost + waiting[1])
        value[2] = np.maximum(waiting[2], grid - exit_cost + waiting[1])
    return {
        "grid": grid.tolist(),
        "continuation": continuation,
        "holding_seconds": holding_seconds,
        "exit_cost": exit_cost,
        "training_cutoff_ns": int(cutoff),
        "training_samples": int(training.sum()),
        "training_pairs": int(pairs.sum()),
        "prior_mean": prior["mean"],
        "prior_rho": prior["rho"],
        "prior_transition_strength": 20,
        "state_transition_counts": np.bincount(states, minlength=len(grid)).tolist(),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    timestamps, spread, cost, good, hashes = public_samples(args.cache)
    model = fit_policy(timestamps, spread, cost, good, timestamps[0] + 48 * 3600 * 10**9)
    model["public_cache_sha256"] = hashes
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "model.json").write_text(json.dumps(model, separators=(",", ":")) + "\n")
    parameters = dict(
        max_qty=1,
        warmup_seconds=10,
        cooldown_seconds=2,
        max_hold_seconds=300,
        stop_loss=4 * model["exit_cost"],
        profit_margin=0.005,
    )
    (args.output / "parameters.json").write_text(json.dumps(parameters, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in model.items() if key not in ("grid", "continuation")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
