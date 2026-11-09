"""Diagnostic: cross positive raw spreads, ignoring fees; expect costs to matter.

Not a recommended strategy. Bounded size/cooldown and one outstanding pair.
"""

from decimal import ROUND_DOWN, ROUND_UP, Decimal


class Strategy:
    def on_start(self, config, portfolio):
        self.config = config
        self.books = {}
        self.pending = set()
        self.counter = 0
        self.last = -(10**30)
        return []

    def on_market_event(self, event, portfolio):
        if event["event_type"] != "book":
            return []
        name = event["exchange"]
        if not event["valid"]:
            self.books.pop(name, None)
            return []
        if not event["is_snapshot"]:
            return []
        self.books[name] = event
        now = event["now_ns"]
        if len(self.books) != 2 or self.pending or now - self.last < 1_000_000_000:
            return []
        names = list(self.config["venues"])
        for buy, sell in (names, names[::-1]):
            a, b = self.books[buy], self.books[sell]
            if any(
                now - x["receive_ts_ns"] + x.get("book_age_ns", 0) > self.config["max_book_age_ns"]
                for x in (a, b)
            ):
                continue
            asks = [(Decimal(p), Decimal(q)) for p, q in a["asks"] if Decimal(q) > 0]
            bids = [(Decimal(p), Decimal(q)) for p, q in b["bids"] if Decimal(q) > 0]
            if not asks or not bids:
                continue
            ask, aq = min(asks)
            bid, bq = max(bids)
            if bid <= ask:
                continue
            av, bv = self.config["venues"][buy], self.config["venues"][sell]
            lot = max(Decimal(av["lot_size"]), Decimal(bv["lot_size"]))
            qty = min(
                Decimal("0.1"),
                aq * Decimal("0.05"),
                bq * Decimal("0.05"),
                Decimal(portfolio[sell]["base"]),
                Decimal(portfolio[buy]["quote"]) / (ask * Decimal("1.01")),
            )
            qty = (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot
            if (
                qty <= 0
                or qty * ask < Decimal(av["min_notional"])
                or qty * bid < Decimal(bv["min_notional"])
            ):
                continue
            orders = []
            for exchange, side, price, rounding in (
                (buy, "Buy", ask, ROUND_UP),
                (sell, "Sell", bid, ROUND_DOWN),
            ):
                tick = Decimal(self.config["venues"][exchange]["tick_size"])
                price = (price / tick).to_integral_value(rounding=rounding) * tick
                self.counter += 1
                oid = str(self.counter)
                self.pending.add(oid)
                orders.append(
                    dict(
                        id=oid,
                        exchange=exchange,
                        symbol=self.config["symbol"],
                        category="spot",
                        side=side,
                        type="Limit",
                        time_in_force="IOC",
                        qty=str(qty),
                        price=str(price),
                    )
                )
            self.last = now
            return orders
        return []

    def on_execution_report(self, report, portfolio):
        self.pending.discard(report["id"])
        return []
