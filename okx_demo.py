"""Signed OKX DEMO-only execution with an exchange-side ATR bracket."""
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import urlencode
from uuid import uuid4

import aiohttp
from yarl import URL

from orderflow_engine import now_ms, number, positive

LOG = logging.getLogger(__name__)
REST = "https://openapi.okx.com"
TAG = "catflow"
ORDER_PREFIX = "catof"
ALGO_TYPES = ("conditional,oco", "trigger", "move_order_stop", "twap",
              "iceberg", "smart_iceberg", "chase")
# Explicit business rejects. Transport errors and other codes go to reconciliation.
ORDER_REJECTS = {"50016", "51000", "51001", "51006", "51008", "51010", "51015",
                 "51120", "51121", "51169", "51277", "51278", "51279", "51280"}


class OKXError(RuntimeError):
    def __init__(self, message, code=""):
        super().__init__(message)
        self.code = code


class SkipOrder(RuntimeError):
    pass


def require_one(rows, label):
    if len(rows) != 1:
        raise OKXError(f"{label} 回傳筆數異常")
    return rows[0]


def px_text(value):
    return format(value, "f")


class DemoAPI:
    def __init__(self, session):
        self.session = session
        if os.getenv("ENABLE_OKX_DEMO_ORDERS", "").strip().lower() != "true":
            raise ValueError("DEMO 模式需要 ENABLE_OKX_DEMO_ORDERS=true")
        self.key = os.getenv("OKX_API_KEY", "").strip()
        self.secret = os.getenv("OKX_API_SECRET", "").strip()
        self.passphrase = os.getenv("OKX_API_PASSPHRASE", "").strip()
        if not all((self.key, self.secret, self.passphrase)):
            raise ValueError("缺少 OKX DEMO API 的三個 Secrets")

    def signed_headers(self, method, path, body, deadline=None):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        timestamp = timestamp.replace("+00:00", "Z")
        prehash = f"{timestamp}{method}{path}{body}".encode()
        signature = hmac.new(self.secret.encode(), prehash, hashlib.sha256).digest()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": base64.b64encode(signature).decode(),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "x-simulated-trading": "1",
        }
        if deadline is not None:
            headers["expTime"] = str(deadline)
        return headers

    async def request(self, method, path, *, query=None, payload=None, deadline=None):
        target = f"{path}?{urlencode(query)}" if query else path
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = self.signed_headers(method, target, body, deadline)
        try:
            async with self.session.request(
                method, URL(REST + target, encoded=True), headers=headers,
                data=body if body else None, allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise OKXError(f"{path} HTTP {response.status}")
                result = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise OKXError(f"{path} {type(exc).__name__}") from None

        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise OKXError(f"{path} API 回應格式異常")
        rows = result["data"]
        if any(not isinstance(row, dict) for row in rows) and path != "/api/v5/market/candles":
            raise OKXError(f"{path} API 資料格式異常")
        code = str(result.get("code", ""))
        if path == "/api/v5/trade/order" and method == "POST" and rows:
            code = str(rows[0].get("sCode") or code)
        if result.get("code") != "0" or code != "0":
            code = code if code.isascii() and code.isdecimal() else ""
            raise OKXError(f"{path} OKX {code or 'API_ERROR'}", code)
        return rows

    async def get(self, path, **query):
        return await self.request("GET", path, query=query)

    async def post(self, path, payload, deadline=None):
        return await self.request("POST", path, payload=payload, deadline=deadline)


@dataclass(frozen=True, slots=True)
class TradeSettings:
    notional: Decimal
    max_positions: int
    cooldown_ms: int
    atr_period: int
    stop_atr: Decimal
    tp_r: Decimal
    slip_bps: Decimal


@dataclass(frozen=True, slots=True)
class Instrument:
    tick: Decimal
    lot: Decimal
    minimum: Decimal
    ct_val: Decimal
    max_limit: Decimal | None

    @classmethod
    def read(cls, symbol, row):
        if (row["state"] != "live" or row["ctType"] != "linear"
                or row["settleCcy"] != "USDT"
                or row["ctValCcy"] != symbol.removesuffix("-USDT-SWAP")):
            raise SkipOrder(f"{symbol} 需為啟用中的 USDT 線性永續")
        return cls(
            positive(row["tickSz"]), positive(row["lotSz"]), positive(row["minSz"]),
            positive(row["ctVal"]),
            positive(row["maxLmtSz"]) if row.get("maxLmtSz") else None,
        )


@dataclass(frozen=True, slots=True)
class Bracket:
    side: str
    limit: Decimal
    size: Decimal
    tp: Decimal
    sl: Decimal


def step(value, unit, rounding):
    return (value / unit).to_integral_value(rounding=rounding) * unit


def plan_bracket(signal, instrument, atr, settings):
    bid, ask = signal.book.bid, signal.book.ask
    slip = settings.slip_bps / 10_000
    risk = atr * settings.stop_atr
    tick = instrument.tick
    if signal.direction == "LONG":
        side = "buy"
        limit = step(ask * (1 + slip), tick, ROUND_FLOOR)
        tp = step(ask + risk * settings.tp_r, tick, ROUND_CEILING)
        sl = step(ask - risk, tick, ROUND_FLOOR)
        valid = limit >= ask and tp > max(limit, signal.last_price) and sl < min(bid, signal.last_price)
    else:
        side = "sell"
        limit = step(bid * (1 - slip), tick, ROUND_CEILING)
        tp = step(bid - risk * settings.tp_r, tick, ROUND_FLOOR)
        sl = step(bid + risk, tick, ROUND_CEILING)
        valid = limit <= bid and tp < min(limit, signal.last_price) and sl > max(ask, signal.last_price)
    if not valid or min(limit, tp, sl) <= 0:
        raise SkipOrder("ATR 保護價與目前盤口／滑價上限過近")
    size = settings.notional / (max(ask, limit) * instrument.ct_val)
    if instrument.max_limit is not None:
        size = min(size, instrument.max_limit)
    size = step(size, instrument.lot, ROUND_FLOOR)
    if size < instrument.minimum:
        raise SkipOrder("設定的名義金額低於 OKX 最小下單張數")
    return Bracket(side, limit, size, tp, sl)


def closed_atr(rows, period, at):
    bars = {int(row[0]): row for row in rows if row[8] == "1"}
    if not bars:
        raise SkipOrder("OKX 尚未提供已收盤 1m K")
    latest = max(bars)
    if not 60_000 <= at - latest <= 180_000:
        raise SkipOrder("已收盤 1m K 資料逾時")
    consecutive = []
    ts = latest
    while ts in bars:
        consecutive.append(bars[ts])
        ts -= 60_000
    if len(consecutive) < period + 1:
        raise SkipOrder("連續的已收盤 1m K 不足以計算 ATR")
    oldest, *rest = reversed(consecutive)
    previous_close = positive(oldest[4])
    ranges = []
    for row in rest:
        high, low, close = (positive(row[i]) for i in (2, 3, 4))
        if not low <= close <= high:
            raise SkipOrder("1m K 高低價資料異常")
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        previous_close = close
    atr = sum(ranges[:period]) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    if atr <= 0:
        raise SkipOrder("ATR 必須大於零")
    return atr


class DemoTrader:
    def __init__(self, api, settings):
        self.api = api
        self.settings = settings
        self.instruments = {}
        self.paused = False

    async def check_account(self, symbols):
        config = require_one(await self.api.get("/api/v5/account/config"), "OKX 帳戶設定")
        if config.get("posMode") != "net_mode" or config.get("acctLv") not in ("2", "3"):
            raise OKXError("DEMO 帳戶需為合約或跨幣種保證金模式，並設定單向持倉")
        rows = await self.api.get("/api/v5/account/instruments", instType="SWAP")
        instruments = {row["instId"]: row for row in rows}
        for symbol in symbols:
            if symbol not in instruments:
                raise OKXError(f"{symbol} 在此 OKX DEMO 帳戶不可交易")
            self.instruments[symbol] = Instrument.read(symbol, instruments[symbol])

    async def positions(self):
        rows = await self.api.get("/api/v5/account/positions", instType="SWAP")
        return {row["instId"]: row for row in rows if number(row["pos"]) != 0}

    async def preflight(self, symbol):
        cutoff = now_ms() - self.settings.cooldown_ms
        results = await asyncio.gather(
            self.positions(),
            self.api.get("/api/v5/trade/orders-pending", instType="SWAP", limit=100),
            self.api.get("/api/v5/trade/orders-history", instType="SWAP",
                         instId=symbol, begin=cutoff, limit=100),
            self.api.get("/api/v5/market/candles", instId=symbol, bar="1m",
                         limit=max(100, self.settings.atr_period * 3 + 1)),
            *(self.api.get("/api/v5/trade/orders-algo-pending", ordType=kind,
                           instId=symbol, instType="SWAP", limit=1)
              for kind in ALGO_TYPES),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        positions, pending, history, candles, *algos = results
        occupied = positions.keys() | {row["instId"] for row in pending}
        if symbol in occupied or any(algos):
            raise SkipOrder("已有該幣的持倉、掛單或策略單")
        if len(occupied) >= self.settings.max_positions or len(pending) >= 100:
            raise SkipOrder("DEMO 永續持倉／掛單已達上限")
        if len(history) >= 100 or any(
            row.get("clOrdId", "").startswith(ORDER_PREFIX) and int(row["cTime"]) >= cutoff
            for row in history
        ):
            raise SkipOrder("仍在 OKX 訂單歷史冷卻期")
        return closed_atr(candles, self.settings.atr_period, now_ms())

    async def set_leverage(self, symbol):
        query = {"instId": symbol, "mgnMode": "isolated"}
        info = require_one(await self.api.get("/api/v5/account/leverage-info", **query), "逐倉槓桿")
        if info.get("lever") == "5":
            return
        result = require_one(await self.api.post(
            "/api/v5/account/set-leverage", {**query, "lever": "5"}
        ), "設定逐倉槓桿")
        if result.get("lever") != "5" or result.get("mgnMode") != "isolated":
            raise OKXError("OKX 未確認逐倉 5 倍槓桿")

    async def order_state(self, symbol, client_id):
        for _ in range(5):
            try:
                rows = await self.api.get("/api/v5/trade/order", instId=symbol, clOrdId=client_id)
                if len(rows) == 1 and rows[0].get("state") in ("filled", "canceled", "mmp_canceled"):
                    if rows[0].get("clOrdId") == client_id and rows[0].get("instId") == symbol:
                        return rows[0]
            except OKXError:
                pass
            await asyncio.sleep(0.7)
        return None

    async def verify_bracket(self, symbol, algo_client_id, plan):
        for _ in range(5):
            try:
                rows = await self.api.get("/api/v5/trade/order-algo", algoClOrdId=algo_client_id)
                if len(rows) == 1 and rows[0].get("instId") == symbol:
                    algo = rows[0]
                    if algo.get("state") == "live" and algo.get("ordType") == "oco":
                        if (number(algo["tpTriggerPx"]) == plan.tp
                                and number(algo["slTriggerPx"]) == plan.sl
                                and algo.get("tpOrdPx") == "-1" and algo.get("slOrdPx") == "-1"
                                and number(algo["sz"]) == plan.size
                                and algo.get("side") == ("sell" if plan.side == "buy" else "buy")):
                            return "OKX 已確認 TP／SL 同組保護單"
                    if algo.get("state") == "effective" and symbol not in await self.positions():
                        return "TP／SL 已觸發，OKX 已確認此幣無持倉"
            except (OKXError, ValueError, KeyError, TypeError):
                pass
            await asyncio.sleep(0.7)
        return None

    async def enter(self, symbol, current_signal):
        if self.paused:
            raise SkipOrder("本次執行的 DEMO 下單已暫停")
        if current_signal() is None:
            raise SkipOrder("訊號已過期、反向或正在重連")
        atr = await self.preflight(symbol)
        if current_signal() is None:
            raise SkipOrder("預檢完成時訊號已失效")
        await self.set_leverage(symbol)
        signal = current_signal()
        if signal is None:
            raise SkipOrder("送單前訊號已失效")
        plan = plan_bracket(signal, self.instruments[symbol], atr, self.settings)
        suffix = uuid4().hex[:24]
        client_id, algo_client_id = ORDER_PREFIX + suffix, "catab" + suffix
        payload = {
            "instId": symbol, "tdMode": "isolated", "posSide": "net",
            "side": plan.side, "ordType": "fok", "sz": px_text(plan.size),
            "px": px_text(plan.limit), "clOrdId": client_id, "tag": TAG,
            "attachAlgoOrds": [{
                "attachAlgoClOrdId": algo_client_id,
                "tpTriggerPx": px_text(plan.tp), "tpOrdPx": "-1", "tpTriggerPxType": "last",
                "slTriggerPx": px_text(plan.sl), "slOrdPx": "-1", "slTriggerPxType": "last",
            }],
        }
        LOG.info("DEMO submit %s %s clOrdId=%s", symbol, plan.side, client_id)
        send_issue = ""
        try:
            ack = require_one(await self.api.post(
                "/api/v5/trade/order", payload, deadline=now_ms() + 2_500
            ), "送單回覆")
            LOG.info("DEMO acknowledgement clOrdId=%s ordId=%s", client_id, ack.get("ordId"))
        except OKXError as exc:
            if exc.code in ORDER_REJECTS:
                return f"⚪ OKX DEMO 拒單｜{symbol}\nOKX {exc.code}｜{client_id}"
            send_issue = f"｜{exc}"
            LOG.warning("DEMO submit response uncertain clOrdId=%s %s", client_id, exc)

        order = await self.order_state(symbol, client_id)
        if order is not None and order["state"] in ("canceled", "mmp_canceled"):
            try:
                unfilled = number(order["accFillSz"]) == 0
            except (KeyError, ValueError):
                unfilled = False
            if unfilled:
                return f"⚪ OKX DEMO 未成交｜{symbol}\nFOK 已撤銷｜{client_id}"
        if order is not None and order["state"] == "filled":
            try:
                complete = number(order["accFillSz"]) == plan.size
                average = positive(order["avgPx"])
            except (KeyError, ValueError):
                complete = False
            if complete:
                protection = await self.verify_bracket(symbol, algo_client_id, plan)
                details = (f"{symbol} {signal.direction}｜{plan.size} 張｜均價 {average}\n"
                           f"🧁 TP {plan.tp}｜🛡 SL {plan.sl}\n"
                           f"單號 {order.get('ordId', client_id)}")
                if protection:
                    return f"✅ OKX DEMO 已成交｜逐倉 5×\n{details}\n{protection}"
                self.paused = True
                return (f"🚨 OKX DEMO 已成交｜保護單待確認\n{details}\n"
                        f"保護單識別碼 {algo_client_id}｜本次執行暫停新單")
        self.paused = True
        return (f"🚨 OKX DEMO 訂單狀態待確認｜{symbol}\n"
                f"clOrdId {client_id}{send_issue}\n本次執行暫停新單")
