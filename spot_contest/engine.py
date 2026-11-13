"""Trusted deterministic judge. Never import participant code in this process."""

import heapq
from collections import deque
from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Dict, Iterable, List, Optional

from .models import BPS, ZERO, Config, OrderRequest, decimal


def floor_lot(qty: Decimal, lot: Decimal) -> Decimal:
    return max(ZERO, (qty / lot).to_integral_value(rounding=ROUND_DOWN) * lot)


class Book:
    def __init__(self, fraction: Decimal):
        self.fraction = fraction
        self.levels = {"bids": {}, "asks": {}}
        self.remaining = {"bids": {}, "asks": {}}
        self.valid = False
        self.initialized = False
        self.epoch = None
        self.update_id = -1
        self.ts = 0
        self.source_age = 0

    def apply(self, event: Dict[str, Any]) -> None:
        epoch = (event["producer_session_id"], event["stream_epoch"])
        if epoch != self.epoch:
            self.valid = self.initialized = False
            self.levels = {"bids": {}, "asks": {}}
            self.remaining = {"bids": {}, "asks": {}}
            self.update_id = -1
            self.epoch = epoch
        if not event["valid"]:
            self.valid = self.initialized = False
            return
        if event["update_id"] < self.update_id:
            self.valid = self.initialized = False
            return
        previous = event.get("previous_update_id")
        if not event["is_snapshot"] and (
            not self.initialized or previous is not None and previous != self.update_id
        ):
            self.valid = self.initialized = False
            return
        for side in ("bids", "asks"):
            old = self.levels[side]
            new = {} if event["is_snapshot"] else dict(old)
            for p, q in event[side]:
                price, qty = decimal(p), decimal(q)
                if qty:
                    new[price] = qty
                else:
                    new.pop(price, None)
            capacity = {}
            for price, qty in new.items():
                if price not in old:
                    capacity[price] = qty * self.fraction
                else:
                    # Only observed positive depth increments replenish capacity.
                    # Identical snapshots and repeated update IDs never replenish it.
                    capacity[price] = min(
                        qty * self.fraction,
                        self.remaining[side].get(price, ZERO)
                        + max(ZERO, qty - old[price]) * self.fraction,
                    )
            self.levels[side] = new
            self.remaining[side] = capacity
        self.update_id = event["update_id"]
        self.initialized = True
        self.ts = event["market_ts_ns"]
        self.source_age = event["book_age_ns"]
        self.valid = bool(
            self.levels["bids"]
            and self.levels["asks"]
            and max(self.levels["bids"]) < min(self.levels["asks"])
        )

    def fresh(self, now: int, max_age: int) -> bool:
        return self.valid and now - self.ts + self.source_age <= max_age

    def prices(self, side: str) -> List[Decimal]:
        return sorted(self.levels[side], reverse=side == "bids")


