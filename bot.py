import time
import json
import logging
import logging.handlers
import math
from datetime import datetime, timezone
from pathlib import Path
from strategy import MeanReversionStrategy
from trading212_client import Trading212Client

# ── Logging Setup ──────────────────────────────────────────────────────────────
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

# ── Ticker Helpers ─────────────────────────────────────────────────────────────
# yfinance uses bare tickers (COIN); Trading212 v0 needs the full instrument code.
# Tickers in config that already contain "_" are assumed to be pre-qualified.
_NON_EQUITY = {"BTC-USD", "ETH-USD"}  # crypto pairs handled differently by T212

def to_t212_ticker(ticker: str) -> str:
    """Convert a bare yfinance-style ticker to the Trading212 instrument code."""
    if "_" in ticker or ticker in _NON_EQUITY:
        return ticker
    return f"{ticker}_US_EQ"

# ── Constants ──────────────────────────────────────────────────────────────────
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

    # ── Config / State I/O ─────────────────────────────────────────────────────

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
          peak_equity      – highest total equity seen (for kill switch)
          open_trades      – { ticker: { qty, sl_order_id, entry_price } }
          cooldowns        – { ticker: ISO-timestamp of last close }
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

    # ── Initialisation ─────────────────────────────────────────────────────────

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
            rsi_threshold=self.config.get("rsi_threshold", 30)
        )
        return True

    # ── Kill Switch ────────────────────────────────────────────────────────────

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

    # ── Position Reconciliation ────────────────────────────────────────────────

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

        # ── Import untracked live positions ────────────────────────────────────
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
                    "sl_order_id":  None,   # unknown – was opened outside the bot
                    "t212_ticker":  t212_ticker,
                    "imported":     True
                }
                imported.append(f"{short} (qty={qty} @ {avg_price:.2f})")

        if imported:
            logger.info(
                f"[sync] Imported {len(imported)} untracked position(s): "
                + ", ".join(imported)
            )

        # ── Remove stale local records ─────────────────────────────────────────
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

          FILLED    → promote to open_trades, place stop-loss, remove from pending
          CANCELLED/REJECTED/EXPIRED → clean up, remove from pending
          still WORKING/PLACED       → leave it; already_in_trade() will skip a new BUY

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
                    # Order filled during downtime — promote and place SL
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

                # WORKING / PLACED / PARTIALLY_FILLED → leave in pending; skip re-buy

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
                f"[{ticker}] Cannot place SL – missing entry price or qty in state."
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
            method = f"ATR({atr_stop:.4f}) vs Pct({pct_stop:.4f}) → chose {stop_price:.4f}"
        else:
            stop_price = pct_stop
            method = f"Pct-based only ({pct_stop:.4f}) – ATR unavailable"

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

    # ── Pre-Trade Checks ───────────────────────────────────────────────────────

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
            logger.info(f"[{ticker}] Already have an open position – skipping BUY.")
            return True
        if any(base(o.get('ticker', '')) == our_base for o in active_orders):
            logger.info(f"[{ticker}] Already have a pending order – skipping BUY.")
            return True
        if ticker in self.state.get("open_trades", {}):
            logger.info(f"[{ticker}] Locally tracked open trade exists – skipping BUY.")
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
                f"({total} held/pending) – no new buys this cycle."
            )
            return True
        return False

    # ── Order Execution ────────────────────────────────────────────────────────

    def wait_for_fill(self, order_id, timeout_secs: int = 60) -> bool:
        """
        Polls /equity/orders/{order_id} until either:
          - status == 'FILLED' → returns True
          - status == 'CANCELLED' / 'REJECTED' → returns False
          - timeout expires → returns False (order remains open as DAY order)
        """
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            order = self.client.get_order_by_id(order_id)
            status = order.get("status", "").upper()
            if status == "FILLED":
                logger.info(f"Order {order_id} FILLED.")
                return True
            if status in ("CANCELLED", "REJECTED"):
                logger.warning(f"Order {order_id} ended with status {status}.")
                return False
            logger.debug(f"Order {order_id} status={status} – waiting {FILL_POLL_INTERVAL}s…")
            time.sleep(FILL_POLL_INTERVAL)

        logger.warning(
            f"Order {order_id} did not fill within {timeout_secs}s. "
            f"It remains as a DAY order; stop-loss not yet placed."
        )
        return False

    def handle_buy(self, ticker: str, signal_data: dict, available_capital: float):
        """Full BUY flow: size → place limit → poll fill → place SL."""
        price     = signal_data.get("price", 0.0)
        target_tp = signal_data.get("target_tp", 0.0)

        if price <= 0 or available_capital < price:
            logger.info(
                f"[{ticker}] Insufficient capital for 1 share "
                f"(available={available_capital:.2f}, price={price:.2f})."
            )
            return

        quantity       = math.floor(available_capital / price)
        limit_price    = price
        stop_pct       = self.config.get("stop_loss_pct", 0.02)
        stop_loss_price = round(limit_price * (1.0 - stop_pct), 4)
        t212_ticker    = to_t212_ticker(ticker)

        logger.info(
            f"[{ticker}] Placing Limit BUY | t212={t212_ticker} | "
            f"Qty={quantity} @ ${limit_price:.2f} | "
            f"SL=${stop_loss_price:.2f} | TP=${target_tp:.2f}"
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

        # ── Track the pending order immediately so restarts won't double-buy ──
        stop_pct_check = self.config.get("stop_loss_pct", 0.02)
        pending_sl     = round(limit_price * (1.0 - stop_pct_check), 4)
        self.state.setdefault("pending_orders", {})[str(order_id)] = {
            "ticker":       ticker,
            "t212_ticker":  t212_ticker,
            "qty":          quantity,
            "entry_price":  limit_price,
            "sl_price":     stop_loss_price,
        }
        self.save_state()

        # ── Poll for fill before attaching stop-loss ──────────────────────────
        fill_timeout = self.config.get("order_fill_timeout_secs", 60)
        filled = self.wait_for_fill(order_id, timeout_secs=fill_timeout)

        if filled:
            sl_res = self.client.place_stop_order(
                ticker=t212_ticker,
                quantity=quantity,
                stop_price=stop_loss_price
            )
            if sl_res and sl_res.get('id'):
                sl_id = sl_res['id']
                logger.info(
                    f"[{ticker}] Stop-Loss placed. SL ID: {sl_id} @ ${stop_loss_price:.2f}"
                )
                # Promote from pending → open_trades
                self.state.setdefault("open_trades", {})[ticker] = {
                    "qty":         quantity,
                    "entry_price": limit_price,
                    "sl_order_id": sl_id,
                    "sl_price":    stop_loss_price,
                    "t212_ticker": t212_ticker
                }
                self.state.get("pending_orders", {}).pop(str(order_id), None)
                self.save_state()
            else:
                logger.warning(
                    f"[{ticker}] FILLED but stop-loss order FAILED. "
                    f"Manual SL at ${stop_loss_price:.2f} STRONGLY recommended!"
                )
        else:
            logger.warning(
                f"[{ticker}] Limit BUY not yet filled after {self.config.get('order_fill_timeout_secs',60)}s. "
                f"Order remains live on exchange. SL will be placed on next restart or when fill is confirmed."
            )

    def handle_sell(self, ticker: str):
        """
        If we hold a tracked position in this ticker, close it at market and
        cancel any associated stop-loss order, then set a cooldown.
        """
        open_trades = self.state.get("open_trades", {})
        if ticker not in open_trades:
            return  # We don't actually hold this ticker; strategy is just observing

        trade = open_trades[ticker]
        t212_ticker = trade.get("t212_ticker", to_t212_ticker(ticker))
        qty         = trade.get("qty", 0)
        sl_id       = trade.get("sl_order_id")

        logger.info(
            f"[{ticker}] SELL signal – closing position "
            f"(Qty={qty} @ market, cancelling SL {sl_id})"
        )

        # Cancel the standing stop-loss first to avoid double-sell
        if sl_id:
            if self.client.cancel_order(sl_id):
                logger.info(f"[{ticker}] SL order {sl_id} cancelled.")
            else:
                logger.warning(
                    f"[{ticker}] Could not cancel SL order {sl_id} – "
                    f"may already have triggered."
                )

        # Market sell
        res = self.client.place_market_sell(t212_ticker, qty)
        if res and res.get('id'):
            logger.info(
                f"[{ticker}] Market SELL submitted. Order ID: {res['id']}"
            )
        else:
            logger.error(
                f"[{ticker}] Market SELL failed: {res}. "
                f"MANUAL CLOSE REQUIRED."
            )

        # Clean up state and set cooldown
        del open_trades[ticker]
        self.state.setdefault("cooldowns", {})[ticker] = (
            datetime.now(timezone.utc).isoformat()
        )
        self.save_state()

    # ── Main Cycle ─────────────────────────────────────────────────────────────

    def run_cycle(self):
        """A single end-to-end iteration of the trading loop."""
        if self.config.get("bot_status") != "RUNNING":
            return

        if not self.init_clients():
            logger.warning("Bot is RUNNING but API credentials are missing.")
            return

        # 1. Kill-switch check
        try:
            cash_state     = self.client.get_account_cash()
            current_equity = cash_state.get('total', 0.0)
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

        # 3. Reconcile live positions with local state (imports pre-existing trades)
        self.sync_open_trades(open_positions)

        # 3b. Resume any pending orders from a previous run (prevents double-buy on restart)
        self.resume_pending_orders()

        # 4. Max-positions guard for buys
        positions_full = self.at_max_positions(open_positions, active_orders)

        # 5. Fetch available capital ONCE per cycle (avoids per-ticker 429 rate-limits)
        available_capital = self.get_available_capital()
        logger.info(f"Available capital this cycle: £{available_capital:.2f}")

        # 6. Iterate tickers
        tickers = self.config.get("tickers", [])
        for ticker in tickers:
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
                if positions_full:
                    logger.info(f"[{ticker}] BUY skipped – max positions reached.")
                elif self.is_on_cooldown(ticker):
                    pass  # already logged inside is_on_cooldown
                elif self.already_in_trade(ticker, open_positions, active_orders):
                    pass  # already logged inside already_in_trade
                else:
                    self.handle_buy(ticker, signal_data, available_capital)
                    # Refresh capital after a buy attempt so next ticker sees updated balance
                    available_capital = self.get_available_capital()

            elif signal == "SELL":
                self.handle_sell(ticker)

            time.sleep(1)  # rate-limit between tickers

    # ── Entry Point ────────────────────────────────────────────────────────────

    def start(self):
        logger.info("Trading Bot Daemon Started.")
        while True:
            self.config = self.load_config()
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"Unexpected error in run cycle: {e}", exc_info=True)

            # Flush log handlers so the dashboard/files stay current
            for handler in logging.root.handlers:
                handler.flush()

            time.sleep(60)  # 1-minute loop


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
