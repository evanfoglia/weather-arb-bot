"""
Configuration for Weather Arbitrage Bot
"""
import os
from dataclasses import dataclass
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class CityConfig:
    """Configuration for a single city's weather market."""
    name: str
    series_ticker: str
    weather_station: str
    metar_id: str
    timezone: str


# City configurations - matched to Kalshi settlement sources
CITIES: Dict[str, CityConfig] = {
    "nyc": CityConfig(
        name="New York City",
        series_ticker="KXHIGHNY",
        weather_station="KNYC",
        metar_id="KNYC",
        timezone="America/New_York"
    ),
    "chicago": CityConfig(
        name="Chicago",
        series_ticker="KXHIGHCHI",
        weather_station="KMDW",
        metar_id="KMDW",
        timezone="America/Chicago"
    ),
    "miami": CityConfig(
        name="Miami",
        series_ticker="KXHIGHMIA",
        weather_station="KMIA",
        metar_id="KMIA",
        timezone="America/New_York"
    ),
    "la": CityConfig(
        name="Los Angeles",
        series_ticker="KXHIGHLAX",
        weather_station="KLAX",
        metar_id="KLAX",
        timezone="America/Los_Angeles"
    ),
    "austin": CityConfig(
        name="Austin",
        series_ticker="KXHIGHAUS",
        weather_station="KAUS",
        metar_id="KAUS",
        timezone="America/Chicago"
    ),
    "denver": CityConfig(
        name="Denver",
        series_ticker="KXHIGHDEN",
        weather_station="KDEN",
        metar_id="KDEN",
        timezone="America/Denver"
    ),
    "houston": CityConfig(
        name="Houston",
        series_ticker="KXHIGHOU",
        weather_station="KIAH",
        metar_id="KIAH",
        timezone="America/Chicago"
    ),
    "philly": CityConfig(
        name="Philadelphia",
        series_ticker="KXHIGHPHIL",
        weather_station="KPHL",
        metar_id="KPHL",
        timezone="America/New_York"
    ),
    "dc": CityConfig(
        name="Washington DC",
        series_ticker="KXHIGHTDC",
        weather_station="KDCA",
        metar_id="KDCA",
        timezone="America/New_York"
    ),
    "seattle": CityConfig(
        name="Seattle",
        series_ticker="KXHIGHTSEA",
        weather_station="KSEA",
        metar_id="KSEA",
        timezone="America/Los_Angeles"
    ),
    "vegas": CityConfig(
        name="Las Vegas",
        series_ticker="KXHIGHTLV",
        weather_station="KLAS",
        metar_id="KLAS",
        timezone="America/Los_Angeles"
    ),
    "sf": CityConfig(
        name="San Francisco",
        series_ticker="KXHIGHTSFO",
        weather_station="KSFO",
        metar_id="KSFO",
        timezone="America/Los_Angeles"
    ),
    "nola": CityConfig(
        name="New Orleans",
        series_ticker="KXHIGHTNOLA",
        weather_station="KMSY",
        metar_id="KMSY",
        timezone="America/Chicago"
    ),
    "boston": CityConfig(
        name="Boston",
        series_ticker="KXHIGHTBOS",
        weather_station="KBOS",
        metar_id="KBOS",
        timezone="America/New_York"
    ),
    "atlanta": CityConfig(
        name="Atlanta",
        series_ticker="KXHIGHTATL",
        weather_station="KATL",
        metar_id="KATL",
        timezone="America/New_York"
    ),
    "phoenix": CityConfig(
        name="Phoenix",
        series_ticker="KXHIGHTPHX",
        weather_station="KPHX",
        metar_id="KPHX",
        timezone="America/Phoenix"
    ),
    "minneapolis": CityConfig(
        name="Minneapolis",
        series_ticker="KXHIGHTMIN",
        weather_station="KMSP",
        metar_id="KMSP",
        timezone="America/Chicago"
    )
}


@dataclass
class TradingConfig:
    """Trading parameters."""
    mode: str  # 'paper' or 'live'
    max_position_size: float
    min_edge: float
    poll_interval: int
    cities: List[str]
    kalshi_api_key_id: str
    kalshi_private_key_path: str
    max_contract_limit: int


def load_config() -> TradingConfig:
    """Load configuration from environment variables."""
    cities_str = os.getenv("CITIES", "nyc,chicago,miami")
    cities = [c.strip().lower() for c in cities_str.split(",")]
    
    return TradingConfig(
        mode=os.getenv("TRADING_MODE", "paper"),
        max_position_size=float(os.getenv("MAX_POSITION_SIZE", "50")),
        min_edge=float(os.getenv("MIN_EDGE", "0.03")),
        poll_interval=int(os.getenv("POLL_INTERVAL", "300")),
        cities=cities,
        kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
        kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi.key"),
        max_contract_limit=int(os.getenv("MAX_CONTRACT_LIMIT", "50"))
    )


# API endpoints
NWS_API_BASE = "https://api.weather.gov"
METAR_API_BASE = "https://aviationweather.gov/api/data"
IEM_API_BASE = "https://mesonet.agron.iastate.edu"
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_TRADING_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
