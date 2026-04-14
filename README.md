# btc-trading-bot

Algorithmic trading bot for BTC/USDT on Binance, written in Python 3.13 with asyncio.
Implements a momentum strategy (RSI + SMA) with a macro-filter overlay, paper-trades on Binance Testnet, and streams prices via WebSocket with automatic REST fallback.
All strategy decisions were validated through a 90-day backtest on 129 602 one-minute candles before being deployed.

---

## Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.13.5 |
| Async runtime | asyncio (single-threaded event loop) |
| Exchange connectivity | ccxt 4.5.48 (REST) + ccxt.pro / ccxtpro (WebSocket) |
| Data / indicators | pandas 3.0, pure-function pipeline |
| Macro signals | aiohttp · Binance Futures public API · CoinDesk RSS · Gemini 1.5 Flash |
| Configuration | python-dotenv (.env file, no secrets in source) |
| Tests | unittest (IsolatedAsyncioTestCase) — 227 tests, 0 failures |
| Target host | GCP e2-micro, bare Python (no Docker on this VM) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         main.py                                 │
│   asyncio.run(main())  →  trading_loop()                        │
└──────────────┬──────────────────────────────────────────────────┘
               │  await watch_candles(callback)
               ▼
┌─────────────────────────────────────────────────────────────────┐
│               exchange/client.py  BinanceClient                 │
│                                                                  │
│  ┌─────────────────────────┐   ┌──────────────────────────┐    │
│  │   ccxt.pro WebSocket    │   │   ccxt REST fallback      │    │
│  │   watch_ohlcv()         │   │   fetch_ohlcv()           │    │
│  │   30 s timeout          │   │   3 retries + backoff     │    │
│  │   exp. backoff 1→60 s   │   │   polling every N s       │    │
│  │   5 failures → REST ──► │──►│                           │    │
│  └─────────────────────────┘   └──────────────────────────┘    │
└──────────────┬──────────────────────────────────────────────────┘
               │  list[{ts, open, high, low, close, volume}]
               ▼
┌─────────────────────────────────────────────────────────────────┐
│                    core/loop.py  _on_candles()                  │
│                                                                  │
│   ┌──────────────┐    ┌──────────────────┐    ┌─────────────┐  │
│   │  RiskManager │    │   MacroFilter    │    │ CandleBuffer│  │
│   │  circuit     │    │  funding rate    │    │ deque(200)  │  │
│   │  breaker 3%  │    │  + LLM sentiment │    │ → DataFrame │  │
│   └──────────────┘    └──────────────────┘    └──────┬──────┘  │
│                                                       │         │
│                              strategy/indicators.py   │         │
│                              ┌────────────────────────┘         │
│                              │  sma(20) · rsi(14) · vol_sma(20) │
│                              ▼                                   │
│                      strategy/signals.py                         │
│                      should_enter() · check_exit()               │
│                              │                                   │
│                              ▼                                   │
│                      core/state.py  StateManager                 │
│                      WAITING_SIGNAL ↔ IN_POSITION                │
│                      persisted to bot_state.json                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation layers

### Layer 1 — Signal engine (`strategy/`)

Pure functions with no side effects or I/O. The entry condition requires both filters to fire simultaneously:

```
should_enter = RSI(14) < 40  AND  close > SMA(20)
```

- **RSI < 40** — price is in a locally oversold region without being in freefall. RSI < 35 produced only 1 trade over 90 days (too selective); RSI < 45 produced 69 trades with Sharpe −0.21 (no edge).
- **close > SMA(20)** — medium-term trend is up; the RSI dip is a pullback, not a breakdown. Removing the SMA filter produced 99 trades with −30 USDT PnL and Sharpe −0.12.
- **Exit:** fixed stop-loss at −2.5 % and take-profit at +4.0 % (ratio 1:1.6), selected by grid search over six SL/TP combinations.

`check_exit`, `calc_pnl`, and `should_enter` are pure functions tested in isolation. The backtest engine and the live loop call the same functions — there is no simulation/live divergence.

### Layer 2 — Risk and state machine (`core/`, `risk/`)

**StateManager** is a two-state FSM (`WAITING_SIGNAL` → `IN_POSITION`) backed by `bot_state.json`. State is flushed to disk on every transition so a process restart reconciles without data loss.

**RiskManager** implements two controls:

| Control | Behaviour |
|---------|-----------|
| Position sizing | `position_usdt = balance × risk_pct (1 %)` — fixed-fractional, not fixed-USDT |
| Circuit breaker | Trips when cumulative daily PnL ≤ −3 %. Returns `position_size = 0` while active; `_on_candles` skips signal evaluation entirely |

**MacroFilter** (optional, injectable) queries two public data sources every 15 minutes and emits one of three modes:

| Funding rate | Sentiment | Mode |
|---|---|---|
| ≥ +0.1 % (longs pay shorts) | positive | `AGGRESSIVE` |
| ≥ +0.1 % | negative | `NO_TRADE` |
| anything else | anything | `NORMAL` |

`NO_TRADE` causes `_on_candles` to return before any indicator is computed.

