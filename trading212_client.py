import time
import requests
import logging
import base64

logger = logging.getLogger(__name__)

# Transient HTTP status codes worth retrying
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class Trading212Client:
    """
    Native REST Client for the Trading 212 V0 Equity API.
    Supports both Practice and Live environments.

    All requests are retried up to `max_retries` times on transient errors
    (rate-limit 429, or 5xx server errors) with exponential back-off.
    """

    def __init__(self, api_key: str, api_secret: str, mode: str = "Practice",
                 max_retries: int = 3, retry_delay: float = 2.0):
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.mode = mode
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        if mode.lower() == "live":
            self.base_url = "https://live.trading212.com/api/v0"
        else:
            self.base_url = "https://demo.trading212.com/api/v0"

        auth_string = f"{self.api_key}:{self.api_secret}"
        encoded_auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

        self.headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """
        Central request dispatcher with retry logic.
        Retries on 429 (rate-limit) and 5xx (server error) up to max_retries times.
        Returns {} on permanent failure to keep callers safe.
        """
        url = f"{self.base_url}{endpoint}"
        last_exc = None

        # Mandatory Request Pacing: Stay under 50 req/min (0.83 req/s)
        # 1.5s delay = 40 req/min max. Guaranteed to stay safe.
        time.sleep(1.5)

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(method, url, headers=self.headers,
                                        timeout=10, **kwargs)
                
                # Check for rate limit headers for visibility
                rem = resp.headers.get("x-ratelimit-remaining")
                if rem:
                    logger.debug(f"[API] {method} {endpoint} | Limit remaining: {rem}")

                if resp.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                    # Respect Retry-After header if provided by T212
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except ValueError:
                            wait = self.retry_delay * (2 ** (attempt - 1))
                    else:
                        wait = self.retry_delay * (2 ** (attempt - 1))
                    
                    logger.warning(
                        f"[{method} {endpoint}] HTTP {resp.status_code} – "
                        f"retrying in {wait:.1f}s (attempt {attempt}/{self.max_retries})"
                    )
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as e:
                logger.error(f"Error {method} {endpoint}: {e}")
                try:
                    # Return error JSON so the caller can inspect specific error types 
                    # like /api-errors/selling-equity-not-owned
                    err_json = e.response.json()
                    if isinstance(err_json, dict):
                        err_json["_status_code"] = e.response.status_code
                    logger.error(f"Response: {err_json}")
                    return err_json
                except Exception:
                    logger.error(f"Response (non-JSON): {e.response.text}")
                    return {"_status_code": e.response.status_code, "error": "non-json response", "text": e.response.text}
            except Exception as e:
                logger.error(f"Error {method} {endpoint}: {e}")
                last_exc = e
                break
        return {}

    def _get(self, endpoint: str) -> dict:
        return self._request("GET", endpoint)

    def _post(self, endpoint: str, payload: dict) -> dict:
        logger.debug(f"POST {endpoint} payload: {payload}")
        return self._request("POST", endpoint, json=payload)

    def _delete(self, endpoint: str) -> bool:
        url = f"{self.base_url}{endpoint}"
        try:
            resp = requests.delete(url, headers=self.headers, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Error DELETE {endpoint}: {e}")
            return False

    # ── Account ───────────────────────────────────────────────────────────────

    def get_account_cash(self) -> dict:
        """
        Returns account cash state:
        { "free": 1000.0, "total": 1200.0, "ppl": 0.0, ... }
        """
        return self._get("/equity/account/cash")

    def get_open_positions(self) -> list:
        """Returns a list of currently open positions."""
        result = self._get("/equity/portfolio")
        return result if isinstance(result, list) else []

    def get_active_orders(self) -> list:
        """Returns a list of pending / working orders."""
        result = self._get("/equity/orders")
        return result if isinstance(result, list) else []

    def get_order_by_id(self, order_id) -> dict:
        """Fetch a single order by ID to check its fill status."""
        return self._get(f"/equity/orders/{order_id}")

    # ── Order Placement ───────────────────────────────────────────────────────

    def place_limit_order(self, ticker: str, quantity: float,
                          limit_price: float) -> dict:
        """
        Submits a Limit BUY order.

        The Trading212 v0 /equity/orders/limit endpoint only accepts:
          ticker, quantity, limitPrice, timeValidity.
        Stop-loss must be placed as a separate order after the buy fills.
        """
        payload = {
            "ticker": ticker,
            "quantity": round(float(quantity), 2),  # positive = BUY, T212 max precision = 2
            "limitPrice": round(limit_price, 4),
            "timeValidity": "DAY"
        }
        return self._post("/equity/orders/limit", payload)

    def place_stop_order(self, ticker: str, quantity: float,
                         stop_price: float) -> dict:
        """
        Places a standalone Stop (stop-market) SELL order.
        Used as the bracket stop-loss leg after a limit buy fills.
        quantity should be the number of shares purchased (sign is forced negative).
        """
        payload = {
            "ticker": ticker,
            "quantity": -abs(round(float(quantity), 2)),  # negative = SELL
            "stopPrice": round(stop_price, 4),
            "timeValidity": "DAY"
        }
        return self._post("/equity/orders/stop", payload)

    def place_limit_sell(self, ticker: str, quantity: float,
                         limit_price: float) -> dict:
        """
        Places a limit SELL order at the specified price (take-profit leg).
        Used after a confirmed BUY fill to lock in profit at the target.
        quantity should be the number of shares to sell (sign is forced negative).
        """
        payload = {
            "ticker": ticker,
            "quantity": -abs(round(float(quantity), 2)),  # negative = SELL
            "limitPrice": round(limit_price, 4),
            "timeValidity": "DAY"
        }
        return self._post("/equity/orders/limit", payload)


    def place_market_sell(self, ticker: str, quantity: float) -> dict:
        """
        Closes an open position immediately at market.
        Used for position-aware SELL signals and Kill Switch.
        """
        payload = {
            "ticker": ticker,
            "quantity": -abs(round(float(quantity), 2))   # Market orders do NOT support timeValidity
        }
        return self._post("/equity/orders/market", payload)

    # ── Order Management ──────────────────────────────────────────────────────

    def cancel_order(self, order_id) -> bool:
        """Cancel a single order by ID."""
        return self._delete(f"/equity/orders/{order_id}")

    def cancel_all_orders(self) -> bool:
        """Cancels all open active orders (used in Kill Switch)."""
        orders = self.get_active_orders()
        success = True
        for order in orders:
            order_id = order.get('id')
            if not self.cancel_order(order_id):
                logger.error(f"Failed to cancel order {order_id}")
                success = False
        return success

    def market_sell_all_positions(self) -> bool:
        """
        Closes all open positions at market price.
        Used exclusively for the Global Kill Switch.
        """
        positions = self.get_open_positions()
        success = True
        for pos in positions:
            ticker = pos.get('ticker')
            quantity = pos.get('quantity', 0)
            if quantity > 0:
                res = self.place_market_sell(ticker, quantity)
                if not res:
                    logger.error(f"Failed to market-sell {ticker}")
                    success = False
        return success
