"""
Weather Data Client - Fetches real-time temperature observations from NOAA/NWS APIs.

Data Sources:
1. IEM ASOS (Iowa Environmental Mesonet) - Hourly observations with full-day lookback
2. METAR (Aviation Weather API) - Latest hourly observation (~15 min latency)
3. NWS (National Weather Service API) - Latest observation (~15 min latency)

All sources provide hourly data (observations at :53/:54). IEM has ~1 hour lag but 
provides historical lookback to capture daily max. METAR/NWS are fresher for current temp.
"""
import asyncio
import aiohttp
import logging
import re
import ssl
import certifi
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from typing import Dict, Optional, Tuple, List
from zoneinfo import ZoneInfo

from config import CITIES, NWS_API_BASE, METAR_API_BASE, IEM_API_BASE, CityConfig

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
            if old_max == float('-inf'):
                logger.debug(f"{self.city}: First reading {self.max_temp_f:.1f}°F")
            else:
                logger.info(f"📈 {self.city}: New high {self.max_temp_f:.1f}°F (was {old_max:.1f}°F)")
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
    
    async def fetch_iem_observations(self, station_id: str, hours_back: int = 2) -> List[WeatherObservation]:
        """
        Fetch ASOS observations from Iowa Environmental Mesonet.
        
        This provides all routine METARs plus SPECI (special) observations,
        which can capture significant weather changes between hourly reports.
        
        Args:
            station_id: Station ID (e.g., "KPHL" or "PHL")
            hours_back: How many hours of data to fetch
            
        Returns:
            List of WeatherObservation objects, newest first
        """
        # IEM uses 3-letter station codes (without K prefix)
        station_code = station_id.lstrip('K')
        
        # Calculate time range
        now = datetime.now(ZoneInfo("UTC"))
        start = now - timedelta(hours=hours_back)
        
        # Use regular ASOS endpoint without report_type filter to get all observations
        # including routine (3) and specials (4)
        params = {
            "station": station_code,
            "data": "tmpf",  # Temperature in Fahrenheit
            "year1": str(start.year),
            "month1": str(start.month),
            "day1": str(start.day),
            "year2": str(now.year),
            "month2": str(now.month),
            "day2": str(now.day),
            "tz": "Etc/UTC",
            "format": "onlycomma",
            "latlon": "no",
            "elev": "no",
            "missing": "empty",
            "trace": "empty",
            "direct": "no",
            # No report_type filter - get all available observations
        }
        
        observations = []
        
        try:
            async with self.session.get(f"{IEM_API_BASE}/cgi-bin/request/asos.py", params=params, timeout=15) as resp:
                if resp.status != 200:
                    logger.warning(f"IEM API error for {station_id}: {resp.status}")
                    return []
                
                text = await resp.text()
                lines = text.strip().split('\n')
                
                # Skip header line (station,valid,tmpf)
                for line in lines[1:]:
                    parts = line.split(',')
                    if len(parts) < 3:
                        continue
                    
                    station, timestamp_str, temp_str = parts[0], parts[1], parts[2]
                    
                    # Skip empty temperature values
                    if not temp_str or temp_str.strip() == '':
                        continue
                    
                    try:
                        temp_f = float(temp_str)
                        # Parse timestamp (format: "2026-01-23 22:55")
                        timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M")
                        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
                        
                        if self._is_plausible_temp(temp_f, f"IEM/{station_id}"):
                            observations.append(WeatherObservation(
                                station_id=station_id,
                                timestamp=timestamp,
                                temperature_f=temp_f,
                                source="iem"
                            ))
                    except (ValueError, IndexError) as e:
                        continue
                
                # Sort by timestamp, newest first
                observations.sort(key=lambda x: x.timestamp, reverse=True)
                
                if observations:
                    max_temp = max(o.temperature_f for o in observations)
                    logger.debug(f"IEM: fetched {len(observations)} obs for {station_id} (unfiltered max={max_temp:.1f}°F)")
                    
        except asyncio.TimeoutError:
            logger.warning(f"IEM API timeout for {station_id}")
        except Exception as e:
            logger.error(f"IEM API error for {station_id}: {e}")
        
        return observations


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
                
                obs = WeatherObservation(
                    station_id=station_id,
                    timestamp=timestamp,
                    temperature_f=temp_f,
                    source="metar"
                ) if self._is_plausible_temp(temp_f, f"METAR/{station_id}") else None
                
                if obs:
                    logger.debug(f"METAR: {station_id} = {temp_f:.1f}°F")
                return obs
                
        except asyncio.TimeoutError:
            logger.warning(f"METAR API timeout for {station_id}")
        except Exception as e:
            logger.error(f"METAR API error for {station_id}: {e}")
            
        return None

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
                
                obs = WeatherObservation(
                    station_id=station_id,
                    timestamp=timestamp,
                    temperature_f=temp_f,
                    source="nws"
                ) if self._is_plausible_temp(temp_f, f"NWS/{station_id}") else None
                
                if obs:
                    logger.debug(f"NWS: {station_id} = {temp_f:.1f}°F")
                return obs
                
        except asyncio.TimeoutError:
            logger.warning(f"NWS API timeout for {station_id}")
        except Exception as e:
            logger.error(f"NWS API error for {station_id}: {e}")
            
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
        """
        Get the most recent observation for a city, using multi-source strategy.
        
        Priority:
        1. IEM data (lookback from midnight local time - captures daily max)
        2. METAR (hourly, fresh)
        3. NWS (official, fallback)
        
        Returns the observation with the HIGHEST temperature across all sources,
        since we're tracking daily max and higher is always correct.
        """
        if city not in CITIES:
            logger.error(f"Unknown city: {city}")
            return None
            
        city_config = CITIES[city]
        
        # Calculate hours since midnight in the city's local timezone
        tz = ZoneInfo(city_config.timezone)
        now_local = datetime.now(tz)
        midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        hours_since_midnight = (now_local - midnight_local).total_seconds() / 3600
        # Add 1 hour buffer to ensure we capture data right after midnight
        hours_back = int(hours_since_midnight) + 1
        
        # Try all sources in parallel
        iem_task = self.fetch_iem_observations(city_config.metar_id, hours_back=hours_back)
        nws_task = self.fetch_nws_observation(city_config.weather_station)
        metar_task = self.fetch_metar_observation(city_config.metar_id)
        
        iem_obs_list, nws_obs, metar_obs = await asyncio.gather(
            iem_task, nws_task, metar_task
        )
        
        # Get the highest reading from IEM for the whole day (no freshness filter)
        iem_max_obs = None
        if iem_obs_list:
            # Filter to only today's observations (in the city's timezone)
            today_local = now_local.date()
            for obs in iem_obs_list:
                obs_local = obs.timestamp.astimezone(tz)
                if obs_local.date() == today_local:
                    if iem_max_obs is None or obs.temperature_f > iem_max_obs.temperature_f:
                        iem_max_obs = obs
        
        # Check freshness of other sources
        if nws_obs and not self._is_fresh(nws_obs):
            nws_obs = None
        if metar_obs and not self._is_fresh(metar_obs):
            metar_obs = None
        
        # Collect all valid observations
        all_obs = [obs for obs in [iem_max_obs, metar_obs, nws_obs] if obs is not None]
        
        if not all_obs:
            logger.warning(f"No valid observations for {city}")
            return None
        
        # Return the observation with the highest temperature
        best_obs = max(all_obs, key=lambda x: x.temperature_f)
        
        # Log all sources clearly
        metar_str = f"{metar_obs.temperature_f:.1f}°F" if metar_obs else "N/A"
        nws_str = f"{nws_obs.temperature_f:.1f}°F" if nws_obs else "N/A"
        if iem_max_obs:
            iem_time = iem_max_obs.timestamp.astimezone(tz).strftime("%H:%M")
            iem_str = f"{iem_max_obs.temperature_f:.1f}°F @ {iem_time}"
        else:
            iem_str = "N/A"
        
        logger.info(f"{city.upper()}: METAR {metar_str} | NWS {nws_str} | IEM High {iem_str}")
        
        return best_obs
    
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
