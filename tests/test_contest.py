import copy
import io
import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from spot_contest.cli import evaluate, make_demo
from spot_contest.data import events, normalize, prepare
from spot_contest.engine import Book, Judge
from spot_contest.models import Config, decimal, dumps
from spot_contest.service import extract_submission

ROOT = Path(__file__).resolve().parents[1]
T = 1_000_000_000
MS = 1_000_000


def config(**changes):
    row = json.loads((ROOT / "examples/config.json").read_text())
    row.update(changes)
    return Config.parse(row)


def book(ms, exchange="binance", bid="99.99", ask="100", qty="1000", **changes):
    row = {
        "event_type": "book",
        "exchange": exchange,
        "symbol": "SOLUSDT",
        "market_type": 0,
        "market_ts_ns": T + ms * MS,
        "receive_ts_ns": T + ms * MS,
        "is_snapshot": True,
        "update_id": ms + 1,
        "valid": True,
        "stream_epoch": 1,
        "producer_session_id": 1,
        "bids": [[bid, qty]],
        "asks": [[ask, qty]],
    }
    row.update(changes)
    return normalize(row, "events")


def order(id="one", exchange="binance", side="Buy", qty="1", price="100"):
    return {
        "id": id,
        "exchange": exchange,
        "category": "spot",
        "symbol": "SOLUSDT",
        "side": side,
        "type": "Limit",
        "time_in_force": "IOC",
        "qty": qty,
        "price": price,
    }


class Script:
    def __init__(self, first=None, callback=None):
        self.first, self.callback = first or [], callback
        self.messages = []

    def request(self, message):
        self.messages.append(copy.deepcopy(message))
        if message["type"] == "start":
            return self.first
        return self.callback(message) if self.callback else []


def run(strategy, middle=None, rules=None, final=None):
    logs = []
    stream = [book(0), book(0, "bybit")]
    stream += middle or []
    stream += final or [book(100), book(100, "bybit")]
    stream.sort(key=lambda e: e["market_ts_ns"])
    judge = Judge(rules or config(), strategy, logs.append)
    result = judge.run(stream)
    reports = [r["report"] for r in logs if r["type"] == "execution"]
    return result, reports, judge


