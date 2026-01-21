"""
Weather Data Client - Fetches real-time temperature observations from NOAA/NWS APIs.

Data Sources:
1. NWS Station Observations API - Official station data
2. Aviation Weather METAR API - More frequent updates (every ~5 mins)

Both sources are used for redundancy. METAR data is typically more fresh.
"""
import asyncio
import aiohttp
import logging
import re
import ssl
import certifi
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from config import CITIES, NWS_API_BASE, METAR_API_BASE, CityConfig

logger = logging.getLogger(__name__)


@dataclass
class WeatherObservation:
    """A single weather observation."""
    station_id: str
    timestamp: datetime
    temperature_f: float
    source: str  # 'nws' or 'metar'


@dataclass 
class DailyMaxTracker:
    """Tracks the maximum temperature observed for a city today."""
    city: str
    date: date
    max_temp_f: float
    last_observation: Optional[WeatherObservation] = None
    
    def update(self, obs: WeatherObservation) -> bool:
        """Update with new observation. Returns True if max was updated."""
        if obs.temperature_f > self.max_temp_f:
            old_max = self.max_temp_f
            self.max_temp_f = obs.temperature_f
            self.last_observation = obs
            logger.info(f"📈 {self.city}: New max temp {self.max_temp_f:.1f}°F (was {old_max:.1f}°F)")
            return True
        return False


