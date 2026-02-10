#!/usr/bin/env python3
"""
Scheduled Bot Runner
Runs the weather arbitrage bot between 6:00 AM and 11:00 PM EST only.

Usage:
    python3 run_scheduled.py --live
"""
import subprocess
import time
import signal
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Trading hours (EST)
START_HOUR = 4   # 4 AM
END_HOUR = 23    # 11 PM

# Timezone
EST = ZoneInfo("America/New_York")

def is_trading_hours():
    """Check if current time is within trading hours (4 AM - 11 PM EST)."""
    now = datetime.now(EST)
    return START_HOUR <= now.hour < END_HOUR

def wait_until_start():
    """Wait until trading hours begin."""
    while not is_trading_hours():
        now = datetime.now(EST)
        print(f"⏰ [{now.strftime('%I:%M %p')}] Outside trading hours. Waiting for 4:00 AM EST...")
        time.sleep(60)  # Check every minute

def run_bot():
    """Run the bot and monitor trading hours."""
    print("🚀 Starting bot...")
    
    # Start the bot as a subprocess
    process = subprocess.Popen(
        [sys.executable, "src/bot.py", "--live"],
        cwd="/Users/evanfoglia/Documents/kalshi-weather-arb"
    )
    
    try:
        while True:
            # Check if we're still in trading hours
            if not is_trading_hours():
                now = datetime.now(EST)
                print(f"\n🛑 [{now.strftime('%I:%M %p')}] Trading hours ended (11:00 PM). Stopping bot...")
                process.terminate()
                process.wait(timeout=10)
                print("✅ Bot stopped gracefully.")
                return
            
            # Check if process is still running
            if process.poll() is not None:
                print("⚠️  Bot process exited unexpectedly.")
                return
            
            time.sleep(30)  # Check every 30 seconds
            
    except KeyboardInterrupt:
        print("\n⚠️  Shutdown signal received...")
        process.terminate()
        try:
            process.wait(timeout=10)
            print("✅ Bot stopped.")
        except subprocess.TimeoutExpired:
            print("⚠️  Force killing bot...")
            process.kill()

def main():
    """Main loop: wait for trading hours, run bot, repeat."""
    print("=" * 60)
    print("🌤️  SCHEDULED BOT RUNNER")
    print("   Trading Hours: 4:00 AM - 11:00 PM EST")
    print("=" * 60)
    
    while True:
        # Wait until trading hours
        wait_until_start()
        
        # Run the bot
        run_bot()
        
        # After bot stops, wait a bit before checking again
        print("\n💤 Sleeping until next trading window...")
        time.sleep(60)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Scheduler stopped.")
        sys.exit(0)