class ExecutionTests(unittest.TestCase):
    def test_idle_scores_zero_even_when_market_rises(self):
        result, _, _ = run(
            Script(),
            final=[book(100, bid="199", ask="200"), book(100, "bybit", bid="199", ask="200")],
        )
        self.assertEqual(result["net_pnl"], 0)
        self.assertEqual(result["initial_equity"], 100000)

    def test_order_uses_book_at_arrival(self):
        result, reports, _ = run(Script([order(price="101")]), [book(4, bid="100.99", ask="101")])
        self.assertEqual(reports[0]["notional"], 101)
        self.assertEqual(reports[0]["execution_ts_ns"], T + 6 * MS)
        self.assertLess(result["net_pnl"], 0)

    def test_equal_time_market_precedes_order(self):
        _, reports, _ = run(Script([order()]), [book(6, bid="101", ask="102")])
        self.assertEqual(reports[0]["filled_qty"], 0)

    def test_partial_ioc_and_no_future_queue(self):
        _, reports, _ = run(Script([order(qty="1")]), [book(2, qty="10")])
        self.assertEqual(reports[0]["filled_qty"], Decimal("0.50"))
        self.assertEqual(reports[0]["status"], "PartiallyFilled")
        self.assertEqual(len(reports), 1)

    def test_depth_is_not_reused_across_orders(self):
        _, reports, _ = run(Script([order("a", qty="1"), order("b", qty="1")]), [book(2, qty="10")])
        self.assertEqual(sum(r["filled_qty"] for r in reports), Decimal("0.50"))

    def test_fee_and_terminal_rebalance_are_charged(self):
        result, reports, _ = run(Script([order()]))
        self.assertEqual(reports[0]["fee"], Decimal("0.1"))
        # Buy 100 + .1; restore initial inventory by selling at 99.99 minus .09999.
        self.assertEqual(result["net_pnl"], Decimal("-0.20999"))
        self.assertEqual(result["terminal_fees"], Decimal("0.09999"))

    def test_legs_are_independent(self):
        _, reports, _ = run(Script([order("a"), order("b", "bybit", "Sell", price="100")]))
        self.assertEqual(reports[0]["status"], "Filled")
        self.assertEqual(reports[1]["filled_qty"], 0)

    def test_pending_orders_reserve_cash(self):
        cfg = json.loads((ROOT / "examples/config.json").read_text())
        cfg["venues"]["binance"]["initial_quote"] = "100.1"
        _, reports, judge = run(Script([order("a"), order("b")]), rules=Config.parse(cfg))
        self.assertEqual(sum(r["filled_qty"] for r in reports), 1)
        self.assertIn("INSUFFICIENT_BALANCE", [r["reason"] for r in reports])
        self.assertEqual(judge.accounts["binance"]["quote"], 0)

    def test_no_short_borrowing_or_cross_venue_cash(self):
        cfg = json.loads((ROOT / "examples/config.json").read_text())
        cfg["venues"]["binance"].update(initial_base="0", initial_quote="0")
        _, reports, _ = run(
            Script([order("a"), order("b", side="Sell", price="99.99")]), rules=Config.parse(cfg)
        )
        self.assertTrue(all(r["reason"] == "INSUFFICIENT_BALANCE" for r in reports))

    def test_net_exposure_clamps_fill(self):
        _, reports, _ = run(Script([order(qty="1")]), rules=config(max_net_exposure="50"))
        self.assertEqual(reports[0]["filled_qty"], Decimal("0.50"))

    def test_submitted_notional_window_cannot_be_bypassed_by_splitting(self):
        _, reports, _ = run(
            Script([order("a"), order("b")]), rules=config(max_window_turnover="100")
        )
        self.assertIn("WINDOW_NOTIONAL_LIMIT", [r["reason"] for r in reports])

    def test_invalid_orders_rejected_without_crashing(self):
        invalid = [
            dict(order("a"), qty="NaN"),
            dict(order("b"), exchange=[]),
            dict(order("c"), type="Market"),
            dict(order("d"), category="futures"),
            dict(order("e"), qty="0.001"),
            dict(order("f"), price="100.001"),
        ]
        _, reports, _ = run(Script(invalid))
        self.assertEqual(len(reports), 6)
        self.assertTrue(all(r["status"] == "Rejected" for r in reports))

    def test_duplicate_client_id_rejected(self):
        _, reports, _ = run(Script([order(), order()]))
        self.assertIn("INVALID_OR_DUPLICATE_ID", [r["reason"] for r in reports])

    def test_low_sell_limit_cannot_bypass_actual_notional_limits(self):
        _, reports, _ = run(
            Script(
                [
                    order("a", side="Sell", qty="50", price="1"),
                    order("b", side="Sell", qty="50", price="1"),
                ]
            ),
            rules=config(max_window_turnover="1500"),
        )
        self.assertTrue(all(r["notional"] <= 1000 for r in reports))
        self.assertLessEqual(sum(r["notional"] for r in reports), 1500)
        self.assertGreater(sum(r["notional"] for r in reports), 1400)

    def test_stale_book_rejects_execution(self):
        _, reports, _ = run(Script([order()]), rules=config(max_book_age_ns=MS))
        self.assertEqual(reports[0]["reason"], "STALE_OR_INVALID_BOOK")

    def test_no_execution_beyond_end_of_data(self):
        strategy = Script(
            callback=lambda m: (
                [order()]
                if m["type"] == "market"
                and m["event"]["exchange"] == "binance"
                and m["event"]["update_id"] == 101
                else []
            )
        )
        _, reports, _ = run(strategy)
        self.assertEqual(reports[0]["reason"], "END_OF_DATA")

    def test_future_fills_are_not_visible_in_portfolio(self):
        script = Script([order()])
        run(script, [book(7, "bybit")])
        observation = next(
            m
            for m in script.messages
            if m["type"] == "market" and m["event"]["market_ts_ns"] == T + 7 * MS
        )
        self.assertEqual(observation["portfolio"]["binance"]["base"], 250)
        execution = next(m for m in script.messages if m["type"] == "execution")
        self.assertEqual(execution["now_ns"], T + 9 * MS)
        self.assertEqual(execution["portfolio"]["binance"]["base"], 251)

    def test_delayed_feed_does_not_reach_strategy_early(self):
        script = Script()
        run(script, [book(10, receive_ts_ns=T + 30 * MS)])
        observation = next(
            m
            for m in script.messages
            if m["type"] == "market" and m["event"]["market_ts_ns"] == T + 10 * MS
        )
        self.assertGreaterEqual(observation["now_ns"], T + 30 * MS)

    def test_callback_compute_busy_time_is_serialized(self):
        script = Script()
        run(script, [book(1), book(2)], rules=config(compute_latency_ns=10 * MS))
        times = [m["now_ns"] for m in script.messages]
        self.assertTrue(all(b - a >= 10 * MS for a, b in zip(times, times[1:])))

    def test_unpriced_terminal_inventory_cannot_receive_score(self):
        with self.assertRaisesRegex(ValueError, "terminal hedge depth"):
            run(Script([order()]), final=[book(100, qty="0.1"), book(100, "bybit")])


