# polybot: Binance BTC vs Polymarket mispricing scanner (paper trading)

Streams live BTC/USDT from Binance, scans up to 50 short-dated Polymarket BTC
price markets, computes a fair probability for each one from spot, time to
expiry and realised volatility, and flags or **paper-trades** mispricings
that remain after Polymarket's taker fee and the bid/ask spread.

> **Paper trading only.** This build has no live order path. `execution.mode: live`
> is refused at startup. Live trading would need its own review (see below).

## Read this first: will it make money?

It probably won't make money as written. The reasons:

* **Fees.** Polymarket crypto markets charge takers `0.07 × p × (1 − p)` per share,
  which is **1.75¢ per share at p = 0.50**. Add about 0.5¢ for crossing half of a 1¢ spread.
  So a coin-flip 5-minute market must be mispriced by about **2.3¢ just to break even**.
  With the default 3¢ safety margin you need roughly a 5¢ gap.
* **Speed.** The 5m/15m Up/Down markets are quoted by bots that reprice off
  Binance/Chainlink in milliseconds. This bot polls books once a second from a VPS.
  A gap that lasts long enough for us to see it is more often stale or adverse
  (the market knows something the model doesn't) than free money. Polymarket
  added these taker fees to short crypto markets specifically to stop latency arbitrage.
* **Settlement source.** The 5m/15m Up/Down markets settle on the **Chainlink BTC/USD**
  stream, not on Binance. The two differ by a few basis points, and that is
  most of the signal near expiry. The model treats this as `basis_uncertainty_bps`,
  but it is still a real risk.
* **Model risk.** A lognormal model with realised volatility understates fat tails. It
  will tend to "find edge" selling the tails (cheap YES on far strikes). The
  `min_price`/`max_price` filters reduce this but don't remove it.

The honest use of this bot is to run paper mode for 1–2 weeks and then
run `scripts/evaluate.py`. Compare **realised** edge after fees with **predicted** edge, and
compare the model's Brier score with the market's. If realised edge isn't clearly positive
with a t-stat above about 2 over a few hundred trades, there is no edge. A maker
strategy (resting quotes: no taker fee, plus rebates) has much better economics,
but it is a different and riskier bot.

## What it does

| Stage | Module | Notes |
|---|---|---|
| Binance feed | `polybot/feeds/binance.py` | `wss://data-stream.binance.vision/stream?streams=btcusdt@bookTicker/btcusdt@aggTrade`. Public, no key. Exponential-backoff reconnect, rotates connections before Binance's 24h cut, answers server pings. Realised vol from 1m klines plus 1s samples. |
| Market scanner | `polybot/feeds/polymarket.py` | Gamma `GET /events?series_slug=btc-up-or-down-5m` (and `-15m`), plus `GET /events?tag_slug=bitcoin` for "above $X", "below", and "between" strike markets. Classifies questions, reads `clobTokenIds` and `feeSchedule`, and keeps the 50 most relevant markets (nearest Up/Down windows, then strikes closest to spot). |
| Order books | same | CLOB `POST /books` once a second for all tokens. Resolution comes from Gamma `GET /markets/{id}`. |
| Pricing | `polybot/pricing.py` | Driftless GBM: `P(S_T > K) = N(d2)`, ranges, barrier/touch (off by default), Polymarket taker fee, and order-book walking. |
| Signals | `polybot/signals.py` | Edge = fair − VWAP over the ask levels − fee per share. A signal fires only if edge ≥ `min_edge` **after** fee and spread, size ≥ the market's minimum, and the price is within `[min_price, max_price]`. |
| Risk | `polybot/risk.py` | Per-trade cap, total exposure cap, one position per market, a daily loss limit that latches until the next UTC day, a kill-switch file, and a stale-data guard (Binance and both books ≤ 3s old). |
| Execution | `polybot/execution/paper.py` | Simulated taker fills that re-walk the *current* book and charge the fee at each level. |
| Storage | `polybot/storage.py` | SQLite tables: `signals`, `trades`, `positions`, `equity`, `events`. |
| Dashboard | `polybot/dashboard/` | FastAPI + one HTML page. HTTP Basic auth, lockout after 10 failed logins, kill button. Works on iPad and phone. |
| Evaluation | `scripts/evaluate.py` | Signals, win rate, predicted vs realised edge after fees, total P&L, max drawdown, Brier scores. |

Up/Down markets use the Binance 1m open at the window start as the
reference ("strike") price. Before a window opens, fair value is 0.5.

## Configuration

* `config.yaml` holds every non-secret setting: thresholds, sizes, market selection, and mode.
* `.env` holds secrets only. Copy `.env.example` to `.env`. `.env` is git-ignored, and secret
  values are redacted from logs. Putting a `secrets:` block in `config.yaml` is rejected.

Required: `DASHBOARD_PASSWORD`. The bot won't start the dashboard without it.

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # set DASHBOARD_PASSWORD
pytest                  # 68 tests, offline
python -m polybot       # dashboard on http://127.0.0.1:8080 (user: admin)
```

Offline end-to-end run (no internet needed). It uses a fake Binance/Polymarket that drops the WebSocket
every 20s to exercise reconnects:

```bash
PYTHONPATH=. python scripts/fake_exchange.py --drop-every 20 &
DASHBOARD_PASSWORD=test python -m polybot --config scripts/e2e_config.yaml --env-file /dev/null
python scripts/evaluate.py --db data/e2e/polybot.sqlite3
```

## Kill switch

Any one of these stops all new trading on the next loop tick (within 0.5s):

```bash
python -m polybot.kill                    # creates data/KILL
docker exec polybot python -m polybot.kill
touch data/KILL
```

The dashboard also has a **Kill switch** button. To release it:
`python -m polybot.kill --off`. The dashboard cannot release it, by design.

## Evaluate paper results

```bash
python scripts/evaluate.py                # all time
python scripts/evaluate.py --days 7
docker exec polybot python scripts/evaluate.py
```

---

## Deploy on a fresh Ubuntu VPS (22.04 / 24.04)

### 0. Pick a legal location

* Polymarket's international exchange **geo-blocks several countries,
  including the US**. Markets carry `restricted: true`. Check that you are allowed
  to use it where you live **and** where the VPS is.
* `api.binance.com` blocks US IPs. This bot uses the public market-data mirrors
  (`data-stream.binance.vision`, `data-api.binance.vision`), which need no account.

### 1. Base setup

```bash
ssh root@YOUR_VPS
adduser polybot && usermod -aG sudo polybot
apt update && apt -y upgrade && apt -y install git ufw unattended-upgrades
ufw allow OpenSSH && ufw enable           # don't open 8080; see "Dashboard access"
# Recommended: SSH keys only. Set PasswordAuthentication no in /etc/ssh/sshd_config.
timedatectl set-ntp true                  # accurate clock matters for expiries
```

### 2. Option A: Docker (recommended)

```bash
curl -fsSL https://get.docker.com | sh
usermod -aG docker polybot && su - polybot
git clone <your repo url> polybot && cd polybot
cp .env.example .env && nano .env         # set DASHBOARD_PASSWORD
chmod 600 .env
mkdir -p data && sudo chown 10001 data    # container runs as uid 10001
docker compose up -d --build
docker compose logs -f
```

`restart: unless-stopped` restarts the bot after a crash or reboot. A health check
marks the container unhealthy if the Binance feed goes silent. To update:
`git pull && docker compose up -d --build`.

### 2. Option B: systemd

```bash
sudo apt -y install python3-venv
sudo useradd --system --home /opt/polybot polybot
sudo git clone <your repo url> /opt/polybot && cd /opt/polybot
sudo python3 -m venv .venv && sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo nano .env && sudo chmod 600 .env
sudo mkdir -p data && sudo chown -R polybot:polybot /opt/polybot
sudo cp deploy/polybot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now polybot
journalctl -u polybot -f
```

`Restart=always` restarts the bot 5s after any exit.

### 3. Dashboard access from an iPad

The dashboard listens on `127.0.0.1:8080` only. Basic auth over plain HTTP
sends the password in the clear, so pick one of these:

* **Tailscale (easiest).** Install Tailscale on the VPS and the iPad and log in to both.
  Then run `tailscale serve --bg 8080` on the VPS and open the `https://<vps>.<tailnet>.ts.net`
  URL on the iPad. Nothing is exposed publicly.
* **SSH tunnel.** In an iPad SSH app (Termius, Blink), forward local 8080 to
  `127.0.0.1:8080`, then open `http://localhost:8080`.
* **Public HTTPS with a domain.** Install Caddy and use this Caddyfile:
  ```
  bot.example.com {
      reverse_proxy 127.0.0.1:8080
  }
  ```
  Then run `ufw allow 80,443/tcp`. Caddy gets a TLS certificate automatically.

Log in with `admin` (or `dashboard.username`) and your `DASHBOARD_PASSWORD`.

### 4. Binance API key (optional: this bot does not need one)

Prices come from public streams, so **no Binance key is required** and none is read.
If you create one for future use, lock it down:

1. Binance → Profile → **API Management** → *Create API* → *System generated*.
2. Complete 2FA. **Copy the secret now**. It is shown only once.
3. *Edit restrictions*:
   * keep **Enable Reading** only;
   * **leave "Enable Withdrawals" OFF** (it can only be enabled with an IP restriction anyway);
   * leave Spot/Margin/Futures trading OFF unless a future feature needs it;
   * choose **Restrict access to trusted IPs only** and enter your VPS's public IP
     (`curl -4 ifconfig.me` on the VPS).
4. Put the values in `.env` as `BINANCE_API_KEY` / `BINANCE_API_SECRET`. Never put them
   in `config.yaml`, never commit them, and rotate them if they ever leak.

For any future live Polymarket trading, the sensitive secret is a **Polygon wallet
private key**. Use a dedicated wallet holding only the capital you are willing to lose.

## Key config knobs

| Key | Default | Meaning |
|---|---|---|
| `signals.min_edge` | 0.03 | Minimum edge in probability points **after** fee and spread |
| `risk.max_position_usd` | 25 | Max cost (incl. fees) per trade |
| `risk.max_total_exposure_usd` | 200 | Max total cost of open positions |
| `risk.daily_loss_limit_usd` | 50 | Realised (today, UTC) + unrealised. Halts until 00:00 UTC |
| `risk.max_feed_age_s` | 3 | Stale-data guard |
| `polymarket.max_markets` | 50 | Markets tracked at once |
| `polymarket.min_seconds_to_expiry` | 20 | Don't trade the last seconds (oracle/basis noise) |
| `model.basis_uncertainty_bps` | 5 | Binance vs resolution-source basis, 1 sd |
| `execution.paper_extra_slippage` | 0 | Stress-test fills, e.g. 0.005 |

## Limitations

* Order books are polled over REST once a second. The CLOB market WebSocket
  (`wss://ws-subscriptions-clob.polymarket.com/ws/market`) would be faster.
* Paper fills assume we get the displayed book. Real takers lose some races,
  so paper results are an **upper bound**.
* Touch ("will BTC reach $X") markets are parsed and priced but disabled by default.
  Correct pricing needs the full price path since the window opened.
* Unrealised P&L marks positions at best bid minus fee. Short-dated binaries swing hard,
  so the daily-loss limit can trigger on marks alone.
