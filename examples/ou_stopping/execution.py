"""Paired IOC execution reused from the fee-depth research strategy."""

import json
from collections import deque
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

D = Decimal
ZERO = D("0")


class PairedExecution:
    def on_start(self, config, portfolio):
        self.config = config
        self.names = ["binance", "bybit"]
        self.initial = {name: D(portfolio[name]["base"]) for name in self.names}
        self.fees = {
            name: D(config["venues"][name]["taker_fee_bps"]) / 10000 for name in self.names
        }
        self.lot = max(D(config["venues"][name]["lot_size"]) for name in self.names)
        self.fraction = D(config["depth_fraction"])
        self.books = {}
        self.pending = {}
        self.counter = 0
        self.windows = {name: deque() for name in self.names}
        root = Path(__file__).parent
        self.params = json.loads((root / "parameters.json").read_text())
        self.model = json.loads((root / "model.json").read_text())
        self.started = None
        self.last_entry = -(10**30)
        self.entry_spread = None
        self.position_since = None
        return []

    def current_cost(self):
        cost = ZERO
        for name in self.names:
            book = self.books[name]
            bid, ask = max(book["bids"]), min(book["asks"])
            cost += (ask - bid) / 2 + (ask + bid) / 2 * self.fees[name]
        return float(cost)

    def pair(self, direction, quantity, portfolio, now, entry_spread=None):
        buy, sell = self.names[::-1] if direction > 0 else self.names
        asks, bids = self.levels(buy, "asks"), self.levels(sell, "bids")
        quantity = self.floor(
            min(
                quantity,
                sum((qty for _, qty in asks), ZERO),
                sum((qty for _, qty in bids), ZERO),
                self.budget(buy, now) / asks[-1][0],
                self.budget(sell, now) / bids[0][0],
                D(portfolio[buy]["quote"]) / (asks[-1][0] * (1 + self.fees[buy])),
                D(portfolio[sell]["base"]),
            )
        )
        if quantity <= ZERO:
            return []
        prices = []
        notionals = []
        for name, levels in ((buy, asks), (sell, bids)):
            remaining = quantity
            notional = ZERO
            for price, capacity in levels:
                filled = min(remaining, capacity)
                remaining -= filled
                notional += filled * price
                if remaining == ZERO:
                    break
            if quantity * price < D(self.config["venues"][name]["min_notional"]):
                return []
            prices.append(price)
            notionals.append(notional)
        if entry_spread is not None:
            cash_cost = notionals[0] * (1 + self.fees[buy])
            cash_cost -= notionals[1] * (1 - self.fees[sell])
            execution_cost = float(cash_cost / quantity) - direction * entry_spread
            if self.entry_signal(entry_spread, execution_cost) != direction:
                return []
        return [
            self.order(buy, "Buy", quantity, prices[0], now),
            self.order(sell, "Sell", quantity, prices[1], now),
        ]

    def on_market_event(self, event, portfolio):
        if event["event_type"] != "book":
            return []
        self.update_book(event)
        now = event["now_ns"]
        if not self.pending:
            residual = sum(
                (D(portfolio[name]["base"]) - self.initial[name] for name in self.names), ZERO
            )
            if residual:
                return self.hedge(portfolio, now)
        if not all(self.fresh(name, now) for name in self.names):
            return []
        times = [self.books[name]["t"] for name in self.names]
        if max(times) - min(times) > 20_000_000 or now - max(times) > 30_000_000:
            return []
        mids = [
            (max(self.books[name]["bids"]) + min(self.books[name]["asks"])) / 2
            for name in self.names
        ]
        spread = float(mids[1] - mids[0])
        self.observe(spread, now)
        if self.started is None:
            self.started = now
        if self.pending:
            return []
        position = D(portfolio[self.names[1]]["base"]) - self.initial[self.names[1]]
        cost = self.current_cost()
        if position:
            if self.position_since is None:
                self.position_since = now
                self.entry_spread = spread
            direction = 1 if position > ZERO else -1
            age = (now - self.position_since) / 1e9
            stop_loss = direction * (spread - self.entry_spread) < -self.params["stop_loss"]
            if (
                stop_loss
                or age >= self.params["max_hold_seconds"]
                or self.exit_signal(direction, spread, cost, age)
            ):
                return self.pair(-direction, abs(position), portfolio, now)
            return []
        self.position_since = None
        self.entry_spread = None
        if now - self.started < self.params["warmup_seconds"] * 1e9:
            return []
        if now - self.last_entry < self.params["cooldown_seconds"] * 1e9:
            return []
        direction = self.entry_signal(spread, cost)
        if not direction:
            return []
        orders = self.pair(
            direction, D(str(self.params["max_qty"])), portfolio, now, entry_spread=spread
        )
        if orders:
            self.last_entry = now
        return orders

    def floor(self, value):
        return max(ZERO, (value / self.lot).to_integral_value(rounding=ROUND_DOWN) * self.lot)

    def update_book(self, event):
        name = event["exchange"]
        previous = self.books.get(name)
        epoch = (event["producer_session_id"], event["stream_epoch"])
        if previous is None or previous["epoch"] != epoch:
            previous = dict(
                epoch=epoch, bids={}, asks={}, remaining_bids={}, remaining_asks={}, update=-1
            )
        valid = event["valid"] and event["update_id"] >= previous["update"]
        if not event["is_snapshot"] and (
            not previous.get("valid")
            or event.get("previous_update_id", previous["update"]) != previous["update"]
        ):
            valid = False
        for side in ["bids", "asks"]:
            old = previous[side]
            new = {} if event["is_snapshot"] else dict(old)
            for p, q in event[side]:
                price, qty = D(p), D(q)
                if qty:
                    new[price] = qty
                else:
                    new.pop(price, None)
            remaining = {}
            for price, qty in new.items():
                remaining[price] = (
                    qty * self.fraction
                    if price not in old
                    else min(
                        qty * self.fraction,
                        previous["remaining_" + side].get(price, ZERO)
                        + max(ZERO, qty - old[price]) * self.fraction,
                    )
                )
            previous[side] = new
            previous["remaining_" + side] = remaining
        previous.update(
            valid=valid
            and bool(previous["bids"] and previous["asks"])
            and max(previous["bids"]) < min(previous["asks"]),
            update=event["update_id"],
            t=event["market_ts_ns"],
            age=event["book_age_ns"],
        )
        self.books[name] = previous

    def fresh(self, name, now):
        book = self.books.get(name)
        latency = (
            self.config["compute_latency_ns"] + self.config["venues"][name]["order_latency_ns"]
        )
        return (
            book
            and book["valid"]
            and now + latency - book["t"] + book["age"] <= self.config["max_book_age_ns"]
        )

    def levels(self, name, side):
        book = self.books[name]
        return [
            (p, self.floor(book["remaining_" + side][p]))
            for p in sorted(book[side], reverse=side == "bids")
        ]

    def order(self, name, side, qty, price, now):
        self.counter += 1
        identity = str(self.counter)
        self.pending[identity] = (name, side, qty, price)
        self.windows[name].append((now + self.config["compute_latency_ns"], qty * price))
        # Reserve visible depth conservatively. Canceled quantities replenish only
        # through subsequent observed increases, avoiding repeated depth reuse.
        remaining = qty
        book_side = "asks" if side == "Buy" else "bids"
        for p, capacity in self.levels(name, book_side):
            if side == "Buy" and p > price or side == "Sell" and p < price:
                break
            take = min(capacity, remaining)
            self.books[name]["remaining_" + book_side][p] -= take
            remaining -= take
        return dict(
            id=identity,
            exchange=name,
            category="spot",
            symbol=self.config["symbol"],
            side=side,
            type="Limit",
            time_in_force="IOC",
            qty=str(qty),
            price=str(price),
        )

    def budget(self, name, now):
        window = self.windows[name]
        ready = now + self.config["compute_latency_ns"]
        while window and window[0][0] <= ready - self.config["turnover_window_ns"]:
            window.popleft()
        return min(
            D(self.config["max_order_notional"]),
            max(ZERO, D(self.config["max_window_turnover"]) - sum((x[1] for x in window), ZERO)),
        )

    def hedge(self, portfolio, now):
        residual = sum((D(portfolio[n]["base"]) - self.initial[n] for n in self.names), ZERO)
        if not residual:
            return []
        side = "Sell" if residual > 0 else "Buy"
        candidates = []
        for name in self.names:
            if not self.fresh(name, now):
                continue
            levels = self.levels(name, "bids" if side == "Sell" else "asks")
            if not levels:
                continue
            # Cross all published levels for an urgent risk-reducing hedge.
            price = levels[-1][0]
            qty = min(abs(residual), self.budget(name, now) / price)
            if side == "Sell":
                qty = min(qty, D(portfolio[name]["base"]))
            else:
                qty = min(qty, D(portfolio[name]["quote"]) / (price * (1 + self.fees[name])))
            qty = self.floor(qty)
            minimum = D(self.config["venues"][name]["min_notional"])
            requested = max(
                qty, (minimum / price / self.lot).to_integral_value(rounding=ROUND_UP) * self.lot
            )
            if not qty or requested * price > self.budget(name, now):
                continue
            value = levels[0][0] * (1 - self.fees[name] if side == "Sell" else 1 + self.fees[name])
            candidates.append((value if side == "Sell" else -value, name, requested, price))
        if not candidates:
            return []
        _, name, qty, price = max(candidates)
        return [self.order(name, side, qty, price, now)]

    def on_execution_report(self, report, portfolio):
        self.pending.pop(report["id"], None)
        if self.pending:
            return []
        return self.hedge(portfolio, report["now_ns"])
