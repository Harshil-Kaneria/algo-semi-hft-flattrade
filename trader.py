#!/usr/bin/env python3
"""
=============================================================================
FLATTRADE SEMI-HFT ALGO TRADER  (v2)
Capital  : INR 1,00,000
Target   : INR 180 net profit / trade (after all transaction costs)
Stop-Loss: INR 100
Universe : Nifty 500 (NSE equities) — fetched live from NSE, hardcoded fallback
Trend    : Long-only when BOTH stock AND Nifty-50 trend UP (dual filter)

What's new in v2
-----------------
 1. product_type default fixed to "M" (MIS intraday); startup validation
 2. Pullback/support entry: buy at local_low+1tick within a short lookback window
    while the wider trend_lookback window still shows UP
 3. Side-specific transaction cost model; TP inflated to clear INR 180 NET
 4. Event-driven order fills via WS order_update_callback; REST poll fallback
 5. WS exponential-backoff reconnect; os._exit(1) on exhaustion with open pos
 6. Partial fill tracking; TP/SL placed only after full entry fill
 7. Daily trade CSV (trades_YYYYMMDD.csv) with gross + net PnL per row
 8. Session state persistence (session_state.json); IST timezone throughout
 9. Nifty-500 universe fetched live from NSE archives CSV at startup
10. dry_run flag: simulates all order logic against live ticks, no real orders
11. Background candidate board (CandidateBoard): continuously updated top-N leaderboard of trade candidates; fast-fire entry with live re-validation
12. Leverage-aware position sizing (MarginCache): queries get_security_info margin components to scale position sizes based on scrip leverage

Credentials
-----------
    Copy .env.example → .env  and fill in FLATTRADE_USER_ID and FLATTRADE_API_TOKEN.
    Run  python get_token.py  every morning to refresh the daily token.
    The .env file is git-ignored and NEVER committed.

Usage
-----
    pip install -r requirements.txt
    python get_token.py      # refresh daily session token
    python trader.py         # start the bot

Config is read from trader_config.json (auto-created on first run).
Credentials are read from .env (overrides any values in trader_config.json).
=============================================================================
"""

import csv
import io
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional, Tuple

# ── zoneinfo (Python 3.9+ stdlib; 3.8 needs backports.zoneinfo) ──────────────
try:
    from zoneinfo import ZoneInfo
except ImportError:
    try:
        from backports.zoneinfo import ZoneInfo  # type: ignore
    except ImportError:
        ZoneInfo = None  # type: ignore
        # Deferred fatal — logged after the logging system is initialised (see below).

# ── third-party ───────────────────────────────────────────────────────────────
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

# ── .env loader (python-dotenv preferred; manual fallback) ────────────────────
def _load_env() -> None:
    """
    Load .env into os.environ so credentials never have to live in trader_config.json.
    Uses python-dotenv when available; falls back to a simple manual parser.
    """
    env_file = Path(__file__).parent / ".env"
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=env_file, override=True)
    except ImportError:
        # Manual fallback — no external dependency required
        if env_file.exists():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

_load_env()


# ============================================================
# TIMEZONE
# ============================================================

IST: Optional[object] = ZoneInfo("Asia/Kolkata") if ZoneInfo is not None else None


def _assert_zoneinfo() -> None:
    """Call once after logging is configured.  Exits if IST timezone is unavailable."""
    if IST is None:
        # logging.basicConfig has already run by this point.
        logging.getLogger("HFT-TRADER").error(
            "[FATAL] Neither 'zoneinfo' nor 'backports.zoneinfo' is available. "
            "IST timezone cannot be determined — market-hours checks, square-off "
            "timing, and the daily kill-switch reset will ALL be broken. "
            "Install backports.zoneinfo:  pip install backports.zoneinfo"
        )
        sys.exit(1)


def _now_ist() -> datetime:
    """Current datetime in IST (naive local fallback if zoneinfo missing)."""
    return datetime.now(IST) if IST else datetime.now()  # type: ignore[arg-type]


def _today_ist() -> date:
    """Current date in IST — use datetime.now(IST).date(), NOT date.today(IST)."""
    return _now_ist().date()


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
            str(Path(__file__).parent / "trader_{}.log".format(_today_ist().strftime("%Y%m%d"))),
            mode="a",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("HFT-TRADER")

# Guard: exit early with a clear message if the IST timezone is unavailable.
_assert_zoneinfo()


# ============================================================
# CONFIG
# ============================================================

VALID_PRODUCT_TYPES = {"C", "M", "H", "B"}

CONFIG_FILE = Path(__file__).parent / "trader_config.json"



def validate_config(cfg: Dict) -> None:
    """Fail fast on clearly invalid config before any API calls."""
    pt = cfg.get("product_type", "")
    if pt not in VALID_PRODUCT_TYPES:
        log.error(
            "[CONFIG] product_type='{}' is invalid. Valid values: {}".format(
                pt, sorted(VALID_PRODUCT_TYPES)
            )
        )
        sys.exit(1)
    tw = cfg.get("tick_window", 0)
    tl = cfg.get("trend_lookback", 0)
    if tw < tl:
        log.error(
            "[CONFIG] tick_window ({}) must be >= trend_lookback ({}).".format(tw, tl)
        )
        sys.exit(1)


def load_config() -> Dict:
    """
    Load configuration exclusively from trader_config.json.
    Credentials (user_id, api_token, api_key) are injected on top via
    environment variables loaded from .env by _load_env() at import time.
    """
    if not CONFIG_FILE.exists():
        log.error(
            "[CONFIG] trader_config.json not found at {}. "
            "Create it from the repo template and re-run.".format(CONFIG_FILE)
        )
        sys.exit(1)
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("[CONFIG] trader_config.json is not valid JSON: {}".format(exc))
        sys.exit(1)

    # ── Sentinel defaults (never reach the broker; catch missing credentials) ──
    # If neither the .env nor an env-var provides these, the sentinel strings
    # will cause the login call to fail with a clear error rather than sending
    # an empty/null credential to the API.
    cfg.setdefault("user_id", "YOUR_USER_ID")
    cfg.setdefault("api_token", "YOUR_SESSION_TOKEN")
    cfg.setdefault("board_size", 5)
    cfg.setdefault("board_log_interval_sec", 10)
    cfg.setdefault("margin_refresh_sec", 3600)
    cfg.setdefault("fallback_margin_pct", 20.0)

    # ── Overlay credentials from environment variables ─────────────────────────
    # Credentials live in .env ONLY — never in trader_config.json.
    # Flattrade auth:
    #   FLATTRADE_USER_ID   → broker login ID
    #   FLATTRADE_API_KEY   → Pi app_key (wall.flattrade.in Pi portal)
    #   FLATTRADE_API_TOKEN → daily susertoken from redirect code (get_token.py)
    env_map = {
        "FLATTRADE_USER_ID":   "user_id",
        "FLATTRADE_API_TOKEN": "api_token",
        "FLATTRADE_API_KEY":   "api_key",
        "FLATTRADE_API_SECRET":"api_secret",
    }
    for env_var, cfg_key in env_map.items():
        val = os.environ.get(env_var, "").strip()
        if val:
            cfg[cfg_key] = val

    validate_config(cfg)
    return cfg


CFG = load_config()


# ============================================================
# NIFTY 500 UNIVERSE  (item 9)
# ============================================================

NIFTY500_SYMBOLS_FALLBACK = [
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

_NSE_CSV_URL = "https://archives.nseindia.com/content/indices/ind_nifty500list.csv"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
}