class BookAndDataTests(unittest.TestCase):
    def test_repeated_snapshots_do_not_refill_liquidity(self):
        b = Book(Decimal("0.05"))
        b.apply(book(0, qty="10"))
        b.remaining["asks"][Decimal(100)] = 0
        b.apply(book(10, qty="10"))
        self.assertEqual(b.remaining["asks"][Decimal(100)], 0)
        b.apply(book(20, qty="20"))
        self.assertEqual(b.remaining["asks"][Decimal(100)], Decimal("0.5"))

    def test_gap_requires_new_snapshot(self):
        b = Book(Decimal("0.05"))
        b.apply(book(0))
        b.apply(book(1, is_snapshot=False, previous_update_id=999))
        self.assertFalse(b.valid)
        b.apply(book(2, is_snapshot=False))
        self.assertFalse(b.valid)
        b.apply(book(3))
        self.assertTrue(b.valid)

    def test_new_epoch_delta_cannot_seed_book(self):
        b = Book(Decimal("0.05"))
        b.apply(book(0))
        b.apply(book(1, is_snapshot=False, stream_epoch=2))
        self.assertFalse(b.valid)

    def test_futures_and_invalid_clock_rejected(self):
        for change in ({"market_type": 1}, {"receive_ts_ns": 0}):
            with self.assertRaises(ValueError):
                book(1, **change)

    def test_canonical_flow_features_survive_roundtrip(self):
        row = book(0)
        row["trade_count"] = 8
        self.assertEqual(normalize(row, "events")["trade_count"], 8)

    def test_frame_is_available_only_at_window_close_publication(self):
        row = {
            "exchange": "binance",
            "symbol": "SOLUSDT",
            "market_type": 0,
            "window_end_ns": 1000,
            "ts_exchange_ns": 100,
            "ts_recv_ns": 200,
            "ts_publish_ns": 1100,
            "bid_count": 1,
            "ask_count": 1,
            "bid_prices": [99, 0],
            "bid_quantities": [1, 0],
            "ask_prices": [100, 0],
            "ask_quantities": [1, 0],
            "valid": True,
        }
        event = normalize(row, "market_frames")
        self.assertEqual(event["market_ts_ns"], 1100)
        self.assertEqual(event["receive_ts_ns"], 1100)
        self.assertEqual(event["source"], "sampled_100ms")
        self.assertEqual(len(event["bids"]), 1)

    def test_raw_cpp_book_and_trade_adapters(self):
        raw = {
            "exchange": "binance",
            "symbol": "SOLUSDT",
            "market_type": "spot",
            "is_snapshot": True,
            "server_time_ns": 100,
            "ts_recv_ns": 200,
            "update_id": 10,
            "bids": [{"price": 99, "qty": 1}],
            "asks": [{"price": 100, "qty": 1}],
        }
        self.assertEqual(normalize(raw, "order_book_updates")["market_ts_ns"], 100)
        trade = dict(raw, price="100", qty="0.1", ts_trade_ns=100, trade_id="42")
        self.assertEqual(normalize(trade, "trades")["event_type"], "trade")

    def test_prepare_sorts_and_detects_unsorted_direct_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "raw.jsonl"
            source.write_text(dumps(book(10)) + "\n" + dumps(book(0)) + "\n")
            with self.assertRaisesRegex(ValueError, "not sorted"):
                list(events(source))
            manifest = root / "sources.json"
            manifest.write_text(dumps({"sources": [{"kind": "events", "path": "raw.jsonl"}]}))
            prepare(manifest, root / "events.jsonl")
            result = list(events(root / "events.jsonl"))
            self.assertEqual(result[0]["market_ts_ns"], T)
            self.assertTrue((root / "events.jsonl.manifest.json").exists())

    def test_numeric_bombs_and_nonfinite_values_rejected(self):
        for value in ("NaN", "Infinity", "1e999999", True, "9" * 129):
            with self.assertRaises(ValueError):
                decimal(value)


