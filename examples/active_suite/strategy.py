"""Twelve causal relative-value signals with a shared small-position executor."""

import math
import statistics
from collections import deque

from execution import PairedExecution


class Strategy(PairedExecution):
    def on_start(self, config, portfolio):
        super().on_start(config, portfolio)
        self.history = deque()
        self.fast = None
        self.slow = None
        self.last_observed = None
        self.last_spread = None
        self.edge = 0.0
        self.cusum_up = self.cusum_down = 0.0
        self.trade_flow = {name: [0.0, 0.0, None] for name in self.names}
        self.book_flow = {name: [0.0, None] for name in self.names}
        self.top_quotes = {}
        self.signal = getattr(self, "signal_" + self.params["mode"])
        return []

    def on_market_event(self, event, portfolio):
        if event["event_type"] == "trade":
            state = self.trade_flow[event["exchange"]]
            now = event["now_ns"]
            decay = 0.0 if state[2] is None else math.exp(-(now - state[2]) / 10e9)
            quantity = float(event["qty"])
            signed = -quantity if event["is_buyer_maker"] else quantity
            state[:] = [state[0] * decay + signed, state[1] * decay + quantity, now]
            return []
        return super().on_market_event(event, portfolio)

    def update_book(self, event):
        super().update_book(event)
        name = event["exchange"]
        book = self.books[name]
        if not book["valid"]:
            self.top_quotes.pop(name, None)
            self.book_flow[name] = [0.0, None]
            return
        bid, ask = max(book["bids"]), min(book["asks"])
        current = (
            float(bid),
            float(ask),
            float(book["bids"][bid]),
            float(book["asks"][ask]),
            book["epoch"],
        )
        previous = self.top_quotes.get(name)
        self.top_quotes[name] = current
        if previous is None or current[4] != previous[4]:
            self.book_flow[name] = [0.0, event["now_ns"]]
            return
        bid_flow = current[2] if current[0] >= previous[0] else 0.0
        bid_flow -= previous[2] if current[0] <= previous[0] else 0.0
        ask_flow = current[3] if current[1] <= previous[1] else 0.0
        ask_flow -= previous[3] if current[1] >= previous[1] else 0.0
        depth = max((current[2] + current[3] + previous[2] + previous[3]) / 4, 1e-9)
        state = self.book_flow[name]
        decay = math.exp(-(event["now_ns"] - state[1]) / 10e9)
        state[:] = [
            state[0] * decay + max(-10, min(10, (bid_flow - ask_flow) / depth)),
            event["now_ns"],
        ]

    def observe(self, spread, now):
        self.now = now
        self.spread = spread
        self.mids = [
            float((max(self.books[name]["bids"]) + min(self.books[name]["asks"])) / 2)
            for name in self.names
        ]
        self.bps = 10000 / self.mids[0]
        elapsed = 0 if self.last_observed is None else (now - self.last_observed) / 1e9
        if self.fast is None:
            self.fast = self.slow = spread
        else:
            self.fast += -math.expm1(-elapsed / 5) * (spread - self.fast)
            self.slow += -math.expm1(-elapsed / 60) * (spread - self.slow)
            delta = (spread - self.last_spread) * self.bps
            decay = math.exp(-elapsed / 10)
            self.cusum_up = max(0, decay * self.cusum_up + delta - 0.05 * elapsed)
            self.cusum_down = min(0, decay * self.cusum_down + delta + 0.05 * elapsed)
        self.last_observed, self.last_spread = now, spread
        if not self.history or now - self.history[-1][0] >= 1e9:
            self.history.append((now, spread, *self.mids))
        while self.history and self.history[0][0] < now - 120e9:
            self.history.popleft()
        self.values = [row[1] for row in self.history if row[0] >= now - 60e9]
        self.edge = self.signal()

    def lag(self, seconds):
        return next(
            (row for row in reversed(self.history) if row[0] <= self.now - seconds * 1e9),
            self.history[0],
        )

    def imbalance(self, name, levels):
        book = self.books[name]
        bids = sum(
            float(book["bids"][price]) for price in sorted(book["bids"], reverse=True)[:levels]
        )
        asks = sum(float(book["asks"][price]) for price in sorted(book["asks"])[:levels])
        return (bids - asks) / max(bids + asks, 1e-9)

    def signal_median_mad(self):
        center = statistics.median(self.values)
        mad = statistics.median(abs(value - center) for value in self.values)
        return (center - self.spread) / max(1.4826 * mad, 0.005)

    def signal_quantile_band(self):
        below = sum(value < self.spread for value in self.values)
        tied = sum(value == self.spread for value in self.values)
        return 1 - 2 * (below + tied / 2) / len(self.values)

    def signal_ema_momentum(self):
        return (self.fast - self.slow) * self.bps

    def signal_channel_breakout(self):
        values = [row[1] for row in self.history if self.now - 30e9 <= row[0] < self.now]
        if not values:
            return 0.0
        return (max(0, self.spread - max(values)) + min(0, self.spread - min(values))) * self.bps

    def signal_rsi_reversal(self):
        values = self.values[-21:]
        changes = [following - previous for previous, following in zip(values, values[1:])]
        gains = sum(max(0, value) for value in changes)
        losses = sum(max(0, -value) for value in changes)
        return (losses - gains) / max(gains + losses, 1e-9)

    def signal_lead_lag(self):
        row = self.lag(5)
        return ((self.mids[0] - row[2]) / row[2] - (self.mids[1] - row[3]) / row[3]) * 10000

    def signal_queue_pressure(self):
        return self.imbalance(self.names[1], 5) - self.imbalance(self.names[0], 5)

    def signal_microprice(self):
        shifts = []
        for name in self.names:
            book = self.books[name]
            bid, ask = max(book["bids"]), min(book["asks"])
            shifts.append(self.imbalance(name, 1) * float(ask - bid) / 2)
        return (shifts[1] - shifts[0]) * self.bps

    def signal_trade_flow(self):
        ratios = []
        for name in self.names:
            signed, total, timestamp = self.trade_flow[name]
            if timestamp is None or self.now - timestamp > 10e9:
                ratios.append(0.0)
            else:
                ratios.append(signed / max(total, 1e-9))
        return ratios[1] - ratios[0]

    def signal_book_ofi(self):
        values = []
        for name in self.names:
            value, timestamp = self.book_flow[name]
            values.append(
                0.0 if timestamp is None else value * math.exp(-(self.now - timestamp) / 10e9)
            )
        return values[1] - values[0]

    def signal_cusum_reversal(self):
        return -(self.cusum_up + self.cusum_down)

    def signal_regime_adaptive(self):
        changes = [
            following - previous for previous, following in zip(self.values, self.values[1:])
        ]
        if len(changes) < 10:
            return 0.0
        center = statistics.mean(changes)
        covariance = sum(
            (first - center) * (second - center) for first, second in zip(changes, changes[1:])
        )
        variance = sum((value - center) ** 2 for value in changes)
        correlation = covariance / max(variance, 1e-12)
        if correlation > 0.15:
            return (self.spread - self.lag(5)[1]) * self.bps
        return (statistics.median(self.values) - self.spread) * self.bps

    def entry_signal(self, spread, cost):
        if self.entries >= self.params["max_entries"]:
            return 0
        if cost * self.bps > self.params["max_entry_cost_bps"]:
            return 0
        if abs(self.edge) < self.params["signal_threshold"]:
            return 0
        return 1 if self.edge > 0 else -1

    def exit_signal(self, direction, spread, cost, age):
        return age >= self.params["minimum_hold_seconds"] and direction * self.edge <= 0