class WeatherClient:
    """Client for fetching real-time weather observations."""
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.max_trackers: Dict[str, DailyMaxTracker] = {}
        
    async def init(self):
        """Initialize the HTTP session."""
        # Create SSL context using certifi for proper certificate verification
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        
        headers = {
            "User-Agent": "(kalshi-weather-arb, github.com/weather-arb-bot)",
            "Accept": "application/json"
        }
        self.session = aiohttp.ClientSession(headers=headers, connector=connector)
        
    async def close(self):
        """Close the HTTP session."""
        if self.session:
            await self.session.close()
            
    def _get_tracker(self, city: str, city_config: CityConfig) -> DailyMaxTracker:
        """Get or create a daily max tracker for a city."""
        tz = ZoneInfo(city_config.timezone)
        today = datetime.now(tz).date()
        
        # Reset tracker if it's a new day
        if city not in self.max_trackers or self.max_trackers[city].date != today:
            self.max_trackers[city] = DailyMaxTracker(
                city=city,
                date=today,
                max_temp_f=float('-inf')
            )
            logger.info(f"🔄 Reset daily max tracker for {city} ({today})")
            
        return self.max_trackers[city]
    
    def _is_plausible_temp(self, temp_f: float, source: str) -> bool:
        """Check if a temperature reading is plausible (sanity check)."""
        # Reject readings outside realistic range for continental US
        if temp_f < -50 or temp_f > 140:
            logger.warning(f"⚠️ SANITY CHECK: Ignoring implausible temp {temp_f:.1f}°F from {source}")
            return False
        return True
    
    async def fetch_nws_observation(self, station_id: str) -> Optional[WeatherObservation]:
        """Fetch latest observation from NWS Station API."""
        url = f"{NWS_API_BASE}/stations/{station_id}/observations/latest"
        
        try:
            async with self.session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(f"NWS API error for {station_id}: {resp.status}")
                    return None
                    
                data = await resp.json()
                props = data.get("properties", {})
                
                # Temperature comes in Celsius from NWS
                temp_c = props.get("temperature", {}).get("value")
                if temp_c is None:
                    logger.warning(f"No temperature data from NWS for {station_id}")
                    return None
                
                temp_f = (temp_c * 9/5) + 32
                
                # Parse timestamp
                timestamp_str = props.get("timestamp")
                if timestamp_str:
                    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                else:
                    timestamp = datetime.now(ZoneInfo("UTC"))
                
                return WeatherObservation(
                    station_id=station_id,
                    timestamp=timestamp,
                    temperature_f=temp_f,
                    source="nws"
                ) if self._is_plausible_temp(temp_f, f"NWS/{station_id}") else None
                
        except asyncio.TimeoutError:
            logger.warning(f"NWS API timeout for {station_id}")
        except Exception as e:
            logger.error(f"NWS API error for {station_id}: {e}")
            
        return None
    
    async def fetch_metar_observation(self, station_id: str) -> Optional[WeatherObservation]:
        """Fetch latest METAR observation from Aviation Weather API."""
        url = f"{METAR_API_BASE}/metar"
        params = {"ids": station_id, "format": "json"}
        
        try:
            async with self.session.get(url, params=params, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning(f"METAR API error for {station_id}: {resp.status}")
                    return None
                
                data = await resp.json()
                
                if not data or len(data) == 0:
                    logger.warning(f"No METAR data for {station_id}")
                    return None
                
                metar = data[0]
                
                # Temperature in METAR is in Celsius
                temp_c = metar.get("temp")
                if temp_c is None:
                    # Try parsing from raw METAR if structured data unavailable
                    raw = metar.get("rawOb", "")
                    temp_c = self._parse_metar_temp(raw)
                    
                if temp_c is None:
                    logger.warning(f"No temperature in METAR for {station_id}")
                    return None
                    
                temp_f = (temp_c * 9/5) + 32
                
                # Parse observation time
                obs_time = metar.get("obsTime")
                if obs_time:
                    # obsTime is typically Unix timestamp
                    timestamp = datetime.fromtimestamp(obs_time, tz=ZoneInfo("UTC"))
                else:
                    timestamp = datetime.now(ZoneInfo("UTC"))
                
                return WeatherObservation(
                    station_id=station_id,
                    timestamp=timestamp,
                    temperature_f=temp_f,
                    source="metar"
                ) if self._is_plausible_temp(temp_f, f"METAR/{station_id}") else None
                
        except asyncio.TimeoutError:
            logger.warning(f"METAR API timeout for {station_id}")
        except Exception as e:
            logger.error(f"METAR API error for {station_id}: {e}")
            
        return None
    
    def _parse_metar_temp(self, raw_metar: str) -> Optional[float]:
        """Parse temperature from raw METAR string as fallback."""
        # METAR format: ... T/DP ... where T is temp (M prefix = negative)
        # Example: "26/18" means 26°C temp, 18°C dewpoint
        match = re.search(r'\s(M?\d{2})/(M?\d{2})\s', raw_metar)
        if match:
            temp_str = match.group(1)
            if temp_str.startswith("M"):
                return -float(temp_str[1:])
            return float(temp_str)
        return None
    
        return None

    def _is_fresh(self, obs: WeatherObservation) -> bool:
        """Check if observation is fresh enough to use (within 90 mins)."""
        if not obs:
            return False
            
        now = datetime.now(ZoneInfo("UTC"))
        age = now - obs.timestamp
        age_minutes = age.total_seconds() / 60
        
        # NWS/METAR typically update hourly. 90 mins allows for some delay/jitter
        # but catches stuck/dead stations.
        if age_minutes > 90:
            logger.warning(
                f"⚠️ Stale data from {obs.source.upper()}/{obs.station_id}: "
                f"{age_minutes:.0f} mins old (limit 90 mins) — ignoring"
            )
            return False
            
        return True
    
    async def get_current_observation(self, city: str) -> Optional[WeatherObservation]:
        """Get the most recent observation for a city, using the higher reading."""
        if city not in CITIES:
            logger.error(f"Unknown city: {city}")
            return None
            
        city_config = CITIES[city]
        
        # Try both sources in parallel
        nws_task = self.fetch_nws_observation(city_config.weather_station)
        metar_task = self.fetch_metar_observation(city_config.metar_id)
        
        nws_obs, metar_obs = await asyncio.gather(nws_task, metar_task)
        
        # Check freshness
        if nws_obs and not self._is_fresh(nws_obs):
            nws_obs = None
        if metar_obs and not self._is_fresh(metar_obs):
            metar_obs = None
        
        # Use the higher reading (max temp only goes up, so higher is more current)
        if metar_obs and nws_obs:
            if metar_obs.temperature_f >= nws_obs.temperature_f:
                return metar_obs
            return nws_obs
        
        # Fallback: use whichever is available
        return metar_obs or nws_obs
    
    async def update_max_temp(self, city: str) -> Tuple[float, Optional[WeatherObservation]]:
        """
        Update the max temp tracker for a city and return current max.
        
        Returns:
            Tuple of (current_max_temp_f, latest_observation)
        """
        if city not in CITIES:
            raise ValueError(f"Unknown city: {city}")
            
        city_config = CITIES[city]
        tracker = self._get_tracker(city, city_config)
        
        obs = await self.get_current_observation(city)
        if obs:
            tracker.update(obs)
            
        return tracker.max_temp_f, obs
    
    async def get_all_max_temps(self) -> Dict[str, float]:
        """Get current max temps for all configured cities."""
        results = {}
        
        for city in CITIES:
            max_temp, _ = await self.update_max_temp(city)
            if max_temp != float('-inf'):
                results[city] = max_temp
                
        return results
