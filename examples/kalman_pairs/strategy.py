"""Noisy mean-reverting spread model following Elliott, van der Hoek and Malcolm."""

import math

from execution import PairedExecution


class Strategy(PairedExecution):
    def on_start(self, config, portfolio):
        super().on_start(config, portfolio)
        self.filter_time = None
        self.filter_mean = self.model["mean"]
        self.filter_variance = self.model["stationary_variance"]
        return []

    def observe(self, spread, now):
        if self.filter_time is None:
            self.filter_time = now
            self.filter_mean = spread
            return
        elapsed = (now - self.filter_time) / 1e9
        if elapsed < 1:
            return
        decay = self.model["rho"] ** elapsed
        predicted = self.model["mean"] + decay * (self.filter_mean - self.model["mean"])
        variance = decay * decay * self.filter_variance
        variance += self.model["stationary_variance"] * (1 - decay * decay)
        gain = variance / (variance + self.model["observation_variance"])
        self.filter_mean = predicted + gain * (spread - predicted)
        self.filter_variance = (1 - gain) * variance
        self.filter_time = now

    def forecast(self, seconds):
        decay = self.model["rho"] ** seconds
        expected = self.model["mean"] + decay * (self.filter_mean - self.model["mean"])
        variance = decay * decay * self.filter_variance
        variance += self.model["stationary_variance"] * (1 - decay * decay)
        return expected, math.sqrt(variance + self.model["observation_variance"])

    def entry_signal(self, spread, cost):
        expected, uncertainty = self.forecast(self.params["forecast_seconds"])
        advantage = abs(expected - spread)
        required = cost + self.model["exit_cost"] + self.params["profit_margin"]
        required += self.params["confidence"] * uncertainty
        if advantage <= required:
            return 0
        return 1 if expected > spread else -1

    def exit_signal(self, direction, spread, cost, age):
        expected, _ = self.forecast(self.params["forecast_seconds"])
        target_reached = direction * (spread - self.model["mean"]) >= 0
        return age >= self.params["minimum_hold_seconds"] and (
            target_reached or direction * (expected - spread) <= -cost
        )
