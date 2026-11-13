"""Pydantic contracts; Decimal accounting and bounded numeric inputs."""

import hashlib
import json
from decimal import Decimal, DefaultContext, InvalidOperation, getcontext
from pathlib import Path
from typing import Annotated, Any, Dict, Literal, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

DefaultContext.prec = 100
getcontext().prec = 100
ZERO = Decimal(0)
BPS = Decimal(10000)


def decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or len(str(value)) > 128:
        raise ValueError("invalid decimal input")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal: %r" % value)
    if not result.is_finite():
        raise ValueError("decimal must be finite")
    if len(result.as_tuple().digits) > 30 or result != 0 and abs(result.adjusted()) > 24:
        raise ValueError("decimal outside supported precision/range")
    return result


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("%s must be an integer >= %s" % (name, minimum))
    return value


def dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(
        value,
        default=lambda v: str(v) if isinstance(v, Decimal) else vars(v),
        allow_nan=False,
        **kwargs,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


PositiveMoney = Annotated[Decimal, BeforeValidator(decimal), Field(gt=0)]
NonnegativeMoney = Annotated[Decimal, BeforeValidator(decimal), Field(ge=0)]
PositiveInteger = Annotated[int, Field(strict=True, gt=0)]
Nanoseconds = Annotated[int, Field(strict=True, ge=0)]
Name = Annotated[str, Field(strict=True, min_length=1, max_length=128)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Venue(Contract):
    taker_fee_bps: Annotated[NonnegativeMoney, Field(lt=10000)]
    order_latency_ns: Nanoseconds = 0
    report_latency_ns: Nanoseconds = 0
    extra_feed_latency_ns: Nanoseconds = 0
    tick_size: PositiveMoney
    lot_size: PositiveMoney
    min_notional: NonnegativeMoney
    initial_base: NonnegativeMoney
    initial_quote: NonnegativeMoney


class Config(Contract):
    symbol: Name
    base_asset: Name
    quote_asset: Name
    reference_price: PositiveMoney
    venues: Annotated[Dict[Name, Venue], Field(min_length=2, max_length=2)]
    compute_latency_ns: PositiveInteger = 1_000_000
    trade_compute_latency_ns: Optional[Nanoseconds] = None
    max_book_age_ns: PositiveInteger = 500_000_000
    depth_fraction: Annotated[PositiveMoney, Field(le=1)] = Decimal("0.05")
    max_order_notional: PositiveMoney = Decimal("1000")
    turnover_window_ns: PositiveInteger = 1_000_000_000
    max_window_turnover: PositiveMoney = Decimal("5000")
    max_net_exposure: PositiveMoney = Decimal("2000")
    require_paired_orders: Annotated[bool, Field(strict=True)] = False
    max_unhedged_duration_ns: PositiveInteger = 2_000_000_000
    max_orders_per_callback: PositiveInteger = 8
    max_orders: PositiveInteger = 100000
    callback_timeout_s: PositiveInteger = 5
    max_message_bytes: PositiveInteger = 1_048_576
    sandbox_memory_mb: PositiveInteger = 1024
    sandbox_cpus: PositiveMoney = Decimal("1")
    max_run_seconds: PositiveInteger = 1800

    @model_validator(mode="after")
    def validate_assets_and_equity(self):
        if self.base_asset == self.quote_asset:
            raise ValueError("base and quote assets must differ")
        if self.initial_equity <= 0:
            raise ValueError("starting equity must be positive")
        if self.require_paired_orders:
            if self.max_orders_per_callback < 2:
                raise ValueError("paired mode requires at least two orders per callback")
            lots = [venue.lot_size for venue in self.venues.values()]
            if max(lots) % min(lots):
                raise ValueError("paired mode requires commensurate venue lots")
        return self

    @classmethod
    def load(cls, path: Any) -> "Config":
        return cls.model_validate_json(Path(path).read_text())

    @classmethod
    def parse(cls, row: Dict[str, Any]) -> "Config":
        return cls.model_validate(row)

    @property
    def raw(self):
        return self.model_dump(mode="json")

    @property
    def initial_equity(self) -> Decimal:
        return sum(
            (v.initial_quote + v.initial_base * self.reference_price for v in self.venues.values()),
            ZERO,
        )

    def public(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "quote_asset": self.quote_asset,
            "compute_latency_ns": self.compute_latency_ns,
            "trade_compute_latency_ns": self.trade_compute_latency_ns,
            "max_book_age_ns": self.max_book_age_ns,
            "depth_fraction": self.depth_fraction,
            "max_order_notional": self.max_order_notional,
            "turnover_window_ns": self.turnover_window_ns,
            "max_window_turnover": self.max_window_turnover,
            "max_net_exposure": self.max_net_exposure,
            "require_paired_orders": self.require_paired_orders,
            "max_unhedged_duration_ns": self.max_unhedged_duration_ns,
            "venues": {
                name: {
                    "taker_fee_bps": v.taker_fee_bps,
                    "tick_size": v.tick_size,
                    "lot_size": v.lot_size,
                    "min_notional": v.min_notional,
                    "order_latency_ns": v.order_latency_ns,
                    "report_latency_ns": v.report_latency_ns,
                    "extra_feed_latency_ns": v.extra_feed_latency_ns,
                }
                for name, v in self.venues.items()
            },
        }


class OrderRequest(Contract):
    """Wire field names follow hft::Order; passive orders are deliberately absent."""

    id: Name
    exchange: Name
    category: Literal["spot"]
    symbol: Name
    side: Literal["Buy", "Sell"]
    type: Literal["Limit"]
    qty: PositiveMoney
    price: PositiveMoney
    time_in_force: Literal["IOC"]
    reduce_only: Literal[False] = False
