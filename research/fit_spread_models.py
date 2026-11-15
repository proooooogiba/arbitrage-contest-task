"""Fit two spread models on public chronological data and export standalone policies."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


def prepare_cache(data, cache, include_depth=False):
    import pyarrow.parquet as pq

    cache.mkdir(parents=True, exist_ok=True)
    columns = [
        "window_end_ns",
        "ts_publish_ns",
        "ts_recv_ns",
        "valid",
        "is_filled",
        "book_age_ns",
        "gap_count",
        "resync_count",
        "bid_prices",
        "ask_prices",
    ]
    if include_depth:
        columns.extend(["bid_quantities", "ask_quantities"])
    for venue in ("binance", "bybit"):
        chunks = []
        path = data / (venue + "_spot-market_frames.parquet")
        for batch in pq.ParquetFile(path).iter_batches(columns=columns, batch_size=65536):
            frame = batch.to_pydict()
            bid = np.asarray(frame["bid_prices"], dtype=float)[:, 0]
            ask = np.asarray(frame["ask_prices"], dtype=float)[:, 0]
            age = np.asarray(frame["book_age_ns"], dtype=np.int64)
            good = np.asarray(frame["valid"]) & ~np.asarray(frame["is_filled"])
            good &= (age < 480_000_000) & (np.asarray(frame["gap_count"]) == 0)
            good &= np.asarray(frame["resync_count"]) == 0
            available = np.maximum.reduce(
                [
                    np.asarray(frame[key], dtype=np.int64)
                    for key in ("window_end_ns", "ts_publish_ns", "ts_recv_ns")
                ]
            )
            chunk = dict(t=available, good=good, bid=bid, ask=ask, age=age)
            if include_depth:
                bids = np.asarray(frame["bid_quantities"], dtype=float)
                asks = np.asarray(frame["ask_quantities"], dtype=float)
                bid_depth = bids[:, :5].sum(axis=1)
                ask_depth = asks[:, :5].sum(axis=1)
                chunk.update(
                    bq=bids[:, 0],
                    aq=asks[:, 0],
                    imb5=(bid_depth - ask_depth) / (bid_depth + ask_depth + 1e-12),
                )
            chunks.append(chunk)
        combined = {key: np.concatenate([chunk[key] for chunk in chunks]) for key in chunks[0]}
        np.savez_compressed(cache / (venue + "-bbo.npz"), **combined)


def public_samples(cache):
    venues = {}
    hashes = {}
    for name in ("binance", "bybit"):
        path = cache / (name + "-bbo.npz")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with np.load(path) as archive:
            venues[name] = dict(archive)
    start = max(venue["t"][0] for venue in venues.values())
    end = min(venue["t"][-1] for venue in venues.values())
    timestamps = np.arange(start, end, 1_000_000_000, dtype=np.int64)
    aligned = {}
    for name, venue in venues.items():
        indices = np.searchsorted(venue["t"], timestamps, side="right") - 1
        indices = np.maximum(indices, 0)
        aligned[name] = {key: value[indices] for key, value in venue.items()}
    first, second = aligned["binance"], aligned["bybit"]
    good = first["good"] & second["good"]
    good &= np.abs(first["t"] - second["t"]) <= 20_000_000
    for venue in aligned.values():
        good &= timestamps - venue["t"] + venue["age"] <= 480_000_000
    first_mid = (first["bid"] + first["ask"]) / 2
    second_mid = (second["bid"] + second["ask"]) / 2
    spread = second_mid - first_mid
    cost = (first["ask"] - first["bid"] + second["ask"] - second["bid"]) / 2
    cost += (first_mid + second_mid) * 0.001
    return timestamps, spread, cost, good, hashes


def fit_models(timestamps, spread, cost, good, train_hours):
    cutoff = timestamps[0] + int(train_hours * 3600 * 1e9)
    training = good & (timestamps < cutoff)
    low, high = np.quantile(spread[training], [0.001, 0.999])
    clean = np.clip(spread, low, high)
    pairs = training[1:] & training[:-1]
    previous, following = clean[:-1][pairs], clean[1:][pairs]
    design = np.column_stack([np.ones(len(previous)), previous])
    intercept, rho = np.linalg.lstsq(design, following, rcond=None)[0]
    rho = float(np.clip(rho, 0.01, 0.9995))
    mean = float(np.clip(intercept / (1 - rho), low, high))
    residual = following - mean - rho * (previous - mean)
    innovation = max(float(np.mean(residual**2)), 1e-8)
    variance = max(float(np.var(clean[training])), 1e-8)
    triples = training[2:] & training[1:-1] & training[:-2]
    centered = clean - np.mean(clean[training])
    covariance_one = float(np.mean(centered[:-1][pairs] * centered[1:][pairs]))
    covariance_two = float(np.mean(centered[:-2][triples] * centered[2:][triples]))
    latent_rho = float(np.clip(covariance_two / max(covariance_one, 1e-10), 0.01, 0.9995))
    latent_variance = float(np.clip(covariance_one / latent_rho, variance * 0.05, variance * 0.99))
    common = {
        "mean": mean,
        "exit_cost": float(np.quantile(cost[training], 0.95)),
        "training_samples": int(training.sum()),
        "training_pairs": int(pairs.sum()),
        "training_cutoff_ns": int(cutoff),
        "winsor_limits": [float(low), float(high)],
    }
    ou = dict(
        common,
        rho=rho,
        innovation_variance=innovation,
        stationary_variance=innovation / (1 - rho * rho),
    )
    kalman = dict(
        common,
        mean=float(np.mean(clean[training])),
        rho=latent_rho,
        stationary_variance=latent_variance,
        observation_variance=max(variance - latent_variance, 1e-8),
    )
    return ou, kalman


def stopping_policy(model):
    grid = np.linspace(-2, 2, 801)
    nodes, weights = np.polynomial.hermite.hermgauss(9)
    weights /= np.sqrt(np.pi)
    holding_seconds = 300
    entry_seconds = 600
    policies = {}
    for direction in (-1, 1):
        future = direction * model["mean"] + model["rho"] * (grid - direction * model["mean"])
        transitions = future[None, :] + nodes[:, None] * np.sqrt(2 * model["innovation_variance"])

        def expectation(value):
            interpolated = np.interp(transitions.ravel(), grid, value).reshape(transitions.shape)
            return weights @ interpolated

        reward = grid - model["exit_cost"]
        exit_value = reward.copy()
        exit_thresholds = [float(grid[0])]
        for _ in range(holding_seconds):
            waiting = expectation(exit_value)
            stopping = grid[reward >= waiting - 1e-10]
            exit_thresholds.append(float(stopping[0]) if len(stopping) else float(grid[-1]))
            exit_value = np.maximum(reward, waiting)
        entry_reward = exit_value - grid - model["exit_cost"]
        entry_value = np.maximum(entry_reward, 0)
        for _ in range(entry_seconds):
            entry_value = np.maximum(entry_reward, expectation(entry_value))
        policies[str(direction)] = {
            "exit_value": exit_value.tolist(),
            "exit_thresholds": exit_thresholds,
            "entry_wait": expectation(entry_value).tolist(),
        }
    return dict(
        model,
        grid_start=float(grid[0]),
        grid_step=float(grid[1] - grid[0]),
        holding_seconds=holding_seconds,
        entry_planning_seconds=entry_seconds,
        policies=policies,
    )


def forecast_validation(model, timestamps, spread, good, kalman=False):
    horizon = 60
    forecast, observed = [], []
    estimate = model["mean"]
    variance = model["stationary_variance"]
    last_time = None
    decay = model["rho"] ** horizon
    for index in range(len(spread) - horizon):
        if not good[index]:
            continue
        if kalman:
            elapsed = 1 if last_time is None else (timestamps[index] - last_time) / 1e9
            transition = model["rho"] ** elapsed
            prior = model["mean"] + transition * (estimate - model["mean"])
            variance = transition**2 * variance + model["stationary_variance"] * (1 - transition**2)
            gain = variance / (variance + model["observation_variance"])
            estimate = prior + gain * (spread[index] - prior)
            variance *= 1 - gain
            last_time = timestamps[index]
        else:
            estimate = spread[index]
        if timestamps[index] < model["training_cutoff_ns"] or not good[index + horizon]:
            continue
        forecast.append(model["mean"] + decay * (estimate - model["mean"]))
        observed.append(spread[index + horizon])
    errors = np.asarray(forecast) - np.asarray(observed)
    return {
        "horizon_seconds": horizon,
        "samples": len(errors),
        "rmse_usdt_per_sol": float(np.sqrt(np.mean(errors**2))),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--data", type=Path, help="Public Train directory, used to build the cache")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--kind", choices=("ou", "kalman"), required=True)
    parser.add_argument("--train-hours", type=int, default=48)
    args = parser.parse_args()
    if args.data is not None:
        prepare_cache(args.data, args.cache)
    timestamps, spread, cost, good, hashes = public_samples(args.cache)
    ou, kalman = fit_models(timestamps, spread, cost, good, args.train_hours)
    model = stopping_policy(ou) if args.kind == "ou" else kalman
    model["public_cache_sha256"] = hashes
    model["forecast_validation"] = forecast_validation(
        model, timestamps, spread, good, kalman=args.kind == "kalman"
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "model.json").write_text(json.dumps(model, indent=2) + "\n")
    parameters = {
        "max_qty": 1,
        "warmup_seconds": 10,
        "cooldown_seconds": 2,
        "max_hold_seconds": 300,
        "stop_loss": max(4 * model["exit_cost"], 6 * np.sqrt(model["stationary_variance"])),
        "profit_margin": 0.005,
    }
    if args.kind == "kalman":
        parameters.update(forecast_seconds=60, confidence=1, minimum_hold_seconds=1)
    (args.output / "parameters.json").write_text(json.dumps(parameters, indent=2) + "\n")
    print(json.dumps({key: value for key, value in model.items() if key != "policies"}, indent=2))


if __name__ == "__main__":
    main()
