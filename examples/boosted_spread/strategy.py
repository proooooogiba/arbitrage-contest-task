"""Gradient-boosted relative-price forecast with explicit roundtrip costs."""

from struct import pack, unpack

from execution import PairedExecution


class Strategy(PairedExecution):
    def observe(self, spread, now):
        features = [spread]
        for name in self.names:
            book = self.books[name]
            bid, ask = max(book["bids"]), min(book["asks"])
            bid_qty, ask_qty = float(book["bids"][bid]), float(book["asks"][ask])
            bids = sum(
                float(book["bids"][price]) for price in sorted(book["bids"], reverse=True)[:5]
            )
            asks = sum(float(book["asks"][price]) for price in sorted(book["asks"])[:5])
            features.extend(
                [
                    (bid_qty - ask_qty) / (bid_qty + ask_qty + 1e-12),
                    (bids - asks) / (bids + asks + 1e-12),
                    float(ask - bid),
                ]
            )
        features = [unpack("f", pack("f", value))[0] for value in features]
        self.forecast = self.model["initial"]
        for tree in self.model["trees"]:
            node = 0
            while tree["left"][node] != -1:
                node = (
                    tree["left"][node]
                    if features[tree["feature"][node]] <= tree["threshold"][node]
                    else tree["right"][node]
                )
            self.forecast += self.model["learning_rate"] * tree["value"][node]

    def entry_signal(self, spread, cost):
        change = self.forecast - spread
        required = cost + self.model["exit_cost"] + self.model["uncertainty_margin"]
        if abs(change) <= required + self.params["profit_margin"]:
            return 0
        return 1 if change > 0 else -1

    def exit_signal(self, direction, spread, cost, age):
        return age >= 1 and direction * (self.forecast - spread) <= 0
