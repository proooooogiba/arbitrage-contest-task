"""Active relative-value trading with bounded small spot inventory."""

import math

from execution import PairedExecution


class Strategy(PairedExecution):
    def on_start(self, config, portfolio):
        super().on_start(config, portfolio)
        self.mean = None
        self.variance = 0.0
        self.observed_time = None
        return []

    def observe(self, spread, now):
        if self.mean is None:
            self.mean = spread
        else:
            elapsed = (now - self.observed_time) / 1e9
            alpha = -math.expm1(-elapsed / self.params["mean_seconds"])
            delta = spread - self.mean
            self.mean += alpha * delta
            self.variance = (1 - alpha) * (self.variance + alpha * delta * delta)
        self.observed_time = now

    def entry_signal(self, spread, cost):
        if self.entries >= self.params["max_entries"]:
            return 0
        first = self.books[self.names[0]]
        midpoint = float((max(first["bids"]) + min(first["asks"])) / 2)
        if cost / midpoint * 10000 > self.params["max_entry_cost_bps"]:
            return 0
        threshold = max(
            midpoint * self.params["entry_bps"] / 10000,
            self.params["z_score"] * math.sqrt(self.variance),
        )
        deviation = spread - self.mean
        if abs(deviation) < threshold:
            return 0
        return -1 if deviation > 0 else 1

    def exit_signal(self, direction, spread, cost, age):
        return age >= self.params["minimum_hold_seconds"] and direction * (spread - self.mean) >= 0