class IntegrationTests(unittest.TestCase):
    def test_demo_baseline_hold_and_repeat_are_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_demo(root / "demo")
            arguments = (root / "demo/config.json", root / "demo/events.jsonl")
            baseline = evaluate(*arguments, root / "demo/baseline", root / "a", "subprocess")
            repeated = evaluate(*arguments, root / "demo/baseline", root / "b", "subprocess")
            held = evaluate(*arguments, root / "demo/hold", root / "c", "subprocess")
            self.assertEqual(baseline, repeated)
            self.assertGreater(baseline["net_pnl"], 0)
            self.assertEqual(held["net_pnl"], 0)
            self.assertEqual(json.loads((root / "a/run.json").read_text())["status"], "completed")
            self.assertIsInstance(json.loads((root / "a/scores.json").read_text())["score"], float)

    def test_bad_strategy_fails_without_score(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_demo(root / "demo")
            (root / "bad").mkdir()
            (root / "bad/strategy.py").write_text("raise RuntimeError('bad model')\n")
            with self.assertRaises(ValueError):
                evaluate(
                    root / "demo/config.json",
                    root / "demo/events.jsonl",
                    root / "bad",
                    root / "run",
                    "subprocess",
                )
            self.assertFalse((root / "run/scores.json").exists())
            self.assertEqual(json.loads((root / "run/run.json").read_text())["status"], "failed")

    def test_hanging_strategy_is_terminated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_demo(root / "demo")
            cfg = json.loads((root / "demo/config.json").read_text())
            cfg["callback_timeout_s"] = 1
            (root / "config.json").write_text(dumps(cfg))
            (root / "bad").mkdir()
            (root / "bad/strategy.py").write_text("import time\ntime.sleep(20)\n")
            with self.assertRaises(TimeoutError):
                evaluate(
                    root / "config.json",
                    root / "demo/events.jsonl",
                    root / "bad",
                    root / "run",
                    "subprocess",
                )
            self.assertFalse((root / "run/scores.json").exists())

    def test_oversized_strategy_protocol_response_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_demo(root / "demo")
            cfg = json.loads((root / "demo/config.json").read_text())
            cfg["max_message_bytes"] = 4096
            (root / "config.json").write_text(dumps(cfg))
            (root / "bad").mkdir()
            (root / "bad/strategy.py").write_text(
                "import sys\nsys.__stdout__.write('x' * 8192 + '\\n')\nsys.__stdout__.flush()\n"
            )
            with self.assertRaisesRegex(ValueError, "response exceeds"):
                evaluate(
                    root / "config.json",
                    root / "demo/events.jsonl",
                    root / "bad",
                    root / "run",
                    "subprocess",
                )
            self.assertFalse((root / "run/scores.json").exists())

    def test_zip_traversal_and_symlink_rejected(self):
        for name, symlink in (
            ("../strategy.py", False),
            ("/strategy.py", False),
            ("strategy.py", True),
        ):
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                info = zipfile.ZipInfo(name)
                if symlink:
                    info.external_attr = 0o120777 << 16
                archive.writestr(info, "anything")
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    extract_submission(data.getvalue(), Path(temp))

    def test_valid_submission_archive(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("strategy.py", "class Strategy: pass\n")
            archive.writestr("model/weights.json", "{}")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            extract_submission(data.getvalue(), root)
            self.assertTrue((root / "model/weights.json").is_file())


if __name__ == "__main__":
    unittest.main()
