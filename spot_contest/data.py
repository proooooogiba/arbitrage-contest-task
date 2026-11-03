"""Adapters for hft::OrderBookUpdate/Trade and hft_research Parquet schema v4."""

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List

from .models import decimal, dumps, integer


def _levels(row: Dict[str, Any], side: str, frame: bool) -> List[List[str]]:
    if frame:
        prices, quantities = row[side[:-1] + "_prices"], row[side[:-1] + "_quantities"]
        count = integer(row[side[:-1] + "_count"], side + " count")
        if count > min(len(prices), len(quantities)):
            raise ValueError("book count exceeds array size")
        levels = zip(prices[:count], quantities[:count])
    else:
        levels = ((x["price"], x["qty"]) if isinstance(x, dict) else x for x in row[side])
    result, seen = [], set()
    for p, q in levels:
        price, qty = decimal(p), decimal(q)
        if price <= 0 or qty < 0 or price in seen:
            raise ValueError("invalid or duplicate book level")
        seen.add(price)
        result.append([str(price), str(qty)])
    return result


def normalize(row: Dict[str, Any], kind: str) -> Dict[str, Any]:
    if row.get("market_type", 0) not in (0, "spot") or row.get("category", "spot") != "spot":
        raise ValueError("only spot data are accepted")
    frame = kind == "market_frames"
    trade = kind == "trades"
    if kind not in ("market_frames", "trades", "order_book_updates", "events"):
        raise ValueError("unknown dataset kind: " + kind)
    if kind == "events":
        # Canonical input is validated again, not blindly trusted.
        trade = row.get("event_type") == "trade"
        if row.get("event_type") not in ("book", "trade"):
            raise ValueError("event_type must be book or trade")
    receive = row.get("receive_ts_ns", row.get("ts_recv_ns"))
    if receive is None:
        raise ValueError("receive_ts_ns/ts_recv_ns required; model headers alone lack receive time")
    receive = integer(receive, "receive timestamp")
    if frame:
        # A window contains aggregates unavailable before its close/publication.
        market = max(
            integer(row["window_end_ns"], "window_end_ns"),
            receive,
            integer(row.get("ts_publish_ns", receive), "ts_publish_ns"),
        )
        receive = market
    else:
        market = row.get(
            "market_ts_ns",
            row.get(
                "ts_trade_ns" if trade else "server_time_ns",
                row.get("ts_exchange_ns", row.get("trade_time_ns")),
            ),
        )
        market = integer(market, "market timestamp")
        receive = max(receive, integer(row.get("ts_publish_ns", receive), "publish timestamp"))
        if receive < market:
            raise ValueError(
                "receive time precedes market time; clocks must be normalized upstream"
            )
    exchange, symbol = row.get("exchange"), row.get("symbol")
    if not isinstance(exchange, str) or not exchange or not isinstance(symbol, str) or not symbol:
        raise ValueError("exchange and symbol required")
    event = {
        "event_type": "trade" if trade else "book",
        "exchange": exchange,
        "symbol": symbol,
        "market_type": 0,
        "market_ts_ns": market,
        "receive_ts_ns": receive,
        "stream_epoch": integer(row.get("stream_epoch", 0), "stream_epoch"),
        "producer_session_id": integer(row.get("producer_session_id", 0), "producer_session_id"),
        "source": row.get("source", "sampled_100ms" if frame else "event_stream"),
    }
    if trade:
        price, qty = decimal(row["price"]), decimal(row["qty"])
        if price <= 0 or qty <= 0 or not isinstance(row.get("is_buyer_maker", False), bool):
            raise ValueError("invalid trade")
        event.update(
            price=str(price),
            qty=str(qty),
            trade_id=str(row.get("trade_id", "")),
            is_buyer_maker=row.get("is_buyer_maker", False),
        )
    else:
        valid = (
            row.get("valid", True)
            and not row.get("is_filled", False)
            and not row.get("gap_count", 0)
            and not row.get("resync_count", 0)
        )
        event.update(
            is_snapshot=bool(row.get("is_snapshot", frame)),
            valid=bool(valid),
            update_id=integer(row.get("update_id", 0), "update_id"),
            book_age_ns=integer(row.get("book_age_ns", 0), "book_age_ns"),
            bids=_levels(row, "bids", frame),
            asks=_levels(row, "asks", frame),
        )
        if "previous_update_id" in row:
            event["previous_update_id"] = integer(row["previous_update_id"], "previous_update_id")
        if frame or event["is_snapshot"]:
            bids = [decimal(p) for p, q in event["bids"] if decimal(q) > 0]
            asks = [decimal(p) for p, q in event["asks"] if decimal(q) > 0]
            if valid and (not bids or not asks or max(bids) >= min(asks)):
                raise ValueError("valid snapshot must have a non-crossed two-sided book")
        if frame or kind == "events":
            # Preserve causal, closed-window order-flow features only.
            for key in (
                "window_start_ns",
                "window_end_ns",
                "trade_count",
                "buy_qty",
                "sell_qty",
                "trade_notional",
                "trade_vwap",
                "gap_count",
                "resync_count",
            ):
                if key in row:
                    event[key] = row[key]
    return event