def fetch_nifty500_symbols():
    """
    Fetch current Nifty-500 constituent list from NSE archives CSV.
    Falls back to hardcoded list on any failure.
    Returns a list of base symbol strings (e.g. 'RELIANCE', 'TCS').
    """
    req = urllib.request.Request(_NSE_CSV_URL, headers=_NSE_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                log.warning(
                    "[UNIVERSE] NSE CSV returned HTTP {}, falling back to hardcoded list.".format(
                        resp.status
                    )
                )
                return None
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log.warning(
            "[UNIVERSE] NSE CSV fetch failed: {}. Falling back to hardcoded list.".format(exc)
        )
        return None

    symbols = []
    try:
        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            sym = (row.get("Symbol") or "").strip()
            if sym:
                symbols.append(sym)
    except Exception as exc:
        log.warning("[UNIVERSE] NSE CSV parse error: {}. Using hardcoded list.".format(exc))
        return None

    if not symbols:
        log.warning("[UNIVERSE] NSE CSV yielded 0 symbols, using hardcoded list.")
        return None

    log.info("[UNIVERSE] Source: NSE-CSV ({} symbols)".format(len(symbols)))
    return symbols


def get_universe() -> list:
    syms = fetch_nifty500_symbols()
    if syms:
        return syms
    log.info("[UNIVERSE] Source: hardcoded ({} symbols)".format(len(NIFTY500_SYMBOLS_FALLBACK)))
    return list(NIFTY500_SYMBOLS_FALLBACK)


# ============================================================
# FLATTRADE API CLIENT
# ============================================================

class FlattradeClient(NorenApi):
    """NorenApi subclass with Flattrade-specific endpoints."""

    def __init__(self):
        super().__init__(
            host="https://piconnect.flattrade.in/PiConnectAPI/",
            websocket="wss://piconnect.flattrade.in/PiConnectWSAPI/",
        )


# ============================================================
# TICK STORE
# ============================================================

class TickStore:
    """Thread-safe rolling price + volume buffer. WS thread writes; strategy thread reads."""

    def __init__(self, maxlen: int):
        self._prices: Dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._volumes: Dict[str, deque] = defaultdict(lambda: deque(maxlen=maxlen))
        self._ltp: Dict[str, float] = {}
        self._lock = threading.RLock()

    def update(self, token: str, ltp: float, volume: float = 0.0) -> None:
        with self._lock:
            self._prices[token].append(ltp)
            self._volumes[token].append(volume)
            self._ltp[token] = ltp

    def prices(self, token: str) -> list:
        with self._lock:
            return list(self._prices[token])

    def volumes(self, token: str) -> list:
        with self._lock:
            return list(self._volumes[token])

    def ltp(self, token: str) -> Optional[float]:
        with self._lock:
            return self._ltp.get(token)

    def tick_count(self, token: str) -> int:
        with self._lock:
            return len(self._prices[token])


# ============================================================
# CANDIDATE BOARD  (item 11)
# ============================================================

class CandidateBoard:
    """
    Thread-safe leaderboard holding top N trade candidates ranked by score.
    Updated continuously on every strategy tick in the background.
    """

    def __init__(self, board_size: int = 5):
        self.board_size = board_size
        self._board: list = []
        self._lock = threading.Lock()
        self._last_log_time = 0.0

    def update(self, candidates: list) -> None:
        """
        candidates: list of dicts with {symbol, token, score, entry_price}
        Sorts by score descending, keeps top board_size, assigns rank 1..N.
        """
        now = time.time()
        sorted_cand = sorted(candidates, key=lambda c: c["score"], reverse=True)[: self.board_size]
        new_board = []
        for idx, c in enumerate(sorted_cand, 1):
            new_board.append({
                "rank": idx,
                "symbol": c["symbol"],
                "token": c["token"],
                "score": float(c["score"]),
                "entry_price": float(c["entry_price"]),
                "updated_at": now,
            })
        with self._lock:
            self._board = new_board

    def get_board(self) -> list:
        with self._lock:
            return list(self._board)

    def log_board_if_due(self, interval_sec: float) -> None:
        now = time.time()
        with self._lock:
            if now - self._last_log_time < interval_sec:
                return
            self._last_log_time = now
            board_copy = list(self._board)

        if not board_copy:
            return

        parts = [
            "#{} {} score={:.3f} entry={:.2f}".format(
                item["rank"], item["symbol"], item["score"], item["entry_price"]
            )
            for item in board_copy
        ]
        log.info("[BOARD] {}".format(" | ".join(parts)))


# ============================================================
# MARGIN CACHE  (item 12)
# ============================================================

class MarginCache:
    """
    Fetches and caches security info (margin components) per scrip from Flattrade API.

    NOTE ON LEVERAGE FORMULA:
    Flattrade/Noren API returns margin component fields (varmrg, elmmrg, expmrg, etc.)
    with no official documented formula for combining them. This implementation uses
    margin_pct = max(fallback_margin_pct, varmrg + elmmrg + expmrg) as an APPROXIMATION.
    Users should validate this against a manual MIS order in Flattrade before scaling.
    """

    def __init__(self, api: FlattradeClient, cfg: Dict):
        self.api = api
        self.exchange = cfg.get("exchange", "NSE")
        self.refresh_sec = cfg.get("margin_refresh_sec", 3600)
        self.fallback_pct = float(cfg.get("fallback_margin_pct", 20.0))
        self._cache: Dict[str, Tuple[float, float]] = {}  # token -> (margin_pct, timestamp)
        self._lock = threading.Lock()

    def get_margin_pct(self, token: str) -> float:
        now = time.time()
        with self._lock:
            if token in self._cache:
                m_pct, cached_at = self._cache[token]
                if now - cached_at < self.refresh_sec:
                    return m_pct

        m_pct = self._fetch_margin_pct(token)

        with self._lock:
            self._cache[token] = (m_pct, now)
        return m_pct

    def _fetch_margin_pct(self, token: str) -> float:
        try:
            res = self.api.get_security_info(exchange=self.exchange, token=token)
            if res and isinstance(res, dict) and res.get("stat") == "Ok":
                var_mrg = float(res.get("varmrg") or 0.0)
                elm_mrg = float(res.get("elmmrg") or 0.0)
                exp_mrg = float(res.get("expmrg") or 0.0)
                sum_mrg = var_mrg + elm_mrg + exp_mrg
                return max(self.fallback_pct, sum_mrg)
        except Exception as exc:
            log.warning(
                "[MARGIN] get_security_info failed for token {}: {}. "
                "Using fallback margin_pct={:.1f}%.".format(token, exc, self.fallback_pct)
            )
        return self.fallback_pct


# ============================================================
# SIGNAL ENGINE  (items 2 + original scoring)
# ============================================================

class SignalEngine:
    """
    4-component composite momentum score in [0, 1].

    Components:
      1. Linreg slope (normalised)        — sustained directional move   (40%)
      2. R-squared                        — clean vs noisy move           (25%)
      3. Velocity (late/early mean ratio) — acceleration                  (20%)
      4. Volume surge ratio               — volume confirmation           (15%)

    Trend uses `trend_lookback` window (wide view).
    Pullback uses `pullback_lookback` window (tight short-term dip detection).
    The two windows are intentionally independent.
    """

    def __init__(self, cfg: Dict):
        self.w_slope = cfg["momentum_linreg_weight"]
        self.w_r2 = cfg["consistency_weight"]
        self.w_vel = cfg["velocity_weight"]
        self.w_vol = cfg["volume_surge_weight"]
        self.min_ticks = cfg["min_ticks_for_signal"]
        self.trend_lookback = cfg["trend_lookback"]
        self.pullback_lookback = cfg["pullback_lookback"]
        self.pullback_pct_min = cfg["pullback_pct_min"]

    def score(self, prices: list, volumes: list) -> float:
        n = len(prices)
        if n < self.min_ticks:
            return 0.0
        arr = np.array(prices, dtype=np.float64)
        x = np.arange(n, dtype=np.float64)
        mean = arr.mean()
        if mean <= 0:
            return 0.0

        slope, intercept = np.polyfit(x, arr, 1)
        slope_score = float(np.clip(slope / mean * 5000, 0, 1))

        fitted = slope * x + intercept
        ss_res = float(np.sum((arr - fitted) ** 2))
        ss_tot = float(np.sum((arr - mean) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-10 else 0.0
        r2_score = float(np.clip(r2, 0, 1))

        q = max(1, n // 4)
        early = float(arr[:q].mean())
        late = float(arr[-q:].mean())
        vel_score = float(np.clip((late - early) / (early + 1e-9) * 500, 0, 1))

        if len(volumes) >= self.min_ticks:
            va = np.array(volumes, dtype=np.float64)
            vm = va[:-1].mean() if len(va) > 1 and va[:-1].sum() > 0 else 1.0
            vol_score = float(np.clip((va[-1] / (vm + 1e-9) - 1.0) / 4.0, 0, 1))
        else:
            vol_score = 0.0

        return float(np.clip(
            self.w_slope * slope_score
            + self.w_r2 * r2_score
            + self.w_vel * vel_score
            + self.w_vol * vol_score,
            0.0, 1.0,
        ))

    def trend(self, prices: list) -> str:
        """UP / DOWN / FLAT from linreg slope over the last `trend_lookback` ticks."""
        n = len(prices)
        if n < 5:
            return "FLAT"
        arr = np.array(prices[-self.trend_lookback:], dtype=np.float64)
        x = np.arange(len(arr), dtype=np.float64)
        slope, _ = np.polyfit(x, arr, 1)
        ns = slope / (arr.mean() + 1e-9)
        if ns > 0.000015:
            return "UP"
        if ns < -0.000015:
            return "DOWN"
        return "FLAT"

    def pullback_entry(self, prices: list, tick_size: float) -> Tuple[bool, Optional[float]]:
        """
        Detect a short-term pullback within an uptrend using a tight window.

        Uses `prices[-pullback_lookback:]` (e.g. last 6 ticks).
        Qualifies if (local_high - ltp) / local_high * 100 >= pullback_pct_min.
        Entry price = local_low + 1 tick (improves fill probability vs exact local_low).

        Returns (True, entry_price) or (False, None).
        """
        n = len(prices)
        if n < self.pullback_lookback:
            return False, None
        window = prices[-self.pullback_lookback:]
        local_high = max(window)
        local_low = min(window)
        ltp = prices[-1]
        if local_high <= 0:
            return False, None
        pullback_pct = (local_high - ltp) / local_high * 100.0
        if pullback_pct >= self.pullback_pct_min:
            raw_entry = local_low + tick_size
            entry = round(round(raw_entry / tick_size) * tick_size, 2)
            return True, entry
        return False, None


# ============================================================
# ORDER STATE MACHINE
# ============================================================

class OrderState:
    IDLE = "IDLE"
    ENTRY_PENDING = "ENTRY_PENDING"
    IN_TRADE = "IN_TRADE"


class TradeRecord:
    """Mutable state for a single open trade."""

    def __init__(
        self,
        symbol: str,
        token: str,
        entry_order_id: str,
        entry_price: float,
        qty: int,
        tp_price: float,
        sl_price: float,
    ):
        self.symbol = symbol
        self.token = token
        self.entry_order_id = entry_order_id
        self.entry_price = entry_price      # limit price placed (or simulated)
        self.qty = qty                      # ordered qty
        self.filled_qty = 0                 # entry shares filled so far (item 6)
        self.avg_fill_price = 0.0           # running VWAP of entry fills
        self.tp_price = tp_price
        self.sl_price = sl_price
        self.tp_order_id: Optional[str] = None
        self.sl_order_id: Optional[str] = None
        self.exit_filled_qty = 0            # exit shares filled (item 6)
        self.placed_at = time.time()
        self.filled_at: Optional[float] = None   # epoch when fully filled
        self.exit_time: Optional[float] = None


# ============================================================
# TRADE LEDGER  (item 7)
# ============================================================

class TradeLedger:
    """Appends one CSV row per completed trade to trades_YYYYMMDD.csv."""

    FIELDS = [
        "date", "symbol", "entry_px", "exit_px", "qty",
        "gross_pnl", "cost_buy", "cost_sell", "net_pnl",
        "reason", "entry_time", "exit_time", "dry_run",
    ]

    def __init__(self):
        path = Path(__file__).parent / "trades_{}.csv".format(_today_ist().strftime("%Y%m%d"))
        self._path = path
        self._lock = threading.Lock()
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, row: Dict) -> None:
        with self._lock:
            with open(self._path, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore").writerow(row)


# ============================================================
# SESSION STATE  (item 8)
# ============================================================

class SessionState:
    """Persists daily_pnl and trade_count across process restarts (same IST date)."""

    _PATH = Path(__file__).parent / "session_state.json"

    def load(self) -> Tuple[float, int]:
        """Return (daily_pnl, trade_count) for today, else (0.0, 0)."""
        if not self._PATH.exists():
            return 0.0, 0
        try:
            data = json.loads(self._PATH.read_text(encoding="utf-8"))
            if data.get("date") == _today_ist().isoformat():
                return float(data.get("daily_pnl", 0.0)), int(data.get("trade_count", 0))
        except Exception as exc:
            log.warning("[SESSION] Could not load state: {}".format(exc))
        return 0.0, 0

    def save(self, daily_pnl: float, trade_count: int) -> None:
        """Atomically persist session state."""
        data = {
            "date": _today_ist().isoformat(),
            "daily_pnl": daily_pnl,
            "trade_count": trade_count,
        }
        tmp = self._PATH.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(str(tmp), str(self._PATH))
        except Exception as exc:
            log.warning("[SESSION] Could not save state: {}".format(exc))


# ============================================================
# ORDER MANAGER
# ============================================================

_DRY_SENTINEL = {"DRY_ENTRY", "DRY_TP", "DRY_SL"}


class OrderManager:
    """
    Full trade lifecycle:
      entry -> (partial fills) -> full fill -> TP+SL placed -> exit -> cooldown

    Fills are detected primarily via WS (on_ws_order_event).
    REST polling via monitor_open_trade / check_entry_fill is used as a fallback
    only when the last WS event is older than reconcile_interval_sec.
    """

    def __init__(
        self,
        api: FlattradeClient,
        cfg: Dict,
        ledger: TradeLedger,
        session: SessionState,
        tick_store: "TickStore",
        margin_cache: Optional["MarginCache"] = None,
    ):
        self.api = api
        self.cfg = cfg
        self.ledger = ledger
        self.session = session
        self.tick_store = tick_store
        self.margin_cache = margin_cache
        self._lock = threading.Lock()
        self.state = OrderState.IDLE
        self.current: Optional[TradeRecord] = None
        self.daily_pnl = 0.0
        self.trade_count = 0
        self._cd_end = 0.0
        self._last_ws_event = 0.0      # epoch of most recent WS order update

    def restore_session(self) -> None:
        pnl, count = self.session.load()
        self.daily_pnl = pnl
        self.trade_count = count
        if count > 0 or pnl != 0.0:
            log.info(
                "[SESSION] Restored: DailyPnL=INR{:.2f}  Trades={}".format(pnl, count)
            )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _round(self, p: float) -> float:
        ts = self.cfg["tick_size"]
        return round(round(p / ts) * ts, 2)

    def _qty(self, price: float, margin_pct: float = 20.0) -> int:
        """
        Compute leverage-adjusted order quantity.
        per_share_cost = price * (margin_pct / 100.0)
        """
        if price <= 0 or margin_pct <= 0:
            return 1
        per_share_cost = price * (margin_pct / 100.0)
        return max(1, int(self.cfg["capital"] * self.cfg["max_trade_value_pct"] // per_share_cost))

    def _cost_buy(self, price: float, qty: int) -> float:
        """INR transaction costs on the BUY side."""
        c = self.cfg
        brok = c["brokerage_flat_inr"]
        exch = c["exchange_txn_pct"] / 100.0 * price * qty
        sebi = c["sebi_pct"] / 100.0 * price * qty
        stamp = c["stamp_duty_buy_pct"] / 100.0 * price * qty
        gst = c["gst_pct"] / 100.0 * (brok + exch)
        return brok + exch + sebi + stamp + gst

    def _cost_sell(self, price: float, qty: int) -> float:
        """INR transaction costs on the SELL side (STT only here, no stamp duty)."""
        c = self.cfg
        brok = c["brokerage_flat_inr"]
        exch = c["exchange_txn_pct"] / 100.0 * price * qty
        sebi = c["sebi_pct"] / 100.0 * price * qty
        stt = c["stt_sell_pct"] / 100.0 * price * qty
        gst = c["gst_pct"] / 100.0 * (brok + exch)
        return brok + exch + sebi + stt + gst

    def _tp(self, entry: float, qty: int) -> float:
        """
        Compute TP price such that NET profit after all round-trip costs equals
        target_profit_inr.

        Cost breakdown:
          buy_cost = fixed + variable on entry*qty  (closed form)
          sell_cost(tp) = fixed_sell + k_sell * tp * qty  (linear in tp)

        Rearranging:
          (tp - entry)*qty - buy_cost - fixed_sell - k_sell*tp*qty = target
          tp * qty * (1 - k_sell) = target + entry*qty + buy_cost + fixed_sell
          tp = numerator / (qty * (1 - k_sell))
        """
        c = self.cfg
        target = c["target_profit_inr"]
        buy_cost = self._cost_buy(entry, qty)
        brok = c["brokerage_flat_inr"]
        gst_r = c["gst_pct"] / 100.0
        # variable rate coefficient on sell turnover
        k_sell = (
            c["exchange_txn_pct"] / 100.0
            + c["sebi_pct"] / 100.0
            + c["stt_sell_pct"] / 100.0
            + c["exchange_txn_pct"] / 100.0 * gst_r   # GST on exchange portion
        )
        fixed_sell = brok * (1.0 + gst_r)
        denom = qty * (1.0 - k_sell)
        if denom <= 0:
            # safety fallback (shouldn't happen with realistic values)
            return self._round(entry + (target + buy_cost + fixed_sell) / qty)
        return self._round((target + entry * qty + buy_cost + fixed_sell) / denom)

    def _sl(self, entry: float, qty: int) -> float:
        return self._round(entry - self.cfg["stop_loss_inr"] / qty)

    # ── state queries ─────────────────────────────────────────────────────────

    def is_ready(self) -> bool:
        with self._lock:
            if self.state != OrderState.IDLE:
                return False
            if self.daily_pnl <= -self.cfg["max_daily_loss_inr"]:
                log.warning("[KILL SWITCH] Daily loss limit reached. No more trades today.")
                return False
            return time.time() >= self._cd_end

    # ── entry ─────────────────────────────────────────────────────────────────

    def enter_trade(self, symbol: str, token: str, entry_px: float, margin_pct: Optional[float] = None) -> bool:
        """
        Place a limit BUY at entry_px (= local_low + 1 tick from pullback detector).
        In dry-run: log the would-be order and simulate success.
        """
        with self._lock:
            if self.state != OrderState.IDLE:
                return False

            if margin_pct is None:
                if self.margin_cache:
                    margin_pct = self.margin_cache.get_margin_pct(token)
                else:
                    margin_pct = float(self.cfg.get("fallback_margin_pct", 20.0))

            entry = self._round(entry_px)
            qty = self._qty(entry, margin_pct)
            tp = self._tp(entry, qty)
            sl = self._sl(entry, qty)
            leverage = 100.0 / margin_pct if margin_pct > 0 else 1.0

            if not (self.cfg["min_stock_price"] <= entry <= self.cfg["max_stock_price"]):
                log.warning("[SKIP] {} price {:.2f} out of bounds.".format(symbol, entry))
                return False
            if sl <= 0:
                log.warning("[SKIP] {} SL {:.2f} invalid.".format(symbol, sl))
                return False

            cost_b = self._cost_buy(entry, qty)
            cost_s = self._cost_sell(tp, qty)

            if self.cfg.get("dry_run"):
                log.info(
                    "[DRY-RUN] WOULD BUY {}  Qty={} (margin={:.1f}%, ~{:.1f}x leverage)  "
                    "Entry={:.2f}  TP={:.2f}  SL={:.2f}  "
                    "CostBuy=INR{:.2f}  CostSell=INR{:.2f}  NetTarget=INR{}".format(
                        symbol, qty, margin_pct, leverage, entry, tp, sl,
                        cost_b, cost_s, self.cfg["target_profit_inr"],
                    )
                )
                self.current = TradeRecord(symbol, token, "DRY_ENTRY", entry, qty, tp, sl)
                self.state = OrderState.ENTRY_PENDING
                return True

            log.info(
                "[ENTRY] BUY {}  Qty={} (margin={:.1f}%, ~{:.1f}x leverage)  "
                "Entry={:.2f}  TP={:.2f}  SL={:.2f}  "
                "CostBuy=INR{:.2f}  CostSell=INR{:.2f}  NetTarget=INR{}".format(
                    symbol, qty, margin_pct, leverage, entry, tp, sl,
                    cost_b, cost_s, self.cfg["target_profit_inr"],
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

    # ── fill detection (REST fallback path)  ─────────────────────────────────

    def check_entry_fill(self) -> bool:
        """
        Called each strategy tick while ENTRY_PENDING.
        Primary path: WS event via on_ws_order_event() sets state to IN_TRADE.
        This method handles: dry-run sim, REST reconcile fallback, timeout cancel.
        Returns True when fully filled and transitions to IN_TRADE.
        """
        with self._lock:
            if self.state != OrderState.ENTRY_PENDING:
                return False
            tr = self.current
            elapsed = time.time() - tr.placed_at

            # ── DRY-RUN: simulate fill when market touches entry price ────────
            if self.cfg.get("dry_run"):
                ltp = self.tick_store.ltp(tr.token)
                if ltp is not None and ltp <= tr.entry_price:
                    tr.filled_qty = tr.qty
                    tr.avg_fill_price = ltp
                    tr.entry_price = ltp
                    tr.filled_at = time.time()
                    tr.tp_price = self._tp(ltp, tr.qty)
                    tr.sl_price = self._sl(ltp, tr.qty)
                    log.info(
                        "[DRY-RUN] ENTRY SIMULATED FILLED {} @ {:.2f}  "
                        "TP={:.2f}  SL={:.2f}".format(
                            tr.symbol, ltp, tr.tp_price, tr.sl_price
                        )
                    )
                    self.state = OrderState.IN_TRADE
                    return True
                # Timeout still applies in dry-run
                if elapsed > self.cfg["order_fill_timeout_sec"]:
                    log.warning(
                        "[DRY-RUN] {} not sim-filled in {:.0f}s (LTP never reached {:.2f}). "
                        "Resetting.".format(tr.symbol, elapsed, tr.entry_price)
                    )
                    self._reset()
                return False

            # ── LIVE: skip REST if WS event is fresh ─────────────────────────
            reconcile_sec = self.cfg.get("reconcile_interval_sec", 10)
            if time.time() - self._last_ws_event < reconcile_sec:
                # WS is fresh; on_ws_order_event() will handle the transition
                if elapsed > self.cfg["order_fill_timeout_sec"]:
                    log.warning(
                        "[TIMEOUT] {} not filled in {:.0f}s. Cancelling.".format(
                            tr.symbol, elapsed
                        )
                    )
                    self.api.cancel_order(orderno=tr.entry_order_id)
                    self._reset()
                return False

            # ── LIVE REST reconcile fallback ──────────────────────────────────
            orders = self.api.get_order_book()
            if orders:
                for o in orders:
                    if o.get("norenordno") != tr.entry_order_id:
                        continue
                    st = o.get("status", "").lower()
                    fill_shares = int(o.get("fillshares") or 0)
                    avg_prc = float(o.get("avgprc") or tr.entry_price)

                    if fill_shares > 0:
                        tr.filled_qty = fill_shares
                        tr.avg_fill_price = avg_prc

                    if ("complete" in st) or (fill_shares >= tr.qty > 0):
                        tr.filled_qty = tr.qty
                        tr.avg_fill_price = avg_prc
                        tr.entry_price = avg_prc
                        tr.filled_at = time.time()
                        tr.tp_price = self._tp(avg_prc, tr.qty)
                        tr.sl_price = self._sl(avg_prc, tr.qty)
                        log.info(
                            "[REST FILLED] {} @ {:.2f}  TP={:.2f}  SL={:.2f}".format(
                                tr.symbol, avg_prc, tr.tp_price, tr.sl_price
                            )
                        )
                        self.state = OrderState.IN_TRADE
                        return True
                    elif 0 < fill_shares < tr.qty:
                        log.warning(
                            "[PARTIAL FILL] {} {}/{} shares @ {:.2f}. Waiting.".format(
                                tr.symbol, fill_shares, tr.qty, avg_prc
                            )
                        )
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

    # ── TP/SL placement ───────────────────────────────────────────────────────

    def place_tp_sl(self) -> bool:
        with self._lock:
            if self.state != OrderState.IN_TRADE or (
                self.current and self.current.tp_order_id is not None
            ):
                return False
            tr = self.current

            if self.cfg.get("dry_run"):
                log.info(
                    "[DRY-RUN] WOULD SELL TP {}  Qty={}  Price={:.2f}".format(
                        tr.symbol, tr.qty, tr.tp_price
                    )
                )
                log.info(
                    "[DRY-RUN] WOULD SELL SL {}  Qty={}  Trigger={:.2f}  Limit={:.2f}".format(
                        tr.symbol, tr.qty, tr.sl_price,
                        self._round(tr.sl_price - self.cfg["tick_size"]),
                    )
                )
                tr.tp_order_id = "DRY_TP"
                tr.sl_order_id = "DRY_SL"
                return True

            # Live TP
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

            # Live SL
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

    # ── WS event handler (primary fill detection path)  ──────────────────────

    def on_ws_order_event(self, msg: Dict) -> None:
        """
        Called from _on_order_update (WS thread).
        Handles entry fills, TP/SL fills, rejects, partial fills.
        Acquires the trade lock — must NOT call any method that re-acquires it.
        """
        if not msg:
            return
        # Field names vary across Flattrade WS versions; try both cases
        oid = msg.get("norenordno") or msg.get("NOrdNo") or ""
        status = (msg.get("status") or msg.get("Status") or "").lower()
        fill_s = msg.get("fillshares") or msg.get("Fillshs") or "0"
        avgprc_s = msg.get("avgprc") or msg.get("Avgprc") or "0"
        try:
            fill_shares = int(fill_s)
        except (ValueError, TypeError):
            fill_shares = 0
        try:
            avg_prc = float(avgprc_s)
        except (ValueError, TypeError):
            avg_prc = 0.0

        with self._lock:
            self._last_ws_event = time.time()
            if not self.current or not oid:
                return
            tr = self.current

            # ── Entry order ───────────────────────────────────────────────────
            if oid == tr.entry_order_id and self.state == OrderState.ENTRY_PENDING:
                if fill_shares > 0:
                    tr.filled_qty = fill_shares
                    tr.avg_fill_price = avg_prc
                if ("complete" in status) or (fill_shares >= tr.qty > 0):
                    tr.filled_qty = tr.qty
                    tr.avg_fill_price = avg_prc
                    tr.entry_price = avg_prc
                    tr.filled_at = time.time()
                    tr.tp_price = self._tp(avg_prc, tr.qty)
                    tr.sl_price = self._sl(avg_prc, tr.qty)
                    log.info(
                        "[WS FILLED] {} @ {:.2f}  TP={:.2f}  SL={:.2f}".format(
                            tr.symbol, avg_prc, tr.tp_price, tr.sl_price
                        )
                    )
                    self.state = OrderState.IN_TRADE
                    # place_tp_sl() called by strategy loop on next tick
                elif 0 < fill_shares < tr.qty:
                    log.warning(
                        "[WS PARTIAL FILL] {} {}/{} @ {:.2f}. Waiting.".format(
                            tr.symbol, fill_shares, tr.qty, avg_prc
                        )
                    )
                elif "reject" in status or "cancel" in status:
                    log.warning("[WS REJECTED] Entry for {}".format(tr.symbol))
                    self._reset()
                return

            if self.state != OrderState.IN_TRADE:
                return

            # ── TP order ──────────────────────────────────────────────────────
            if oid == tr.tp_order_id:
                if 0 < fill_shares < tr.qty:
                    log.warning(
                        "[WS TP PARTIAL] {} {}/{} @ {:.2f}".format(
                            tr.symbol, fill_shares, tr.qty, avg_prc
                        )
                    )
                    tr.exit_filled_qty = fill_shares
                if ("complete" in status) or (fill_shares >= tr.qty > 0):
                    tr.exit_filled_qty = tr.qty
                    exit_fill = avg_prc if avg_prc > 0 else tr.tp_price
                    sl_id = tr.sl_order_id
                    self._close_trade(exit_fill, "TP", tr)
                    if sl_id and sl_id not in _DRY_SENTINEL:
                        try:
                            self.api.cancel_order(orderno=sl_id)
                        except Exception:
                            pass
                return

            # ── SL order ──────────────────────────────────────────────────────
            if oid == tr.sl_order_id:
                if 0 < fill_shares < tr.qty:
                    log.warning(
                        "[WS SL PARTIAL] {} {}/{} @ {:.2f}".format(
                            tr.symbol, fill_shares, tr.qty, avg_prc
                        )
                    )
                    tr.exit_filled_qty = fill_shares
                if ("complete" in status) or (fill_shares >= tr.qty > 0):
                    tr.exit_filled_qty = tr.qty
                    exit_fill = avg_prc if avg_prc > 0 else tr.sl_price
                    tp_id = tr.tp_order_id
                    self._close_trade(exit_fill, "SL", tr)
                    if tp_id and tp_id not in _DRY_SENTINEL:
                        try:
                            self.api.cancel_order(orderno=tp_id)
                        except Exception:
                            pass

    # ── REST reconcile monitor (fallback when WS is stale)  ──────────────────

    def monitor_open_trade(self, ltp: Optional[float] = None) -> bool:
        """
        Called each strategy tick while IN_TRADE.
        Dry-run: simulate TP/SL via live LTP.
        Live: REST reconcile only when last WS event is older than reconcile_interval_sec.
        """
        with self._lock:
            if self.state != OrderState.IN_TRADE:
                return False
            tr = self.current

            # ── DRY-RUN: LTP-based TP/SL simulation ──────────────────────────
            if self.cfg.get("dry_run"):
                if ltp is None:
                    ltp = self.tick_store.ltp(tr.token)
                if ltp is None:
                    return False
                if ltp >= tr.tp_price:
                    log.info(
                        "[DRY-RUN] TP SIMULATED HIT {} @ {:.2f}".format(tr.symbol, ltp)
                    )
                    self._close_trade(ltp, "TP", tr)
                    return True
                if ltp <= tr.sl_price:
                    log.info(
                        "[DRY-RUN] SL SIMULATED HIT {} @ {:.2f}".format(tr.symbol, ltp)
                    )
                    self._close_trade(ltp, "SL", tr)
                    return True
                return False

            # ── LIVE: skip REST if WS is fresh ────────────────────────────────
            reconcile_sec = self.cfg.get("reconcile_interval_sec", 10)
            if time.time() - self._last_ws_event < reconcile_sec:
                return False

            orders = self.api.get_order_book()
            if not orders:
                return False
            omap = {o["norenordno"]: o for o in orders if "norenordno" in o}

            # Check TP
            if tr.tp_order_id and tr.tp_order_id in omap:
                o = omap[tr.tp_order_id]
                fill_shares = int(o.get("fillshares") or 0)
                if "complete" in o.get("status", "").lower() and fill_shares >= tr.qty:
                    exit_fill = float(o.get("avgprc") or tr.tp_price)
                    sl_id = tr.sl_order_id
                    self._close_trade(exit_fill, "TP", tr)
                    if sl_id and sl_id not in _DRY_SENTINEL:
                        try:
                            self.api.cancel_order(orderno=sl_id)
                        except Exception:
                            pass
                    return True

            # Check SL
            if tr.sl_order_id and tr.sl_order_id in omap:
                o = omap[tr.sl_order_id]
                fill_shares = int(o.get("fillshares") or 0)
                if "complete" in o.get("status", "").lower() and fill_shares >= tr.qty:
                    exit_fill = float(o.get("avgprc") or tr.sl_price)
                    tp_id = tr.tp_order_id
                    self._close_trade(exit_fill, "SL", tr)
                    if tp_id and tp_id not in _DRY_SENTINEL:
                        try:
                            self.api.cancel_order(orderno=tp_id)
                        except Exception:
                            pass
                    return True
            return False

    # ── emergency exit ────────────────────────────────────────────────────────

    def emergency_square_off(self) -> None:
        with self._lock:
            if self.state not in (OrderState.ENTRY_PENDING, OrderState.IN_TRADE):
                return
            tr = self.current
            is_dry = self.cfg.get("dry_run", False)
            log.warning(
                "[SQUARE-OFF] Exiting {}{}".format(
                    tr.symbol, " [DRY-RUN, no real order]" if is_dry else ""
                )
            )

            if not is_dry:
                for oid in [tr.tp_order_id, tr.sl_order_id, tr.entry_order_id]:
                    if oid and oid not in _DRY_SENTINEL:
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

            if self.state == OrderState.IN_TRADE and tr.filled_at is not None:
                ltp = self.tick_store.ltp(tr.token) if self.tick_store else None
                exit_px = ltp if ltp else tr.entry_price
                self._close_trade(exit_px, "SQUAREOFF", tr)
            else:
                self._reset()

    # ── internal close / reset ────────────────────────────────────────────────

    def _close_trade(self, exit_fill: float, reason: str, tr: TradeRecord) -> None:
        """
        Finalise a completed trade: compute PnL, log, write CSV, persist session.
        Must be called while self._lock is held.
        """
        gross = (exit_fill - tr.entry_price) * tr.qty
        cost_b = self._cost_buy(tr.entry_price, tr.qty)
        cost_s = self._cost_sell(exit_fill, tr.qty)
        net = gross - cost_b - cost_s
        self.daily_pnl += net

        now_ts = time.time()
        tr.exit_time = now_ts
        exit_str = datetime.fromtimestamp(now_ts, tz=IST).strftime("%Y-%m-%d %H:%M:%S") if IST else datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M:%S")  # type: ignore[arg-type]
        entry_str = (
            datetime.fromtimestamp(tr.filled_at, tz=IST).strftime("%Y-%m-%d %H:%M:%S")  # type: ignore[arg-type]
            if tr.filled_at and IST
            else (
                datetime.fromtimestamp(tr.filled_at).strftime("%Y-%m-%d %H:%M:%S")
                if tr.filled_at
                else "N/A"
            )
        )

        log.info(
            "[TRADE EXIT] {}  Reason={}  Entry={:.2f}  Exit={:.2f}  Qty={}  "
            "Gross=INR{:.2f}  CostBuy=INR{:.2f}  CostSell=INR{:.2f}  "
            "Net=INR{:.2f}  DailyPnL=INR{:.2f}".format(
                tr.symbol, reason, tr.entry_price, exit_fill, tr.qty,
                gross, cost_b, cost_s, net, self.daily_pnl,
            )
        )

        dry = self.cfg.get("dry_run", False)
        self.ledger.write({
            "date": _today_ist().isoformat(),
            "symbol": tr.symbol,
            "entry_px": "{:.2f}".format(tr.entry_price),
            "exit_px": "{:.2f}".format(exit_fill),
            "qty": tr.qty,
            "gross_pnl": "{:.2f}".format(gross),
            "cost_buy": "{:.2f}".format(cost_b),
            "cost_sell": "{:.2f}".format(cost_s),
            "net_pnl": "{:.2f}".format(net),
            "reason": "DRY_{}".format(reason) if dry else reason,
            "entry_time": entry_str,
            "exit_time": exit_str,
            "dry_run": str(dry),
        })

        self._end_internal()

    def _end_internal(self) -> None:
        self.trade_count += 1
        log.info(
            "[TRADE #{}]  DailyPnL=INR{:.2f}".format(self.trade_count, self.daily_pnl)
        )
        self.session.save(self.daily_pnl, self.trade_count)
        self.current = None
        self._cd_end = time.time() + self.cfg["cooldown_sec"]
        self.state = OrderState.IDLE

    def _reset(self) -> None:
        """Reset after a cancelled / rejected / timed-out entry (no trade recorded)."""
        self.current = None
        self._cd_end = time.time() + self.cfg["cooldown_sec"]
        self.state = OrderState.IDLE


# ============================================================
# SYMBOL REGISTRY
# ============================================================

class SymbolRegistry:
    def __init__(self, api: FlattradeClient, exchange: str):
        self.api = api
        self.exch = exchange
        self._s2t: Dict[str, str] = {}
        self._t2s: Dict[str, str] = {}
        cache = Path(__file__).parent / "symbol_cache.json"
        if cache.exists():
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                self._s2t = d.get("s2t", {})
                self._t2s = d.get("t2s", {})
                log.info("[REGISTRY] Loaded {} cached symbols.".format(len(self._s2t)))
            except Exception:
                pass
        self._cache = cache

    def _save(self) -> None:
        self._cache.write_text(
            json.dumps({"s2t": self._s2t, "t2s": self._t2s}, indent=2), encoding="utf-8"
        )

    def resolve_batch(self, symbols: list, delay: float = 0.12) -> Dict:
        result: Dict = {}
        new_cnt = 0
        for sym in symbols:
            tsym = "{}-EQ".format(sym)
            if tsym in self._s2t:
                result[sym] = (tsym, self._s2t[tsym])
                continue
            try:
                ret = self.api.searchscrip(exchange=self.exch, searchtext=sym)
                if isinstance(ret, dict) and ret.get("stat") == "Not_Ok":
                    log.error(
                        "[REGISTRY] searchscrip failed for {}: {}. Aborting symbol resolution.".format(
                            sym, ret.get("emsg", ret)
                        )
                    )
                    break
                if ret and ret.get("stat") == "Ok" and ret.get("values"):
                    for v in ret["values"]:
                        if v.get("tsym", "").upper() == tsym.upper():
                            token = v["token"]
                            self._s2t[tsym] = token
                            self._t2s[token] = tsym
                            result[sym] = (tsym, token)
                            new_cnt += 1
                            break
            except Exception as exc:
                log.debug("[REGISTRY] searchscrip error {}: {}".format(sym, exc))
            time.sleep(delay)
        if new_cnt:
            self._save()
        log.info(
            "[REGISTRY] Resolved {}/{} symbols ({} new).".format(
                len(result), len(symbols), new_cnt
            )
        )
        return result

    def token_to_sym(self, token: str) -> Optional[str]:
        return self._t2s.get(token)

    def sym_to_token(self, tsym: str) -> Optional[str]:
        return self._s2t.get(tsym)


# ============================================================
# MARKET HOURS  (item 8 — all IST-aware)
# ============================================================

def _hhmm(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def is_market_open(cfg: Dict) -> bool:
    now = _now_ist()
    mins = now.hour * 60 + now.minute
    return _hhmm(cfg["market_open_hhmm"]) <= mins < _hhmm(cfg["market_close_hhmm"])


def is_square_off(cfg: Dict) -> bool:
    now = _now_ist()
    mins = now.hour * 60 + now.minute
    return mins >= _hhmm(cfg["square_off_hhmm"])


# ============================================================
# MAIN BOT
# ============================================================

class AlgoTrader:
    """
    Orchestrator:
      1. Authenticate.
      2. Fetch Nifty-500 universe (NSE CSV → hardcoded fallback).
      3. Resolve tokens via SymbolRegistry.
      4. Start strategy thread.
      5. Connect WebSocket; exponential-backoff reconnect on disconnect.
    """

    def __init__(self):
        self.cfg = CFG
        self.api = FlattradeClient()
        self.ticks = TickStore(maxlen=self.cfg["tick_window"])
        self.ledger = TradeLedger()
        self.session = SessionState()
        self.signals = SignalEngine(self.cfg)
        self.margin_cache = MarginCache(self.api, self.cfg)
        self.orders = OrderManager(self.api, self.cfg, self.ledger, self.session, self.ticks, self.margin_cache)
        self.board = CandidateBoard(board_size=self.cfg.get("board_size", 5))
        self.registry: Optional[SymbolRegistry] = None
        self._running = threading.Event()
        self._running.set()
        self._ws_open = threading.Event()
        self._ws_closed_event = threading.Event()
        self._t2base: Dict[str, str] = {}
        self._t2tsym: Dict[str, str] = {}
        self._n50 = self.cfg["nifty50_token"]
        self._reconnect_attempts = 0

        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)

    # ── auth ──────────────────────────────────────────────────────────────────

    def auth(self) -> None:
        uid = self.cfg["user_id"]
        tok = self.cfg["api_token"]
        if uid == "YOUR_USER_ID" or tok == "YOUR_SESSION_TOKEN":
            log.error("Set credentials in {} or .env then re-run.".format(CONFIG_FILE))
            sys.exit(1)
        self.api.set_session(userid=uid, password="", usertoken=tok)

        # Validate session token with a fast REST API call before proceeding
        limits = self.api.get_limits()
        if limits is None or (isinstance(limits, dict) and limits.get("stat") == "Not_Ok"):
            emsg = (
                limits.get("emsg", "Invalid or expired session token")
                if isinstance(limits, dict)
                else "No response from API"
            )
            log.error(
                "[AUTH FATAL] Flattrade session token validation failed for user {}: {}.\n"
                "  → Your daily session token (FLATTRADE_API_TOKEN) is expired or invalid.\n"
                "  → Run `python get_token.py` to refresh your token in .env, then restart.".format(
                    uid, emsg
                )
            )
            sys.exit(1)
        log.info("[AUTH] Authenticated as {}".format(uid))

    # ── symbol resolution ─────────────────────────────────────────────────────

    def resolve_symbols(self) -> None:
        universe = get_universe()
        self.registry = SymbolRegistry(self.api, self.cfg["exchange"])
        log.info("[INIT] Resolving {} universe symbols...".format(len(universe)))
        resolved = self.registry.resolve_batch(universe)
        for base, (tsym, token) in resolved.items():
            self._t2base[token] = base
            self._t2tsym[token] = tsym
        self._t2base[self._n50] = "NIFTY50"
        self._t2tsym[self._n50] = "Nifty 50"
        log.info("[INIT] {} symbols ready for subscription.".format(len(self._t2base)))

    # ── WebSocket callbacks ───────────────────────────────────────────────────

    def _on_open(self) -> None:
        log.info("[WS] Connected.")
        self._reconnect_attempts = 0       # reset counter on successful connection
        self._ws_open.set()
        tokens = list(self._t2base.keys())
        sub_list = ["{}|{}".format(self.cfg["exchange"], t) for t in tokens]
        # Ensure Nifty-50 index is always included
        n50_sub = "{}|{}".format(self.cfg["exchange"], self._n50)
        if n50_sub not in sub_list:
            sub_list.append(n50_sub)
        for i in range(0, len(sub_list), 200):
            batch = sub_list[i:i + 200]
            self.api.subscribe(batch)
            log.info("[WS] Subscribed batch {}: {} tokens".format(i // 200 + 1, len(batch)))

    def _on_quote(self, msg: Dict) -> None:
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

    def _on_order_update(self, msg: Dict) -> None:
        """Route WS order update to OrderManager (primary fill detection path)."""
        if msg:
            log.debug("[ORDER UPDATE] {}".format(msg))
            self.orders.on_ws_order_event(msg)

    def _on_error(self, msg) -> None:
        msg_str = str(msg)
        log.error("[WS ERROR] {}".format(msg_str))
        if "NOT_OK" in msg_str or "404" in msg_str or "Handshake status 404" in msg_str:
            log.warning(
                "[WS AUTH WARNING] WebSocket authentication failed ({})!\n"
                "  → Your daily Flattrade session token (FLATTRADE_API_TOKEN) may be expired or invalid.\n"
                "  → Run `python get_token.py` to refresh your token in .env, then restart.".format(msg_str)
            )

    def _on_close(self) -> None:
        log.warning("[WS] Disconnected.")
        self._ws_open.clear()
        if hasattr(self.api, "_NorenApi__stop_event"):
            try:
                self.api._NorenApi__stop_event.set()
            except Exception:
                pass
        self._ws_closed_event.set()

    # ── WebSocket connect helper ───────────────────────────────────────────────

    def _connect_ws(self) -> None:
        """Blocking call: starts WS and blocks until connection drops."""
        self._ws_closed_event.clear()
        self.api.start_websocket(
            order_update_callback=self._on_order_update,
            subscribe_callback=self._on_quote,
            socket_open_callback=self._on_open,
            socket_error_callback=self._on_error,
            socket_close_callback=self._on_close,
        )
        self._ws_closed_event.wait()

    # ── strategy loop ─────────────────────────────────────────────────────────

    def _strategy_loop(self) -> None:
        log.info("[STRATEGY] Loop started.")
        while self._running.is_set():
            try:
                self._tick()
            except Exception as exc:
                log.exception("[STRATEGY] Unhandled error: {}".format(exc))
            time.sleep(self.cfg["scan_interval_sec"])

    def _tick(self) -> None:
        if is_square_off(self.cfg):
            if self.orders.state != OrderState.IDLE:
                log.warning("[STRATEGY] Square-off time. Exiting all positions.")
                self.orders.emergency_square_off()
            return

        if not is_market_open(self.cfg):
            return

        # Always update candidate board on every strategy tick
        self._update_board()

        state = self.orders.state

        if state == OrderState.ENTRY_PENDING:
            if self.orders.check_entry_fill():
                self.orders.place_tp_sl()
            return

        if state == OrderState.IN_TRADE:
            tr = self.orders.current
            # If WS fired entry fill but TP/SL not yet placed, place them now
            if tr and tr.tp_order_id is None:
                self.orders.place_tp_sl()
            else:
                ltp = self.ticks.ltp(tr.token) if tr else None
                self.orders.monitor_open_trade(ltp)
            return

        if state == OrderState.IDLE and self.orders.is_ready():
            self._try_board_or_scan()

    def _update_board(self) -> None:
        """Run candidate-finding logic across ALL qualifying stocks and update CandidateBoard."""
        n50_trend = self.signals.trend(self.ticks.prices(self._n50))
        if n50_trend != "UP":
            self.board.update([])
            self.board.log_board_if_due(self.cfg.get("board_log_interval_sec", 10))
            return

        candidates = []
        for token, base_sym in self._t2base.items():
            if token == self._n50:
                continue
            if self.ticks.tick_count(token) < self.cfg["min_ticks_for_signal"]:
                continue
            ltp = self.ticks.ltp(token)
            if ltp is None or ltp < self.cfg["min_stock_price"]:
                continue
            prices = self.ticks.prices(token)

            # Gate 2: pullback condition
            pb_ok, entry_px = self.signals.pullback_entry(prices, self.cfg["tick_size"])
            if not pb_ok:
                continue

            sc = self.signals.score(prices, self.ticks.volumes(token))
            if sc <= 0.01:
                continue

            # Gate 3: stock's own trend must also be UP (dual trend filter)
            stock_trend = self.signals.trend(prices)
            if stock_trend != "UP":
                continue

            tsym = self._t2tsym.get(token, base_sym)
            candidates.append({
                "symbol": tsym,
                "token": token,
                "score": sc,
                "entry_price": entry_px,
            })

        self.board.update(candidates)
        self.board.log_board_if_due(self.cfg.get("board_log_interval_sec", 10))

    def _try_board_or_scan(self) -> None:
        """
        When OrderManager is IDLE and ready:
        Attempt fast-fire by re-validating ranked candidates from CandidateBoard.
        Falls back to fresh _scan_and_trade() if board is empty or all candidates fail re-validation.
        """
        board = self.board.get_board()
        if not board:
            log.debug("[SCAN] board empty, running fresh scan")
            self._scan_and_trade()
            return

        n50_trend = self.signals.trend(self.ticks.prices(self._n50))
        if n50_trend != "UP":
            log.debug("[SCAN] Nifty-50 trend is {}, skipping fast-fire.".format(n50_trend))
            self._scan_and_trade()
            return

        skipped_reasons = []
        for cand in board:
            token = cand["token"]
            sym = cand["symbol"]
            rank = cand["rank"]
            prices = self.ticks.prices(token)

            # Live re-validation against current tick buffer
            stock_trend = self.signals.trend(prices)
            if stock_trend != "UP":
                skipped_reasons.append("rank {} ({}) failed re-validation (trend flipped)".format(rank, sym))
                continue

            pb_ok, live_entry_px = self.signals.pullback_entry(prices, self.cfg["tick_size"])
            if not pb_ok:
                skipped_reasons.append("rank {} ({}) failed re-validation (pullback invalid)".format(rank, sym))
                continue

            # Re-validation passed! Fast-fire entry
            margin_pct = self.margin_cache.get_margin_pct(token)
            if rank == 1:
                log.info("[FAST-FIRE rank=1] {} entry={:.2f}".format(sym, live_entry_px))
            else:
                reasons_str = "; ".join(skipped_reasons)
                log.info("[FAST-FIRE rank={}] {} -- {}".format(rank, sym, reasons_str))

            if self.orders.enter_trade(symbol=sym, token=token, entry_px=live_entry_px, margin_pct=margin_pct):
                return

        log.info("[SCAN] entire board stale/invalid, running fresh scan")
        self._scan_and_trade()

    def _scan_and_trade(self) -> None:
        # Gate 1: Nifty-50 must be trending UP
        n50_trend = self.signals.trend(self.ticks.prices(self._n50))
        if n50_trend != "UP":
            log.debug("[SCAN] Nifty-50={}. Skipping longs.".format(n50_trend))
            return

        # Score all candidates that also show a valid pullback
        scores: Dict[str, float] = {}
        pullback_entries: Dict[str, float] = {}

        for token, base_sym in self._t2base.items():
            if token == self._n50:
                continue
            if self.ticks.tick_count(token) < self.cfg["min_ticks_for_signal"]:
                continue
            ltp = self.ticks.ltp(token)
            if ltp is None or ltp < self.cfg["min_stock_price"]:
                continue
            prices = self.ticks.prices(token)

            # Gate 2: pullback condition must be met before scoring
            pb_ok, entry_px = self.signals.pullback_entry(prices, self.cfg["tick_size"])
            if not pb_ok:
                continue

            sc = self.signals.score(prices, self.ticks.volumes(token))
            if sc > 0.01:
                scores[token] = sc
                pullback_entries[token] = entry_px  # type: ignore[assignment]

        if not scores:
            log.debug("[SCAN] No qualifying candidates with pullback.")
            return

        best_token = max(scores, key=scores.__getitem__)
        best_score = scores[best_token]
        best_ltp = self.ticks.ltp(best_token)
        best_tsym = self._t2tsym.get(best_token, "UNKNOWN")
        best_entry = pullback_entries[best_token]

        log.info(
            "[CANDIDATE] {}  Score={:.4f}  LTP={:.2f}  Entry={:.2f}  N50={}".format(
                best_tsym, best_score, best_ltp, best_entry, n50_trend
            )
        )

        # Gate 3: stock's own trend must also be UP (dual trend filter)
        stock_trend = self.signals.trend(self.ticks.prices(best_token))
        if stock_trend != "UP":
            log.info(
                "[SKIP] {} stock_trend={} (need UP to match Nifty-50).".format(
                    best_tsym, stock_trend
                )
            )
            return

        margin_pct = self.margin_cache.get_margin_pct(best_token)
        self.orders.enter_trade(symbol=best_tsym, token=best_token, entry_px=best_entry, margin_pct=margin_pct)

    # ── main run loop  (item 5: reconnect built into main thread)  ───────────

    def run(self) -> None:
        log.info("=" * 70)
        log.info("  FLATTRADE SEMI-HFT ALGO TRADER  v2")
        if self.cfg.get("dry_run"):
            log.info("  *** DRY-RUN MODE — NO REAL ORDERS WILL BE PLACED ***")
        log.info(
            "  Capital=INR{:,}  Target=INR{} net  SL=INR{}  MaxDailyLoss=INR{}".format(
                self.cfg["capital"],
                self.cfg["target_profit_inr"],
                self.cfg["stop_loss_inr"],
                self.cfg["max_daily_loss_inr"],
            )
        )
        log.info(
            "  PullbackLookback={}  TrendLookback={}  PullbackPct={:.3f}%%".format(
                self.cfg["pullback_lookback"],
                self.cfg["trend_lookback"],
                self.cfg["pullback_pct_min"],
            )
        )
        log.info("=" * 70)

        self.auth()
        self.orders.restore_session()
        self.resolve_symbols()

        limits = self.api.get_limits()
        if limits:
            cash = limits.get("cash") or limits.get("net") or "N/A"
            log.info("[ACCOUNT] Available margin: INR {}".format(cash))

        strat = threading.Thread(
            target=self._strategy_loop, name="StrategyLoop", daemon=True
        )
        strat.start()

        # Reconnect loop lives on the main thread (start_websocket is blocking)
        max_att = self.cfg.get("ws_max_reconnect_attempts", 10)

        while self._running.is_set():
            if self._reconnect_attempts > 0:
                delay = min(2 ** self._reconnect_attempts * 2, 120)
                log.warning(
                    "[WS RECONNECT] attempt {}/{}  waiting {}s before retry...".format(
                        self._reconnect_attempts, max_att, delay
                    )
                )
                deadline = time.time() + delay
                while time.time() < deadline and self._running.is_set():
                    time.sleep(0.2)
                if not self._running.is_set():
                    break

            log.info("[WS] Connecting...")
            try:
                self._connect_ws()   # blocks until WS disconnects
            except Exception as exc:
                log.error("[WS] Connection error: {}".format(exc))

            if not self._running.is_set():
                break

            # WS dropped unexpectedly
            self._reconnect_attempts += 1
            if self._reconnect_attempts > max_att:
                # ── exhaustion: REST square-off + hard exit ────────────────
                log.critical(
                    "[FATAL] WS reconnect exhausted after {} attempts.".format(max_att)
                )
                if self.orders.state in (OrderState.ENTRY_PENDING, OrderState.IN_TRADE):
                    log.critical("[FATAL] Open position — squaring off via REST.")
                    self.orders.emergency_square_off()
                log.info(
                    "[SUMMARY] Trades={}  DailyPnL=INR{:.2f}".format(
                        self.orders.trade_count, self.orders.daily_pnl
                    )
                )
                # os._exit bypasses Python exception handling; the only reliable
                # way to terminate from here (which may be a non-main thread context
                # indirectly, but more importantly guarantees no hung threads).
                os._exit(1)

        strat.join(timeout=5)

    # ── graceful shutdown ─────────────────────────────────────────────────────

    def _shutdown(self, signum, frame) -> None:
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
