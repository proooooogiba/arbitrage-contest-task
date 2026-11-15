"""Train and export a shallow gradient boosting forecast using public frames only."""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from fit_spread_models import prepare_cache, public_samples
from sklearn.ensemble import GradientBoostingRegressor


def samples(cache):
    venues, hashes = {}, {}
    for name in ("binance", "bybit"):
        path = cache / (name + "-bbo.npz")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with np.load(path) as archive:
            venues[name] = dict(archive)
    start = max(value["t"][0] for value in venues.values())
    end = min(value["t"][-1] for value in venues.values())
    timestamps = np.arange(start, end, 10**9, dtype=np.int64)
    aligned = {}
    good = np.ones(len(timestamps), dtype=bool)
    for name, value in venues.items():
        index = np.searchsorted(value["t"], timestamps, side="right") - 1
        aligned[name] = {key: array[np.maximum(index, 0)] for key, array in value.items()}
        frame = aligned[name]
        good &= (index >= 0) & frame["good"]
        good &= timestamps - frame["t"] + frame["age"] <= 480_000_000
    first, second = aligned["binance"], aligned["bybit"]
    good &= np.abs(first["t"] - second["t"]) <= 20_000_000
    mids = [(frame["ask"] + frame["bid"]) / 2 for frame in (first, second)]
    spread = mids[1] - mids[0]
    features = [spread]
    names = ["spread_usdt"]
    for name, frame in aligned.items():
        features.extend(
            [
                (frame["bq"] - frame["aq"]) / (frame["bq"] + frame["aq"] + 1e-12),
                frame["imb5"],
                frame["ask"] - frame["bid"],
            ]
        )
        names.extend([name + "_imbalance_top", name + "_imbalance_5", name + "_spread"])
    return timestamps, spread, np.column_stack(features), good, hashes, names


def export_tree(estimator):
    tree = estimator.tree_
    return dict(
        left=tree.children_left.tolist(),
        right=tree.children_right.tolist(),
        feature=tree.feature.tolist(),
        threshold=tree.threshold.tolist(),
        value=tree.value[:, 0, 0].tolist(),
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--data", type=Path, help="Public Train directory used to build a depth cache"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.data is not None:
        prepare_cache(args.data, args.cache, include_depth=True)
    timestamps, spread, features, good, hashes, names = samples(args.cache)
    horizon = 60
    cutoff = timestamps[0] + 48 * 3600 * 10**9
    calibration_end = timestamps[0] + 60 * 3600 * 10**9
    indices = np.arange(len(spread) - horizon)
    usable = good[indices] & good[indices + horizon]
    training = indices[usable & (timestamps[indices + horizon] < cutoff)]
    calibration = indices[
        usable & (timestamps[indices] >= cutoff) & (timestamps[indices + horizon] < calibration_end)
    ]
    test = indices[usable & (timestamps[indices] >= calibration_end)]
    estimator = GradientBoostingRegressor(
        n_estimators=80,
        learning_rate=0.05,
        max_depth=3,
        min_samples_leaf=80,
        loss="squared_error",
        random_state=17,
    )
    estimator.fit(features[training], spread[training + horizon])
    # The runtime uses float32 feature comparisons to match sklearn tree inference.
    calibration_errors = estimator.predict(features[calibration]) - spread[calibration + horizon]
    margin = float(np.quantile(np.abs(calibration_errors), 0.95))
    model = dict(
        initial=float(estimator.init_.constant_[0, 0]),
        learning_rate=estimator.learning_rate,
        trees=[export_tree(tree[0]) for tree in estimator.estimators_],
        feature_names=names,
        forecast_seconds=horizon,
        uncertainty_margin=margin,
        exit_cost=0.0,
        training_cutoff_ns=int(cutoff),
        calibration_end_ns=int(calibration_end),
        training_samples=len(training),
        calibration_samples=len(calibration),
        public_cache_sha256=hashes,
    )
    # Estimate closing spread and fees using the same causal public cache.
    cost_t, _, cost, cost_good, _ = public_samples(args.cache)
    model["exit_cost"] = float(np.quantile(cost[cost_good & (cost_t < cutoff)], 0.95))
    prediction = estimator.predict(features[test])
    model["test_forecast"] = dict(
        samples=len(test),
        rmse=float(np.sqrt(np.mean((prediction - spread[test + horizon]) ** 2))),
        unchanged_rmse=float(np.sqrt(np.mean((spread[test] - spread[test + horizon]) ** 2))),
    )
    # Check the exported tree evaluator independently against sklearn.
    from struct import pack, unpack

    exported_predictions = []
    for row in features[test[:: max(1, len(test) // 1000)]]:
        row = [unpack("f", pack("f", value))[0] for value in row]
        value = model["initial"]
        for tree in model["trees"]:
            node = 0
            while tree["left"][node] != -1:
                node = (
                    tree["left"][node]
                    if row[tree["feature"][node]] <= tree["threshold"][node]
                    else tree["right"][node]
                )
            value += model["learning_rate"] * tree["value"][node]
        exported_predictions.append(value)
    difference = np.max(
        np.abs(np.asarray(exported_predictions) - prediction[:: max(1, len(test) // 1000)])
    )
    if difference > 1e-10:
        raise ValueError("exported tree predictions differ from sklearn")
    model["export_max_absolute_error"] = float(difference)
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
    print(json.dumps({key: value for key, value in model.items() if key != "trees"}, indent=2))


if __name__ == "__main__":
    main()
