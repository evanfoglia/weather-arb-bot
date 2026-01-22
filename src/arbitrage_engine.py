"""
Arbitrage Engine - Core strategy logic for weather data arbitrage.

The key insight:
- Kalshi weather markets settle on the official NWS max temp for the day
- Max temperature can only go UP during the day (never down)
- Once the current temp exceeds a threshold, any "above X" market is already settled

Strategy:
1. Monitor real-time temperature from weather stations
2. Track the running max temp for each city
3. When max temp exceeds a contract threshold, that contract is CERTAIN to win
4. Buy YES on any "above X" contract trading below 99¢
5. Buy NO on any "below X" contract (if max already exceeded threshold)
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from kalshi_client import WeatherMarket

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    """A detected arbitrage opportunity."""
    city: str
    ticker: str
    market_title: str
    action: str  # 'BUY_YES' or 'BUY_NO'
    current_max_temp: float
    threshold: float
    market_type: str
    current_price: float  # Price to pay (ask price)
    fair_value: float  # Our estimated fair value
    edge: float  # fair_value - current_price
    certainty: str  # 'CERTAIN', 'NEAR_CERTAIN', 'PROBABLE'
    timestamp: datetime
    
    @property
    def profit_potential(self) -> float:
        """Expected profit per dollar risked."""
        return self.edge / self.current_price if self.current_price > 0 else 0
    
    def __str__(self) -> str:
        return (
            f"🎯 {self.city.upper()} | {self.action} @ {self.current_price*100:.0f}¢ "
            f"(fair: {self.fair_value*100:.0f}¢, edge: {self.edge*100:.1f}¢) | "
            f"Temp: {self.current_max_temp:.1f}°F vs threshold {self.threshold:.0f}°F | "
            f"{self.certainty}"
        )


class ArbitrageEngine:
    """Evaluates weather markets for arbitrage opportunities."""
    
    def __init__(self, min_edge: float = 0.03):
        """
        Args:
            min_edge: Minimum edge required to consider a trade (default 3%)
        """
        self.min_edge = min_edge
        
    def evaluate_market(
        self,
        market: WeatherMarket,
        current_max_temp: float,
        city: str
    ) -> Optional[ArbitrageOpportunity]:
        """
        Evaluate a single market for arbitrage opportunities.
        
        Args:
            market: The weather market to evaluate
            current_max_temp: Current running max temperature for the day
            city: City identifier
            
        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        
        # Handle "above" threshold markets (e.g., "85°F or above")
        if market.is_above_market and market.threshold_low is not None:
            threshold = market.threshold_low
            
            if current_max_temp >= threshold:
                # CERTAIN: Max temp already hit the threshold
                # This market will settle YES - any ask below ~99¢ is profit
                fair_value = 0.99  # Account for 1% fee buffer
                edge = fair_value - market.yes_ask
                
                if edge >= self.min_edge:
                    return ArbitrageOpportunity(
                        city=city,
                        ticker=market.ticker,
                        market_title=f"{market.title} {market.subtitle}",
                        action="BUY_YES",
                        current_max_temp=current_max_temp,
                        threshold=threshold,
                        market_type="above",
                        current_price=market.yes_ask,
                        fair_value=fair_value,
                        edge=edge,
                        certainty="CERTAIN",
                        timestamp=datetime.now(ZoneInfo("UTC"))
                    )
                    
            elif current_max_temp >= threshold - 2:
                # NEAR-CERTAIN: Very close to threshold, likely to hit
                # This is more speculative, require larger edge
                # Calculate a probability-weighted fair value
                # (Simple heuristic: if within 2 degrees, ~80% chance of hitting)
                estimated_prob = 0.80
                fair_value = estimated_prob * 0.99
                edge = fair_value - market.yes_ask
                
                if edge >= self.min_edge * 2:  # Require 2x min edge for uncertain bets
                    return ArbitrageOpportunity(
                        city=city,
                        ticker=market.ticker,
                        market_title=f"{market.title} {market.subtitle}",
                        action="BUY_YES",
                        current_max_temp=current_max_temp,
                        threshold=threshold,
                        market_type="above",
                        current_price=market.yes_ask,
                        fair_value=fair_value,
                        edge=edge,
                        certainty="NEAR_CERTAIN",
                        timestamp=datetime.now(ZoneInfo("UTC"))
                    )
        
        # Handle "below" threshold markets (e.g., "80°F or below")
        if market.is_below_market and market.threshold_high is not None:
            threshold = market.threshold_high
            
            # Add 1°F buffer to avoid edge cases with NWS/METAR variance
            if current_max_temp > threshold + 1:
                # CERTAIN: Max temp clearly exceeded the threshold
                # This market will settle NO - buy NO at any price below ~99¢
                fair_value = 0.99
                edge = fair_value - market.no_ask
                
                if edge >= self.min_edge:
                    return ArbitrageOpportunity(
                        city=city,
                        ticker=market.ticker,
                        market_title=f"{market.title} {market.subtitle}",
                        action="BUY_NO",
                        current_max_temp=current_max_temp,
                        threshold=threshold,
                        market_type="below",
                        current_price=market.no_ask,
                        fair_value=fair_value,
                        edge=edge,
                        certainty="CERTAIN",
                        timestamp=datetime.now(ZoneInfo("UTC"))
                    )
        
        # Handle "between" range markets (e.g., "81°F to 84°F")
        if market.market_type == "between":
            low = market.threshold_low
            high = market.threshold_high
            
            if low is not None and high is not None:
                # Add 1°F buffer for between markets to avoid edge cases
                if current_max_temp > high + 1:
                    # CERTAIN: Max temp clearly exceeded the range
                    # This market will settle NO
                    fair_value = 0.99
                    edge = fair_value - market.no_ask
                    
                    if edge >= self.min_edge:
                        return ArbitrageOpportunity(
                            city=city,
                            ticker=market.ticker,
                            market_title=f"{market.title} {market.subtitle}",
                            action="BUY_NO",
                            current_max_temp=current_max_temp,
                            threshold=high,
                            market_type="between",
                            current_price=market.no_ask,
                            fair_value=fair_value,
                            edge=edge,
                            certainty="CERTAIN",
                            timestamp=datetime.now(ZoneInfo("UTC"))
                        )
        
        return None
    
    def scan_markets(
        self,
        markets: List[WeatherMarket],
        current_max_temp: float,
        city: str
    ) -> List[ArbitrageOpportunity]:
        """
        Scan a list of markets for arbitrage opportunities.
        
        Args:
            markets: List of weather markets to evaluate
            current_max_temp: Current running max temperature
            city: City identifier
            
        Returns:
            List of arbitrage opportunities, sorted by edge (highest first)
        """
        opportunities = []
        
        for market in markets:
            opp = self.evaluate_market(market, current_max_temp, city)
            if opp:
                opportunities.append(opp)
                logger.info(str(opp))
        
        # Sort by edge (highest first)
        opportunities.sort(key=lambda x: x.edge, reverse=True)
        
        return opportunities
    
    def filter_by_certainty(
        self,
        opportunities: List[ArbitrageOpportunity],
        min_certainty: str = "CERTAIN"
    ) -> List[ArbitrageOpportunity]:
        """Filter opportunities by minimum certainty level."""
        certainty_levels = {"CERTAIN": 3, "NEAR_CERTAIN": 2, "PROBABLE": 1}
        min_level = certainty_levels.get(min_certainty, 0)
        
        return [
            opp for opp in opportunities
            if certainty_levels.get(opp.certainty, 0) >= min_level
        ]
