"""OKX books5 and signed taker volume. No exchange or notification I/O."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from heapq import heappop, heappush
from time import time_ns

ZERO = Decimal(0)
FUTURE_MS = 2_000
BOOK_MAX_AGE_MS = 3_000


def now_ms():
    return time_ns() // 1_000_000


def number(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not result.is_finite():
        raise ValueError("invalid decimal")
    return result


def positive(value):
    result = number(value)
    if result <= 0:
        raise ValueError("expected a positive number")
    return result


@dataclass(frozen=True, slots=True)
class Book:
    ts: int
    bid: Decimal
    ask: Decimal
    imbalance: Decimal

    @classmethod
    def read(cls, row):
        ts = int(row["ts"])
        bids, asks = row["bids"][:5], row["asks"][:5]
        if len(bids) != 5 or len(asks) != 5:
            return None
        bid, ask = positive(bids[0][0]), positive(asks[0][0])
        bid_qty = sum(positive(level[1]) for level in bids)
        ask_qty = sum(positive(level[1]) for level in asks)
        if ts <= 0 or bid >= ask:
            return None
        return cls(ts, bid, ask, (bid_qty - ask_qty) / (bid_qty + ask_qty))


class RollingDelta:
    def __init__(self, window_ms):
        self.window_ms = window_ms
        self.heap = []
        self.ids = set()
        self.total = ZERO

    def expire(self, at):
        cutoff = at - self.window_ms
        while self.heap and self.heap[0][0] <= cutoff:
            _, trade_id, size = heappop(self.heap)
            self.ids.remove(trade_id)
            self.total -= size

    def add(self, at, ts, trade_id, size):
        self.expire(at)
        if ts <= at - self.window_ms or trade_id in self.ids:
            return False
        heappush(self.heap, (ts, trade_id, size))
        self.ids.add(trade_id)
        self.total += size
        return True


@dataclass(frozen=True, slots=True)
class Signal:
    symbol: str
    direction: str
    at: int
    book: Book
    short_delta: Decimal
    long_delta: Decimal
    last_price: Decimal


class OrderFlow:
    def __init__(self, symbol, short_ms, long_ms, threshold):
        self.symbol = symbol
        self.short = RollingDelta(short_ms)
        self.long = RollingDelta(long_ms)
        self.threshold = threshold
        self.book = None
        self.last_ts = 0
        self.last_price = None

    def update(self, channel, row, received_at):
        if channel == "books5":
            book = Book.read(row)
            if book is None:
                self.book = None
            elif book.ts <= received_at + FUTURE_MS:
                if self.book is None or book.ts >= self.book.ts:
                    self.book = book
            return True

        ts, trade_id, side = int(row["ts"]), row["tradeId"], row["side"]
        if not trade_id or side not in ("buy", "sell") or ts <= 0:
            raise ValueError("invalid trade")
        if ts > received_at + FUTURE_MS:
            raise ValueError("trade timestamp ahead of runner clock")
        price, size = positive(row["px"]), positive(row["sz"])
        signed_size = size if side == "buy" else -size
        at = max(received_at, ts, self.last_ts)
        if not self.long.add(at, ts, trade_id, signed_size):
            return False
        self.short.add(at, ts, trade_id, signed_size)
        if ts >= self.last_ts:
            self.last_ts, self.last_price = ts, price
        return True

    def evaluate(self, received_at, ready_at):
        book = self.book
        at = max(received_at, self.last_ts, book.ts if book else 0)
        self.short.expire(at)
        self.long.expire(at)
        if ready_at is None or received_at < ready_at or book is None:
            return None
        if at - book.ts > BOOK_MAX_AGE_MS or book.ts > received_at + FUTURE_MS:
            return None

        short_delta, long_delta = self.short.total, self.long.total
        if book.imbalance > self.threshold and short_delta > 0 and long_delta > 0:
            direction = "LONG"
        elif book.imbalance < -self.threshold and short_delta < 0 and long_delta < 0:
            direction = "SHORT"
        else:
            return None
        return Signal(
            self.symbol, direction, received_at, book, short_delta, long_delta,
            self.last_price,
        )
