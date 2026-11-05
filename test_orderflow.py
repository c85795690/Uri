import asyncio
from decimal import Decimal
import os
import unittest
from unittest.mock import AsyncMock, patch

from okx_demo import (DemoAPI, DemoTrader, Instrument, OKXError, SkipOrder,
                      TradeSettings, closed_atr, plan_bracket)
from orderflow_engine import Book, OrderFlow, Signal
from orderflow_runner import Candidate, Market, Settings

D = Decimal
SYMBOL = "BTC-USDT-SWAP"
NOW = 1_800_000_000_000
SETTINGS = TradeSettings(D(100), 5, 1_800_000, 14, D("1.5"), D("1.5"), D(8))
INSTRUMENT = Instrument(D("0.1"), D("0.01"), D("0.01"), D("0.01"), None)


def book(ts=NOW, bid_qty="7", ask_qty="3"):
    return {
        "ts": str(ts),
        "bids": [[str(100 - i), bid_qty, "0", "1"] for i in range(5)],
        "asks": [[str(101 + i), ask_qty, "0", "1"] for i in range(5)],
    }


def trade(trade_id="1", ts=NOW, side="buy", size="4"):
    return {"ts": str(ts), "tradeId": trade_id, "side": side, "sz": size,
            "px": "100.5", "count": "3"}


def setup_signal(direction="LONG"):
    return Signal(SYMBOL, direction, NOW, Book.read(book()), D(4), D(9), D("100.5"))


def candles(count, at=NOW):
    return [[str(at - 60_000 * i), "100", "102", "98", "100", "0", "0", "0", "1"]
            for i in range(1, count + 1)]


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.flow = OrderFlow(SYMBOL, 60_000, 300_000, D("0.30"))

    def test_exact_threshold_is_not_signal(self):
        self.flow.update("books5", book(bid_qty="13", ask_qty="7"), NOW)
        self.flow.update("trades", trade(), NOW)
        self.assertIsNone(self.flow.evaluate(NOW, NOW))

    def test_long_then_short_and_aggregated_quantity_counted_once(self):
        self.flow.update("books5", book(), NOW)
        self.flow.update("trades", trade(), NOW)
        self.assertFalse(self.flow.update("trades", trade(), NOW))
        result = self.flow.evaluate(NOW, NOW)
        self.assertEqual((result.direction, result.short_delta, result.long_delta), ("LONG", D(4), D(4)))
        self.flow.update("trades", trade("2", side="sell", size="12"), NOW)
        self.flow.update("books5", book(bid_qty="3", ask_qty="7"), NOW)
        result = self.flow.evaluate(NOW, NOW)
        self.assertEqual((result.direction, result.short_delta, result.long_delta), ("SHORT", D(-8), D(-8)))

    def test_short_threshold_boundary(self):
        self.flow.update("books5", book(bid_qty="7", ask_qty="13"), NOW)
        self.flow.update("trades", trade(side="sell"), NOW)
        self.assertIsNone(self.flow.evaluate(NOW, NOW))

    def test_windows_use_trade_time_and_handle_out_of_order(self):
        self.flow.update("books5", book(), NOW)
        self.flow.update("trades", trade("1"), NOW)
        self.flow.update("trades", trade("2", NOW - 59_999, "sell", "2"), NOW)
        self.flow.update("trades", trade("3", NOW - 299_999), NOW)
        first = self.flow.evaluate(NOW, NOW)
        second = self.flow.evaluate(NOW + 1, NOW)
        self.assertEqual((first.short_delta, first.long_delta), (D(2), D(6)))
        self.assertEqual((second.short_delta, second.long_delta), (D(4), D(2)))

    def test_warmup_stale_book_and_delta_disagreement(self):
        self.flow.update("books5", book(), NOW)
        self.flow.update("trades", trade("1"), NOW)
        self.assertIsNone(self.flow.evaluate(NOW, NOW + 1))
        self.assertIsNone(self.flow.evaluate(NOW + 3_001, NOW))
        self.flow.update("trades", trade("2", NOW - 60_001, "sell", "10"), NOW)
        self.assertIsNone(self.flow.evaluate(NOW, NOW))

    def test_incomplete_book_disables_evaluation(self):
        self.flow.update("books5", book(), NOW)
        self.flow.update("trades", trade(), NOW)
        incomplete = book()
        incomplete["asks"].pop()
        self.flow.update("books5", incomplete, NOW)
        self.assertIsNone(self.flow.evaluate(NOW, NOW))


class MarketTest(unittest.IsolatedAsyncioTestCase):
    async def test_signal_queue_cooldown_and_reconnect_invalidate_old_signal(self):
        market = Market(None, Settings((SYMBOL,), 60, 300, D("0.30"), 1_800_000))
        market.generation = 1
        market.flows[SYMBOL] = OrderFlow(SYMBOL, 60_000, 300_000, D("0.30"))
        market.ready_at = NOW
        with patch("orderflow_runner.now_ms", return_value=NOW):
            market.tick("books5", SYMBOL, book())
            market.tick("trades", SYMBOL, trade())
            candidate = market.candidates.get_nowait()
            market.tick("trades", SYMBOL, trade("2"))
            self.assertTrue(market.candidates.empty())
            self.assertIsNotNone(market.current_signal(candidate))
            market.generation += 1
            self.assertIsNone(market.current_signal(candidate))
            expired = Candidate(candidate.signal, market.generation)
        with patch("orderflow_runner.now_ms", return_value=NOW + 12_001):
            self.assertIsNone(market.current_signal(expired))


