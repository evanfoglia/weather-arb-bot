"""
Kalshi API Client - Fetches weather market data and executes trades.

API Endpoints:
- Public: https://api.elections.kalshi.com/trade-api/v2 (market data, no auth)
- Trading: https://trading-api.kalshi.com/trade-api/v2 (orders, requires auth)
"""
import asyncio
import aiohttp
import logging
import re
import ssl
import certifi
import base64
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from config import CITIES, KALSHI_API_BASE, KALSHI_TRADING_API_BASE, CityConfig

logger = logging.getLogger(__name__)


@dataclass
class WeatherMarket:
    """A single weather market contract."""
    ticker: str
    title: str
    subtitle: str
    threshold_low: Optional[float]  # Lower bound of temp range (None = no lower bound)
    threshold_high: Optional[float]  # Upper bound of temp range (None = no upper bound)
    market_type: str  # 'above', 'below', 'between'
    yes_bid: float  # Best bid for YES (in dollars, 0-1)
    yes_ask: float  # Best ask for YES (in dollars, 0-1)
    no_bid: float   # Best bid for NO
    no_ask: float   # Best ask for NO
    volume: int
    open_interest: int
    expiration_time: datetime
    
    @property
    def is_above_market(self) -> bool:
        """Returns True if this is a 'temp >= X' or 'temp > X' market."""
        return self.market_type == 'above'
    
    @property
    def is_below_market(self) -> bool:
        """Returns True if this is a 'temp <= X' or 'temp < X' market."""
        return self.market_type == 'below'


@dataclass
class OrderResult:
    """Result of placing an order."""
    success: bool
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_quantity: Optional[int] = None
    error: Optional[str] = None


