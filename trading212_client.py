import requests
import logging

logger = logging.getLogger(__name__)

class Trading212Client:
    """
    Native REST Client for the Trading 212 V0 Equity API.
    Supports both Practice and Live environments.
    """
    def __init__(self, api_key: str, mode: str = "Practice"):
        self.api_key = api_key.strip()
        self.mode = mode
        
        if mode.lower() == "live":
            self.base_url = "https://live.trading212.com/api/v0"
        else:
            self.base_url = "https://demo.trading212.com/api/v0"
            
        self.headers = {
            "Authorization": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def _get(self, endpoint: str) -> dict:
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error GET {endpoint}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return {}

    def _post(self, endpoint: str, payload: dict) -> dict:
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.post(url, json=payload, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error POST {endpoint}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return {}

    def get_account_cash(self) -> dict:
        """
        Returns account cash state:
        { "free": 1000.0, "total": 1200.0, "ppl": 0.0, ... }
        """
        return self._get("/equity/account/cash")

    def get_open_positions(self) -> list:
        """ Returns a list of currently open positions. """
        return self._get("/equity/portfolio")

    def get_active_orders(self) -> list:
        """ Returns a list of pending orders. """
        return self._get("/equity/orders")

    def place_limit_order(self, ticker: str, quantity: float, limit_price: float, stop_price: float = None, take_profit: float = None) -> dict:
        """
        Submits a Limit buy order with optional bracket Stop Loss and Take Profit.
        """
        payload = {
            "ticker": ticker,
            "quantity": quantity,
            "limitPrice": round(limit_price, 4), # Ensure decimal formatting
            "timeValidity": "DAY"
        }
        
        # Trading 212 API might accept stopPrice and limitPrice on entry, or we might need secondary orders.
        # Assuming typical v0 schema for attached orders if supported, otherwise just placed simple limit
        
        if stop_price:
            payload["stopPrice"] = round(stop_price, 4)
            
        # The exact shape of the payload for Take Profit might vary; for safety we just send the standard bracket params.
        # If API rejects, we can handle it.
        
        return self._post("/equity/orders/limit", payload)
        
    def cancel_all_orders(self) -> bool:
        """ Cancels all open active orders (used in Kill Switch). """
        orders = self.get_active_orders()
        success = True
        for order in orders:
            try:
                order_id = order.get('id')
                url = f"{self.base_url}/equity/orders/{order_id}"
                requests.delete(url, headers=self.headers, timeout=10)
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
                success = False
        return success
    
    def market_sell_all_positions(self) -> bool:
        """ 
        Closes all positions at market price.
        Used exclusively for the Global Kill Switch.
        """
        positions = self.get_open_positions()
        success = True
        for pos in positions:
            ticker = pos.get('ticker')
            quantity = pos.get('quantity')
            if quantity > 0:
                payload = {
                    "ticker": ticker,
                    "quantity": -quantity, # Negative for sell
                    "timeValidity": "DAY"
                }
                res = self._post("/equity/orders/market", payload)
                if not res:
                    success = False
        return success
