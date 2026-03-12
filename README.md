# 🤖 PolyBot Live v4.2

<div align="center">

![PolyBot](https://img.shields.io/badge/PolyBot-v4.2-38bdf8?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iIzM4YmRmOCIgZD0iTTEyIDJMMiA3bDEwIDUgMTAtNS0xMC01ek0yIDE3bDEwIDUgMTAtNS0xMC01LTEwIDV6TTIgMTJsMTAgNSAxMC01LTEwLTUtMTAgNXoiLz48L3N2Zz4=)
![Python](https://img.shields.io/badge/Python-3.8+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.x-000000?style=for-the-badge&logo=flask&logoColor=white)
![Polymarket](https://img.shields.io/badge/Polymarket-CLOB_API-6366f1?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**A sophisticated automated trading bot for [Polymarket](https://polymarket.com) prediction markets**

*Real-time market scanning • EIP-712 authentication • Multi-wallet support • Paper & Live trading*

[Quick Start](#-quick-start) • [Features](#-features) • [Setup Guide](#️-setup-guide) • [Deploy to Vercel](#-deploy-to-vercel) • [API Docs](#-api-reference)

</div>

---

## 📖 Overview

PolyBot Live is a full-stack automated trading bot for Polymarket prediction markets, built with Python (Flask) backend and a real-time browser dashboard. It supports **paper trading** (simulation) and **live trading** using proper EIP-712 signed orders on the Polymarket CLOB (Central Limit Order Book).

### What it does
- **Scans** 50+ active Polymarket prediction markets every 8 seconds
- **Scores** each market using Kelly Criterion, liquidity analysis, and edge calculation
- **Signals** trading opportunities with confidence levels (STRONG BUY, OPPORTUNITY, MONITOR)
- **Executes** trades via browser wallet signatures or headless private key (never leaves your control)
- **Tracks** P&L, win rate, equity curve, and open positions in real time

---

## ✨ Features

### 🔐 Authentication Methods
| Method | Description | Requirements |
|--------|-------------|--------------|
| **MetaMask** | Browser extension wallet | MetaMask installed |
| **OKX Wallet** | Browser extension wallet | OKX Wallet installed |
| **Coinbase Wallet** | Browser extension wallet | Coinbase Wallet installed |
| **Rabby** | Browser extension wallet | Rabby installed |
| **Trust Wallet** | Browser extension wallet | Trust Wallet installed |
| **WalletConnect** | QR code for mobile wallets | Free WC Project ID |
| **Email OTP** | Built-in SMTP (no SDK) | Gmail App Password |
| **Google Sign-In** | Built-in PKCE OAuth2 (no SDK) | Google OAuth credentials |
| **Config Key** | Headless private key mode | Private key in config |

### 📊 Trading Engine
- **Kelly Criterion** position sizing with configurable fraction
- **Liquidity filtering** (minimum $25K by default)
- **Price impact** analysis (max 3% slippage protection)
- **Edge detection** with tiered thresholds (2% → 6.5% depending on market)
- **Paper trading** mode with realistic win/loss simulation
- **Live trading** via Polymarket CLOB FOK (Fill-or-Kill) orders

### 🖥️ Dashboard
- Real-time equity curve
- Live execution log (last 500 entries)
- Market scanner with search/filter/sort
- Trade history with P&L breakdown
- Open positions viewer
- Pending trade approval banner (for browser wallets)

### 🔧 Technical Highlights
- **EIP-712 typed data signing** — correct ClobAuthDomain spec (fixed v4.1 bug)
- **Polygon network** (Chain ID 137) — all trades on Polygon
- **No third-party SDK** for Email/Google auth (pure Python SMTP + PKCE)
- **Deterministic ephemeral wallets** derived from email for Email/Google sessions
- Thread-safe bot loop with configurable scan interval

---

## ⚡ Quick Start

### Local Development (5 minutes)

```bash
# 1. Clone the repository
git clone https://github.com/YOUR_USERNAME/polybot-live.git
cd polybot-live

# 2. Install dependencies
pip install flask flask-cors requests

# 3. (Optional) For live trading
pip install py-clob-client eth-account

# 4. Run the bot
python polybot_live.py

# 5. Open dashboard
# Automatically opens http://localhost:8765
```

> **Paper mode is the default** — no wallet or funds needed to start scanning and simulating trades.

---

## 🛠️ Setup Guide

### Step 1 — Basic Configuration

Edit the `CONFIG` dictionary at the top of `polybot_live.py`:

```python
CONFIG = {
    # Trading parameters (safe defaults)
    "CAPITAL_USDC":     500.0,    # Starting capital for paper trading
    "KELLY_FRACTION":   0.25,     # Conservative Kelly (25% of full Kelly)
    "MIN_LIQUIDITY":    25000,    # Minimum market liquidity ($)
    "MIN_EDGE":         0.04,     # Minimum edge to consider a trade
    "MAX_PRICE_IMPACT": 0.03,     # Max 3% price impact
    "MAX_BET_PCT":      0.06,     # Max 6% of capital per bet
    "MIN_BET_USDC":     2.0,      # Minimum bet size
    "SCAN_INTERVAL":    8,        # Seconds between market scans
    "START_MODE":       "paper",  # Start in paper mode (safe default)
}
```

### Step 2 — Email OTP Login (Optional)

Enables secure email-based login with 6-digit OTP codes. Uses Gmail or any SMTP server.

**Get Gmail App Password:**
1. Go to [myaccount.google.com](https://myaccount.google.com) → Security
2. Enable 2-Step Verification (required)
3. Go to App passwords → Generate → Copy 16-character password

```python
"SMTP_HOST":  "smtp.gmail.com",
"SMTP_PORT":  587,
"SMTP_USER":  "your@gmail.com",
"SMTP_PASS":  "xxxx xxxx xxxx xxxx",  # 16-char App Password
"SMTP_FROM":  "PolyBot",
```

### Step 3 — Google Sign-In (Optional)

Enables one-click Google OAuth2 login using PKCE (no third-party SDK).

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. APIs & Services → Credentials → Create OAuth 2.0 Client ID
3. Application type: **Web Application**
4. Add redirect URI: `http://localhost:8765/auth/google/callback`
5. Copy Client ID and Client Secret

```python
"GOOGLE_CLIENT_ID":     "xxxx.apps.googleusercontent.com",
"GOOGLE_CLIENT_SECRET": "GOCSPX-xxxx",
```

### Step 4 — WalletConnect (Optional, for mobile wallets)

1. Register free at [cloud.walletconnect.com](https://cloud.walletconnect.com)
2. Create new project → Copy Project ID

```python
"WC_PROJECT_ID": "your-project-id-here",
```

### Step 5 — Headless Mode (Optional, for server deployment)

For automated trading without a browser:

```python
"PRIVATE_KEY":    "your64charhexprivatekey",  # no 0x prefix
"WALLET_ADDRESS": "0xYourWalletAddress",
"SIG_TYPE":       0,  # 0=MetaMask/EOA, 1=email/Google proxy, 2=Safe
```

> ⚠️ **Never commit your private key to GitHub!** Use environment variables (see [Environment Variables](#environment-variables)).

---

## 🌐 Deploy to Vercel

> **Important:** Vercel is a serverless platform — it is best suited for the **dashboard frontend**. The bot's background scanning loop requires a persistent process. For full functionality, deploy the Python backend on a VPS or use the Vercel deployment for the UI only with a separate backend.

### Option A — Vercel (Frontend Dashboard Only)

This deploys the dashboard as a static/serverless app. Bot scanning runs only during active requests.

#### Prerequisites
- [Vercel account](https://vercel.com) (free)
- [GitHub account](https://github.com)
- [Vercel CLI](https://vercel.com/cli): `npm i -g vercel`

#### Deploy Steps

```bash
# 1. Push to GitHub (see GitHub setup below)
git push origin main

# 2. Install Vercel CLI
npm install -g vercel

# 3. Login to Vercel
vercel login

# 4. Deploy from project root
vercel

# Follow prompts:
# - Link to existing project? No
# - Project name: polybot-live
# - Directory: ./
# - Override settings? No

# 5. Deploy to production
vercel --prod
```

#### Configure Environment Variables on Vercel

Go to your Vercel project dashboard → Settings → Environment Variables:

| Variable | Value | Required |
|----------|-------|----------|
| `FLASK_SECRET` | Random 32-char hex string | Yes |
| `SMTP_USER` | your@gmail.com | Optional |
| `SMTP_PASS` | Gmail App Password | Optional |
| `SMTP_HOST` | smtp.gmail.com | Optional |
| `SMTP_PORT` | 587 | Optional |
| `GOOGLE_CLIENT_ID` | xxxx.apps.googleusercontent.com | Optional |
| `GOOGLE_CLIENT_SECRET` | GOCSPX-xxxx | Optional |
| `WC_PROJECT_ID` | Your WC Project ID | Optional |

> **Do NOT add PRIVATE_KEY to Vercel env vars** unless you fully understand the security implications. Use paper mode on Vercel.

### Option B — VPS Deployment (Recommended for Live Trading)

For full bot functionality with continuous scanning:

```bash
# On your VPS (Ubuntu/Debian)
sudo apt update && sudo apt install python3-pip nginx -y

# Clone and setup
git clone https://github.com/YOUR_USERNAME/polybot-live.git
cd polybot-live
pip3 install flask flask-cors requests py-clob-client eth-account gunicorn

# Run with gunicorn
gunicorn -w 1 -b 0.0.0.0:8765 --timeout 120 polybot_live:app

# Or run as systemd service (recommended)
sudo nano /etc/systemd/system/polybot.service
```

`polybot.service` content:
```ini
[Unit]
Description=PolyBot Live Trading Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/polybot-live
ExecStart=/usr/bin/python3 /home/ubuntu/polybot-live/polybot_live.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polybot
sudo systemctl start polybot
sudo systemctl status polybot
```

---

## 📁 Project Structure

```
polybot-live/
├── polybot_live.py      # Main application (Flask backend + dashboard HTML)
├── requirements.txt     # Python dependencies
├── vercel.json          # Vercel deployment configuration
├── .env.example         # Environment variables template
├── .gitignore           # Git ignore rules
└── README.md            # This file
```

---

## 🔒 Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|----------|-------------|---------|
| `FLASK_SECRET` | Flask session secret key | Auto-generated |
| `CAPITAL_USDC` | Starting capital | 500.0 |
| `KELLY_FRACTION` | Kelly bet fraction | 0.25 |
| `MIN_LIQUIDITY` | Min market liquidity | 25000 |
| `SCAN_INTERVAL` | Seconds between scans | 8 |
| `START_MODE` | `paper` or `live` | paper |
| `SMTP_HOST` | SMTP server host | smtp.gmail.com |
| `SMTP_PORT` | SMTP server port | 587 |
| `SMTP_USER` | SMTP username/email | — |
| `SMTP_PASS` | SMTP password | — |
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID | — |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Secret | — |
| `WC_PROJECT_ID` | WalletConnect Project ID | — |
| `PRIVATE_KEY` | Wallet private key (headless) | — |
| `WALLET_ADDRESS` | Wallet address (headless) | — |

---

## 📡 API Reference

All endpoints are served at `http://localhost:8765/api/`

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Bot status, connection, balance |
| GET | `/api/markets` | Fetches and scores live markets |
| GET | `/api/trades` | Trade history + P&L |
| GET | `/api/log` | Execution log entries |
| GET | `/api/positions` | Open positions (requires wallet) |
| GET | `/api/pending_trades` | Trades awaiting wallet signature |
| GET | `/api/health` | Health check |
| POST | `/api/bot/toggle` | Start/stop bot, set mode |
| POST | `/api/trade` | Execute a single trade |
| POST | `/api/clear_trades` | Clear trade history |
| POST | `/api/wallet/auth` | Authenticate with EIP-712 signature |
| POST | `/api/wallet/disconnect` | Disconnect wallet |
| GET | `/api/wallet/challenge` | Get auth timestamp challenge |
| POST | `/api/wallet/order_data` | Build EIP-712 order payload |
| POST | `/api/wallet/submit_order` | Submit browser-signed order |
| POST | `/api/wallet/submit_server_order` | Submit server-signed order (email/Google) |
| POST | `/api/auth/email/send_otp` | Send OTP email |
| POST | `/api/auth/email/verify_otp` | Verify OTP, establish session |
| GET | `/api/auth/google/start` | Start Google OAuth PKCE flow |
| GET | `/api/auth/google/session` | Check Google OAuth session |

---

## 🧠 How the Trading Algorithm Works

### Market Scoring
Each market is scored on a 0-100 scale:

```
1. Parse outcome prices → derive probability (e.g. YES price of 0.65 = 65% probability)
2. Calculate edge = |probability - 0.5| scaled to thresholds
3. Calculate price impact = $50 / liquidity
4. Apply Kelly Criterion: f* = (p*b - q) / b * KELLY_FRACTION
5. Signal assignment:
   - STRONG BUY: liquidity >$100K, edge ≥5%, impact <2%  (score: 95)
   - OPPORTUNITY: liquidity >$25K, edge ≥4%, impact <3%  (score: 65)
   - MONITOR: everything else                              (score: 10)
   - LOW LIQ: liquidity <$2K                              (score: 2)
```

### Kelly Criterion
Position sizes use fractional Kelly to reduce volatility:

```
Full Kelly:  f* = (p*b - q) / b
PolyBot:     bet = min(capital * f* * 0.25, capital * 6%)
```

### EIP-712 Auth Flow
```
1. Frontend requests timestamp from /api/wallet/challenge
2. Signs ClobAuth struct: { address, timestamp, nonce:0, message }
   Domain: { name:"ClobAuthDomain", version:"1", chainId:137 }
   NOTE: No verifyingContract field (Polymarket 401s if present)
3. Backend POSTs signature to clob.polymarket.com/auth/api-key
4. Receives API key/secret for CLOB order signing
5. Orders signed with eth_signTypedData_v4 in browser
```

---

## ⚠️ Important Warnings

> **LIVE TRADING RISK**: Real USDC will be spent. Only trade what you can afford to lose.

> **PAPER TRADE FIRST**: Run in paper mode for at least 2 weeks before going live. Monitor win rate, drawdown, and signal quality.

> **PRIVATE KEY SECURITY**: Never commit your private key to any repository. Use environment variables. Consider using a dedicated trading wallet with limited funds.

> **NO FINANCIAL ADVICE**: This software is for educational purposes. Past performance does not guarantee future results. Prediction markets carry significant risk.

---

## 🐛 Troubleshooting

### "Auth failed: 401" when connecting wallet
- Ensure you're on Polygon network (Chain ID 137) in your wallet
- The domain must NOT include `verifyingContract` — this is intentional per Polymarket spec
- Try refreshing and reconnecting

### Bot connects but no trades execute
- Check market liquidity (must be > $25K by default)
- Verify edge threshold (default 4%)
- Lower `MIN_EDGE` or `MIN_LIQUIDITY` in CONFIG for more signals

### Email OTP not received
- Check spam/junk folder
- Ensure Gmail 2-Step Verification is enabled before creating App Password
- Verify `SMTP_PASS` is the App Password (16 chars), not your Gmail password

### WalletConnect QR not showing
- Add a valid `WC_PROJECT_ID` from [cloud.walletconnect.com](https://cloud.walletconnect.com)

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit your changes: `git commit -m 'Add amazing feature'`
4. Push to branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgments

- [Polymarket](https://polymarket.com) — prediction market platform
- [Polymarket CLOB API](https://docs.polymarket.com) — trading infrastructure
- [ethers.js](https://ethers.io) — Ethereum JavaScript library
- [py-clob-client](https://github.com/Polymarket/py-clob-client) — Python CLOB client

---

<div align="center">
  <sub>Built with ❤️ for prediction market enthusiasts. Trade responsibly.</sub>
</div>