def source_rows(path: Path, kind: str) -> Iterator[Dict[str, Any]]:
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ValueError("Parquet input requires pip install '.[parquet]'")
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=8192):
            yield from batch.to_pylist()
    else:
        with path.open() as stream:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError as exc:
                        raise ValueError("%s:%d: %s" % (path, number, exc))


def prepare(manifest_path: Path, output: Path) -> Dict[str, Any]:
    """Disk-backed sort: memory does not grow with the entire history."""
    manifest = json.loads(manifest_path.read_text())
    if not manifest.get("sources"):
        raise ValueError("manifest needs a nonempty sources list")
    output.parent.mkdir(parents=True, exist_ok=True)
    count, sources, modes = 0, [], set()
    with tempfile.TemporaryDirectory(prefix="spot-prepare-") as temp:
        connection = sqlite3.connect(str(Path(temp) / "sort.db"))
        connection.execute("CREATE TABLE events (t INTEGER, i INTEGER, payload TEXT)")
        for source in manifest["sources"]:
            path = (manifest_path.parent / source["path"]).resolve()
            if path == output.resolve():
                raise ValueError("output cannot overwrite a source")
            digest = file_hash(path)
            sources.append({"path": source["path"], "sha256": digest, "kind": source["kind"]})
            for row in source_rows(path, source["kind"]):
                try:
                    event = normalize(row, source["kind"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("%s row %d: %s" % (path, count + 1, exc))
                connection.execute(
                    "INSERT INTO events VALUES (?, ?, ?)",
                    (event["market_ts_ns"], count, dumps(event)),
                )
                count += 1
                modes.add(event["source"])
                if count % 10000 == 0:
                    connection.commit()
        if not count:
            raise ValueError("dataset is empty")
        connection.commit()
        connection.execute("CREATE INDEX ordered ON events(t, i)")
        with output.open("w") as stream:
            for (payload,) in connection.execute("SELECT payload FROM events ORDER BY t, i"):
                stream.write(payload + "\n")
        connection.close()
    metadata = {
        "schema_version": 1,
        "events": count,
        "sha256": file_hash(output),
        "sources": sources,
        "time_models": sorted(modes),
    }
    output.with_suffix(output.suffix + ".manifest.json").write_text(
        dumps(metadata, indent=2) + "\n"
    )
    return metadata


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def events(path: Path) -> Iterator[Dict[str, Any]]:
    last = -1
    for i, row in enumerate(source_rows(path, "events"), 1):
        event = normalize(row, "events")
        if event["market_ts_ns"] < last:
            raise ValueError("events not sorted at line %d; run prepare first" % i)
        last = event["market_ts_ns"]
        yield event
