#!/usr/bin/env python3
"""
Weather Data Arbitrage Bot for Kalshi

Main entry point that orchestrates:
1. Fetching real-time weather observations
2. Scanning Kalshi weather markets
3. Detecting arbitrage opportunities
4. Executing trades (paper or live)

Usage:
    python src/bot.py --paper     # Paper trading mode (default)
    python src/bot.py --live      # Live trading mode
"""
import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Dict, List
import json
import os

from config import load_config, CITIES, TradingConfig
from weather_client import WeatherClient
from kalshi_client import KalshiClient
from arbitrage_engine import ArbitrageEngine, ArbitrageOpportunity
from notifier import AlertNotifier

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.FileHandler("weather_arb.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class WeatherArbitrageBot:
    """Main bot orchestrating weather data arbitrage."""
    
    def __init__(self, config: TradingConfig):
        self.config = config
        self.weather_client = WeatherClient()
        self.kalshi_client = KalshiClient(
            key_id=config.kalshi_api_key_id,
            private_key_path=config.kalshi_private_key_path
        )
        self.arb_engine = ArbitrageEngine(min_edge=config.min_edge)
        self.notifier = AlertNotifier()
        self.running = False
        self.stop_event = asyncio.Event()
        
        # Stats tracking
        self.stats = {
            "scans": 0,
            "opportunities_found": 0,
            "trades_executed": 0,
            "paper_balance": 1000.0,  # Starting paper balance
            "paper_pnl": 0.0,
            "start_time": None,
            "initial_live_balance": 0.0,
            "session_loss": 0.0  # Track cumulative losses for circuit breaker
        }
        
        # Circuit breaker: halt if loss exceeds this % of initial balance
        self.max_loss_pct = 0.20  # 50%
        
        # Track tickers we've already traded to prevent duplicate orders
        self.traded_tickers: set = set()
        
        # Load previously traded tickers from trades.json on startup
        self._load_traded_tickers_from_file()
        
    async def init(self):
        """Initialize all clients."""
        await self.weather_client.init()
        await self.kalshi_client.init()
        self.stats["start_time"] = datetime.now()
        
        # Fetch initial live balance if in live mode
        if self.config.mode == "live":
            balance_cents = await self.kalshi_client.get_balance()
            self.stats["initial_live_balance"] = balance_cents / 100.0
            
        logger.info("=" * 60)
        logger.info("🌤️  WEATHER ARBITRAGE BOT STARTING")
        logger.info(f"   Mode: {'PAPER' if self.config.mode == 'paper' else 'LIVE'}")
        if self.config.mode == "live":
            logger.info(f"   Inital Balance: ${self.stats['initial_live_balance']:.2f}")
            
            # Fetch existing positions to prevent double buying
            try:
                positions = await self.kalshi_client.get_market_positions()
                for ticker in positions:
                    self.traded_tickers.add(ticker)
            except Exception as e:
                logger.error(f"Failed to fetch initial positions: {e}")
                
        logger.info(f"   Cities: {', '.join(self.config.cities)}")
        logger.info(f"   Min Edge: {self.config.min_edge * 100:.1f}%")
        logger.info(f"   Max Position: ${self.config.max_position_size}")
        logger.info(f"   Max Contracts: {self.config.max_contract_limit}")
        logger.info(f"   Poll Interval: Dynamic (60s peak / {self.config.poll_interval}s off-peak)")
        logger.info(f"   Circuit Breaker: {self.max_loss_pct * 100:.0f}% max loss")
        logger.info("=" * 60)
        
    def _load_traded_tickers_from_file(self):
        """Load tickers from trades.json to prevent re-trading across restarts."""
        trades_file = "trades.json"
        if os.path.exists(trades_file):
            try:
                with open(trades_file, "r") as f:
                    data = json.load(f)
                for trade in data.get("trades", []):
                    ticker = trade.get("ticker", "")
                    if ticker:
                        self.traded_tickers.add(ticker)
                logger.info(f"📋 Loaded {len(self.traded_tickers)} previously traded tickers from trades.json")
            except Exception as e:
                logger.warning(f"Could not load trades.json for dedup: {e}")
    
    def _is_already_traded(self, ticker: str) -> bool:
        """Check if a ticker has already been traded (in-memory + file check)."""
        if ticker in self.traded_tickers:
            return True
        
        # Double-check trades.json as a safety net
        trades_file = "trades.json"
        if os.path.exists(trades_file):
            try:
                with open(trades_file, "r") as f:
                    data = json.load(f)
                for trade in data.get("trades", []):
                    if trade.get("ticker") == ticker:
                        self.traded_tickers.add(ticker)  # Cache it
                        return True
            except Exception:
                pass
        
        return False
    
    async def close(self):
        """Cleanup all clients."""
        await self.weather_client.close()
        await self.kalshi_client.close()
        
    async def scan_city(self, city: str) -> List[ArbitrageOpportunity]:
        """Scan a single city for arbitrage opportunities."""
        if city not in CITIES:
            logger.warning(f"Unknown city: {city}")
            return []
        
        # Get current max temperature
        max_temp, obs = await self.weather_client.update_max_temp(city)
        
        if max_temp == float('-inf'):
            logger.warning(f"No temperature data for {city}")
            return []
        
        # Get active markets
        markets = await self.kalshi_client.get_weather_markets(city)
        
        if not markets:
            logger.debug(f"No active markets for {city}")
            return []
        
        # Evaluate for opportunities
        opportunities = self.arb_engine.scan_markets(markets, max_temp, city)
        
        return opportunities
    
    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> bool:
        """Execute a trade on an arbitrage opportunity."""
        # Skip if we've already traded this ticker (persistent check)
        if self._is_already_traded(opp.ticker):
            logger.info(f"⏭️  Skipping {opp.ticker} - already traded")
            return False
        
        is_paper = self.config.mode == "paper"
        
        # Calculate position size based on paper balance (10% max per trade)
        if is_paper:
            available = self.stats["paper_balance"] * 0.10  # 10% of balance
        else:
            available = self.config.max_position_size
        
        price_cents = int(opp.current_price * 100)
        max_contracts = int(available / opp.current_price)
        
        # Cap at configured limit
        quantity = min(max_contracts, self.config.max_contract_limit)
        
        if quantity <= 0:
            logger.warning(f"Position size too small for {opp.ticker}")
            return False
        
        cost = quantity * opp.current_price
        
        # Check if we have enough balance
        if is_paper:
            if cost > self.stats["paper_balance"]:
                logger.warning(f"Insufficient paper balance: ${self.stats['paper_balance']:.2f}")
                return False
        else:
            # LIVE BALANCE CHECK
            balance_cents = await self.kalshi_client.get_balance()
            balance = balance_cents / 100.0
            
            if cost > balance:
                logger.error(f"❌ INSUFFICIENT FUNDS: Needed ${cost:.2f}, Have ${balance:.2f}")
                return False
        
        # Determine side
        side = "yes" if opp.action == "BUY_YES" else "no"
        
        # Mark as traded BEFORE execution to prevent race conditions
        self.traded_tickers.add(opp.ticker)
        
        try:
            # Execute order
            result = await self.kalshi_client.place_order(
                ticker=opp.ticker,
                side=side,
                quantity=quantity,
                limit_price=price_cents,
                is_paper=is_paper
            )
            
            if not result.success:
                # Keep ticker in traded set to prevent retry on same market
                logger.warning(f"Order failed for {opp.ticker}, will not retry")
                
        except Exception:
            # Keep ticker in traded set to prevent retry
            raise
        
        if result.success:
            # Track paper balance and P&L
            if is_paper:
                expected_payout = quantity * 1.0  # Each contract pays $1 if we win
                expected_profit = expected_payout - cost
                self.stats["paper_pnl"] += expected_profit
                self.stats["paper_balance"] += expected_profit
                current_balance = self.stats["paper_balance"]
            else:
                expected_profit = (quantity * 1.0) - cost
                # For live, we don't track balance in stats locally in the same way, 
                # but we can query it or just show P&L potential
                current_balance = 0.0 # Placeholder or fetch again if needed
                
            self.stats["trades_executed"] += 1
            self.traded_tickers.add(opp.ticker)  # Mark as traded
            
            # Log trade to file
            self._log_trade(opp, side, quantity, price_cents, is_paper)
            
            logger.info(
                f"✅ {'PAPER ' if is_paper else ''}TRADE: {side.upper()} "
                f"{quantity}x {opp.ticker} @ {price_cents}¢ "
                f"(potential profit: ${expected_profit:.2f})"
            )
            return True
        else:
            logger.error(f"❌ Trade failed: {result.error}")
            return False
    
    def _log_trade(self, opp: ArbitrageOpportunity, side: str, quantity: int, price_cents: int, is_paper: bool):
        """Log trade to JSON file for tracking."""
        trades_file = "trades.json"
        
        # Load existing trades
        if os.path.exists(trades_file):
            with open(trades_file, "r") as f:
                data = json.load(f)
        else:
            data = {"trades": []}
        
        # Add new trade
        trade_record = {
            "ticker": opp.ticker,
            "market_title": opp.market_title,
            "city": opp.city,
            "side": side,
            "quantity": quantity,
            "price_cents": price_cents,
            "cost": round(quantity * price_cents / 100, 2),
            "market_type": opp.market_type,
            "threshold": opp.threshold,
            "temp_at_trade": opp.current_max_temp,
            "certainty": opp.certainty,
            "edge": round(opp.edge * 100, 1),
            "timestamp": datetime.now().isoformat(),
            "mode": "paper" if is_paper else "live"
        }
        data["trades"].append(trade_record)
        
        # Save back
        with open(trades_file, "w") as f:
            json.dump(data, f, indent=2)
        
        logger.debug(f"Trade logged to {trades_file}")
    
    async def run_scan_cycle(self):
        """Run one complete scan cycle across all cities."""
        self.stats["scans"] += 1
        all_opportunities = []
        
        for city in self.config.cities:
            try:
                opps = await self.scan_city(city)
                all_opportunities.extend(opps)
            except Exception as e:
                logger.error(f"Error scanning {city}: {e}")
        
        if all_opportunities:
            self.stats["opportunities_found"] += len(all_opportunities)
            
            # Filter to only CERTAIN opportunities for automatic execution
            certain_opps = self.arb_engine.filter_by_certainty(
                all_opportunities, 
                min_certainty="CERTAIN"
            )
            
            logger.info(f"🔍 Found {len(all_opportunities)} opportunities "
                       f"({len(certain_opps)} CERTAIN)")
            
            # Execute on certain opportunities
            for opp in certain_opps:
                # Send alert notification
                await self.notifier.opportunity_alert(
                    city=opp.city,
                    ticker=opp.ticker,
                    edge=opp.edge,
                    action=opp.action
                )
                await self.execute_opportunity(opp)
        else:
            logger.debug("No opportunities this cycle")
    
    async def print_status(self):
        """Print current bot status."""
        runtime = datetime.now() - self.stats["start_time"]
        hours = runtime.total_seconds() / 3600
        
        logger.info("")
        logger.info("=" * 50)
        logger.info(f"📊 STATUS | Runtime: {hours:.1f}h | Scans: {self.stats['scans']}")
        logger.info(f"   Opportunities: {self.stats['opportunities_found']} | "
                   f"Trades: {self.stats['trades_executed']}")
                   
        if self.config.mode == "paper":
            logger.info(f"   💰 Paper Balance: ${self.stats['paper_balance']:.2f} "
                       f"(P&L: ${self.stats['paper_pnl']:+.2f})")
        elif self.config.mode == "live":
            # Fetch current balance
            balance_cents = await self.kalshi_client.get_balance()
            current_balance = balance_cents / 100.0
            initial = self.stats.get("initial_live_balance", 0.0)
            pnl = current_balance - initial
            
            logger.info(f"   💰 Live Balance: ${current_balance:.2f} "
                       f"(Session P&L: ${pnl:+.2f})")
                       
        logger.info("=" * 50)
        logger.info("")
    
    async def run(self):
        """Main bot loop."""
        self.running = True
        scan_count = 0
        
        try:
            while self.running:
                # Circuit breaker check
                if self.config.mode == "live":
                    balance_cents = await self.kalshi_client.get_balance()
                    current_balance = balance_cents / 100.0
                    initial = self.stats.get("initial_live_balance", 0.0)
                    
                    if initial > 0:
                        loss = initial - current_balance
                        max_loss = initial * self.max_loss_pct
                        
                        if loss >= max_loss:
                            logger.error(f"🛑 CIRCUIT BREAKER: Lost ${loss:.2f} (≥{self.max_loss_pct*100:.0f}% of ${initial:.2f}). HALTING.")
                            self.running = False
                            break
                
                await self.run_scan_cycle()
                scan_count += 1
                
                # Print status every 5 scans
                if scan_count % 5 == 0:
                    await self.print_status()
                
                # Use configured poll interval (30s)
                poll_interval = self.config.poll_interval
                
                # Wait before next scan (responsively)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=poll_interval)
                    # If we get here, stop_event was set
                    break
                except asyncio.TimeoutError:
                    # Timeout reached (interval passed), continue to next scan
                    continue
                
        except asyncio.CancelledError:
            logger.info("Bot cancelled")
        finally:
            await self.print_status()
            logger.info("Bot stopped")
    
    def stop(self):
        """Signal the bot to stop."""
        self.running = False
        self.stop_event.set()


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Weather Data Arbitrage Bot")
    parser.add_argument(
        "--paper", 
        action="store_true", 
        default=True,
        help="Run in paper trading mode (default)"
    )
    parser.add_argument(
        "--live", 
        action="store_true",
        help="Run in live trading mode"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Poll interval in seconds (overrides config)"
    )
    parser.add_argument(
        "--cities",
        type=str,
        default=None,
        help="Comma-separated list of cities to monitor"
    )
    
    args = parser.parse_args()
    
    # Load config
    config = load_config()
    
    # Override with command line args (--paper takes precedence over --live)
    if args.paper and not args.live:
        config.mode = "paper"
    elif args.live:
        config.mode = "live"
    if args.interval:
        config.poll_interval = args.interval
    if args.cities:
        config.cities = [c.strip().lower() for c in args.cities.split(",")]
    
    # Validate cities
    for city in config.cities:
        if city not in CITIES:
            logger.error(f"Unknown city: {city}. Available: {list(CITIES.keys())}")
            sys.exit(1)
    
    # Create and run bot
    bot = WeatherArbitrageBot(config)
    
    # Handle graceful shutdown
    loop = asyncio.get_event_loop()
    
    def signal_handler():
        logger.info("Shutdown signal received...")
        bot.stop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    try:
        await bot.init()
        await bot.run()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
