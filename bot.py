import time
import json
import logging
import logging.handlers
import math
from datetime import datetime, timezone
from pathlib import Path
from strategy import MeanReversionStrategy
from trading212_client import Trading212Client
from quant_inference import QuantInference

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
    
    # US Equities/ETFs
    # Heuristic: Most US ETFs on T212 use _US_ETF suffix
    _US_ETFS = {"GDX", "GLD", "SLV", "ARKK", "VUSA", "QQQ", "SPY", "XLE", "XLK", "ICLN", "IBIT", "FBTC"}
    if ticker in _US_ETFS:
        return f"{ticker}_US_ETF"
    
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

        # Per-run set of tickers the API has confirmed we do NOT own.
        # Used to prevent phantom positions being re-imported by sync_open_trades
        # and causing an infinite "selling-equity-not-owned" retry loop.
        self.purged_tickers: set = set()

        # Initialize AI Inference engine with config-defined path
        model_path = self.config.get("ml_model_path", None)
        self.quant_engine = QuantInference(model_path=model_path) if model_path else QuantInference()

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
          ticker_health     { ticker: { error_count, is_paused, last_error } }
        """
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r") as f:
                    s = json.load(f)
                    # Back-compat defaults
                    s.setdefault("peak_equity", 0.0)
                    s.setdefault("open_trades", {})
                    s.setdefault("cooldowns", {})
                    s.setdefault("ticker_health", {})
                    # Load purged_tickers into the set
                    self.purged_tickers = set(s.get("purged_tickers", []))
                    return s
            except Exception:
                pass
        self.purged_tickers = set()
        return {
            "peak_equity":   0.0,
            "open_trades":   {},
            "pending_orders": {},   # order_id -> {ticker, qty, sl_price, t212_ticker}
            "cooldowns":     {},
            "ticker_health":  {},
            "purged_tickers": [] 
        }

    def save_state(self):
        try:
            # Sync set back to list for JSON
            self.state["purged_tickers"] = list(self.purged_tickers) if hasattr(self, 'purged_tickers') else []
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
            logger.info("Lockdown complete. All positions liquidated. Bot LOCKED.")

    #  Error Helpers

    def is_equity_not_owned_error(self, res: dict) -> bool:
        """
        Returns True if the API response indicates a 'selling-equity-not-owned' error.
        We check the 'type' field and the '_status_code' added by our client.
        """
        if not isinstance(res, dict):
            return False
        
        # Check T212 native error type
        err_type = res.get('type', '')
        if isinstance(err_type, str) and "selling-equity-not-owned" in err_type:
            return True
        
        # Fallback: check status code and detail string if JSON is incomplete
        if res.get('_status_code') == 400:
            detail = str(res.get('detail', '')).lower()
            if "not owned" in detail or "selling more" in detail:
                return True
                
        return False

    #  Position Reconciliation 

    def sync_open_trades(self, open_positions: list, active_orders: list):
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
            qty = float(pos.get('quantity', 0))
            if t212_ticker not in tracked_t212 and qty > 0:
                # Derive the short ticker (strip _US_EQ suffix if present)
                short = t212_ticker.replace("_US_EQ", "").replace("_US_ETF", "")

                # Skip tickers that the API has already confirmed as not owned.
                # This prevents a position that was just closed (but still shows in the
                # portfolio snapshot due to API lag) from being re-imported and queued
                # for another sell, causing an infinite "selling-equity-not-owned" loop.
                if short in self.purged_tickers:
                    logger.debug(f"[sync] Skipping import of {short} — already blacklisted as phantom.")
                    continue

                avg_price = pos.get('averagePrice') or pos.get('currentPrice', 0.0)
                open_trades[short] = {
                    "qty":          qty,
                    "entry_price":  avg_price,
                    "sl_order_id":  None,   # unknown  was opened outside the bot
                    "t212_ticker":  t212_ticker,
                    "imported":     True,
                    "opened_at":    datetime.now(timezone.utc).isoformat()
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
            # Proactive purge: If it's in purged_tickers, remove it now.
            if short_ticker in self.purged_tickers:
                logger.warning(f"[sync] Cleaning up {short_ticker} from state — it is blacklisted.")
                stale.append(short_ticker)
                del open_trades[short_ticker]
                continue

            t212 = trade.get('t212_ticker', to_t212_ticker(short_ticker))
            pos  = live_by_t212.get(t212)
            # Remove if position is missing from API or has zero quantity
            if not pos or float(pos.get('quantity', 0)) <= 0:
                stale.append(short_ticker)
                del open_trades[short_ticker]

        if stale:
            logger.info(
                f"[sync] Removed {len(stale)} stale local trade(s) "
                f"(position closed externally): {', '.join(stale)}"
            )

        if imported or stale:
            self.save_state()

        # Verify that any tracked SL/TP IDs are actually live on the exchange
        live_orders_by_id = {str(o.get('id')): o for o in active_orders}
        for short_ticker, trade in open_trades.items():
            # Check SL
            sl_id = trade.get('sl_order_id')
            if sl_id and str(sl_id) not in live_orders_by_id:
                logger.warning(f"[{short_ticker}] Tracked SL {sl_id} not found on exchange. Clearing for fresh placement.")
                trade['sl_order_id'] = None
                
            # Check TP
            tp_id = trade.get('tp_order_id')
            if tp_id and str(tp_id) not in live_orders_by_id:
                logger.warning(f"[{short_ticker}] Tracked TP {tp_id} not found on exchange. Clearing for fresh placement.")
                trade['tp_order_id'] = None

            # Place brackets for any trades that don't have them
            if trade and (trade.get('sl_order_id') is None or trade.get('tp_order_id') is None):
                self.place_missing_brackets(short_ticker, trade)

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
                    # Order filled during downtime  promote and place SL/TP
                    logger.info(f"[resume] {ticker} filled during restart gap. Placing SL @ ${sl_price:.4f}")
                    sl_res = self.client.place_stop_order(t212, qty, sl_price)
                    
                    tp_price = meta.get("tp_price")
                    tp_res = None
                    if tp_price and tp_price > meta.get("entry_price", 0.0):
                        tp_res = self.client.place_limit_sell(t212, qty, float(tp_price))

                    if sl_res and sl_res.get('id'):
                        sl_id = sl_res['id']
                        tp_id = tp_res.get('id') if tp_res else None
                        self.state.setdefault("open_trades", {})[ticker] = {
                            "qty":         qty,
                            "entry_price": meta.get("entry_price", 0.0),
                            "sl_order_id": sl_id,
                            "sl_price":    sl_price,
                            "tp_order_id": tp_id,
                            "tp_price":    tp_price,
                            "t212_ticker": t212
                        }
                        logger.info(f"[resume] Brackets placed for {ticker}. SL ID: {sl_id} " + (f"| TP ID: {tp_id}" if tp_id else ""))
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

    def place_missing_brackets(self, ticker: str, trade: dict):
        """
        Calculate and place SL for a position imported without one.
        Take Profit is stored as a virtual target only (T212 reserves shares
        for the first sell order, making a second standalone sell impossible).
        """
        entry_price = trade.get('entry_price', 0.0)
        qty         = trade.get('qty', 0)
        t212_ticker = trade.get('t212_ticker', to_t212_ticker(ticker))

        if not entry_price or not qty:
            return

        # 1. STOP LOSS (physical order on exchange)
        if not trade.get('sl_order_id'):
            stop_pct = self.config.get('stop_loss_pct', 0.02)
            pct_stop = round(entry_price * (1.0 - stop_pct), 4)
            atr_stop = 0.0
            if self.strategy:
                atr = self.strategy.get_current_atr(ticker, multiplier=1.0)
                if atr > 0:
                    atr_stop = round(entry_price - atr, 4)
            
            stop_price = min(pct_stop, atr_stop) if atr_stop > 0 else pct_stop
            sl_res = self.client.place_stop_order(t212_ticker, qty, stop_price)
            if sl_res and sl_res.get('id'):
                trade['sl_order_id'] = sl_res['id']
                trade['sl_price']    = stop_price
                logger.info(f"[{ticker}] Catch-up SL placed @ {stop_price:.4f}")
            elif self.is_equity_not_owned_error(sl_res):
                logger.warning(f"[{ticker}] Sync SL failed: API confirms equity not owned. Purging.")
                self.purged_tickers.add(ticker)
                self.state.get("open_trades", {}).pop(ticker, None)
                self.save_state()

        # 2. TAKE PROFIT (virtual — stored locally, monitored by check_virtual_tp)
        # T212 reserves shares for the SL order, preventing a second sell order.
        if not trade.get('tp_price') and self.strategy:
            try:
                analysis = self.strategy.analyze(ticker)
                target_tp = analysis.get('target_tp')
                if not target_tp or target_tp <= entry_price:
                    target_tp = entry_price * 1.015
                trade['tp_price'] = round(float(target_tp), 4)
                logger.info(f"[{ticker}] Virtual TP target set @ {trade['tp_price']:.4f} (monitored by bot)")
            except Exception as e:
                logger.warning(f"[{ticker}] Failed to set virtual TP target: {e}")

        self.save_state()

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

    #  Order Execution 
    def handle_buy(self, ticker: str, signal_data: dict, available_capital: float):
        """
        Places a limit BUY order for the given ticker using a fixed-risk sizing model.
        Stop-loss: ATR-based (entry - sl_atr_multiplier * ATR).
        Position size: fixed risk amount / SL distance.
        After fill: both a stop-loss AND a take-profit limit order are placed.
        """
        price     = float(signal_data.get("price", 0.0))
        atr       = float(signal_data.get("atr", 0.0))

        # Dynamic Take-Profit overrides strategy TP
        tp_atr_mult = float(self.config.get("tp_atr_multiplier", 2.0))
        if atr > 0:
            target_tp = round(price + (atr * tp_atr_mult), 4)
        else:
            target_tp = float(signal_data.get("target_tp", 0.0))

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

        # Position sizing (Fixed vs Quant/Kelly)
        total_equity  = float(getattr(self, '_cycle_equity', available_capital))
        quant_enabled = self.config.get("quant_sizing_enabled", False)
        
        tp_distance = target_tp - price
        
        if quant_enabled and hasattr(self, 'quant_engine'):
            win_prob = float(signal_data.get("ai_win_prob", 0.55))
            if tp_distance > 0 and sl_distance > 0:
                reward_risk_ratio = tp_distance / sl_distance
                kelly_frac = float(self.config.get("kelly_fraction", 0.25))
                raw_kelly = self.quant_engine.calculate_kelly_fraction(
                    win_prob=win_prob, 
                    reward_risk_ratio=reward_risk_ratio,
                    max_allocation=0.05
                )
                risk_pct = raw_kelly * kelly_frac
                logger.info(f"[{ticker}] 🤖 Quant Sizing: WinProb={win_prob:.2f}, R:R={reward_risk_ratio:.2f} -> Base Kelly {raw_kelly*100:.2f}% | Applied ({kelly_frac}x) -> {risk_pct*100:.2f}%")
            else:
                risk_pct = float(self.config.get("risk_per_trade_pct", 0.01))
        else:
            risk_pct = float(self.config.get("risk_per_trade_pct", 0.01))
            
        risk_amount   = total_equity * risk_pct
        risk_qty      = round(risk_amount / sl_distance, 2)  # T212 max precision = 2 d.p.
        max_qty       = round(available_capital / price, 2)
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
            "tp_price":     target_tp,
        }
        self.save_state()

        # Poll for fill
        fill_timeout = self.config.get("order_fill_timeout_secs", 60)
        filled = self.wait_for_fill(order_id, t212_ticker=t212_ticker, timeout_secs=fill_timeout)

        if filled:
            sl_res = self.client.place_stop_order(
                ticker=t212_ticker,
                quantity=quantity,
                stop_price=stop_loss_price
            )

            if sl_res and sl_res.get('id'):
                sl_id = sl_res['id']
                logger.info(
                    f"[{ticker}] SL placed @ {stop_loss_price:.4f} (ID: {sl_id}) | "
                    f"Virtual TP target: {target_tp:.4f} (bot-monitored, 60s heartbeat)"
                )
                # Promote from pending to open_trades
                self.state.setdefault("open_trades", {})[ticker] = {
                    "qty":          quantity,
                    "entry_price":  limit_price,
                    "sl_order_id":  sl_id,
                    "sl_price":     stop_loss_price,
                    "tp_price":     float(target_tp) if target_tp else None,  # Virtual TP
                    "t212_ticker":  t212_ticker,
                    "opened_at":    datetime.now(timezone.utc).isoformat()
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

    def wait_for_fill(self, order_id: str, t212_ticker: str = None, timeout_secs: int = 60) -> bool:
        """
        Poll the API until the order is FILLED or the timeout is reached.
        Returns True if filled, False otherwise.
        """
        start_t = time.time()
        
        while (time.time() - start_t) < timeout_secs:
            try:
                # 1. Position Check (most robust against eventual consistency 404s)
                if t212_ticker:
                    positions = self.client.get_open_positions()
                    if any(p.get('ticker') == t212_ticker for p in positions):
                        logger.info(f"[{t212_ticker}] Fill confirmed via position check.")
                        return True
                
                # 2. Order Status Check (fallback)
                order = self.client.get_order_by_id(order_id)
                status = order.get("status", "")
                
                # If status is an int (like 404) or missing, it's an API error, not a trade status
                if isinstance(status, str):
                    status_upper = status.upper()
                    if status_upper == "FILLED":
                        logger.info(f"[{t212_ticker}] Fill confirmed via order status FILLED.")
                        return True
                    elif status_upper in ("REJECTED", "CANCELLED", "EXPIRED"):
                        logger.warning(f"[{t212_ticker}] Order {status_upper} during fill-wait.")
                        return False
                elif order.get("_status_code") == 404:
                    # Order no longer exists  implies it may have filled and moved to history
                    # We rely on the Position Check above to confirm.
                    pass
                
            except Exception as e:
                logger.error(f"[fill] Error polling order {order_id} ({t212_ticker}): {e}")

            time.sleep(FILL_POLL_INTERVAL)

        logger.warning(f"[fill] Timeout waiting for order {order_id} ({t212_ticker}) to fill.")
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
        Checks if the session is currently open.
        Trading212 supports 24/5 trading for most US equities, 
        so we simplify this to allow weekday trading and block weekends.
        """
        if not self.config.get("market_hours_check", True):
            return True
            
        try:
            now_utc = datetime.now(timezone.utc)
            
            # Crypto is 24/7
            if ticker.endswith("-USD") or "-USD" in ticker:
                return True
                
            # Equities are 24/5 on Trading212
            # Block Saturday (5) and Sunday (6)
            if now_utc.weekday() >= 5:
                return False
                
            return True

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
            import math
            df = yf.download(regime_ticker, period="90d", interval="1d", progress=False)
            if df.empty or len(df) < 50:
                logger.warning("[regime] Not enough data to evaluate market regime  allowing BUYs.")
                return False
            if isinstance(df.columns, __import__('pandas').MultiIndex):
                df.columns = df.columns.droplevel(1)
            sma50 = float(df['Close'].rolling(50).mean().iloc[-1])
            current = float(df['Close'].iloc[-1])

            if math.isnan(current) or math.isnan(sma50):
                logger.warning(f"[regime] {regime_ticker} returned NaN. Defaulting to BEARISH to suppress risky BUYs.")
                return True

            bearish = current < sma50
            logger.info(
                f"[regime] {regime_ticker} @ {current:.2f} vs 50-SMA {sma50:.2f} "
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
          Tier 1   price  entry + 1.5ATR    move SL to break-even (entry price + buffer)
          Tier 2   price  entry + 3.0ATR    move SL to entry + 1.5ATR (lock profit)
        """
        open_trades = self.state.get("open_trades", {})
        if not open_trades:
            return

        tier1_atr = self.config.get("trailing_sl_tier1_atr", 1.5)  # break-even trigger
        tier2_atr = self.config.get("trailing_sl_tier2_atr", 3.0)  # lock-profit trigger
        be_buffer = self.config.get("breakeven_buffer_pct", 0.002) # fee protection

        # Build a quick lookup of live positions for current prices
        live_by_base = {}
        for pos in open_positions:
            base = pos.get('ticker', '').split('_')[0].upper()
            live_by_base[base] = pos

        changed = False
        for ticker, trade in open_trades.items():
            entry_price = float(trade.get('entry_price', 0.0))
            current_sl  = float(trade.get('sl_price', 0.0))
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
            current_price = float(pos.get('currentPrice') or pos.get('averagePrice', 0.0))
            if not current_price:
                continue

            # Get ATR for this ticker
            atr = self.strategy.get_current_atr(ticker) if self.strategy else (current_price * 0.01)
            if atr <= 0:
                continue

            # Calculate target SL prices based on tiers
            # Tier 1: Move to Break-Even (Entry + Buffer)
            target_t1 = round(entry_price * (1.0 + be_buffer), 4)
            # Tier 2: Lock profit at Entry + 1.5 ATR
            target_t2 = round(entry_price + (1.5 * atr), 4)

            new_sl = None
            tier = 0
            if current_price >= (entry_price + (tier2_atr * atr)):
                if target_t2 > current_sl:
                    new_sl = target_t2
                    tier = 2
            elif current_price >= (entry_price + (tier1_atr * atr)):
                if target_t1 > current_sl:
                    new_sl = target_t1
                    tier = 1

            if new_sl is None:
                continue

            logger.info(
                f"[trail] {ticker} | price={current_price:.4f} entry={entry_price:.4f} | "
                f"Tier {tier}: bumping SL {current_sl:.4f} -> {new_sl:.4f}"
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
                if self.is_equity_not_owned_error(sl_res):
                    open_trades.pop(ticker, None)
                    self.purged_tickers.add(ticker)
                    changed = True

        if changed:
            self.save_state()

    def check_trade_duration(self, open_positions: list):
        """
        Check if any trade has been open for longer than the maximum allowed duration.
        If it has, and it hasn't hit TP/SL, perform an emergency exit.
        """
        open_trades = self.state.get("open_trades", {})
        if not open_trades: return

        max_hours = self.config.get("max_trade_duration_hours", 48)
        now = datetime.now(timezone.utc)

        for ticker, trade in list(open_trades.items()):
            opened_str = trade.get("opened_at")
            if not opened_str: continue

            try:
                opened_dt = datetime.fromisoformat(opened_str)
                age_hours = (now - opened_dt).total_seconds() / 3600
                if age_hours >= max_hours:
                    logger.warning(f"[stale] {ticker} hit max duration ({age_hours:.1f}h >= {max_hours}h). Executing emergency exit.")
                    self.handle_sell(ticker, "Target (Stale Exit)")
            except Exception as e:
                logger.error(f"[stale] Error checking age for {ticker}: {e}")

    def check_virtual_tp(self, open_positions: list):
        """
        Check live prices against 'tp_price' stored in open_trades.
        If dynamic_tp_enabled is True, it starts a tight trailing stop instead of immediate sell.
        """
        open_trades = self.state.get("open_trades", {})
        dynamic_tp  = self.config.get("dynamic_tp_enabled", True)
        
        # Build lookup for live prices
        live_prices = {}
        for pos in open_positions:
            t = pos.get('ticker', '').replace("_US_EQ", "").replace("_US_ETF", "")
            live_prices[t] = float(pos.get('currentPrice') or pos.get('averagePrice', 0.0))

        for ticker, trade in list(open_trades.items()):
            tp_price    = trade.get('tp_price')
            sl_order_id = trade.get('sl_order_id')
            t212_ticker = trade.get('t212_ticker', to_t212_ticker(ticker))
            qty         = trade.get('qty', 0)
            
            if not tp_price or ticker not in live_prices:
                continue

            current_p = live_prices[ticker]

            # 1. Check if we are currently "Chasing" (Dynamic TP)
            if trade.get("is_chasing"):
                chase_sl = trade.get("chase_sl", 0.0)
                if current_p < chase_sl:
                    logger.info(f"[vTP] {ticker} Profit Chasing ended (Dip below {chase_sl:.4f}). Selling.")
                    # Sell logic follows below
                else:
                    # Trailing upward: Update chase_sl with 0.5 ATR
                    atr = self.strategy.get_current_atr(ticker) if self.strategy else (current_p * 0.01)
                    new_chase_sl = round(current_p - (0.5 * atr), 4)
                    if new_chase_sl > chase_sl:
                        trade["chase_sl"] = new_chase_sl
                        logger.debug(f"[vTP] {ticker} Chasing: Bumped SL to {new_chase_sl:.4f}")
                    continue # Keep riding the wave

            # 2. Check if target hit for the first time
            if current_p >= tp_price:
                if dynamic_tp:
                    if not trade.get("is_chasing"):
                        logger.info(f"[vTP] {ticker} Target reached ({current_p:.4f} >= {tp_price:.4f}). PHASE 2: Chasing Profit...")
                        trade["is_chasing"] = True
                        atr = self.strategy.get_current_atr(ticker) if self.strategy else (current_p * 0.01)
                        trade["chase_sl"] = round(current_p - (0.5 * atr), 4)
                        self.save_state()
                        continue
                else:
                    logger.info(f"[vTP] {ticker} Virtual TP Triggered ({current_p:.4f} >= {tp_price:.4f}). Selling.")

                # Cancel SL order first to free reserved shares
                if sl_order_id:
                    self.client.cancel_order(sl_order_id)

                # Step 2: Market sell to lock profit
                res = self.client.place_market_sell(t212_ticker, qty)
                if res and res.get('id'):
                    logger.info(f"[vTP] {ticker} Market SELL submitted. ID: {res['id']}")
                    # Record Realised P&L
                    try:
                        _ent = float(trade.get("entry_price", 0))
                        _qt  = float(qty)
                        _rpnl = round((current_p - _ent) * _qt, 4) if _ent > 0 else 0.0
                        self.state.setdefault("realised_pnl", []).append({
                            "ticker":    ticker,
                            "pnl":       _rpnl,
                            "entry":     round(_ent, 4),
                            "exit":      round(current_p, 4),
                            "qty":       _qt,
                            "reason":    "Virtual TP (Dynamic)" if trade.get("is_chasing") else "Virtual TP",
                            "closed_at": datetime.now(timezone.utc).isoformat(),
                        })
                        logger.info(f"[vTP] Realised P&L: {ticker} -> £{_rpnl:+.4f}")
                    except Exception as _pnl_err:
                        logger.warning(f"[vTP] P&L recording failed for {ticker}: {_pnl_err}")

                    del self.state["open_trades"][ticker]
                    self.state.setdefault("cooldowns", {})[ticker] = datetime.now(timezone.utc).isoformat()
                    self.save_state()
                else:
                    logger.error(f"[vTP] {ticker} Market SELL FAILED: {res}")
                    if self.is_equity_not_owned_error(res):
                        self.state.get("open_trades", {}).pop(ticker, None)
                        self.purged_tickers.add(ticker)
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
        self.sync_open_trades(open_positions, active_orders)
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

        # 5b. Pre-fetch benchmarks for Phase 4 SRS ONCE per cycle
        benchmark_dfs_1d = {}
        benchmark_dfs_15m = {}
        if getattr(self, 'quant_engine', None) and self.quant_engine.is_ai_active():
            for bm in ["SPY", "QQQ", "IWM"]:
                try:
                    b_1d = self.strategy.get_historical_data(bm, interval="1d", period="5d")
                    if not b_1d.empty: benchmark_dfs_1d[bm] = b_1d
                    
                    b_15m = self.strategy.get_historical_data(bm, interval="15m", period="5d")
                    if not b_15m.empty: benchmark_dfs_15m[bm] = b_15m
                except Exception as e:
                    logger.warning(f"[Benchmarks] Failed to pre-fetch {bm}: {e}")

        # 6. Iterate tickers
        tickers = self.config.get("tickers", [])

        #  PHASE 1: Scan all tickers 
        # Collect every BUY signal and handle all SELLs before touching capital.
        buy_candidates   = []   # (score, ticker, signal_data)
        sell_tickers     = []
        this_cycle_buys  = set()  # tickers still showing BUY this cycle

        for ticker in tickers:
            # Ticker Health Check
            health = self.state.setdefault("ticker_health", {}).setdefault(ticker, {"error_count": 0, "is_paused": False})
            if health.get("is_paused"):
                logger.info(f"[{ticker}] Ticker is PAUSED due to persistent errors. Skipping.")
                continue

            # Per-ticker session check
            if not self.is_ticker_session_open(ticker):
                logger.info(f"[{ticker}] Market closed. Skipping.")
                continue

            logger.info(f"Analyzing {ticker}...")
            try:
                signal_data = self.strategy.analyze(
                    ticker, 
                    quant_engine=getattr(self, 'quant_engine', None),
                    benchmarks_1d=benchmark_dfs_1d,
                    benchmarks_15m=benchmark_dfs_15m
                )
                
                # Treat 'NEUTRAL' signals (which represent NaN/no data) as analysis errors 
                # so they increment error_count and get paused if persistent.
                if signal_data.get("signal") == "NEUTRAL":
                    raise ValueError(f"Invalid data or calculation failed: {signal_data.get('reason', 'Unknown')}")
                
                # [AI Visibility] Log the win probability for the user dashboard
                ai_prob = signal_data.get('ai_win_prob')
                ai_log = f" [AI: {ai_prob:.4f}]" if ai_prob is not None else " [AI: OFF]"
                logger.info(f"[{ticker}] Analysis complete.{ai_log}")
                
                # Reset error count on success
                health["error_count"] = 0
                health["is_paused"] = False
            except Exception as e:
                health["error_count"] = health.get("error_count", 0) + 1
                health["last_error"]  = str(e)
                if health["error_count"] >= 3:
                    health["is_paused"] = True
                    logger.error(f"⚠️ [{ticker}] CRITICAL: 3 consecutive errors. PAUSING ticker for user review: {e}")
                else:
                    logger.error(f"[{ticker}] Strategy error (attempt {health['error_count']}/3): {e}", exc_info=True)
                
                self.save_state()
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

        if buy_candidates and not positions_full:
            if market_bearish:
                # Only allow AI-driven high confidence signals to pass the bear filter
                buy_candidates = [c for c in buy_candidates if c[2].get('ai_win_prob', 0) >= 0.65]
                if not buy_candidates:
                    logger.info("[regime] Market is bearish - no high-confidence AI signals. Suppressing all BUYs.")
                    return
                logger.info(f"[regime] Market is bearish, but found {len(buy_candidates)} High-Confidence AI signals. Bypassing filter.")

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

            interval = self.config.get("cycle_interval_secs", 900)  # default 15 min
            logger.info(f"Next cycle in {interval}s ({interval//60}m {interval%60}s).")

            # Heartbeat: check Virtual TPs according to config (default 30s)
            # Fetch positions once per heartbeat to stay efficient.
            heartbeat_secs = self.config.get("heartbeat_interval_secs", 30)
            elapsed = 0
            while elapsed < interval:
                time.sleep(heartbeat_secs)
                elapsed += heartbeat_secs
                
                # Check shutdown or skip if cycle is about to start
                if elapsed >= interval:
                    break

                try:
                    positions = self.client.get_open_positions()
                    if positions:
                        self.check_virtual_tp(positions)
                        self.check_trailing_stops(positions) 
                        self.check_trade_duration(positions)
                except Exception as e:
                    logger.warning(f"[heartbeat] Monitor cycle failed: {e}")


if __name__ == "__main__":
    bot = TradingBot()
    bot.start()

