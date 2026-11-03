"""Small inventory-aware taker–taker baseline. No ML model is required by the SDK."""

from decimal import ROUND_DOWN, Decimal


class Strategy:
    def on_start(self, config, portfolio):
        self.config = config
        self.books = {}
        self.pending = set()
        self.counter = 0
        self.last_trade_ns = -(10**30)
        return []

    def on_market_event(self, event, portfolio):
        if event["event_type"] != "book":
            return []
        # The baseline accepts snapshots; a custom strategy can reconstruct deltas.
        if not event["valid"]:
            self.books.pop(event["exchange"], None)
            return []
        if not event["is_snapshot"]:
            return []
        self.books[event["exchange"]] = event
        if len(self.books) != 2 or self.pending:
            return []
        if event["receive_ts_ns"] - self.last_trade_ns < 200_000_000:
            return []
        names = list(self.config["venues"])
        for buy_name, sell_name in (names, names[::-1]):
            buy_book, sell_book = self.books[buy_name], self.books[sell_name]
            if abs(buy_book["receive_ts_ns"] - sell_book["receive_ts_ns"]) > 200_000_000:
                continue
            asks = [(Decimal(p), Decimal(q)) for p, q in buy_book["asks"] if Decimal(q) > 0]
            bids = [(Decimal(p), Decimal(q)) for p, q in sell_book["bids"] if Decimal(q) > 0]
            if not asks or not bids:
                continue
            ask, ask_qty = min(asks)
            bid, bid_qty = max(bids)
            buy_venue, sell_venue = (
                self.config["venues"][buy_name],
                self.config["venues"][sell_name],
            )
            buy_fee = Decimal(buy_venue["taker_fee_bps"]) / 10000
            sell_fee = Decimal(sell_venue["taker_fee_bps"]) / 10000
            if bid * (1 - sell_fee) <= ask * (1 + buy_fee) + Decimal("0.02"):
                continue
            qty = min(
                Decimal("0.5"),
                ask_qty * Decimal("0.01"),
                bid_qty * Decimal("0.01"),
                Decimal(portfolio[sell_name]["base"]),
                Decimal(portfolio[buy_name]["quote"]) / (ask * (1 + buy_fee)),
            )
            # Quantize to a common step for the demo's two equal lot sizes.
            lot = max(Decimal(buy_venue["lot_size"]), Decimal(sell_venue["lot_size"]))
            qty = (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot
            if (
                qty <= 0
                or qty * ask < Decimal(buy_venue["min_notional"])
                or qty * bid < Decimal(sell_venue["min_notional"])
            ):
                continue
            orders = []
            for exchange, side, price in ((buy_name, "Buy", ask), (sell_name, "Sell", bid)):
                self.counter += 1
                order_id = str(self.counter)
                self.pending.add(order_id)
                orders.append(
                    {
                        "id": order_id,
                        "exchange": exchange,
                        "category": "spot",
                        "symbol": self.config["symbol"],
                        "side": side,
                        "type": "Limit",
                        "time_in_force": "IOC",
                        "qty": str(qty),
                        "price": str(price),
                    }
                )
            self.last_trade_ns = event["receive_ts_ns"]
            return orders
        return []

    def on_execution_report(self, report, portfolio):
        self.pending.discard(report["id"])
        # Real strategies should hedge a residual when only one leg fills.
        return []
