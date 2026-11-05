"""GitHub Actions entry point for the OKX order-flow monitor."""
import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import json
import logging
import os
import re
import signal
from zoneinfo import ZoneInfo

import aiohttp

from okx_demo import DemoAPI, DemoTrader, OKXError, SkipOrder, TradeSettings
from orderflow_engine import OrderFlow, Signal, now_ms, number

LOG = logging.getLogger("orderflow")
OKX_WS = "wss://ws.okx.com/ws/v5/public"
TAIPEI = ZoneInfo("Asia/Taipei")
CHANNELS = ("books5", "trades")
ENTRY_FRESH_MS = 12_000
DEFAULT_SYMBOLS = "BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP"


def env_value(name, default):
    return os.getenv(name, "").strip() or default


def env_int(name, default, lower, upper):
    value = int(env_value(name, str(default)))
    if not lower <= value <= upper:
        raise ValueError(f"{name} 允許範圍為 {lower}～{upper}")
    return value


def env_decimal(name, default, lower, upper):
    value = number(env_value(name, default))
    if not number(lower) <= value <= number(upper):
        raise ValueError(f"{name} 允許範圍為 {lower}～{upper}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    symbols: tuple[str, ...]
    short_seconds: int
    long_seconds: int
    threshold: Decimal
    cooldown_ms: int

    @classmethod
    def read(cls):
        symbols = tuple(dict.fromkeys(
            item.strip().upper() for item in env_value("ORDERFLOW_SYMBOLS", DEFAULT_SYMBOLS).split(",")
            if item.strip()
        ))
        if not 1 <= len(symbols) <= 20 or any(
            not re.fullmatch(r"[A-Z0-9]{1,24}-USDT-SWAP", name) for name in symbols
        ):
            raise ValueError("ORDERFLOW_SYMBOLS 需填 1～20 個 OKX USDT-SWAP 代號，以逗號分隔")
        short = env_int("ORDERFLOW_SHORT_SECONDS", 60, 5, 300)
        long = env_int("ORDERFLOW_LONG_SECONDS", 300, 15, 600)
        if short >= long:
            raise ValueError("短 Delta 視窗必須小於長視窗")
        return cls(
            symbols, short, long, env_decimal("ORDERFLOW_IMBALANCE", "0.30", "0.01", "0.99"),
            env_int("ORDERFLOW_COOLDOWN_SECONDS", 1800, 60, 7200) * 1000,
        )

    def trade_settings(self):
        return TradeSettings(
            env_decimal("POSITION_USDT", "100", "1", "10000"),
            env_int("MAX_POSITIONS", 5, 1, 20),
            self.cooldown_ms,
            env_int("ATR_PERIOD", 14, 2, 80),
            env_decimal("ATR_SL_MULTIPLIER", "1.5", "0.1", "10"),
            env_decimal("TAKE_PROFIT_R", "1.5", "0.1", "10"),
            env_decimal("ENTRY_SLIPPAGE_BPS", "8", "0", "50"),
        )


class Telegram:
    def __init__(self, session):
        self.session = session
        token = os.getenv("TG_TOKEN", "").strip()
        self.chat = os.getenv("TG_CHAT_ID", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or not re.fullmatch(
            r"-?\d+|@[A-Za-z0-9_]+", self.chat
        ):
            raise ValueError("請設定 TG_TOKEN 與 TG_CHAT_ID")
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.messages = asyncio.Queue(maxsize=128)

    async def send(self, message):
        for attempt in range(2):
            try:
                async with self.session.post(
                    self.url, json={"chat_id": self.chat, "text": message},
                    allow_redirects=False,
                ) as response:
                    body = await response.json()
                    if response.status == 200 and body.get("ok") is True:
                        return
                    if response.status == 429 and attempt == 0:
                        delay = int(body.get("parameters", {}).get("retry_after", 1))
                    else:
                        raise RuntimeError(f"Telegram sendMessage HTTP {response.status}")
            except (aiohttp.ClientError, TimeoutError, ValueError, AttributeError) as exc:
                raise RuntimeError(f"Telegram sendMessage {type(exc).__name__}") from None
            await asyncio.sleep(min(max(delay, 1), 15))

    async def publish(self, message):
        LOG.info("%s", message.replace("\n", " | "))
        await self.messages.put(message)

    async def deliver(self):
        while True:
            message = await self.messages.get()
            try:
                await self.send(message)
                await asyncio.sleep(1.1)
            except RuntimeError as exc:
                LOG.warning("%s", exc)
            finally:
                self.messages.task_done()


@dataclass(frozen=True, slots=True)
class Candidate:
    signal: Signal
    generation: int


class StreamError(RuntimeError):
    pass


class Market:
    def __init__(self, session, settings):
        self.session = session
        self.settings = settings
        self.flows = {}
        self.ready_at = None
        self.generation = 0
        self.next_signal = {}
        self.candidates = asyncio.Queue(maxsize=len(settings.symbols))
        self.connects = 0
        self.warmed = 0
        self.updates = 0
        self.signals = 0

    def current_signal(self, candidate):
        stamp = now_ms()
        observed = candidate.signal
        if (candidate.generation != self.generation or self.ready_at is None
                or not 0 <= stamp - observed.at <= ENTRY_FRESH_MS):
            return None
        fresh = self.flows[observed.symbol].evaluate(stamp, self.ready_at)
        if fresh is not None and fresh.direction == observed.direction:
            return fresh
        return None

    def tick(self, channel, symbol, row):
        stamp = now_ms()
        flow = self.flows[symbol]
        if not flow.update(channel, row, stamp):
            return
        self.updates += 1
        if self.ready_at is not None and stamp >= self.ready_at and self.warmed < self.connects:
            self.warmed = self.connects
            LOG.info("暖機完成，開始評估多空訊號")
        signal = flow.evaluate(stamp, self.ready_at)
        loop_now = asyncio.get_running_loop().time()
        if signal and loop_now >= self.next_signal.get(symbol, 0):
            if not self.candidates.full():
                self.candidates.put_nowait(Candidate(signal, self.generation))
                self.next_signal[symbol] = loop_now + self.settings.cooldown_ms / 1000
                self.signals += 1

    async def listen(self):
        self.generation += 1
        self.ready_at = None
        self.flows = {
            symbol: OrderFlow(symbol, self.settings.short_seconds * 1000,
                              self.settings.long_seconds * 1000, self.settings.threshold)
            for symbol in self.settings.symbols
        }
        subscriptions = {(channel, symbol) for channel in CHANNELS for symbol in self.flows}
        async with self.session.ws_connect(
            OKX_WS, timeout=aiohttp.ClientWSTimeout(ws_close=3), max_msg_size=1_048_576,
        ) as socket:
            try:
                await self.read_socket(socket, subscriptions)
            finally:
                self.ready_at = None

    async def read_socket(self, socket, subscriptions):
        awaiting = subscriptions.copy()
        arguments = [{"channel": channel, "instId": symbol} for channel, symbol in sorted(subscriptions)]
        loop = asyncio.get_running_loop()
        await socket.send_json({"id": "orderflow", "op": "subscribe", "args": arguments})
        ack_deadline = loop.time() + 15
        pong_deadline = None
        while True:
            deadline = pong_deadline if pong_deadline is not None else loop.time() + 20
            if awaiting:
                deadline = min(deadline, ack_deadline)
            try:
                async with asyncio.timeout(max(0, deadline - loop.time())):
                    message = await socket.receive()
            except TimeoutError:
                if awaiting or pong_deadline is not None:
                    raise StreamError("OKX 訂閱或心跳逾時") from None
                await socket.send_str("ping")
                pong_deadline = loop.time() + 5
                continue
            if message.type != aiohttp.WSMsgType.TEXT:
                raise StreamError("OKX WebSocket 已中斷")
            if message.data == "pong":
                pong_deadline = None
                continue
            payload = json.loads(message.data)
            event = payload.get("event")
            if event == "error":
                code = str(payload.get("code", ""))
                raise StreamError(f"OKX 訂閱錯誤 {code if code.isdecimal() else ''}")
            argument = payload.get("arg", {})
            key = argument.get("channel"), argument.get("instId")
            if key not in subscriptions:
                continue
            if event == "subscribe":
                if key in awaiting:
                    awaiting.remove(key)
                    if not awaiting:
                        self.connects += 1
                        self.ready_at = now_ms() + self.settings.long_seconds * 1000
                        LOG.info("OKX 已訂閱 %d 個標的，暖機 %d 秒",
                                 len(self.flows), self.settings.long_seconds)
                continue
            for row in payload.get("data", ()):
                self.tick(*key, row)

    async def run(self):
        delay = 1
        startup_failures = 0
        while True:
            previous_connects = self.connects
            try:
                await self.listen()
            except (StreamError, aiohttp.ClientError, TimeoutError, ValueError,
                    KeyError, TypeError, IndexError, AttributeError) as exc:
                detail = str(exc) if isinstance(exc, StreamError) else type(exc).__name__
                LOG.warning("OKX 行情連線重試｜%s", detail)
            if self.connects == 0:
                startup_failures += 1
                if startup_failures >= 6:
                    raise StreamError("無法訂閱 OKX 行情，請檢查標的或 Actions 日誌")
            if self.connects > previous_connects:
                delay = 1
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def signal_message(observed, settings, mode):
    direction = "🐰 做多 LONG" if observed.direction == "LONG" else "🐻 做空 SHORT"
    stamp = datetime.fromtimestamp(observed.at / 1000, TAIPEI).strftime("%m/%d %H:%M:%S")
    return (f"🐱 OKX Order Flow｜{direction}\n"
            f"{observed.symbol}｜台灣 {stamp}\n"
            f"📚 五檔不平衡 {observed.book.imbalance:+.1%}\n"
            f"⚡ Delta {settings.short_seconds}s {observed.short_delta:+f} 張"
            f"｜{settings.long_seconds}s {observed.long_delta:+f} 張\n"
            f"🎀 {mode}")


async def process_candidates(market, telegram, trader):
    while True:
        candidate = await market.candidates.get()
        if candidate is None:
            return
        observed = candidate.signal
        mode = "僅通知" if trader is None else "OKX DEMO"
        await telegram.publish(signal_message(observed, market.settings, mode))
        if trader is None:
            continue
        try:
            outcome = await trader.enter(observed.symbol, lambda: market.current_signal(candidate))
        except (SkipOrder, OKXError, ValueError, KeyError, TypeError, IndexError) as exc:
            reason = str(exc) if isinstance(exc, (SkipOrder, OKXError)) else "OKX 資料或規格檢查失敗"
            outcome = f"⚪ OKX DEMO 略過｜{observed.symbol}\n{reason}"
        await telegram.publish(outcome)
        # Serial submissions stay within the shared account algo-query limit.
        await asyncio.sleep(1.05)


async def monitor(mode, minutes):
    settings = Settings.read()
    if not 10 <= minutes <= 230 or minutes * 60 <= settings.long_seconds + 10:
        raise ValueError("執行時間需在 10～230 分鐘，且須比長視窗多 10 秒以上")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6, connect=4)) as session:
        telegram = Telegram(session)
        trader = None
        if mode == "demo":
            trader = DemoTrader(DemoAPI(session), settings.trade_settings())
            try:
                await trader.check_account(settings.symbols)
            except (OKXError, SkipOrder, ValueError, KeyError, TypeError) as exc:
                reason = str(exc) if isinstance(exc, (OKXError, SkipOrder)) else "DEMO 規格資料異常"
                await telegram.send(f"🚨 OKX DEMO 啟動失敗\n{reason}")
                raise RuntimeError(reason) from None
        await telegram.send(
            f"🐾 OKX Order Flow 已啟動｜{'DEMO 下單' if trader else '僅通知'}\n"
            f"🔎 {', '.join(settings.symbols)}\n"
            f"⏱ {minutes} 分鐘｜暖機 {settings.long_seconds} 秒｜門檻 ±{settings.threshold}"
        )
        market = Market(session, settings)
        feed = asyncio.create_task(market.run())
        consumer = asyncio.create_task(process_candidates(market, telegram, trader))
        delivery = asyncio.create_task(telegram.deliver())
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait(
                (feed, consumer, delivery, stopped), timeout=minutes * 60,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (feed, consumer, delivery):
                if task in done:
                    task.result()
                    raise RuntimeError("監控程序提前結束")
        finally:
            market.ready_at = None
            feed.cancel()
            stopped.cancel()
            await asyncio.gather(feed, stopped, return_exceptions=True)
            while not market.candidates.empty():
                market.candidates.get_nowait()
            market.candidates.put_nowait(None)
            try:
                await asyncio.wait_for(consumer, timeout=90)
                await telegram.publish(
                    f"🐾 OKX Order Flow 本輪結束｜連線 {market.connects} 次"
                    f"｜更新 {market.updates} 次｜訊號 {market.signals} 筆"
                )
                await asyncio.wait_for(telegram.messages.join(), timeout=30)
            finally:
                delivery.cancel()
                await asyncio.gather(delivery, return_exceptions=True)
        if not market.warmed:
            raise RuntimeError("本輪尚未收滿長視窗行情，請檢查 Actions 日誌")
        if trader and trader.paused:
            raise RuntimeError("請核對 Telegram 或 Actions 日誌中的 OKX DEMO 訂單")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("notify", "demo"), default="notify")
    parser.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(monitor(args.mode, args.minutes))
    except (ValueError, RuntimeError, TimeoutError) as exc:
        LOG.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
