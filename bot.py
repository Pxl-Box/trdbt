import time
import json
import logging
import math
from pathlib import Path
from strategy import MeanReversionStrategy
from trading212_client import Trading212Client

# Set up logging for both console and file (which the dashboard can read if needed)
file_handler = logging.FileHandler("bot.log")
file_handler.terminator = "\n"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        file_handler,
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("bot")

# Force immediate flush for Streamlit responsiveness
for handler in logging.root.handlers:
    handler.flush = lambda: [h.flush() for h in logging.root.handlers]
logger = logging.getLogger("bot")

CONFIG_FILE = "config.json"
STATE_FILE = "bot_state.json"

class TradingBot:
    def __init__(self):
        self.config = self.load_config()
        self.state = self.load_state()
        self.client = None
        self.strategy = None

    def load_config(self):
        """ Dynamically loads config.json. """
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
        """ Loads state variables like peak equity for the Kill Switch. """
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"peak_equity": 0.0}

    def save_state(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(self.state, f, indent=4)
        except Exception as e:
            logger.error(f"Error saving state: {e}")

    def init_clients(self):
        """ Initialize or Re-initialize API Clients if config changed. """
        api_key = self.config.get("api_key", "")
        mode = self.config.get("api_mode", "Practice")
        
        if not api_key:
            return False
            
        self.client = Trading212Client(api_key, mode)
        
        # Initialize strategy with current config params
        self.strategy = MeanReversionStrategy(
            bb_length=self.config.get("bb_length", 20),
            bb_std=self.config.get("bb_std", 2.0),
            rsi_length=self.config.get("rsi_length", 14),
            rsi_threshold=self.config.get("rsi_threshold", 30)
        )
        return True

    def check_kill_switch(self, current_equity: float) -> bool:
        """
        Global Kill Switch Logic:
        If total account equity drops by more than limit (e.g., 5%) from its peak,
        trigger termination protocols.
        """
        peak = self.state.get("peak_equity", 0.0)
        if current_equity > peak:
            self.state["peak_equity"] = current_equity
            self.save_state()
            return False
            
        drop_pct = (peak - current_equity) / peak if peak > 0 else 0
        limit_pct = self.config.get("kill_switch_drop_pct", 0.05)
        
        if drop_pct >= limit_pct:
            logger.critical(f"KILL SWITCH TRIGGERED! Equity dropped {drop_pct*100:.2f}% from peak {peak:.2f}")
            self.lock_down()
            return True
            
        return False

    def lock_down(self):
        """ Executes the Kill Switch protocol: cancel all, sell all, LOCK. """
        logger.info("Executing Lockdown Protocol...")
        self.config["bot_status"] = "LOCKED"
        self.save_config()
        
        if self.client:
            self.client.cancel_all_orders()
            self.client.market_sell_all_positions()
            logger.info("Lockdown complete. All open positions liquidated. Bot LOCKED.")

    def check_account(self) -> float:
        """
        Pre-Trade Validation:
        Returns exactly 95% of available buying power (free cash).
        """
        try:
            cash_state = self.client.get_account_cash()
            free_cash = cash_state.get('free', 0.0)
            target_pct = self.config.get("capital_utilization_pct", 0.95)
            # Math: $Account_Free * 0.95
            allowed_cash = free_cash * target_pct
            return allowed_cash
        except Exception as e:
            logger.error(f"Failed to fetch account cash constraints: {e}")
            return 0.0

    def run_cycle(self):
        """ A single iteration of the trading loop. """
        # Only proceed if bot is allowed to run
        if self.config.get("bot_status") != "RUNNING":
            return
            
        if not self.init_clients():
            logger.warning("Bot is RUNNING but API Key is missing. Check settings.")
            return

        # 1. Fetch current equity and check Kill Switch
        try:
            cash_state = self.client.get_account_cash()
            # Assuming 'total' is total equity in cash dict (varies by API, fail-safe to 0)
            current_equity = cash_state.get('total', 0.0) 
            if current_equity > 0:
                if self.check_kill_switch(current_equity):
                    return # Exit cycle if LOCKED
        except Exception as e:
            logger.error(f"Error fetching equity for kill switch: {e}")
            
        # 2. Iterate Configured Tickers
        tickers = self.config.get("tickers", [])
        for ticker in tickers:
            logger.info(f"Analyzing {ticker}...")
            signal_data = self.strategy.analyze(ticker)
            signal = signal_data.get("signal")
            
            logger.info(f"[{ticker}] Signal: {signal} | {signal_data.get('reason','')}")
            
            if signal == "BUY":
                # Check 95% cash allowance
                available_trade_capital = self.check_account()
                price = signal_data.get("price")
                target_tp = signal_data.get("target_tp")
                
                if available_trade_capital < price:
                    logger.info(f"[{ticker}] Insufficient funds to buy 1 share. Free * 0.95 = {available_trade_capital:.2f}")
                    continue
                    
                # Calculate integer quantity (No fractional if API doesn't support them fully yet)
                quantity = math.floor(available_trade_capital / price)
                limit_price = price
                stop_loss_price = limit_price * (1.0 - self.config.get("stop_loss_pct", 0.02))
                
                logger.info(f"Placing Bracket Limit BUY for {ticker}: Qty {quantity} @ Limit {limit_price:.2f} | SL {stop_loss_price:.2f} | TP {target_tp:.2f}")
                res = self.client.place_limit_order(
                    ticker=ticker,
                    quantity=quantity,
                    limit_price=limit_price,
                    stop_price=stop_loss_price,
                    take_profit=target_tp
                )
                if res and res.get('id'):
                    logger.info(f"Order submitted successfully. ID: {res['id']}")
                else:
                    logger.error(f"Order failed or no ID returned: {res}")

            time.sleep(1) # Rate limiting between tickers

    def start(self):
        logger.info("Trading Bot Daemon Started.")
        while True:
            # Always reload config before the cycle so changes via the dashboard take effect
            self.config = self.load_config()
            try:
                self.run_cycle()
            except Exception as e:
                logger.error(f"Unexpected error in run cycle: {e}", exc_info=True)
                
            # Force log flush to disk for Streamlit to read
            for handler in logging.root.handlers:
                handler.flush()
                
            time.sleep(60) # 1-minute loop

if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
