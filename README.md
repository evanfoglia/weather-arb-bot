# 🌡️ Kalshi Weather Arbitrage Bot

An automated trading bot that finds **guaranteed arbitrage opportunities** on Kalshi weather prediction markets by exploiting the **monotonic nature of daily maximum temperature**.

## 🎯 What Does This Bot Do?

Kalshi offers prediction markets like: *"Will the high temperature in Chicago be above 85°F today?"*

The key insight: **Daily maximum temperature can only go UP, never down.** Once the high reaches 85°F at 2 PM, it's guaranteed to end at 85°F or higher.

This bot:
1. **Monitors real-time weather data** from IEM, NWS, and METAR
2. **Tracks daily max temperature** with full-day lookback from midnight
3. **Compares current max temps to market thresholds**
4. **Identifies guaranteed winning trades** when outcomes are already locked
5. **Executes trades automatically** (live or paper mode)

---

## 📈 How It Makes Money

### The Strategy: "Pure Arbitrage"
The bot is now configured for **pure, risk-free arbitrage**. It does not speculate on future temperature rises. It only trades when the event has **already happened** according to official data.

| Market Type | Example | Winning Condition | Trade Action |
|-------------|---------|-------------------|--------------|
| **Above** | "Above 85°F?" | Temp ≥ 86.0°F (Threshold + 1.0°) | BUY YES |
| **Below** | "Below 80°F?" | Temp > 81.0°F (Threshold + 1.0°) | BUY NO |
| **Between** | "81-84°F?" | Temp > 85.0°F (High Limit + 1.0°) | BUY NO |

### Safety Buffers
To ensure 100% win rate and avoid "bad beats" from sensor variance or minor data corrections, we apply a **strict +1.0°F safety buffer** to ALL trades.

- If the market is "High > 71°F", the bot waits for **72.0°F** (or higher) before buying YES.
- It will NOT trade at 71.1°F, ensuring we are well clear of the "borderline" risk zone.

---

## 💰 How It Determines Bet Size

The bot uses two limits to size positions:

```python
# From .env:
MAX_POSITION_SIZE=20    # Maximum dollars per trade
MAX_CONTRACT_LIMIT=50   # Maximum contracts per trade
```

### Calculation

```
1. Start with MAX_POSITION_SIZE ($20)
2. Divide by contract price: $20 / $0.45 = 44 contracts
3. Cap at MAX_CONTRACT_LIMIT: min(44, 50) = 44 contracts
4. Final trade: 44 contracts × $0.45 = $19.80
```

### Examples

| Contract Price | Calculation | Contracts | Cost |
|---------------|-------------|-----------|------|
| 1¢ | $20 / $0.01 = 2000 → cap at 50 | 50 | $0.50 |
| 45¢ | $20 / $0.45 = 44 | 44 | $19.80 |
| 92¢ | $20 / $0.92 = 21 | 21 | $19.32 |

---

## 🛡️ Safety Features

| Feature | Description |
|---------|-------------|
| **Triple-Source Weather** | Fetches IEM, NWS, and METAR, uses the highest reading |
| **Full-Day Lookback** | IEM data from local midnight captures daily max even if current temp is lower |
| **Safety Buffers** | +1°F buffer on ALL market types (above, below, between) |
| **Circuit Breaker** | Automatically stops trading if session loss exceeds 20% |
| **Balance Check** | Verifies sufficient funds before each trade |
| **Duplicate Prevention** | Only trades each market once per session |
| **Data Sanity Check** | Rejects implausible temperatures (-50°F to 140°F range) |
| **Trade Logging** | All trades saved to `trades.json` with market title for review |

---

## 🏙️ Supported Markets

The bot monitors weather markets for 12 cities:

| City | Ticker Prefix |
|------|--------------|
| New York | KXHIGHNY |
| Chicago | KXHIGHCHI |
| Miami | KXHIGHMIA |
| Los Angeles | KXHIGHLAX |
| Austin | KXHIGHAUS |
| Denver | KXHIGHDEN |
| Philadelphia | KXHIGHPHIL |
| Washington DC | KXHIGHTDC |
| Seattle | KXHIGHTSEA |
| Las Vegas | KXHIGHTLV |
| San Francisco | KXHIGHTSFO |
| New Orleans | KXHIGHTNOLA |

---

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- Kalshi account with API access
- RSA API key from Kalshi (Settings → API Keys)

### 1. Clone & Install

```bash
git clone https://github.com/evanfoglia/weather-edge.git
cd weather-edge
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure API Keys

Create your private key file:
```bash
# Save your RSA private key (from Kalshi) to:
nano kalshi.key
# Paste your private key, save and exit
```

Edit `.env`:
```bash
# Kalshi Credentials
KALSHI_API_KEY_ID=your_key_id_here
KALSHI_PRIVATE_KEY_PATH=kalshi.key

# Trading Settings
TRADING_MODE=paper          # Start with paper mode!
MAX_POSITION_SIZE=20        # Max dollars per trade
MAX_CONTRACT_LIMIT=50       # Max contracts per trade
MIN_EDGE=0.01               # Minimum 1% edge required

# Cities to monitor (only those with active Kalshi markets)
CITIES=miami,nyc,la,chicago,denver,philly,seattle,sf,vegas,dc,austin,nola

# Poll interval (seconds)
POLL_INTERVAL=30            # 30 seconds for near-real-time monitoring
```

### 3. Run the Bot

**Paper Trading (recommended first):**
```bash
venv/bin/python3 src/bot.py --paper
```

**Live Trading:**
```bash
venv/bin/python3 src/bot.py --live
```

---

## 📊 Output Example

```
14:31:17 | INFO     | CHICAGO: METAR 87.2°F | NWS 87.0°F | IEM High 87.2°F @ 13:53
14:31:18 | INFO     | 📊 Found 6 active markets for chicago (KXHIGHCHI)
14:31:18 | INFO     | 🎯 CHICAGO | BUY_YES @ 4¢ (fair: 99¢, edge: 95.0¢) | Temp: 87.2°F vs threshold 85°F | CERTAIN
14:31:19 | INFO     | ✅ ORDER PLACED: YES 50x KXHIGHCHI-26JAN20-T85
14:31:19 | INFO     | ✅ TRADE: YES 50x KXHIGHCHI-26JAN20-T85 @ 4¢ (potential profit: $48.00)
```

---

## 📁 Project Structure

```
kalshi-weather-arb/
├── src/
│   ├── bot.py              # Main orchestrator
│   ├── config.py           # Configuration and city definitions
│   ├── weather_client.py   # NWS/METAR data fetching
│   ├── kalshi_client.py    # Kalshi API wrapper
│   ├── arbitrage_engine.py # Trade decision logic
│   └── notifier.py         # Alert notifications
├── .env                    # Your configuration (not committed)
├── kalshi.key             # Your private key (not committed)
├── trades.json            # Trade history log
├── weather_arb.log        # Application logs
├── requirements.txt
└── README.md
```

---

## ⚠️ Disclaimer

This bot is for educational purposes. Trading involves risk. The authors are not responsible for any financial losses. Always:
- Start with paper trading mode
- Use money you can afford to lose
- Monitor the bot during operation
- Understand the strategy before going live

---

## 📝 License

MIT License - See LICENSE file for details.
