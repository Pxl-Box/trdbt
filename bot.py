import time
import json
import logging
import logging.handlers
import math
from datetime import datetime, timezone
from pathlib import Path
from strategy import MeanReversionStrategy
from trading212_client import Trading212Client

#  Logging Setup 
# Logs rotate daily in the logs/ subfolder, 30 days retained.
# File captures DEBUG (including full payloads); console stays at INFO.
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_log_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

_file_handler = logging.handlers.TimedRotatingFileHandler(
    LOG_DIR / "bot.log",
    when="midnight",
    backupCount=30,
    encoding="utf-8"
)
_file_handler.setFormatter(_log_format)
_file_handler.setLevel(logging.DEBUG)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_format)
_console_handler.setLevel(logging.INFO)

logging.root.setLevel(logging.DEBUG)
logging.root.addHandler(_file_handler)
logging.root.addHandler(_console_handler)

# Suppress noisy DEBUG output from third-party libraries.
# These produce hundreds of lines per cycle and drown out real bot events.
for _noisy_lib in ("yfinance", "urllib3", "peewee", "hpack", "httpx"):
    logging.getLogger(_noisy_lib).setLevel(logging.WARNING)

logger = logging.getLogger("bot")

#  Ticker Helpers 
# yfinance uses bare tickers (COIN); Trading212 v0 needs the full instrument code.
# Tickers in config that already contain "_" are assumed to be pre-qualified.
_NON_EQUITY = {"BTC-USD", "ETH-USD"}  # crypto pairs handled differently by T212

def to_t212_ticker(ticker: str) -> str:
    """Convert a bare yfinance-style ticker to the Trading212 instrument code."""
    if "_" in ticker or ticker in _NON_EQUITY:
        return ticker
    # Heuristic: .PA = Euronext Paris, .XC = XETRA, .L = London
    if ticker.endswith(".PA"): return f"{ticker.replace('.PA', '')}_BE_EQ" # Example mapping
    if ticker.endswith(".XC"): return f"{ticker.replace('.XC', '')}_DE_EQ"
    if ticker.endswith(".L"):  return f"{ticker.replace('.L', '')}_UK_EQ"
    
    return f"{ticker}_US_EQ"

#  Constants 
CONFIG_FILE = "config.json"
STATE_FILE  = "bot_state.json"

# How long between polling polls when waiting for a limit order to fill (seconds)
FILL_POLL_INTERVAL = 5