class Judge:
    """Priority at equal time: market, order arrival, report delivery, callback.

    Single-threaded strategy callbacks occupy virtual compute time. Reports affect
    the observed wallet only on delivery; pending/future fills cannot leak into it.
    """

    def __init__(
        self,
        config: Config,
        strategy: Any,
        audit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.config, self.strategy = config, strategy
        self.audit = audit or (lambda row: None)
        self.books = {name: Book(config.depth_fraction) for name in config.venues}
        self.visible_books = (
            {name: Book(config.depth_fraction) for name in config.venues}
            if config.require_paired_orders
            else {}
        )
        self.pending_orders = set()
        self.hedge_deadline = None
        self.accounts = {
            name: {
                "base": v.initial_base,
                "quote": v.initial_quote,
                "reserved_base": ZERO,
                "reserved_quote": ZERO,
            }
            for name, v in config.venues.items()
        }
        self.observed = {
            name: {"base": v.initial_base, "quote": v.initial_quote}
            for name, v in config.venues.items()
        }
        self.queue = []
        self.counter = 0
        self.now = 0
        self.busy_until = 0
        self.end = None
        self.ids = set()
        self.turnover = {name: deque() for name in config.venues}
        self.executed_turnover = {name: deque() for name in config.venues}
        self.order_count = self.fill_count = self.rejected = 0
        self.fees = self.notional = self.peak_exposure = ZERO
        self.peak_equity = config.initial_equity
        self.max_drawdown = ZERO
        self.sources = set()
        self.callbacks = 0

    def push(self, t: int, priority: int, kind: str, payload: Any) -> None:
        self.counter += 1
        heapq.heappush(self.queue, (t, priority, self.counter, kind, payload))

    def log(self, kind: str, **fields: Any) -> None:
        self.audit(dict(type=kind, timestamp_ns=self.now, **fields))

    def exposure(self) -> Decimal:
        # Signed base-asset change relative to prefunded inventory, across venues.
        return sum(
            (self.accounts[n]["base"] - v.initial_base for n, v in self.config.venues.items()), ZERO
        )

    def exposure_price(self) -> Decimal:
        return max(
            [self.config.reference_price]
            + [max(b.levels["asks"]) for b in self.books.values() if b.valid]
        )

    def mark(self) -> None:
        if not all(b.fresh(self.now, self.config.max_book_age_ns) for b in self.books.values()):
            return
        # Baseline-relative MTM avoids rewarding passive appreciation of initial BTC/SOL.
        equity = self.config.initial_equity
        for name, venue in self.config.venues.items():
            account, book = self.accounts[name], self.books[name]
            mid = (max(book.levels["bids"]) + min(book.levels["asks"])) / 2
            equity += account["quote"] - venue.initial_quote
            equity += (account["base"] - venue.initial_base) * mid
        self.peak_equity = max(self.peak_equity, equity)
        self.max_drawdown = max(self.max_drawdown, (self.peak_equity - equity) / self.peak_equity)
        self.peak_exposure = max(self.peak_exposure, abs(self.exposure()) * self.exposure_price())

    def run(self, stream: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        iterator = iter(stream)
        first = next(iterator, None)
        if first is None:
            raise ValueError("empty dataset")
        self.now = first["market_ts_ns"]
        self.push(self.now, 0, "market", first)
        self.push(self.now, 3, "callback", {"type": "start", "config": self.config.public()})
        event_count = 0
        while self.queue:
            self.now, _, _, kind, payload = heapq.heappop(self.queue)
            if kind == "market":
                event_count += 1
                if payload["symbol"] != self.config.symbol or payload["exchange"] not in self.books:
                    raise ValueError("dataset contains an unconfigured instrument/venue")
                self.sources.add(payload["source"])
                if payload["event_type"] == "book":
                    self.books[payload["exchange"]].apply(payload)
                venue = self.config.venues[payload["exchange"]]
                delivery = payload["receive_ts_ns"] + venue.extra_feed_latency_ns
                self.push(delivery, 2, "deliver", {"type": "market", "event": payload})
                following = next(iterator, None)
                if following is None:
                    self.end = self.now
                else:
                    if following["market_ts_ns"] < self.now:
                        raise ValueError("market stream is not sorted")
                    self.push(following["market_ts_ns"], 0, "market", following)
                self.mark()
            elif kind == "arrival":
                self.execute(payload)
            elif kind == "hedge_deadline":
                if self.end is not None and self.now > self.end:
                    continue
                if self.hedge_deadline == payload and self.exposure() != ZERO:
                    raise ValueError("UNHEDGED_EXPOSURE_TIMEOUT")
            elif kind == "deliver":
                if payload["type"] == "execution":
                    base_delta = payload.pop("base_delta", ZERO)
                    quote_delta = payload.pop("quote_delta", ZERO)
                    wallet = self.observed.get(payload["exchange"])
                    if wallet is not None:
                        wallet["base"] += base_delta
                        wallet["quote"] += quote_delta
                self.push(max(self.now, self.busy_until), 3, "callback", payload)
            elif kind == "callback":
                if self.end is not None and self.now > self.end:
                    continue
                if self.now < self.busy_until:
                    self.push(self.busy_until, 3, "callback", payload)
                    continue
                if payload.pop("_settle_pending", False):
                    self.pending_orders.discard(payload["id"])
                if (
                    self.config.require_paired_orders
                    and payload["type"] == "market"
                    and payload["event"]["event_type"] == "book"
                ):
                    event = payload["event"]
                    self.visible_books[event["exchange"]].apply(event)
                request = dict(
                    payload,
                    now_ns=self.now,
                    portfolio={k: dict(v) for k, v in self.observed.items()},
                )
                orders = self.strategy.request(request)
                self.callbacks += 1
                if (
                    not isinstance(orders, list)
                    or len(orders) > self.config.max_orders_per_callback
                ):
                    raise ValueError("strategy must return a bounded orders list")
                self.busy_until = self.now + self.config.compute_latency_ns
                self.submit_orders(orders, self.busy_until)
        result = self.score()
        result.update(
            events=event_count,
            callbacks=self.callbacks,
            time_models=sorted(self.sources),
            end_ns=self.end,
        )
        self.log("summary", result=result)
        return result

    def report(
        self,
        order: Dict[str, Any],
        status: str,
        reason: str,
        qty: Decimal = ZERO,
        notional: Decimal = ZERO,
        fee: Decimal = ZERO,
        t: Optional[int] = None,
        settle_pending: bool = False,
    ) -> None:
        exchange = order.get("exchange", "")
        if not isinstance(exchange, str):
            exchange = ""
        buy = order.get("side") == "Buy"
        base_delta = qty if buy else -qty
        quote_delta = -notional - fee if buy else notional - fee
        report = {
            "type": "execution",
            "id": str(order.get("id", "")),
            "exchange": exchange,
            "status": status,
            "reason": reason,
            "filled_qty": qty,
            "notional": notional,
            "fee": fee,
            "execution_ts_ns": self.now if t is None else t,
            "base_delta": base_delta,
            "quote_delta": quote_delta,
        }
        if self.config.require_paired_orders and settle_pending:
            report["_settle_pending"] = True
        self.log("execution", report=dict(report))
        latency = self.config.venues[exchange].report_latency_ns if exchange in self.books else 0
        self.push((self.now if t is None else t) + latency, 2, "deliver", report)
        if status == "Rejected":
            self.rejected += 1

    def _count_orders(self, count: int) -> None:
        self.order_count += count
        if self.order_count > self.config.max_orders:
            raise ValueError("submission exceeds total order budget")

    def _validate_order(self, raw: Any, ready: int) -> Dict[str, Any]:
        order = OrderRequest.model_validate(raw).model_dump()
        if order["id"] in self.ids:
            raise ValueError("INVALID_OR_DUPLICATE_ID")
        self.ids.add(order["id"])
        if order["exchange"] not in self.books or order["symbol"] != self.config.symbol:
            raise ValueError("UNKNOWN_INSTRUMENT")
        venue = self.config.venues[order["exchange"]]
        qty, price = order["qty"], order["price"]
        if qty <= 0 or price <= 0 or qty % venue.lot_size or price % venue.tick_size:
            raise ValueError("INVALID_TICK_OR_LOT")
        notional = price * qty
        if not venue.min_notional <= notional <= self.config.max_order_notional:
            raise ValueError("ORDER_NOTIONAL_LIMIT")
        window = self.turnover[order["exchange"]]
        while window and window[0][0] <= ready - self.config.turnover_window_ns:
            window.popleft()
        if sum((n for _, n in window), ZERO) + notional > self.config.max_window_turnover:
            raise ValueError("WINDOW_NOTIONAL_LIMIT")
        wallet = self.accounts[order["exchange"]]
        reserve = notional * (1 + venue.taker_fee_bps / BPS) if order["side"] == "Buy" else qty
        asset = "quote" if order["side"] == "Buy" else "base"
        if wallet[asset] - wallet["reserved_" + asset] < reserve:
            raise ValueError("INSUFFICIENT_BALANCE")
        order.update(_reserve=reserve, _asset=asset)
        return order

    def _accept_order(self, order: Dict[str, Any], ready: int) -> None:
        name = order["exchange"]
        self.accounts[name]["reserved_" + order["_asset"]] += order["_reserve"]
        self.turnover[name].append((ready, order["price"] * order["qty"]))
        if self.config.require_paired_orders:
            self.pending_orders.add(order["id"])
        arrival = ready + self.config.venues[name].order_latency_ns
        self.push(arrival, 1, "arrival", order)
        self.log(
            "accepted",
            order={k: v for k, v in order.items() if not k.startswith("_")},
            arrival_ns=arrival,
        )

    def _reject_orders(self, orders: List[Any], reason: str, ready: int) -> None:
        for raw in orders:
            self.report(raw if isinstance(raw, dict) else {}, "Rejected", reason, t=ready)

    def _require_marketable(self, order: Dict[str, Any], ready: int, full_depth: bool) -> None:
        book = self.visible_books[order["exchange"]]
        if not book.fresh(ready, self.config.max_book_age_ns):
            raise ValueError("OBSERVED_BOOK_UNAVAILABLE")
        buy = order["side"] == "Buy"
        side = "asks" if buy else "bids"
        prices = book.prices(side)
        best = prices[0]
        if buy and order["price"] < best or not buy and order["price"] > best:
            raise ValueError("NON_MARKETABLE_LIMIT")
        if full_depth:
            lot = self.config.venues[order["exchange"]].lot_size
            available = ZERO
            for price in prices:
                if buy and price > order["price"] or not buy and price < order["price"]:
                    break
                available += floor_lot(book.levels[side][price] * book.fraction, lot)
            if available < order["qty"]:
                raise ValueError("INSUFFICIENT_OBSERVED_DEPTH")

    def submit_orders(self, orders: List[Any], ready: int) -> None:
        if not self.config.require_paired_orders:
            for raw in orders:
                self.submit(raw, ready)
            return
        self._count_orders(len(orders))
        if not orders:
            return
        if self.pending_orders:
            self._reject_orders(orders, "AWAITING_EXECUTION_REPORTS", ready)
            return
        exposure = self.exposure()
        if exposure != ZERO:
            if len(orders) != 1:
                self._reject_orders(orders, "RESIDUAL_HEDGE_REQUIRED", ready)
                return
            try:
                hedge = self._validate_order(orders[0], ready)
                if (hedge["side"] == "Buy") != (exposure < ZERO):
                    raise ValueError("HEDGE_MUST_REDUCE_EXPOSURE")
                self._require_marketable(hedge, ready, full_depth=False)
                hedge["_hedge"] = True
                self._accept_order(hedge, ready)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                self._reject_orders(orders, str(exc), ready)
            return
        if len(orders) % 2:
            self._reject_orders(orders, "ARBITRAGE_PAIR_REQUIRED", ready)
            return
        for index in range(0, len(orders), 2):
            raw_pair = orders[index : index + 2]
            try:
                first, second = [self._validate_order(raw, ready) for raw in raw_pair]
                if (
                    first["exchange"] == second["exchange"]
                    or first["side"] == second["side"]
                    or first["qty"] != second["qty"]
                ):
                    raise ValueError("INVALID_ARBITRAGE_PAIR")
                self._require_marketable(first, ready, full_depth=True)
                self._require_marketable(second, ready, full_depth=True)
                # Both legs pass validation before either reserves funds or uses turnover.
                self._accept_order(first, ready)
                self._accept_order(second, ready)
            except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
                self._reject_orders(raw_pair, str(exc), ready)

    def submit(self, raw: Any, ready: int) -> None:
        if self.config.require_paired_orders:
            self.submit_orders([raw], ready)
            return
        self._count_orders(1)
        order = dict(raw) if isinstance(raw, dict) else {}
        try:
            order = self._validate_order(order, ready)
            self._accept_order(order, ready)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            self.report(order, "Rejected", str(exc), t=ready)

    def _track_unhedged_exposure(self) -> None:
        if not self.config.require_paired_orders:
            return
        if self.exposure() == ZERO:
            self.hedge_deadline = None
        elif self.hedge_deadline is None:
            self.hedge_deadline = self.now + self.config.max_unhedged_duration_ns
            self.push(self.hedge_deadline, 4, "hedge_deadline", self.hedge_deadline)

    def execute(self, order: Dict[str, Any]) -> None:
        name = order["exchange"]
        wallet, book, venue = self.accounts[name], self.books[name], self.config.venues[name]
        wallet["reserved_" + order["_asset"]] -= order["_reserve"]
        if self.end is not None and self.now > self.end:
            self.report(order, "Rejected", "END_OF_DATA", settle_pending=True)
            return
        if not book.fresh(self.now, self.config.max_book_age_ns):
            self.report(order, "Rejected", "STALE_OR_INVALID_BOOK", settle_pending=True)
            return
        buy = order["side"] == "Buy"
        side = "asks" if buy else "bids"
        filled = notional = ZERO
        reason = "NO_LIQUIDITY_WITHIN_LIMIT"
        window = self.executed_turnover[name]
        while window and window[0][0] <= self.now - self.config.turnover_window_ns:
            window.popleft()
        window_used = sum((amount for _, amount in window), ZERO)
        for price in book.prices(side):
            if buy and price > order["price"] or not buy and price < order["price"]:
                break
            exposure_limit = self.config.max_net_exposure / self.exposure_price()
            exposure = self.exposure()
            risk_qty = max(ZERO, exposure_limit - exposure if buy else exposure_limit + exposure)
            if order.get("_hedge"):
                risk_qty = min(risk_qty, max(ZERO, -exposure if buy else exposure))
            available = book.remaining[side][price]
            # A sell limit is a minimum: validate budgets at actual fill prices too.
            order_budget = max(ZERO, self.config.max_order_notional - notional)
            window_budget = max(ZERO, self.config.max_window_turnover - window_used - notional)
            qty = floor_lot(
                min(
                    order["qty"] - filled,
                    available,
                    risk_qty,
                    order_budget / price,
                    window_budget / price,
                ),
                venue.lot_size,
            )
            if not qty:
                if risk_qty < venue.lot_size:
                    reason = "NET_EXPOSURE_LIMIT"
                elif (
                    order_budget < price * venue.lot_size or window_budget < price * venue.lot_size
                ):
                    reason = "EXECUTED_NOTIONAL_LIMIT"
                continue
            cash = qty * price
            fee = cash * venue.taker_fee_bps / BPS
            wallet["base"] += qty if buy else -qty
            wallet["quote"] += -cash - fee if buy else cash - fee
            if wallet["base"] < 0 or wallet["quote"] < 0:
                raise RuntimeError("accounting invariant: negative spot wallet")
            book.remaining[side][price] -= qty
            filled += qty
            notional += cash
            self.peak_exposure = max(
                self.peak_exposure, abs(self.exposure()) * self.exposure_price()
            )
            if filled == order["qty"]:
                break
        fee = notional * venue.taker_fee_bps / BPS
        self.notional += notional
        self.fees += fee
        if filled:
            self.fill_count += 1
            window.append((self.now, notional))
            self._track_unhedged_exposure()
        status = "Filled" if filled == order["qty"] else "PartiallyFilled" if filled else "Canceled"
        self.report(
            order,
            status,
            "" if status == "Filled" else reason,
            filled,
            notional,
            fee,
            settle_pending=True,
        )
        self.mark()

    def score(self) -> Dict[str, Any]:
        """Restore starting inventory per venue using terminal depth and taker fees.

        Zero-trade benchmark scores zero. Refuse a score when the available data
        cannot price the terminal hedge, rather than granting a free residual.
        """
        if self.config.require_paired_orders and self.exposure() != ZERO:
            raise ValueError("cannot score: unhedged exposure at end of episode")
        pnl = closing_fees = ZERO
        terminal = []
        for name, venue in self.config.venues.items():
            wallet, book = self.accounts[name], self.books[name]
            if not book.fresh(self.end, self.config.max_book_age_ns):
                raise ValueError("cannot score: no fresh terminal book for " + name)
            delta = wallet["base"] - venue.initial_base
            qty = abs(delta)
            buy = delta < 0
            side = "asks" if buy else "bids"
            cash = ZERO
            # Closing also respects previously consumed historical liquidity.
            for price in book.prices(side):
                used = (
                    book.levels[side][price] * self.config.depth_fraction
                    - book.remaining[side][price]
                )
                take = min(qty, max(ZERO, book.levels[side][price] - used))
                cash += take * price
                qty -= take
                if not qty:
                    break
            if qty:
                raise ValueError("cannot score: insufficient terminal hedge depth for " + name)
            fee = cash * venue.taker_fee_bps / BPS
            closing_fees += fee
            pnl += wallet["quote"] - venue.initial_quote + (-cash if buy else cash) - fee
            terminal.append(
                {"exchange": name, "restore_base_qty": -delta, "notional": cash, "fee": fee}
            )
        return {
            "score": pnl / self.config.initial_equity,
            "net_pnl": pnl,
            "initial_equity": self.config.initial_equity,
            "trading_fees": self.fees,
            "terminal_fees": closing_fees,
            "turnover": self.notional,
            "orders": self.order_count,
            "filled_orders": self.fill_count,
            "rejected_orders": self.rejected,
            "max_drawdown": self.max_drawdown,
            "peak_net_exposure": self.peak_exposure,
            "ending_balances": self.accounts,
            "terminal_rebalance": terminal,
            "funding": ZERO,
            "baseline": "hold_initial_inventory_per_venue",
        }
