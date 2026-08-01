#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE SEMI-HFT ALGO TRADER
Capital  : INR 1,00,000
Target   : INR 180 profit / trade  (approx 0.18 pct move)
Stop-Loss: INR 100
Universe : Nifty 500 (NSE equities)
Trend    : Long-only when BOTH stock AND Nifty-50 trend UP

Strategy
--------
1. Stream live ticks for Nifty-500 stocks + Nifty-50 index via WebSocket.
2. Maintain a rolling price window (last N ticks) per symbol.
3. Score every symbol using a 4-component composite signal:
     - Linreg slope (sustained directional move)        weight 40 pct
     - R-squared / consistency (clean vs noisy move)    weight 25 pct
     - Recent velocity (last quarter vs first quarter)  weight 20 pct
     - Volume surge ratio                               weight 15 pct
4. Apply dual trend filter: both stock AND Nifty-50 must trend UP.
5. Enter at ltp minus 1 tick (limit BUY for better fill quality).
   TP  = entry + INR180 / qty
   SL  = entry - INR100 / qty
6. Place limit SELL at TP and SL-LMT SELL at SL after fill confirmed.
7. After TP or SL fires, apply cooldown then re-scan.
8. Hard safety guards:
     - Daily loss kill-switch (INR 500 default)
     - Hard square-off at 15:25 IST
     - Market hours guard (09:15 - 15:20)
     - Emergency market sell on Ctrl+C / SIGTERM

Usage
-----
    pip install NorenRestApiPy numpy
    python trader.py