class KalshiClient:
    """Client for Kalshi API interactions."""
    
    
    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        self.private_key_path = private_key_path
        self.session: Optional[aiohttp.ClientSession] = None
        self.private_key = None
        
    async def init(self):
        """Initialize the HTTP session and load private key."""
        # Load private key
        try:
            with open(self.private_key_path, "rb") as key_file:
                self.private_key = serialization.load_pem_private_key(
                    key_file.read(),
                    password=None
                )
            logger.info("✅ Private key loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load private key from {self.private_key_path}: {e}")
            raise

        # Create SSL context using certifi for proper certificate verification
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        self.session = aiohttp.ClientSession(connector=connector)
        
    async def close(self):
        """Close the HTTP session."""
        if self.session:
            await self.session.close()
            
    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Sign request parts with RSA key."""
        # Format: timestamp + method + path (no query params)
        # e.g. "1631234567890GET/trade-api/v2/markets"
        msg = timestamp + method + path
        
        signature = self.private_key.sign(
            msg.encode('utf-8'),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=32  # SHA256 digest length
            ),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode('utf-8')

    def _get_headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate headers with signature."""
        timestamp = str(int(time.time() * 1000))
        signature = self._sign_request(method, path, timestamp)
        
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json"
        }
    
    async def _ensure_authenticated(self) -> bool:
        """Compatibility method - always True as we sign every request."""
        return self.private_key is not None
    
    def _parse_threshold_from_title(self, title: str, subtitle: str) -> tuple:
        """
        Parse temperature thresholds from market title/subtitle.
        
        Examples:
        - "85°F or above" -> (85, None, 'above')
        - ">85°" -> (85, None, 'above')
        - "80°F or below" -> (None, 80, 'below')  
        - "<80°" -> (None, 80, 'below')
        - "81°F to 84°F" -> (81, 84, 'between')
        - "83°F to 84°F" -> (83, 84, 'between')
        """
        text = f"{title} {subtitle}".lower()
        
        # Pattern: ">X°" (greater than - ABOVE market)
        gt_match = re.search(r'>(\d+)°?', text)
        if gt_match:
            # >64 means >= 65 (integers)
            val = float(gt_match.group(1))
            return (val + 1.0, None, 'above')
        
        # Pattern: "<X°" (less than - BELOW market)  
        lt_match = re.search(r'<(\d+)°?', text)
        if lt_match:
            return (None, float(lt_match.group(1)), 'below')
        
        # Pattern: "X°F or above" / "X°F or higher" / "at least X°F"
        above_match = re.search(r'(\d+)\s*°?\s*f?\s*(?:or\s+)?(?:above|higher|or\s+more|at\s+least)', text)
        if above_match:
            return (float(above_match.group(1)), None, 'above')
        
        # Pattern: "X°F or below" / "X°F or lower" / "at most X°F"
        below_match = re.search(r'(\d+)\s*°?\s*f?\s*(?:or\s+)?(?:below|lower|or\s+less|at\s+most)', text)
        if below_match:
            return (None, float(below_match.group(1)), 'below')
        
        # Pattern: "X°F to Y°F" / "between X and Y" / "X-Y°"
        range_match = re.search(r'(\d+)\s*°?\s*f?\s*(?:to|-)\s*(\d+)\s*°?\s*f?', text)
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            return (low, high, 'between')
        
        # Fallback: try to extract any number as threshold
        num_match = re.search(r'(\d+)', text)
        if num_match:
            threshold = float(num_match.group(1))
            # Be very careful with fallback - only use explicit keywords
            if 'above' in text or 'over' in text or 'higher' in text:
                return (threshold, None, 'above')
            elif 'below' in text or 'under' in text or 'lower' in text:
                return (None, threshold, 'below')
        
        return (None, None, 'unknown')
    
    async def get_weather_markets(self, city: str) -> List[WeatherMarket]:
        """Fetch active weather markets for a city."""
        if city not in CITIES:
            logger.error(f"Unknown city: {city}")
            return []
            
        city_config = CITIES[city]
        series_ticker = city_config.series_ticker
        
        # Get today's date in the city's timezone for filtering
        tz = ZoneInfo(city_config.timezone)
        today = datetime.now(tz).date()
        
        path = "/markets"
        url = f"{KALSHI_TRADING_API_BASE}{path}"
        headers = self._get_headers("GET", f"/trade-api/v2{path}")
        
        params = {
            "series_ticker": series_ticker,
            "limit": 100
        }
        
        markets = []
        
        try:
            async with self.session.get(url, headers=headers, params=params, timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"Kalshi API error: {resp.status}")
                    return []
                    
                data = await resp.json()
                raw_markets = data.get("markets", [])
                
                for m in raw_markets:
                    # Skip non-active markets
                    if m.get("status") != "active":
                        continue
                        
                    # Parse expiration date
                    exp_str = m.get("expiration_time", "")
                    if not exp_str:
                        continue
                        
                    try:
                        expiration = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    except:
                        continue
                    
                    # Parse the event date from title (e.g., "on Jan 19, 2026")
                    # Weather markets settle days after the event, so we check title
                    title = m.get("title", "")
                    subtitle = m.get("subtitle", "")
                    
                    # Extract date from title like "on Jan 19, 2026"
                    import re
                    date_match = re.search(r'on\s+(\w+)\s+(\d+),?\s*(\d{4})?', title)
                    if date_match:
                        month_str = date_match.group(1)
                        day = int(date_match.group(2))
                        year = int(date_match.group(3)) if date_match.group(3) else today.year
                        
                        # Parse month
                        month_map = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                                    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
                        month = month_map.get(month_str.lower()[:3], 0)
                        
                        if month > 0:
                            from datetime import date as date_type
                            event_date = date_type(year, month, day)
                            
                            # Only include today's markets
                            if event_date != today:
                                continue
                        else:
                            continue
                    else:
                        # If we can't parse date, skip
                        continue
                    
                    # Parse thresholds from title
                    title = m.get("title", "")
                    subtitle = m.get("subtitle", "")
                    low, high, mtype = self._parse_threshold_from_title(title, subtitle)
                    
                    if mtype == 'unknown':
                        logger.debug(f"Could not parse threshold from: {title} | {subtitle}")
                        continue
                    
                    # Extract prices (Kalshi returns cents as integers)
                    yes_bid = (m.get("yes_bid") or 0) / 100.0
                    yes_ask = (m.get("yes_ask") or 100) / 100.0
                    no_bid = (m.get("no_bid") or 0) / 100.0
                    no_ask = (m.get("no_ask") or 100) / 100.0
                    
                    market = WeatherMarket(
                        ticker=m.get("ticker", ""),
                        title=title,
                        subtitle=subtitle,
                        threshold_low=low,
                        threshold_high=high,
                        market_type=mtype,
                        yes_bid=yes_bid,
                        yes_ask=yes_ask,
                        no_bid=no_bid,
                        no_ask=no_ask,
                        volume=m.get("volume", 0),
                        open_interest=m.get("open_interest", 0),
                        expiration_time=expiration
                    )
                    markets.append(market)
                    
                logger.info(f"📊 Found {len(markets)} active markets for {city} ({series_ticker})")
                return markets
                
        except asyncio.TimeoutError:
            logger.warning(f"Kalshi API timeout for {city}")
        except Exception as e:
            logger.error(f"Kalshi API error for {city}: {e}")
            
        return []
    
    async def get_orderbook(self, ticker: str) -> Dict:
        """Get the full orderbook for a market."""
        path = f"/markets/{ticker}/orderbook"
        url = f"{KALSHI_TRADING_API_BASE}{path}"
        headers = self._get_headers("GET", f"/trade-api/v2{path}")
        
        try:
            async with self.session.get(url, headers=headers, timeout=10) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except Exception as e:
            logger.error(f"Orderbook error for {ticker}: {e}")
            return {}
    
    async def place_order(
        self,
        ticker: str,
        side: str,  # 'yes' or 'no'
        quantity: int,  # Number of contracts
        limit_price: int,  # Price in cents (1-99)
        is_paper: bool = True
    ) -> OrderResult:
        """
        Place an order on Kalshi.
        
        Args:
            ticker: Market ticker
            side: 'yes' or 'no'
            quantity: Number of contracts
            limit_price: Price in cents (1-99)
            is_paper: If True, simulate the order without executing
        """
        if is_paper:
            logger.info(f"📝 PAPER ORDER: {side.upper()} {quantity}x {ticker} @ {limit_price}¢")
            return OrderResult(
                success=True,
                order_id="paper-" + ticker,
                filled_price=limit_price / 100.0,
                filled_quantity=quantity
            )
        
        # Real order requires authentication (already checked by _ensure_authenticated implicitly)
        path = "/portfolio/orders"
        url = f"{KALSHI_TRADING_API_BASE}{path}"
        
        # Method is POST
        headers = self._get_headers("POST", f"/trade-api/v2{path}")
        
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": quantity,
            "yes_price" if side == "yes" else "no_price": limit_price
        }
        
        try:
            async with self.session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json()
                
                if resp.status in (200, 201):
                    order = data.get("order", {})
                    logger.info(f"✅ ORDER PLACED: {side.upper()} {quantity}x {ticker}")
                    return OrderResult(
                        success=True,
                        order_id=order.get("order_id"),
                        filled_price=order.get("avg_fill_price", limit_price) / 100.0,
                        filled_quantity=order.get("filled_count", 0)
                    )
                else:
                    error = data.get("error", {}).get("message", str(data))
                    logger.error(f"Order failed: {error}")
                    return OrderResult(success=False, error=error)
                    
        except Exception as e:
            logger.error(f"Order error: {e}")
            return OrderResult(success=False, error=str(e))
    
    async def get_portfolio(self) -> Dict:
        """Get current portfolio positions (requires auth)."""
        # GET /portfolio/positions
        path = "/portfolio/positions"
        url = f"{KALSHI_TRADING_API_BASE}{path}"
        headers = self._get_headers("GET", f"/trade-api/v2{path}")
        
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.error(f"Portfolio error: {e}")
            
        return {}
        
    async def get_balance(self) -> int:
        """
        Get available balance in cents.
        Returns 0 if error or unauthenticated.
        """
        # GET /portfolio/balance
        path = "/portfolio/balance"
        url = f"{KALSHI_TRADING_API_BASE}{path}"
        headers = self._get_headers("GET", f"/trade-api/v2{path}")
        
        try:
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("balance", 0)
        except Exception as e:
            logger.error(f"Balance check error: {e}")
            
        return 0

    async def get_market_positions(self) -> List[str]:
        """
        Get list of market tickers where we currently hold a position.
        Used to populate traded_tickers on startup to prevent double-buying.
        """
        endpoint = "/portfolio/positions"
        url = f"{KALSHI_TRADING_API_BASE}{endpoint}"
        
        try:
            headers = self._get_headers("GET", f"/trade-api/v2{endpoint}")
            
            async with self.session.get(url, headers=headers) as resp:
                data = await resp.json()
                
                if resp.status == 200:
                    positions = data.get("positions", [])
                    # Return all tickers found in the positions list
                    log_tickers = [p['ticker'] for p in positions]
                    logger.info(f"📋 Found {len(log_tickers)} existing positions on Kalshi")
                    return log_tickers
                else:
                    logger.error(f"Failed to fetch positions: {resp.status}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return []