### Layer 3 — Transport (`exchange/client.py`)

`watch_candles(symbol, timeframe, callback, *, limit, rest_interval)` is the single public streaming interface. All connection management is hidden from the loop:

```
WebSocket (ccxt.pro watch_ohlcv)
  └─ inner loop: asyncio.wait_for(timeout=30 s)
       └─ on timeout / error:
            had data before error?  → _WsDisconnectedError
                                    → reset backoff, reconnect immediately
            never received data?    → increment failure counter
                                    → sleep 1s → 2s → 4s … capped at 60s
                                    → 5 consecutive failures → _WsFallbackError
       └─ ImportError (ccxt.pro absent) → _WsFallbackError immediately
  └─ _WsFallbackError → REST polling every rest_interval seconds
```

`fetch_candles` (REST) remains callable directly and is used exclusively by the backtest engine.

---

## Backtest results

**Dataset:** BTC/USDT 1-minute OHLCV · 90 days · 2026-01-14 → 2026-04-14 · 129 602 candles  
**Capital:** 10 000 USDT simulated · 1 % risk per trade · Sharpe = per-trade, non-annualised

### Sweep 1 — Entry condition grid (SL 2 % / TP 3 % fixed)

| Config | Trades | Win rate | PnL (USDT) | Sharpe | Max DD |
|--------|-------:|--------:|----------:|-------:|-------:|
| **RSI<40 + SMA20** ← selected | **10** | **50.0 %** | **+4.21** | **+0.152** | **0.04 %** |
| RSI<40 + SMA50 | 11 | 36.4 % | −2.21 | −0.077 | 0.08 % |
| RSI<35 + SMA20 | 1 | 0.0 % | −2.02 | 0.000 | 0.02 % |
| RSI<35 + SMA50 | 1 | 0.0 % | −2.56 | 0.000 | 0.03 % |
| RSI<35 (no SMA) | 99 | 35.4 % | −29.99 | −0.120 | 0.44 % |
| RSI<45 + SMA20 | 69 | 30.4 % | −35.69 | −0.214 | 0.46 % |

### Sweep 2 — SL/TP optimisation (RSI<40 + SMA20 fixed)

| Config | Trades | Win rate | PnL (USDT) | Sharpe | Max DD |
|--------|-------:|--------:|----------:|-------:|-------:|
| **SL 2.5 % / TP 4.0 %** ← selected | **10** | **50.0 %** | **+6.76** | **+0.187** | **0.11 %** |
| SL 2.0 % / TP 3.0 % (base) | 10 | 50.0 % | +4.21 | +0.152 | 0.04 % |
| SL 1.5 % / TP 3.0 % | 10 | 40.0 % | +2.74 | +0.115 | 0.03 % |
| SL 2.0 % / TP 4.0 % | 10 | 40.0 % | +3.28 | +0.101 | 0.09 % |
| SL 1.5 % / TP 2.5 % | 10 | 40.0 % | +1.61 | +0.072 | 0.03 % |
| SL 2.0 % / TP 3.5 % | 10 | 40.0 % | +2.04 | +0.066 | 0.09 % |

### Sweep 3 — Strategy comparison

| Strategy | Trades | Win rate | PnL (USDT) | Sharpe |
|----------|-------:|--------:|----------:|-------:|
| **RSI<40 + SMA20** ← deployed | **10** | **50.0 %** | **+6.76** | **+0.187** |
| MeanRev drop>1.5 %/10 min (SL1 %/TP1 %) | 31 | 58.1 % | +3.74 | +0.102 |
| RSI<40 + SMA20 + drop>1 %/10 min | 0 | — | — | — |

MeanRev has a higher win rate but lower Sharpe and trades 3× as often with a tighter margin. The combined strategy produced zero trades — RSI<40 (falling price) and close>SMA20 (above the moving average) are structurally contradictory when combined with a further drop requirement.

---

## Data-driven discards

### ETH/USDT and SOL/USDT — same config, different distribution

Running the winning BTC config unchanged on ETH and SOL over the same 90-day window:

| Symbol | Trades | Win rate | PnL (USDT) | Sharpe |
|--------|-------:|--------:|----------:|-------:|
| BTC/USDT | 10 | 50.0 % | +6.76 | +0.187 |
| ETH/USDT | 16 | 31.2 % | −9.72 | −0.185 |
| SOL/USDT | 12 | 25.0 % | −12.09 | −0.324 |
| Combined | 38 | 34.2 % | −15.06 | −0.120 |

ETH and SOL produce more entry signals (alts are more volatile) at a significantly lower win rate. The same SL/TP levels that fit BTC's volatility regime do not fit theirs. This is a parameter-portability problem. Both are excluded until a per-symbol parameter sweep is run.

### Volume confirmation (Vol > 1.2 × SMA20) — tested and discarded

| Config | Trades | Win rate | PnL (USDT) | Sharpe |
|--------|-------:|--------:|----------:|-------:|
| RSI<40 + SMA20 | 10 | 50.0 % | +6.76 | +0.187 |
| RSI<40 + SMA20 + Vol>1.2× | 1 | 0.0 % | −2.58 | 0.000 |