Config is read from trader_config.json in the same directory.
A template is auto-created on first run. Edit credentials then re-run.
=============================================================================
"""

import json
import logging
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict

try:
    from NorenRestApiPy.NorenApi import NorenApi
except ImportError:
    print("[FATAL] NorenRestApiPy not installed.  Run: pip install NorenRestApiPy")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("[FATAL] numpy not installed.  Run: pip install numpy")
    sys.exit(1)


# ============================================================
# LOGGING
# ============================================================

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            "trader_{}.log".format(date.today().strftime("%Y%m%d")),
            mode="a",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("HFT-TRADER")


# ============================================================
# CONFIG
# ============================================================

DEFAULT_CONFIG = {
    # Flattrade credentials
    "user_id": "YOUR_USER_ID",
    "api_token": "YOUR_SESSION_TOKEN",
    # Capital and risk
    "capital": 100000,
    "target_profit_inr": 180,
    "stop_loss_inr": 100,
    "max_daily_loss_inr": 500,
    "max_trade_value_pct": 0.95,
    # Strategy knobs
    "tick_window": 20,
    "min_ticks_for_signal": 10,
    "entry_slippage_ticks": 1,
    "tick_size": 0.05,
    "min_stock_price": 50,
    "max_stock_price": 10000,
    "order_fill_timeout_sec": 30,
    "cooldown_sec": 10,
    "scan_interval_sec": 1,
    # Signal weights (should sum to 1.0)
    "momentum_linreg_weight": 0.40,
    "consistency_weight": 0.25,
    "velocity_weight": 0.20,
    "volume_surge_weight": 0.15,
    # Market hours IST
    "market_open_hhmm": "09:15",
    "market_close_hhmm": "15:20",
    "square_off_hhmm": "15:25",
    # Exchange
    "exchange": "NSE",
    "product_type": "I",
    # Nifty 50 index NSE token
    "nifty50_token": "26000",
}

CONFIG_FILE = Path(__file__).parent / "trader_config.json"


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=4))
        log.warning(
            "Created config template at {}. Fill credentials and re-run.".format(CONFIG_FILE)
        )
        sys.exit(0)
    cfg = json.loads(CONFIG_FILE.read_text())
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


CFG = load_config()


# ============================================================
# NIFTY 500 UNIVERSE
# ============================================================

NIFTY500_SYMBOLS = [
    "3MINDIA", "ABB", "ACC", "APLAPOLLO", "AUBANK", "AARTIIND", "AAVAS", "ABBOTINDIA",
    "ADANIENT", "ADANIPORTS", "ADANIPOWER", "ATGL", "ABCAPITAL", "ABFRL", "AEGISCHEM",
    "AETHER", "AJANTPHARM", "AKZOINDIA", "ALKEM", "ALLCARGO", "AMBUJACEM", "ANGELONE",
    "APARINDS", "APOLLOHOSP", "APOLLOTYRE", "ASTRAL", "ATUL", "AUROPHARMA",
    "DMART", "AXISBANK", "BEML", "BEL", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
    "BALKRISIND", "BALRAMCHIN", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "BATAINDIA",
    "BAYERCROP", "BERGEPAINT", "BHARATFORG", "BHEL", "BPCL", "BHARTIARTL", "BIOCON",
    "BLUESTARCO", "BOSCHLTD", "BRIGADE", "BRITANNIA", "CANBK", "CANFINHOME",
    "CASTROLIND", "CEATLTD", "CENTRALBK", "CENTURYPLY", "CENTURYTEX", "CERA",
    "CHOLAFIN", "CIPLA", "COALINDIA", "COCHINSHIP", "COFORGE", "COLPAL", "COROMANDEL",
    "CROMPTON", "CUMMINSIND", "CYIENT", "DCMSHRIRAM", "DEEPAKNTR", "DELHIVERY",
    "DEVYANI", "DIXON", "DLF", "DRREDDY", "EICHERMOT", "EMAMILTD", "ENDURANCE",
    "ESCORTS", "EXIDEIND", "FEDERALBNK", "FACT", "FORTIS", "GAIL", "GMRINFRA",
    "GLAND", "GLAXO", "GLENMARK", "GODREJCP", "GODREJPROP", "GRANULES", "GRASIM",
    "GUJGASLTD", "GNFC", "GRINDWELL", "HAVELLS", "HCLTECH", "HDFCAMC", "HDFCBANK",
    "HDFCLIFE", "HATSUN", "HEROMOTOCO", "HINDALCO", "HAL", "HINDPETRO", "HINDUNILVR",
    "ICICIBANK", "ICICIGI", "ICICIPRULI", "IDFCFIRSTB", "IEX", "IIFL", "IRB",
    "IRCON", "ITC", "INDHOTEL", "INDIAMART", "INDIGO", "INDUSINDBK", "INDUSTOWER",
    "INFY", "INTELLECT", "IOC", "IRCTC", "IRFC", "JBCHEPHARM", "JKTYRE", "JMFINANCIL",
    "JKCEMENT", "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "JUBLINDS", "JUSTDIAL",
    "JYOTHYLAB", "KPRMILL", "KALYANKJIL", "KANSAINER", "KAYNES", "KEI", "KOTAKBANK",
    "KPIL", "LTTS", "LTIM", "LT", "LICHSGFIN", "LICI", "LUPIN", "LUXIND", "MARICO",
    "MARUTI", "MASTEK", "MCX", "METROPOLIS", "MINDTREE", "MGL", "MOTILALOFS", "MPHASIS",
    "MUTHOOTFIN", "NATCOPHARM", "NBCC", "NCC", "NHPC", "NLCINDIA", "NMDC", "NTPC",
    "NAUKRI", "NAVINFLUOR", "NESTLEIND", "NILKAMAL", "OBEROIRLTY", "ONGC", "OIL",
    "OFSS", "PAGEIND", "PAYTM", "PERSISTENT", "PETRONET", "PFIZER", "PHOENIXLTD",
    "PIDILITIND", "PIIND", "PNB", "PNBHOUSING", "POLICYBZR", "POLYCAB", "POONAWALLA",
    "POWERGRID", "PFC", "PVRINOX", "RBLBANK", "RECLTD", "RELIANCE", "RITES", "RADICO",
    "RAIN", "RAMCOCEM", "ROUTE", "SBICARD", "SBILIFE", "SJVN", "SRF", "SANOFI",
    "SCHAEFFLER", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOBHA", "SONACOMS",
    "SUNPHARMA", "SUNTV", "SUPREMEIND", "SUZLON", "SYNGENE", "TVSMOTOR", "TANLA",
    "TATACHEM", "TATACOMM", "TCS", "TATAELXSI", "TATAMOTORS", "TATAPOWER", "TATASTEEL",
    "TECHM", "TIINDIA", "TITAN", "TORNTPHARM", "TORNTPOWER", "TRENT", "TRIDENT",
    "UCOBANK", "ULTRACEMCO", "UBL", "UNIONBANK", "UPL", "UTIAMC", "VGUARD", "VBL",
    "VEDL", "VOLTAS", "WIPRO", "WOCKPHARMA", "ZEEL", "ZOMATO", "ZYDUSLIFE",
]


# ============================================================
# FLATTRADE API CLIENT
# ============================================================

class FlattradeClient(NorenApi):
    """Subclass NorenApi with Flattrade-specific endpoints."""
    def __init__(self):
        super().__init__(
            host="https://piconnect.flattrade.in/NorenWClientTP/",
            websocket="wss://piconnect.flattrade.in/NorenWSTP/",
        )


# ============================================================
# TICK STORE -- thread-safe rolling price + volume buffer
# ============================================================

class TickStore:
    """Stores last N ticks per token. WS thread writes, strategy thread reads."""

    def __init__(self, maxlen):
        self._prices = defaultdict(lambda: deque(maxlen=maxlen))
        self._volumes = defaultdict(lambda: deque(maxlen=maxlen))
        self._ltp = {}
        self._lock = threading.RLock()

    def update(self, token, ltp, volume=0.0):
        with self._lock:
            self._prices[token].append(ltp)
            self._volumes[token].append(volume)
            self._ltp[token] = ltp

    def prices(self, token):
        with self._lock:
            return list(self._prices[token])

    def volumes(self, token):
        with self._lock:
            return list(self._volumes[token])

    def ltp(self, token):
        with self._lock:
            return self._ltp.get(token)

    def tick_count(self, token):
        with self._lock:
            return len(self._prices[token])


# ============================================================
# SIGNAL ENGINE -- composite momentum scoring
# ============================================================

class SignalEngine:
    """
    4-component composite score in [0, 1].

    Components:
      1. Linreg slope (normalised)     sustained directional move
      2. R-squared                     clean vs noisy move
      3. Velocity (late/early mean)    acceleration in price
      4. Volume surge ratio            volume confirmation
    """

    def __init__(self, cfg):
        self.w_slope = cfg["momentum_linreg_weight"]
        self.w_r2 = cfg["consistency_weight"]
        self.w_vel = cfg["velocity_weight"]
        self.w_vol = cfg["volume_surge_weight"]
        self.min_ticks = cfg["min_ticks_for_signal"]

    def score(self, prices, volumes):
        n = len(prices)
        if n < self.min_ticks:
            return 0.0
        arr = np.array(prices, dtype=np.float64)
        x = np.arange(n, dtype=np.float64)
        mean = arr.mean()
        if mean <= 0:
            return 0.0

        # 1. Linreg slope score
        slope, intercept = np.polyfit(x, arr, 1)
        slope_score = float(np.clip(slope / mean * 5000, 0, 1))

        # 2. R-squared score
        fitted = slope * x + intercept
        ss_res = float(np.sum((arr - fitted) ** 2))
        ss_tot = float(np.sum((arr - mean) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0
        r2_score = float(np.clip(r2, 0, 1))

        # 3. Velocity score
        q = max(1, n // 4)
        early = float(arr[:q].mean())
        late = float(arr[-q:].mean())
        vel_score = float(np.clip((late - early) / (early + 1e-9) * 500, 0, 1))

        # 4. Volume surge score
        if len(volumes) >= self.min_ticks:
            va = np.array(volumes, dtype=np.float64)
            vm = va[:-1].mean() if len(va) > 1 and va[:-1].sum() > 0 else 1.0
            vol_score = float(np.clip((va[-1] / (vm + 1e-9) - 1.0) / 4.0, 0, 1))
        else:
            vol_score = 0.0

        composite = (
            self.w_slope * slope_score
            + self.w_r2 * r2_score
            + self.w_vel * vel_score
            + self.w_vol * vol_score
        )
        return float(np.clip(composite, 0.0, 1.0))

    def trend(self, prices):
        """Returns UP / DOWN / FLAT from linreg slope of last 20 ticks."""
        n = len(prices)
        if n < 5:
            return "FLAT"
        arr = np.array(prices[-20:], dtype=np.float64)
        x = np.arange(len(arr), dtype=np.float64)
        slope, _ = np.polyfit(x, arr, 1)
        ns = slope / (arr.mean() + 1e-9)
        if ns > 0.000015:
            return "UP"
        if ns < -0.000015:
            return "DOWN"
        return "FLAT"


# ============================================================
# ORDER STATE MACHINE
# ============================================================

class OrderState:
    IDLE = "IDLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    IN_TRADE = "IN_TRADE"


class TradeRecord:
    def __init__(self, symbol, token, entry_order_id, entry_price, qty, tp_price, sl_price):
        self.symbol = symbol
        self.token = token
        self.entry_order_id = entry_order_id
        self.entry_price = entry_price
        self.qty = qty
        self.tp_price = tp_price
        self.sl_price = sl_price
        self.tp_order_id = None
        self.sl_order_id = None
        self.placed_at = time.time()


class OrderManager:
    """Full trade lifecycle: entry -> fill -> TP/SL -> exit -> cooldown."""

    def __init__(self, api, cfg):
        self.api = api
        self.cfg = cfg
        self._lock = threading.Lock()
        self.state = OrderState.IDLE
        self.current = None
        self.daily_pnl = 0.0
        self.trade_count = 0
        self._cd_end = 0.0

    def _round(self, p):
        ts = self.cfg["tick_size"]
        return round(round(p / ts) * ts, 2)

    def _qty(self, price):
        return max(1, int(self.cfg["capital"] * self.cfg["max_trade_value_pct"] // price))

    def _tp(self, entry, qty):
        return self._round(entry + self.cfg["target_profit_inr"] / qty)

    def _sl(self, entry, qty):
        return self._round(entry - self.cfg["stop_loss_inr"] / qty)

    def is_ready(self):
        with self._lock:
            if self.state != OrderState.IDLE:
                return False
            if self.daily_pnl <= -self.cfg["max_daily_loss_inr"]:
                log.warning("[KILL SWITCH] Daily loss limit reached. No more trades today.")
                return False
            return time.time() >= self._cd_end

    def enter_trade(self, symbol, token, ltp):
        with self._lock:
            if self.state != OrderState.IDLE:
                return False
            entry = self._round(
                ltp - self.cfg["entry_slippage_ticks"] * self.cfg["tick_size"]
            )
            qty = self._qty(entry)
            tp = self._tp(entry, qty)
            sl = self._sl(entry, qty)

            if not (self.cfg["min_stock_price"] <= entry <= self.cfg["max_stock_price"]):
                log.warning("[SKIP] {} price {} out of bounds.".format(symbol, entry))
                return False
            if sl <= 0:
                log.warning("[SKIP] {} SL {} invalid.".format(symbol, sl))
                return False

            log.info(
                "[ENTRY] BUY {}  Qty={}  Entry={:.2f}  TP={:.2f}  SL={:.2f}  "
                "Target=INR{}  Risk=INR{}".format(
                    symbol, qty, entry, tp, sl,
                    self.cfg["target_profit_inr"],
                    self.cfg["stop_loss_inr"],
                )
            )
            ret = self.api.place_order(
                buy_or_sell="B",
                product_type=self.cfg["product_type"],
                exchange=self.cfg["exchange"],
                tradingsymbol=symbol,
                quantity=qty,
                discloseqty=0,
                price_type="LMT",
                price=entry,
                trigger_price=None,
                retention="DAY",
                remarks="HFT_{}".format(symbol),
            )
            if not ret or ret.get("stat") != "Ok":
                log.error("[ENTRY FAILED] {}: {}".format(symbol, ret))
                return False

            oid = ret["norenordno"]
            log.info("[ENTRY PLACED] {} order_id={}".format(symbol, oid))
            self.current = TradeRecord(symbol, token, oid, entry, qty, tp, sl)
            self.state = OrderState.ENTRY_PENDING
            return True

    def check_entry_fill(self):
        with self._lock:
            if self.state != OrderState.ENTRY_PENDING:
                return False
            tr = self.current
            elapsed = time.time() - tr.placed_at
            orders = self.api.get_order_book()
            if orders:
                for o in orders:
                    if o.get("norenordno") != tr.entry_order_id:
                        continue
                    st = o.get("status", "").lower()
                    if "complete" in st or "filled" in st:
                        fill = float(o.get("avgprc") or tr.entry_price)
                        tr.entry_price = fill
                        tr.tp_price = self._tp(fill, tr.qty)
                        tr.sl_price = self._sl(fill, tr.qty)
                        log.info(
                            "[FILLED] {} @ {:.2f}  TP={:.2f}  SL={:.2f}".format(
                                tr.symbol, fill, tr.tp_price, tr.sl_price
                            )
                        )
                        self.state = OrderState.IN_TRADE
                        return True
                    elif "reject" in st or "cancel" in st:
                        log.warning("[REJECTED] Entry for {}".format(tr.symbol))
                        self._reset()
                        return False
            if elapsed > self.cfg["order_fill_timeout_sec"]:
                log.warning(
                    "[TIMEOUT] {} not filled in {:.0f}s. Cancelling.".format(
                        tr.symbol, elapsed
                    )
                )
                self.api.cancel_order(orderno=tr.entry_order_id)
                self._reset()
            return False

    def place_tp_sl(self):
        with self._lock:
            if self.state != OrderState.IN_TRADE or self.current.tp_order_id:
                return False
            tr = self.current

            # TP limit sell
            log.info(
                "[TP] SELL {}  Qty={}  Price={:.2f}".format(tr.symbol, tr.qty, tr.tp_price)
            )
            tp_ret = self.api.place_order(
                buy_or_sell="S",
                product_type=self.cfg["product_type"],
                exchange=self.cfg["exchange"],
                tradingsymbol=tr.symbol,
                quantity=tr.qty,
                discloseqty=0,
                price_type="LMT",
                price=tr.tp_price,
                trigger_price=None,
                retention="DAY",
                remarks="TP_{}".format(tr.symbol),
            )
            if tp_ret and tp_ret.get("stat") == "Ok":
                tr.tp_order_id = tp_ret["norenordno"]
                log.info("[TP PLACED] id={}".format(tr.tp_order_id))
            else:
                log.error("[TP FAILED] {}: {}".format(tr.symbol, tp_ret))

            # SL-LMT sell
            sl_trigger = tr.sl_price
            sl_limit = self._round(tr.sl_price - self.cfg["tick_size"])
            log.info(
                "[SL] SELL {}  Qty={}  Trigger={:.2f}  Limit={:.2f}".format(
                    tr.symbol, tr.qty, sl_trigger, sl_limit
                )
            )
            sl_ret = self.api.place_order(
                buy_or_sell="S",
                product_type=self.cfg["product_type"],
                exchange=self.cfg["exchange"],
                tradingsymbol=tr.symbol,
                quantity=tr.qty,
                discloseqty=0,
                price_type="SL-LMT",
                price=sl_limit,
                trigger_price=sl_trigger,
                retention="DAY",
                remarks="SL_{}".format(tr.symbol),
            )
            if sl_ret and sl_ret.get("stat") == "Ok":
                tr.sl_order_id = sl_ret["norenordno"]
                log.info("[SL PLACED] id={}".format(tr.sl_order_id))
            else:
                log.error("[SL FAILED] {}: {}".format(tr.symbol, sl_ret))
            return True

    def monitor_open_trade(self, ltp=None):
        with self._lock:
            if self.state != OrderState.IN_TRADE:
                return False
            tr = self.current
            orders = self.api.get_order_book()
            if not orders:
                return False
            omap = {o["norenordno"]: o for o in orders if "norenordno" in o}

            # Check TP hit
            if tr.tp_order_id and tr.tp_order_id in omap:
                o = omap[tr.tp_order_id]
                if "complete" in o.get("status", "").lower():
                    fill = float(o.get("avgprc") or tr.tp_price)
                    pnl = (fill - tr.entry_price) * tr.qty
                    self.daily_pnl += pnl
                    log.info(
                        "[TP HIT] {}  Fill={:.2f}  PnL=INR{:.2f}  DailyPnL=INR{:.2f}".format(
                            tr.symbol, fill, pnl, self.daily_pnl
                        )
                    )
                    if tr.sl_order_id:
                        self.api.cancel_order(orderno=tr.sl_order_id)
                    self._end(True)
                    return True

            # Check SL hit
            if tr.sl_order_id and tr.sl_order_id in omap:
                o = omap[tr.sl_order_id]
                if "complete" in o.get("status", "").lower():
                    fill = float(o.get("avgprc") or tr.sl_price)
                    pnl = (fill - tr.entry_price) * tr.qty
                    self.daily_pnl += pnl
                    log.info(
                        "[SL HIT] {}  Fill={:.2f}  PnL=INR{:.2f}  DailyPnL=INR{:.2f}".format(
                            tr.symbol, fill, pnl, self.daily_pnl
                        )
                    )
                    if tr.tp_order_id:
                        self.api.cancel_order(orderno=tr.tp_order_id)
                    self._end(False)
                    return True
            return False

    def emergency_square_off(self):
        with self._lock:
            if self.state not in (OrderState.ENTRY_PENDING, OrderState.IN_TRADE):
                return
            tr = self.current
            log.warning("[SQUARE-OFF] Exiting {}".format(tr.symbol))
            for oid in [tr.tp_order_id, tr.sl_order_id, tr.entry_order_id]:
                if oid:
                    try:
                        self.api.cancel_order(orderno=oid)
                    except Exception:
                        pass
            if self.state == OrderState.IN_TRADE:
                self.api.place_order(
                    buy_or_sell="S",
                    product_type=self.cfg["product_type"],
                    exchange=self.cfg["exchange"],
                    tradingsymbol=tr.symbol,
                    quantity=tr.qty,
                    discloseqty=0,
                    price_type="MKT",
                    price=0,
                    retention="DAY",
                    remarks="SQUAREOFF",
                )
            self._reset()

    def _end(self, won):
        self.trade_count += 1
        log.info(
            "[TRADE #{}] {}  DailyPnL=INR{:.2f}".format(
                self.trade_count, "WIN" if won else "LOSS", self.daily_pnl
            )
        )
        self.current = None
        self._cd_end = time.time() + self.cfg["cooldown_sec"]
        self.state = OrderState.IDLE

    def _reset(self):
        self.current = None
        self._cd_end = time.time() + self.cfg["cooldown_sec"]
        self.state = OrderState.IDLE


# ============================================================
# SYMBOL REGISTRY -- resolve symbol strings to exchange tokens
# ============================================================

class SymbolRegistry:
    def __init__(self, api, exchange):
        self.api = api
        self.exch = exchange
        self._s2t = {}
        self._t2s = {}
        cache = Path("symbol_cache.json")
        if cache.exists():
            d = json.loads(cache.read_text())
            self._s2t = d.get("s2t", {})
            self._t2s = d.get("t2s", {})
            log.info("[REGISTRY] Loaded {} cached symbols.".format(len(self._s2t)))
        self._cache = cache

    def _save(self):
        self._cache.write_text(json.dumps({"s2t": self._s2t, "t2s": self._t2s}, indent=2))

    def resolve_batch(self, symbols, delay=0.12):
        result = {}
        new_cnt = 0
        for sym in symbols:
            tsym = "{}-EQ".format(sym)
            if tsym in self._s2t:
                result[sym] = (tsym, self._s2t[tsym])
                continue
            try:
                ret = self.api.searchscrip(exchange=self.exch, searchtext=sym)
                if ret and ret.get("stat") == "Ok" and ret.get("values"):
                    for v in ret["values"]:
                        if v.get("tsym", "").upper() == tsym.upper():
                            token = v["token"]
                            self._s2t[tsym] = token
                            self._t2s[token] = tsym
                            result[sym] = (tsym, token)
                            new_cnt += 1
                            break
            except Exception as e:
                log.debug("[REGISTRY] searchscrip error {}: {}".format(sym, e))
            time.sleep(delay)
        if new_cnt:
            self._save()
        log.info(
            "[REGISTRY] Resolved {}/{} symbols ({} new).".format(
                len(result), len(symbols), new_cnt
            )
        )
        return result

    def token_to_sym(self, token):
        return self._t2s.get(token)

    def sym_to_token(self, tsym):
        return self._s2t.get(tsym)


# ============================================================
# MARKET HOURS HELPERS
# ============================================================

def _hhmm(s):
    h, m = map(int, s.split(":"))
    return h * 60 + m


def is_market_open(cfg):
    now = datetime.now().hour * 60 + datetime.now().minute
    return _hhmm(cfg["market_open_hhmm"]) <= now < _hhmm(cfg["market_close_hhmm"])


def is_square_off(cfg):
    now = datetime.now().hour * 60 + datetime.now().minute
    return now >= _hhmm(cfg["square_off_hhmm"])


# ============================================================
# MAIN BOT
# ============================================================

class AlgoTrader:
    """
    Orchestrator:
    1. Authenticate with Flattrade.
    2. Resolve Nifty-500 symbols to exchange tokens.
    3. Subscribe WebSocket for all tokens.
    4. Run strategy loop in a dedicated thread.
    5. Handle OS signals for graceful shutdown.
    """

    def __init__(self):
        self.cfg = CFG
        self.api = FlattradeClient()
        self.ticks = TickStore(maxlen=self.cfg["tick_window"])
        self.signals = SignalEngine(self.cfg)
        self.orders = OrderManager(self.api, self.cfg)
        self.registry = None
        self._running = threading.Event()
        self._running.set()
        self._ws_open = threading.Event()
        self._t2base: Dict[str, str] = {}   # token -> base symbol
        self._t2tsym: Dict[str, str] = {}   # token -> trading symbol
        self._n50 = self.cfg["nifty50_token"]

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    def auth(self):
        uid = self.cfg["user_id"]
        tok = self.cfg["api_token"]
        if uid == "YOUR_USER_ID" or tok == "YOUR_SESSION_TOKEN":
            log.error("Set credentials in {} then re-run.".format(CONFIG_FILE))
            sys.exit(1)
        ret = self.api.set_session(userid=uid, password="", usertoken=tok)
        if ret is None:
            log.error("Auth failed -- check user_id and api_token.")
            sys.exit(1)
        log.info("[AUTH] Authenticated as {}".format(uid))

    def resolve_symbols(self):
        self.registry = SymbolRegistry(self.api, self.cfg["exchange"])
        log.info("[INIT] Resolving {} symbols...".format(len(NIFTY500_SYMBOLS)))
        resolved = self.registry.resolve_batch(NIFTY500_SYMBOLS)
        for base, (tsym, token) in resolved.items():
            self._t2base[token] = base
            self._t2tsym[token] = tsym
        self._t2base[self._n50] = "NIFTY50"
        self._t2tsym[self._n50] = "Nifty 50"
        log.info("[INIT] {} symbols ready.".format(len(self._t2base)))

    # -- WebSocket callbacks --

    def _on_open(self):
        log.info("[WS] Connected.")
        self._ws_open.set()
        tokens = list(self._t2base.keys())
        sub_list = ["{}|{}".format(self.cfg["exchange"], t) for t in tokens]
        sub_list.append("{}|{}".format(self.cfg["exchange"], self._n50))
        for i in range(0, len(sub_list), 200):
            batch = sub_list[i:i + 200]
            self.api.subscribe(batch)
            log.info("[WS] Subscribed batch {}: {} tokens".format(i // 200 + 1, len(batch)))

    def _on_quote(self, msg):
        if not msg:
            return
        token = msg.get("tk", "")
        ltp_s = msg.get("lp") or msg.get("ltp") or ""
        vol_s = msg.get("v") or "0"
        if not ltp_s:
            return
        try:
            ltp = float(ltp_s)
            vol = float(vol_s)
        except (ValueError, TypeError):
            return
        if ltp > 0:
            self.ticks.update(token, ltp, vol)

    def _on_order_update(self, msg):
        if msg:
            log.info("[ORDER UPDATE] {}".format(msg))

    def _on_error(self, msg):
        log.error("[WS ERROR] {}".format(msg))

    def _on_close(self):
        log.warning("[WS] Disconnected.")
        self._ws_open.clear()

    # -- Strategy loop --

    def _strategy_loop(self):
        log.info("[STRATEGY] Loop started.")
        while self._running.is_set():
            try:
                self._tick()
            except Exception as e:
                log.exception("[STRATEGY] Error: {}".format(e))
            time.sleep(self.cfg["scan_interval_sec"])

    def _tick(self):
        # Hard square-off time check
        if is_square_off(self.cfg):
            if self.orders.state != OrderState.IDLE:
                log.warning("[STRATEGY] Square-off time. Exiting all positions.")
                self.orders.emergency_square_off()
            return

        # Market closed
        if not is_market_open(self.cfg):
            return

        state = self.orders.state

        # Monitor entry fill
        if state == OrderState.ENTRY_PENDING:
            if self.orders.check_entry_fill():
                self.orders.place_tp_sl()
            return

        # Monitor open trade
        if state == OrderState.IN_TRADE:
            tr = self.orders.current
            ltp = self.ticks.ltp(tr.token) if tr else None
            self.orders.monitor_open_trade(ltp)
            return

        # Scan for new trade
        if state == OrderState.IDLE and self.orders.is_ready():
            self._scan_and_trade()

    def _scan_and_trade(self):
        # Check Nifty-50 trend
        n50_trend = self.signals.trend(self.ticks.prices(self._n50))
        if n50_trend != "UP":
            log.debug("[SCAN] Nifty-50={}. Skipping longs.".format(n50_trend))
            return

        # Score all candidates
        scores = {}
        for token, base_sym in self._t2base.items():
            if token == self._n50:
                continue
            if self.ticks.tick_count(token) < self.cfg["min_ticks_for_signal"]:
                continue
            ltp = self.ticks.ltp(token)
            if ltp is None or ltp < self.cfg["min_stock_price"]:
                continue
            sc = self.signals.score(
                self.ticks.prices(token),
                self.ticks.volumes(token),
            )
            if sc > 0.01:
                scores[token] = sc

        if not scores:
            log.debug("[SCAN] No qualifying candidates.")
            return

        best_token = max(scores, key=scores.__getitem__)
        best_score = scores[best_token]
        best_ltp = self.ticks.ltp(best_token)
        best_tsym = self._t2tsym.get(best_token, "UNKNOWN")

        log.info(
            "[CANDIDATE] {}  Score={:.4f}  LTP={:.2f}  N50={}".format(
                best_tsym, best_score, best_ltp, n50_trend
            )
        )

        # Stock trend must also be UP (dual trend filter)
        stock_trend = self.signals.trend(self.ticks.prices(best_token))
        if stock_trend != "UP":
            log.info(
                "[SKIP] {} stock_trend={} != Nifty trend.".format(best_tsym, stock_trend)
            )
            return

        self.orders.enter_trade(symbol=best_tsym, token=best_token, ltp=best_ltp)

    def run(self):
        log.info("=" * 70)
        log.info("  FLATTRADE SEMI-HFT ALGO TRADER")
        log.info(
            "  Capital=INR{:,}  Target=INR{}  SL=INR{}  MaxDailyLoss=INR{}".format(
                self.cfg["capital"],
                self.cfg["target_profit_inr"],
                self.cfg["stop_loss_inr"],
                self.cfg["max_daily_loss_inr"],
            )
        )
        log.info("=" * 70)
        self.auth()
        self.resolve_symbols()

        limits = self.api.get_limits()
        if limits:
            cash = limits.get("cash") or limits.get("net") or "N/A"
            log.info("[ACCOUNT] Available margin: INR {}".format(cash))

        strat = threading.Thread(
            target=self._strategy_loop, name="StrategyLoop", daemon=True
        )
        strat.start()

        log.info("[WS] Connecting...")
        self.api.start_websocket(
            order_update_callback=self._on_order_update,
            subscribe_callback=self._on_quote,
            socket_open_callback=self._on_open,
            socket_error_callback=self._on_error,
            socket_close_callback=self._on_close,
        )
        strat.join()

    def _shutdown(self, signum, frame):
        log.warning("[SHUTDOWN] Signal {}.".format(signum))
        self._running.clear()
        if self.orders.state in (OrderState.ENTRY_PENDING, OrderState.IN_TRADE):
            self.orders.emergency_square_off()
        log.info(
            "[SUMMARY] Trades={}  DailyPnL=INR{:.2f}".format(
                self.orders.trade_count, self.orders.daily_pnl
            )
        )
        sys.exit(0)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    AlgoTrader().run()