class TradingBot:
    def __init__(self):
        self.config   = self.load_config()
        self.state    = self.load_state()
        self.client   = None
        self.strategy = None

    #  Config / State I/O 

    def load_config(self):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return {}

    def save_config(self):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    def load_state(self):
        """
        State persists across restarts:
          peak_equity       highest total equity seen (for kill switch)
          open_trades       { ticker: { qty, sl_order_id, entry_price } }
          cooldowns         { ticker: ISO-timestamp of last close }
        """
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r") as f:
                    s = json.load(f)
                    # Back-compat defaults
                    s.setdefault("peak_equity", 0.0)
                    s.setdefault("open_trades", {})
                    s.setdefault("cooldowns", {})
                    return s
            except Exception:
                pass
        return {
            "peak_equity":   0.0,
            "open_trades":   {},
            "pending_orders": {},   # order_id -> {ticker, qty, sl_price, t212_ticker}
            "cooldowns":     {}
        }

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    #  Initialisation 

    def init_clients(self) -> bool:
        """Initialise / re-initialise API clients from the current config."""
        api_key = self.config.get("api_key", "")
        api_secret = self.config.get("api_secret", "")
        mode = self.config.get("api_mode", "Practice")

        if not api_key or not api_secret:
            return False

        self.client = Trading212Client(api_key, api_secret, mode)
        self.strategy = MeanReversionStrategy(
            bb_length=self.config.get("bb_length", 20),
            bb_std=self.config.get("bb_std", 2.0),
            rsi_length=self.config.get("rsi_length", 14),
            rsi_threshold=self.config.get("rsi_threshold", 30),
            smart_regime_enabled=self.config.get("smart_regime_enabled", False),
            tp_target_mode=self.config.get("tp_target_mode", "Mean")
        )
        return True

    #  Kill Switch 

    def check_kill_switch(self, current_equity: float) -> bool:
        """
        If total equity has dropped >= kill_switch_drop_pct% from its peak,
        trigger a full lockdown.
        """
        peak = self.state.get("peak_equity", 0.0)
        if current_equity > peak:
            self.state["peak_equity"] = current_equity
            self.save_state()
            return False

        drop_pct = (peak - current_equity) / peak if peak > 0 else 0
        limit_pct = self.config.get("kill_switch_drop_pct", 0.05)

        if drop_pct >= limit_pct:
            logger.critical(
                f"KILL SWITCH TRIGGERED! Equity dropped {drop_pct*100:.2f}% "
                f"from peak {peak:.2f}"
            )
            self.lock_down()
            return True

        return False

    def lock_down(self):
        """Cancel all orders, close all positions, lock the bot."""
        logger.info("Executing Lockdown Protocol...")
        self.config["bot_status"] = "LOCKED"
        self.save_config()
        if self.client:
            self.client.cancel_all_orders()
            self.client.market_sell_all_positions()
            self.state["open_trades"] = {}
            self.save_state()
            logger.info("Lockdown complete. All positions liquidated. Bot LOCKED.")

    #  Position Reconciliation 

    def sync_open_trades(self, open_positions: list):
        """
        Reconcile local open_trades state against the live API positions.

        - Any position returned by the API that isn't tracked locally gets
          imported automatically so the bot can manage it (apply SELL signals,
          place a missing SL, etc.).
        - Any locally tracked trade that no longer has a matching live position
          is removed (it was closed externally or the SL triggered).

        For imported positions we don't know the original stop-loss order ID,
        so sl_order_id is set to None.  handle_sell() will skip the cancel
        step gracefully in that case.
        """
        open_trades = self.state.setdefault("open_trades", {})
        live_by_t212 = {p['ticker']: p for p in open_positions if 'ticker' in p}

        #  Import untracked live positions 
        # Build a reverse map: t212_ticker -> short ticker, for positions we DO track
        tracked_t212 = {
            v.get('t212_ticker', to_t212_ticker(k)): k
            for k, v in open_trades.items()
        }

        imported = []
        for t212_ticker, pos in live_by_t212.items():
            if t212_ticker not in tracked_t212:
                # Derive the short ticker (strip _US_EQ suffix if present)
                short = t212_ticker.replace("_US_EQ", "").replace("_US_ETF", "")
                qty   = pos.get('quantity', 0)
                avg_price = pos.get('averagePrice') or pos.get('currentPrice', 0.0)
                open_trades[short] = {
                    "qty":          qty,
                    "entry_price":  avg_price,
                    "sl_order_id":  None,   # unknown  was opened outside the bot
                    "t212_ticker":  t212_ticker,
                    "imported":     True
                }
                imported.append(f"{short} (qty={qty} @ {avg_price:.2f})")

        if imported:
            logger.info(
                f"[sync] Imported {len(imported)} untracked position(s): "
                + ", ".join(imported)
            )

        #  Remove stale local records 
        stale = []
        for short_ticker, trade in list(open_trades.items()):
            t212 = trade.get('t212_ticker', to_t212_ticker(short_ticker))
            if t212 not in live_by_t212:
                stale.append(short_ticker)
                del open_trades[short_ticker]

        if stale:
            logger.info(
                f"[sync] Removed {len(stale)} stale local trade(s) "
                f"(position closed externally): {', '.join(stale)}"
            )

        if imported or stale:
            self.save_state()

        # Place stop-loss orders for any newly imported positions that don't have one
        for short_ticker in [s.split(' ')[0] for s in imported]:
            trade = open_trades.get(short_ticker)
            if trade and trade.get('sl_order_id') is None:
                self.place_missing_stop_loss(short_ticker, trade)

    def resume_pending_orders(self):
        """
        Called at the start of every cycle.
        Checks every pending buy order (tracked by ID) against the live API:

          FILLED     promote to open_trades, place stop-loss, remove from pending
          CANCELLED/REJECTED/EXPIRED  clean up, remove from pending
          still WORKING/PLACED        leave it; already_in_trade() will skip a new BUY

        This prevents the bot from placing a second buy on restart when a limit
        order from the previous run is still live on the exchange.
        """
        pending = self.state.get("pending_orders", {})
        if not pending:
            return

        for order_id, meta in list(pending.items()):
            try:
                order   = self.client.get_order_by_id(order_id)
                status  = order.get("status", "").upper()
                ticker  = meta.get("ticker", "?")
                t212    = meta.get("t212_ticker", ticker)
                qty     = meta.get("qty", 0)
                sl_price= meta.get("sl_price", 0.0)

                logger.info(f"[resume] Pending order {order_id} ({ticker}) status={status}")

                if status == "FILLED":
                    # Order filled during downtime  promote and place SL
                    logger.info(f"[resume] {ticker} filled during restart gap. Placing SL @ ${sl_price:.4f}")
                    sl_res = self.client.place_stop_order(t212, qty, sl_price)
                    if sl_res and sl_res.get('id'):
                        self.state.setdefault("open_trades", {})[ticker] = {
                            "qty":         qty,
                            "entry_price": meta.get("entry_price", 0.0),
                            "sl_order_id": sl_res['id'],
                            "sl_price":    sl_price,
                            "t212_ticker": t212
                        }
                        logger.info(f"[resume] SL placed for {ticker}. SL ID: {sl_res['id']}")
                    else:
                        logger.error(f"[resume] SL FAILED for {ticker} after fill! Manual SL at ${sl_price:.4f} needed.")
                    del pending[order_id]

                elif status in ("CANCELLED", "REJECTED", "EXPIRED", ""):
                    logger.info(f"[resume] Removing stale pending order {order_id} ({ticker}, status={status}).")
                    del pending[order_id]

                # WORKING / PLACED / PARTIALLY_FILLED  leave in pending; skip re-buy

            except Exception as e:
                logger.error(f"[resume] Error checking pending order {order_id}: {e}")

        self.save_state()

    def place_missing_stop_loss(self, ticker: str, trade: dict):
        """
        Calculate and place a stop-loss for a position that was imported without one.

        Stop price = the LOWER of:
          (a) ATR-based:  entry_price - 1 * ATR   (respects real market volatility)
          (b) Pct-based:  entry_price * (1 - stop_loss_pct)  (config floor/ceiling)

        Using the lower of the two means we always give the trade at least as much
        room as the ATR suggests, so normal intraday swings don't trigger the stop.
        If ATR data is unavailable we fall back to the pct-based price only.
        """
        entry_price = trade.get('entry_price', 0.0)
        qty         = trade.get('qty', 0)
        t212_ticker = trade.get('t212_ticker', to_t212_ticker(ticker))

        if not entry_price or not qty:
            logger.warning(
                f"[{ticker}] Cannot place SL  missing entry price or qty in state."
            )
            return

        stop_pct = self.config.get('stop_loss_pct', 0.02)
        pct_stop = round(entry_price * (1.0 - stop_pct), 4)

        # Try ATR-aware stop (requires strategy to be initialised)
        atr_stop = 0.0
        if self.strategy:
            atr = self.strategy.get_current_atr(ticker, multiplier=1.0)
            if atr > 0:
                atr_stop = round(entry_price - atr, 4)
                logger.info(
                    f"[{ticker}] ATR-based SL: {entry_price:.4f} - {atr:.4f} ATR = {atr_stop:.4f}"
                )

        # Pick the lower (wider) of the two to avoid premature stops
        if atr_stop > 0:
            stop_price = min(pct_stop, atr_stop)
            method = f"ATR({atr_stop:.4f}) vs Pct({pct_stop:.4f})  chose {stop_price:.4f}"
        else:
            stop_price = pct_stop
            method = f"Pct-based only ({pct_stop:.4f})  ATR unavailable"

        logger.info(
            f"[{ticker}] Placing catch-up SL | {method} | Qty={qty} t212={t212_ticker}"
        )

        sl_res = self.client.place_stop_order(
            ticker=t212_ticker,
            quantity=qty,
            stop_price=stop_price
        )

        if sl_res and sl_res.get('id'):
            sl_id = sl_res['id']
            trade['sl_order_id'] = sl_id
            trade['sl_price']    = stop_price
            logger.info(f"[{ticker}] Catch-up SL placed. SL ID: {sl_id} @ ${stop_price:.4f}")
            self.save_state()
        else:
            logger.error(
                f"[{ticker}] Catch-up SL order FAILED: {sl_res}. "
                f"MANUAL SL at ${stop_price:.4f} STRONGLY recommended!"
            )

    #  Pre-Trade Checks 

    def get_available_capital(self) -> float:
        """Returns free_cash * capital_utilization_pct."""
        try:
            cash_state = self.client.get_account_cash()
            free_cash = cash_state.get('free', 0.0)
            target_pct = self.config.get("capital_utilization_pct", 0.95)
            return free_cash * target_pct
        except Exception as e:
            logger.error(f"Failed to fetch account cash: {e}")
            return 0.0

    def is_on_cooldown(self, ticker: str) -> bool:
        """True if we exited this ticker recently and are within the cooldown window."""
        cooldowns = self.state.get("cooldowns", {})
        last_close_str = cooldowns.get(ticker)
        if not last_close_str:
            return False
        try:
            last_close = datetime.fromisoformat(last_close_str)
            cooldown_mins = self.config.get("per_ticker_cooldown_mins", 30)
            elapsed_mins = (datetime.now(timezone.utc) - last_close).total_seconds() / 60
            if elapsed_mins < cooldown_mins:
                logger.info(
                    f"[{ticker}] On cooldown. {cooldown_mins - elapsed_mins:.0f}m remaining."
                )
                return True
        except Exception:
            pass
        return False

    def already_in_trade(self, ticker: str,
                         open_positions: list, active_orders: list) -> bool:
        """
        Returns True if we already hold or have a pending order for this ticker.

        Uses BASE symbol comparison (strips _US_EQ, _US_ETF suffixes from both sides)
        so that orders placed by old code (bare "TSLA") and new code ("TSLA_US_EQ")
        both match correctly and don't cause duplicate buys on restart.
        """
        def base(t: str) -> str:
            """Strip exchange suffix: TSLA_US_EQ -> TSLA"""
            return t.split("_")[0].upper() if t else ""

        our_base = base(ticker)

        if any(base(p.get('ticker', '')) == our_base for p in open_positions):
            logger.info(f"[{ticker}] Already have an open position  skipping BUY.")
            return True
        if any(base(o.get('ticker', '')) == our_base for o in active_orders):
            logger.info(f"[{ticker}] Already have a pending order  skipping BUY.")
            return True
        if ticker in self.state.get("open_trades", {}):
            logger.info(f"[{ticker}] Locally tracked open trade exists  skipping BUY.")
            return True
        return False

    def at_max_positions(self, open_positions: list, active_orders: list) -> bool:
        """Guard against opening too many concurrent positions."""
        max_pos = self.config.get("max_open_positions", 5)
        # Count unique tickers across positions + pending buy orders
        held = {p.get('ticker') for p in open_positions}
        pending = {o.get('ticker') for o in active_orders
                   if o.get('side', '').upper() == 'BUY'
                   or o.get('quantity', 0) > 0}
        total = len(held | pending)
        if total >= max_pos:
            logger.info(
                f"Max open positions ({max_pos}) reached "
                f"({total} held/pending)  no new buys this cycle."
            )
            return True
        return False

    #  Order Execution     def handle_buy(self, ticker: str, signal_data: dict, available_capital: float):
        """
        Places a limit BUY order for the given ticker using a fixed-risk sizing model.
        Stop-loss: ATR-based (entry - sl_atr_multiplier * ATR).
        Position size: fixed risk amount / SL distance.
        After fill: both a stop-loss AND a take-profit limit order are placed.
        """
        price     = float(signal_data.get("price", 0.0))
        target_tp = float(signal_data.get("target_tp", 0.0))
        atr       = float(signal_data.get("atr", 0.0))

        min_investment = self.config.get("min_investment_amount", 1.0)
        if price <= 0 or available_capital < min_investment:
            logger.info(
                f"[{ticker}] Allocation {available_capital:.2f} below minimum {min_investment:.2f} skipping."
            )
            return

        # ATR-based stop-loss price
        sl_atr_mult = float(self.config.get("sl_atr_multiplier", 1.5))
        if atr > 0:
            stop_loss_price = round(price - (atr * sl_atr_mult), 4)
        else:
            stop_pct        = self.config.get("stop_loss_pct", 0.02)
            stop_loss_price = round(price * (1.0 - stop_pct), 4)

        sl_distance = price - stop_loss_price
        if sl_distance <= 0:
            logger.warning(f"[{ticker}] Invalid SL distance ({sl_distance:.4f}) skipping.")
            return

        # Fixed-risk position sizing
        total_equity  = float(getattr(self, '_cycle_equity', available_capital))
        risk_pct      = float(self.config.get("risk_per_trade_pct", 0.01))
        risk_amount   = total_equity * risk_pct
        risk_qty      = round(risk_amount / sl_distance, 4)
        max_qty       = round(available_capital / price, 4)
        quantity      = min(risk_qty, max_qty)
        limit_price   = price
        t212_ticker   = to_t212_ticker(ticker)

        logger.info(
            f"[{ticker}] Placing Limit BUY | t212={t212_ticker} | "
            f"Qty={quantity} (risk-sized) @ {limit_price:.4f} | "
            f"SL={stop_loss_price:.4f} ({sl_atr_mult}*ATR={atr:.4f}) | "
            f"TP={target_tp:.4f}"
        )

        res = self.client.place_limit_order(
            ticker=t212_ticker,
            quantity=quantity,
            limit_price=limit_price
        )

        if not res or not res.get('id'):
            logger.error(f"[{ticker}] BUY order failed: {res}")
            return

        order_id = res['id']
        logger.info(f"[{ticker}] Limit BUY submitted. Order ID: {order_id}")

        # Track the pending order
        self.state.setdefault("pending_orders", {})[str(order_id)] = {
            "ticker":       ticker,
            "t212_ticker":  t212_ticker,
            "qty":          quantity,
            "entry_price":  limit_price,
            "sl_price":     stop_loss_price,
        }
        self.save_state()

        # Poll for fill
        fill_timeout = self.config.get("order_fill_timeout_secs", 60)
        filled = self.wait_for_fill(order_id, timeout_secs=fill_timeout)

        if filled:
            sl_res = self.client.place_stop_order(
                ticker=t212_ticker,
                quantity=quantity,
                stop_price=stop_loss_price
            )
            tp_res = None
            if target_tp and target_tp > limit_price:
                tp_res = self.client.place_limit_sell(
                    ticker=t212_ticker,
                    quantity=quantity,
                    limit_price=round(float(target_tp), 4)
                )

            if sl_res and sl_res.get('id'):
                sl_id = sl_res['id']
                tp_id = tp_res.get('id') if tp_res else None
                logger.info(
                    f"[{ticker}] SL placed @ {stop_loss_price:.4f} (ID: {sl_id})"
                    + (f" | TP placed @ {target_tp:.4f} (ID: {tp_id})" if tp_id else "")
                )
                # Promote from pending to open_trades
                self.state.setdefault("open_trades", {})[ticker] = {
                    "qty":          quantity,
                    "entry_price":  limit_price,
                    "sl_order_id": sl_id,
                    "sl_price":     stop_loss_price,
                    "tp_order_id": tp_id,
                    "tp_price":     float(target_tp) if target_tp else None,
                    "t212_ticker":  t212_ticker
                }
                self.state.get("pending_orders", {}).pop(str(order_id), None)
                self.save_state()
            else:
                logger.warning(
                    f"[{ticker}] FILLED but stop-loss order FAILED. "
                    f"Manual SL recommended!"
                )
        else:
            logger.warning(
                f"[{ticker}] Limit BUY not yet filled after {self.config.get('order_fill_timeout_secs',60)}s. "
                f"Order remains live on exchange. SL will be placed on next restart or when fill is confirmed."
            )

    def wait_for_fill(self, order_id: int, timeout_secs: int = 60) -> bool:
        """
        Poll the API until the order is FILLED or the timeout is reached.
        Returns True if filled, False otherwise.
        """
        start_t = time.time()
        while (time.time() - start_t) < timeout_secs:
            try:
                order = self.client.get_order_by_id(order_id)
                status = order.get("status", "").upper()
                if status == "FILLED":
                    logger.info(f"[fill] Order {order_id} filled.")
                    return True
                if status in ("CANCELLED", "REJECTED", "EXPIRED"):
                    logger.warning(f"[fill] Order {order_id} stopped with status: {status}")
                    return False
                # Still working...
            except Exception as e:
                logger.error(f"[fill] Error polling order {order_id}: {e}")

            time.sleep(FILL_POLL_INTERVAL)

        logger.warning(f"[fill] Timeout waiting for order {order_id} to fill.")
        return False

    def handle_sell(self, ticker: str):
        """
        Close position at market, cancel associated SL and TP orders.
        """
        open_trades = self.state.get("open_trades", {})
        if ticker not in open_trades:
            return

        trade       = open_trades[ticker]
        t212_ticker = trade.get("t212_ticker", to_t212_ticker(ticker))
        qty         = trade.get("qty", 0)
        sl_id       = trade.get("sl_order_id")
        tp_id       = trade.get("tp_order_id")

        logger.info(
            f"[{ticker}] SELL signal  closing position "
            f"(Qty={qty} @ market, cancelling SL={sl_id}, TP={tp_id})"
        )

        # Cancel SL and TP orders first to avoid double-sell
        for order_id, label in [(sl_id, "SL"), (tp_id, "TP")]:
            if order_id:
                if self.client.cancel_order(order_id):
                    logger.info(f"[{ticker}] {label} order {order_id} cancelled.")
                else:
                    logger.warning(f"[{ticker}] Could not cancel {label} order {order_id}  may have triggered.")

        # Market sell
        res = self.client.place_market_sell(t212_ticker, qty)
        if res and res.get('id'):
            logger.info(f"[{ticker}] Market SELL submitted. Order ID: {res['id']}")
        else:
            logger.error(f"[{ticker}] Market SELL failed: {res}. MANUAL CLOSE REQUIRED.")

        del open_trades[ticker]
        self.state.setdefault("cooldowns", {})[ticker] = datetime.now(timezone.utc).isoformat()
        self.save_state()

    #  Market Hours Guard 

    def is_ticker_session_open(self, ticker: str) -> bool:
        """
        Detects exchange suffix and checks if the session is currently open.
        """
        if not self.config.get("market_hours_check", True):
            return True
            
        try:
            from datetime import timedelta
            now_utc = datetime.now(timezone.utc)
            
            # 1. Crypto - 24/7
            if ticker.endswith("-USD") or "-USD" in ticker:
                return True
                
            # 2. EU/London Suffixes
            # .PA = Paris, .XC = XETRA, .L = London
            if any(ticker.endswith(s) for s in [".PA", ".XC", ".L"]):
                # Paris/XETRA: 9:00 - 17:30 CET (UTC+1)
                # London: 8:00 - 16:30 GMT (UTC+0)
                # Simplification: EU markets generally 8am-4:30pm UTC (approx)
                # For precision, we use offsets. 
                # Paris is UTC+1. 09:00 CET = 08:00 UTC. 17:30 CET = 16:30 UTC.
                if now_utc.weekday() >= 5: return False
                
                open_utc  = now_utc.replace(hour=8, minute=0, second=0, microsecond=0)
                close_utc = now_utc.replace(hour=16, minute=30, second=0, microsecond=0)
                return open_utc <= now_utc <= close_utc

            # 3. Default: US Equity Market
            # 9:30am - 4:00pm ET (Approx UTC-4)
            now_et = now_utc + timedelta(hours=-4)
            if now_et.weekday() >= 5:
                return False
                
            open_et  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
            close_et = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
            return open_et <= now_et <= close_et

        except Exception as e:
            logger.warning(f"[hours] Error checking session for {ticker}: {e}")
            return True

    #  Market Regime Filter 

    def is_market_bearish(self) -> bool:
        """
        Returns True if the broad market is in a confirmed downtrend.

        Checks whether the regime ticker (default: SPY) is trading below its
        50-day simple moving average on daily candles.  Buying individual stocks
        into a bear market is low-probability; this filter suppresses all BUY
        signals when the macro environment is against us.

        Set `regime_ticker: null` in config.json to disable this filter.
        """
        regime_ticker = self.config.get("regime_ticker", "SPY")
        if not regime_ticker:
            return False  # filter disabled
        try:
            import yfinance as yf
            df = yf.download(regime_ticker, period="90d", interval="1d", progress=False)
            if df.empty or len(df) < 50:
                logger.warning("[regime] Not enough data to evaluate market regime  allowing BUYs.")
                return False
            if isinstance(df.columns, __import__('pandas').MultiIndex):
                df.columns = df.columns.droplevel(1)
            sma50 = df['Close'].rolling(50).mean().iloc[-1]
            current = float(df['Close'].iloc[-1])
            bearish = current < float(sma50)
            logger.info(
                f"[regime] {regime_ticker} @ {current:.2f} vs 50-SMA {float(sma50):.2f} "
                f"-> {'BEARISH    suppressing BUYs' if bearish else 'BULLISH '}"
            )
            return bearish
        except Exception as e:
            logger.warning(f"[regime] Could not evaluate market regime: {e}. Allowing BUYs.")
            return False

    #  Trailing Stop-Loss 

    def check_trailing_stops(self, open_positions: list):
        """
        Bump stop-losses upward for positions that have moved into meaningful profit.

        Two tiers (thresholds configurable in config.json):
          Tier 1   price  entry + 1.5ATR    move SL to break-even (entry price)
          Tier 2   price  entry + 3.0ATR    move SL to entry + 1.5ATR (lock profit)

        Only bumps *upward*  never tightens a stop thats already higher.
        Cancels the old SL order and places a new one via the API.
        """
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return

        tier1_atr = self.config.get("trailing_sl_tier1_atr", 1.5)  # break-even trigger
        tier2_atr = self.config.get("trailing_sl_tier2_atr", 3.0)  # lock-profit trigger

        # Build a quick lookup of live positions for current prices
        live_by_base = {}
        for pos in open_positions:
            base = pos.get('ticker', '').split('_')[0].upper()
            live_by_base[base] = pos

        changed = False
        for ticker, trade in open_trades.items():
            entry_price = trade.get('entry_price', 0.0)
            current_sl  = trade.get('sl_price', 0.0)
            sl_order_id = trade.get('sl_order_id')
            t212_ticker = trade.get('t212_ticker', to_t212_ticker(ticker))
            qty         = trade.get('qty', 0)

            if not entry_price or not qty:
                continue

            # Get current price from live position snapshot
            base = ticker.split('_')[0].upper()
            pos = live_by_base.get(base)
            if not pos:
                continue
            current_price = pos.get('currentPrice') or pos.get('averagePrice', 0.0)
            if not current_price:
                continue

            # Get ATR for this ticker
            atr = self.strategy.get_current_atr(ticker) if self.strategy else 0.0
            if atr <= 0:
                continue

            profit = current_price - entry_price

            # Determine target SL level
            new_sl = None
            if profit >= tier2_atr * atr:
                # Tier 2: lock in profit at entry + 1.5ATR
                candidate = round(entry_price + tier1_atr * atr, 4)
                if candidate > current_sl:
                    new_sl = candidate
                    tier = 2
            elif profit >= tier1_atr * atr:
                # Tier 1: move to break-even
                candidate = round(entry_price, 4)
                if candidate > current_sl:
                    new_sl = candidate
                    tier = 1

            if new_sl is None:
                continue

            logger.info(
                f"[trail] {ticker} | price={current_price:.4f} entry={entry_price:.4f} "
                f"profit={profit:.4f} ({profit/entry_price*100:.1f}%) | "
                f"Tier {tier}: bumping SL {current_sl:.4f}  {new_sl:.4f}"
            )

            # Cancel old SL, place new one
            if sl_order_id:
                self.client.cancel_order(sl_order_id)

            sl_res = self.client.place_stop_order(t212_ticker, qty, new_sl)
            if sl_res and sl_res.get('id'):
                trade['sl_order_id'] = sl_res['id']
                trade['sl_price']    = new_sl
                logger.info(f"[trail] {ticker} SL updated to {new_sl:.4f}. New SL ID: {sl_res['id']}")
                changed = True
            else:
                logger.error(f"[trail] {ticker} Failed to place updated SL: {sl_res}")

        if changed:
            self.save_state()

    #  Signal Scoring 

    def score_signal(self, signal_data: dict) -> float:
        """
        Composite quality score for a BUY signal (higher = better).

        Components:
          60%  RSI room below threshold  (50 - RSI)   more oversold = stronger
          40%  BB distance              (bb_pct_below)  further below band = stronger

        Both are normalised so a combined score of 100 is a perfect setup
        (RSI = 0, price at maximum distance below lower band).
        The weighting can be tuned via config (rsi_score_weight, bb_score_weight).
        """
        rsi          = signal_data.get("rsi", 50.0)
        bb_pct_below = signal_data.get("bb_pct_below", 0.0)

        rsi_score = max(0.0, 50.0 - rsi)          # 0-50 range, higher = more oversold
        bb_score  = bb_pct_below                  # % below lower band, already 0+

        w_rsi = self.config.get("rsi_score_weight", 0.6)
        w_bb  = self.config.get("bb_score_weight",  0.4)

        final_score = round((rsi_score * w_rsi) + (bb_score * w_bb * 100), 4)
        logger.info(
            f"[{signal_data.get('ticker','SIGNAL')}] Scoring Math: "
            f"(RSI_Score {rsi_score:.2f} * {w_rsi}) + (BB_Score {bb_score:.2f} * {w_bb} * 100) = {final_score}"
        )
        return final_score

    #  Main Cycle 

    def run_cycle(self):
        """A single end-to-end iteration of the trading loop."""
        if self.config.get("bot_status") != "RUNNING":
            return

        if not self.init_clients():
            logger.warning("Bot is RUNNING but API credentials are missing.")
            return

        # 1. Kill-switch check
        try:
            cash_state         = self.client.get_account_cash()
            current_equity     = cash_state.get('total', 0.0)
            self._cycle_equity = current_equity   # cache for fixed-risk sizing in handle_buy
            if current_equity > 0 and self.check_kill_switch(current_equity):
                return
        except Exception as e:
            logger.error(f"Error fetching equity for kill switch: {e}")

        # 2. Snapshot positions + orders once per cycle (saves API calls)
        try:
            open_positions = self.client.get_open_positions()
            active_orders  = self.client.get_active_orders()
        except Exception as e:
            logger.error(f"Failed to fetch portfolio snapshot: {e}")
            open_positions, active_orders = [], []

        logger.info(
            f"Cycle start | Positions: {len(open_positions)} | "
            f"Pending orders: {len(active_orders)}"
        )

        # 3. Reconcile live positions
        self.sync_open_trades(open_positions)
        # 3b. Resume pending orders from previous run
        self.resume_pending_orders()
        # 3c. Trailing stop-loss bump
        self.check_trailing_stops(open_positions)

        # 4. Global hours check removed in favour of per-ticker check below.

        # 5. Max-positions guard
        positions_full = self.at_max_positions(open_positions, active_orders)

        # 5. Fetch available capital ONCE per cycle (avoids per-ticker 429 rate-limits)
        available_capital = self.get_available_capital()
        logger.info(f"Available capital this cycle: {available_capital:.2f}")

        # 6. Iterate tickers
        tickers = self.config.get("tickers", [])

        #  PHASE 1: Scan all tickers 
        # Collect every BUY signal and handle all SELLs before touching capital.
        buy_candidates   = []   # (score, ticker, signal_data)
        sell_tickers     = []
        this_cycle_buys  = set()  # tickers still showing BUY this cycle

        for ticker in tickers:
            # Per-ticker session check
            if not self.is_ticker_session_open(ticker):
                logger.info(f"[{ticker}] Market closed. Skipping.")
                continue

            logger.info(f"Analyzing {ticker}...")
            try:
                signal_data = self.strategy.analyze(ticker)
            except Exception as e:
                logger.error(f"[{ticker}] Strategy error: {e}", exc_info=True)
                time.sleep(1)
                continue

            signal = signal_data.get("signal")
            logger.info(f"[{ticker}] Signal: {signal} | {signal_data.get('reason', '')}")

            if signal == "BUY":
                this_cycle_buys.add(ticker)
                if self.is_on_cooldown(ticker):
                    pass
                elif self.already_in_trade(ticker, open_positions, active_orders):
                    pass
                else:
                    score = self.score_signal(signal_data)
                    buy_candidates.append((score, ticker, signal_data))
                    logger.info(f"[{ticker}] BUY queued | score={score:.2f}")

            elif signal == "SELL":
                sell_tickers.append(ticker)

            time.sleep(1)  # rate-limit between tickers

        #  Rebalance: cancel pending orders whose signal has gone stale 
        # If a pending order's ticker is no longer showing BUY this cycle
        # (recovered to WAIT/SELL), cancel it  the strategy no longer agrees.
        pending = self.state.get("pending_orders", {})
        cancelled_pending_tickers = []
        for order_id, meta in list(pending.items()):
            pending_ticker = meta.get("ticker", "")
            if pending_ticker and pending_ticker not in this_cycle_buys:
                logger.info(
                    f"[rebalance] {pending_ticker} no longer shows BUY  "
                    f"cancelling stale pending order {order_id}."
                )
                try:
                    cancelled = self.client.cancel_order(int(order_id))
                    if cancelled:
                        del pending[order_id]
                        cancelled_pending_tickers.append(pending_ticker)
                        logger.info(f"[rebalance] Order {order_id} ({pending_ticker}) cancelled.")
                    else:
                        logger.warning(f"[rebalance] Could not cancel {order_id}  may have just filled.")
                except Exception as e:
                    logger.error(f"[rebalance] Error cancelling order {order_id}: {e}")
        if cancelled_pending_tickers:
            self.save_state()
            logger.info(f"[rebalance] Freed {len(cancelled_pending_tickers)} slot(s) for better signals.")

        # Process all SELLs first (frees capital before we buy)
        for ticker in sell_tickers:
            self.handle_sell(ticker)

        #  PHASE 2: Rank, allocate and buy 
        # Market regime filter: skip all buys if market is in a confirmed downtrend
        market_bearish = self.is_market_bearish()

        if buy_candidates and not positions_full and not market_bearish:
            # Sort best score first
            buy_candidates.sort(key=lambda x: x[0], reverse=True)

            # Cap at remaining position slots (cancelled pendings free up slots)
            max_pos    = self.config.get("max_open_positions", 5)
            held_count = len({p.get('ticker') for p in open_positions})
            # Re-fetch active orders since we just cancelled some
            if cancelled_pending_tickers:
                try:
                    active_orders = self.client.get_active_orders()
                except Exception:
                    pass
            pending_count = len([o for o in active_orders
                                  if o.get('type', '').upper() in ('LIMIT', 'STOP')])
            slots_free = max(0, max_pos - held_count - pending_count)
            buy_candidates = buy_candidates[:slots_free]

            if buy_candidates:
                # Refresh capital (SELLs + cancellations may have freed cash)
                available_capital = self.get_available_capital()

                # Even split  each candidate gets an equal share
                per_trade_capital = available_capital / len(buy_candidates)
                logger.info(
                    f"Phase 2 | {len(buy_candidates)} BUY candidate(s) | "
                    f"Total capital {available_capital:.2f} | "
                    f"Per-trade allocation {per_trade_capital:.2f}"
                )

                for score, ticker, signal_data in buy_candidates:
                    logger.info(
                        f"[{ticker}] Executing BUY | score={score:.2f} | "
                        f"allocation={per_trade_capital:.2f}"
                    )
                    self.handle_buy(ticker, signal_data, per_trade_capital)
        elif market_bearish:
            logger.info("[regime] Market is bearish  all BUY signals suppressed this cycle.")
        elif positions_full:
            logger.info("Max positions reached  no new buys this cycle.")

    #  Entry Point 

    def start(self):
        logger.info("Trading Bot Daemon Started.")
        while True:
            self.config = self.load_config()
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"Unexpected error in run cycle: {e}", exc_info=True)

            for handler in logging.root.handlers:
                handler.flush()

            interval = self.config.get("cycle_interval_secs", 900)  # default 15 min = matches 15m candles
            logger.info(f"Next cycle in {interval}s ({interval//60}m {interval%60}s).")
            time.sleep(interval)


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()