class TradeTest(unittest.IsolatedAsyncioTestCase):
    def test_demo_switch_and_header(self):
        with patch.dict(os.environ, {"ENABLE_OKX_DEMO_ORDERS": "false"}, clear=True):
            with self.assertRaises(ValueError):
                DemoAPI(None)
        with patch.dict(os.environ, {
            "ENABLE_OKX_DEMO_ORDERS": "true", "OKX_API_KEY": "key",
            "OKX_API_SECRET": "secret", "OKX_API_PASSPHRASE": "pass",
        }, clear=True):
            api = DemoAPI(None)
            headers = api.signed_headers("POST", "/api/v5/trade/order", "{}", deadline=NOW)
            self.assertEqual(headers["x-simulated-trading"], "1")
            self.assertEqual(headers["expTime"], str(NOW))

    def test_bracket_prices_size_and_wilder_atr(self):
        bars = candles(20)
        bars.insert(0, [str(NOW), "100", "1000", "1", "100", "0", "0", "0", "0"])
        atr = closed_atr(bars, 14, NOW)
        self.assertEqual(atr, D(4))
        long = plan_bracket(setup_signal("LONG"), INSTRUMENT, atr, SETTINGS)
        short = plan_bracket(setup_signal("SHORT"), INSTRUMENT, atr, SETTINGS)
        self.assertEqual((long.side, long.tp, long.sl), ("buy", D(110), D(95)))
        self.assertEqual((short.side, short.tp, short.sl), ("sell", D(91), D(106)))
        self.assertLessEqual(long.limit * long.size * INSTRUMENT.ct_val, SETTINGS.notional)
        self.assertEqual(short.size % INSTRUMENT.lot, 0)
        with self.assertRaises(SkipOrder):
            closed_atr(bars[:-8], 14, NOW)

    async def test_preflight_rejects_recent_orders_and_pending_algo(self):
        api = AsyncMock()

        async def respond(path, **query):
            if path.endswith("/candles"):
                return candles(20)
            if path.endswith("/orders-history"):
                return [{"clOrdId": "catof123", "cTime": str(NOW - 60_000)}]
            return []

        api.get.side_effect = respond
        trader = DemoTrader(api, SETTINGS)
        with patch("okx_demo.now_ms", return_value=NOW):
            with self.assertRaisesRegex(SkipOrder, "冷卻"):
                await trader.preflight(SYMBOL)
            async def pending_algo(path, **query):
                return [{}] if query.get("ordType") == "conditional,oco" else []
            api.get.side_effect = pending_algo
            with self.assertRaisesRegex(SkipOrder, "策略單"):
                await trader.preflight(SYMBOL)

    def make_trader(self):
        api = AsyncMock()
        api.post.return_value = [{"ordId": "123"}]
        trader = DemoTrader(api, SETTINGS)
        trader.preflight = AsyncMock(return_value=D(4))
        trader.set_leverage = AsyncMock()
        trader.instruments[SYMBOL] = INSTRUMENT
        return trader

    async def test_ack_is_reconciled_and_order_has_both_native_legs(self):
        trader = self.make_trader()
        actual = plan_bracket(setup_signal(), INSTRUMENT, D(4), SETTINGS)
        def submitted_payload():
            return trader.api.post.call_args.args[1]

        async def respond(path, **query):
            if path.endswith("/trade/order"):
                return [{
                    "instId": SYMBOL, "clOrdId": query["clOrdId"], "ordId": "123",
                    "state": "filled", "avgPx": "101", "accFillSz": str(actual.size),
                }]
            attach = submitted_payload()["attachAlgoOrds"][0]
            self.assertEqual(query["algoClOrdId"], attach["attachAlgoClOrdId"])
            return [{
                "instId": SYMBOL, "ordType": "oco", "state": "live", "side": "sell",
                "sz": str(actual.size), "tpTriggerPx": str(actual.tp),
                "slTriggerPx": str(actual.sl), "tpOrdPx": "-1", "slOrdPx": "-1",
            }]

        trader.api.get.side_effect = respond
        outcome = await trader.enter(SYMBOL, setup_signal)
        self.assertIn("已成交", outcome)
        self.assertIn("已確認", outcome)
        payload = submitted_payload()
        self.assertEqual((payload["tdMode"], payload["posSide"], payload["ordType"]),
                         ("isolated", "net", "fok"))

    async def test_unknown_order_locks_entries_without_reposting(self):
        trader = self.make_trader()
        trader.api.post.side_effect = OKXError("POST timeout")
        trader.order_state = AsyncMock(return_value=None)
        result = await trader.enter(SYMBOL, setup_signal)
        self.assertIn("待確認", result)
        self.assertTrue(trader.paused)
        with self.assertRaises(SkipOrder):
            await trader.enter(SYMBOL, setup_signal)
        trader.api.post.assert_awaited_once()

    async def test_unverified_bracket_alerts_and_pauses(self):
        trader = self.make_trader()
        actual = plan_bracket(setup_signal(), INSTRUMENT, D(4), SETTINGS)
        trader.order_state = AsyncMock(return_value={
            "state": "filled", "avgPx": "101", "ordId": "123", "accFillSz": str(actual.size),
        })
        trader.verify_bracket = AsyncMock(return_value=None)
        result = await trader.enter(SYMBOL, setup_signal)
        self.assertTrue(trader.paused)
        self.assertIn("保護單待確認", result)

    async def test_expired_signal_never_submits(self):
        trader = self.make_trader()
        signals = iter((setup_signal(), setup_signal(), None))
        with self.assertRaises(SkipOrder):
            await trader.enter(SYMBOL, lambda: next(signals))
        trader.api.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