Adding a volume confirmation threshold collapsed 10 entries to 1. The surviving entry hit the stop-loss. Volume on 1-minute BTC bars is too noisy for a fixed 1.2× multiplier to act as a reliable filter — it eliminates almost all valid signals. The infrastructure is preserved in the backtest engine (`_WINNER_VOL`, `volume_comp_report.json`) and `should_enter()` still accepts optional volume parameters, but the filter is not active in the live loop.

---

## Installation

```bash
git clone <repo>
cd trading-bot
python3.13 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create `.env` in the project root:

```dotenv
BINANCE_API_KEY=your_testnet_key
BINANCE_API_SECRET=your_testnet_secret
GEMINI_API_KEY=your_gemini_key        # optional — only needed for MacroFilter
```

Binance Testnet credentials: https://testnet.binance.vision  
Gemini API key: https://aistudio.google.com

Edit `config.py` to adjust trading parameters before running:

```python
BOT_CONFIG = {
    'symbol':           'BTC/USDT',
    'timeframe':        '1m',
    'limit':            200,          # candles per WebSocket page
    'interval_seconds': 60,           # REST fallback poll interval (seconds)
    'paper_balance':    10_000.0,     # simulated USDT
    'risk_pct':         0.01,         # 1 % of balance per trade
    'stop_loss_pct':    0.025,        # 2.5 %
    'take_profit_pct':  0.04,         # 4.0 %
}
```

---

## Usage

### Run tests

```bash
source venv/bin/activate
python -m unittest discover -s tests -q
# Ran 227 tests in ~0.1 s   OK
```

### Run the backtest

Downloads 90 days of 1-minute OHLCV from Binance production (public endpoint, no auth required). Allow ~5 minutes for the full data fetch.

```bash
python -m backtest.engine
```

Results are written to `backtest/results/`:

| File | Contents |
|------|---------|
| `multi_report.json` | Entry-condition grid (6 configs) |
| `sltp_report.json` | SL/TP optimisation (6 configs) |
| `symbol_report.json` | Multi-symbol comparison (BTC / ETH / SOL) |
| `strategy_comp_report.json` | RSI+SMA vs MeanRev vs Combined |
| `volume_comp_report.json` | Volume confirmation experiment |

### Run the bot

```bash
python main.py
```

The bot connects to Binance Testnet, streams BTC/USDT 1-minute candles via WebSocket, and paper-trades against a simulated balance. State is flushed to `bot_state.json` on every FSM transition. Closed trades are appended to `trades_history.json`.

Stop with `Ctrl+C` — `CancelledError` is caught in `main()`, `client.close()` is called before exit.

---

## Folder structure

```
trading-bot/
├── main.py                    # entry point — wires components, runs asyncio.run()
├── config.py                  # BOT_CONFIG dict
├── .env                       # credentials (gitignored)
├── requirements.txt
│
├── exchange/
│   └── client.py              # BinanceClient: watch_candles (WS) + fetch_candles (REST)
│
├── core/
│   ├── loop.py                # trading_loop, _on_candles callback, per-tick helpers
│   ├── state.py               # BotState FSM + JSON persistence
│   └── macro_filter.py        # MacroFilter: funding rate + Gemini sentiment
│
├── strategy/
│   ├── indicators.py          # sma, ema, rsi, volume_sma — pure pandas functions
│   └── signals.py             # should_enter, check_exit, calc_pnl — pure Python
│
├── data/
│   └── candles.py             # CandleBuffer: deque(maxlen) + to_dataframe()
│
├── risk/
│   └── manager.py             # RiskManager: circuit breaker + position sizing
│
├── backtest/
│   ├── engine.py              # full sweep engine — 5 independent result files
│   └── results/               # JSON outputs from last run
│
└── tests/                     # 227 unit + async tests
    ├── test_loop.py
    ├── test_exchange_client.py
    ├── test_backtest_engine.py
    ├── test_signals.py
    ├── test_indicators.py
    ├── test_candles.py
    ├── test_state.py
    ├── test_risk_manager.py
    └── test_macro_filter.py
```

---

## Current status

**Paper trading active on Binance Testnet.**

| Parameter | Value |
|-----------|-------|
| Symbol | BTC/USDT 1-minute |
| Strategy | RSI(14) < 40 AND close > SMA(20) |
| Stop-loss | 2.5 % |
| Take-profit | 4.0 % |
| Position sizing | 1 % fixed-fractional |
| Circuit breaker | −3 % daily drawdown |
| Transport | WebSocket (ccxt.pro), REST fallback after 5 failures |
| MacroFilter | Disabled by default (requires `GEMINI_API_KEY`) |

The bot cannot place real orders. `BinanceClient.__init__` calls `set_sandbox_mode(True)` unconditionally, routing all requests to `testnet.binance.vision`. Live trading requires implementing `place_order_safe()` in `exchange/client.py` with exchange-side confirmation before removing the sandbox flag.
