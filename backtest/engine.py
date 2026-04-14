"""
Backtest engine — replays historical BTC/USDT 1m data through the strategy.

Usage (from project root):
    python3 -m backtest.engine
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import BOT_CONFIG
from strategy.indicators import rsi, sma
from strategy.signals import calc_pnl, check_exit, should_enter

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)

# ── strategy parameters (mirrors config.py + core/loop.py) ────────────────
_SYMBOL         = BOT_CONFIG['symbol']
_TIMEFRAME      = BOT_CONFIG['timeframe']
_BALANCE        = float(BOT_CONFIG.get('paper_balance', 10_000.0))
_RISK_PCT       = float(BOT_CONFIG.get('risk_pct', 0.01))
_SL_PCT         = float(BOT_CONFIG.get('stop_loss_pct', 0.02))
_TP_PCT         = float(BOT_CONFIG.get('take_profit_pct', 0.03))
_RSI_THRESHOLD  = 35.0
_SMA_PERIOD     = 20
_RSI_PERIOD     = 14
_MIN_CANDLES    = max(_SMA_PERIOD, _RSI_PERIOD)
_LOOKBACK_DAYS  = 30
_PAGE_SIZE      = 1000   # max candles per ccxt call
_RESULTS_DIR    = Path(__file__).parent / 'results'
_REPORT_FILE    = _RESULTS_DIR / 'report.json'


# ── exchange ───────────────────────────────────────────────────────────────

def _make_exchange() -> ccxt.binance:
    """Build a Binance testnet exchange instance. Credentials are optional
    for public OHLCV endpoints but ccxt requires them to construct the object."""
    exchange: ccxt.binance = ccxt.binance({
        'apiKey':  os.getenv('BINANCE_API_KEY',    ''),
        'secret':  os.getenv('BINANCE_API_SECRET', ''),
        'timeout': 15_000,
        'options': {'defaultType': 'spot'},
    })
    exchange.set_sandbox_mode(True)
    return exchange


# ── data fetching ──────────────────────────────────────────────────────────

def _to_dataframe(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df = df.drop_duplicates(subset='ts').sort_values('ts').reset_index(drop=True)
    for col in ('open', 'high', 'low', 'close', 'volume'):
        df[col] = df[col].astype(float)
    return df


def _fetch_historical(exchange: ccxt.binance) -> pd.DataFrame:
    now_ms   = int(time.time() * 1000)
    since_ms = now_ms - _LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    rows: list[list] = []
    current  = since_ms
    pages    = 0

    while current < now_ms:
        page = exchange.fetch_ohlcv(_SYMBOL, _TIMEFRAME, since=current, limit=_PAGE_SIZE)
        if not page:
            break
        rows.extend(page)
        pages   += 1
        current  = page[-1][0] + 60_000   # next minute after last fetched bar
        if pages % 5 == 0:
            logger.info('fetching pages=%d candles=%d', pages, len(rows))
        if len(page) < _PAGE_SIZE:
            break
        time.sleep(0.25)                  # respect rate limits

    logger.info('fetch_complete pages=%d total_candles=%d', pages, len(rows))
    return _to_dataframe(rows)


# ── simulation helpers ─────────────────────────────────────────────────────

def _bar_values(
    df: pd.DataFrame,
    sma_s: pd.Series,
    rsi_s: pd.Series,
    i: int,
) -> tuple[float, float, float]:
    return float(df['close'].iloc[i]), float(sma_s.iloc[i]), float(rsi_s.iloc[i])


def _close_position(
    position: dict,
    exit_price: float,
    exit_ts: int,
    reason: str,
    balance: float,
) -> tuple[dict, float]:
    pnl_usdt, pnl_pct = calc_pnl(exit_price, position['entry_price'], position['qty'])
    trade = {
        'entry_price': position['entry_price'],
        'exit_price':  exit_price,
        'qty':         position['qty'],
        'pnl_usdt':    round(pnl_usdt, 6),
        'pnl_pct':     round(pnl_pct, 6),
        'result':      'WIN' if pnl_usdt >= 0 else 'LOSS',
        'reason':      reason,
        'entry_ts':    position['entry_ts'],
        'exit_ts':     exit_ts,
    }
    return trade, balance + pnl_usdt


def _process_bar(
    close: float,
    sma_val: float,
    rsi_val: float,
    ts: int,
    position: dict | None,
    balance: float,
) -> tuple[dict | None, float, dict | None]:
    """Return (new_position, new_balance, closed_trade_or_None)."""
    if position is None:
        if should_enter(close, sma_val, rsi_val, _RSI_THRESHOLD):
            qty = (balance * _RISK_PCT) / close
            return {'entry_price': close, 'qty': qty, 'entry_ts': ts}, balance, None
        return None, balance, None

    reason = check_exit(close, position['entry_price'], _SL_PCT, _TP_PCT)
    if reason:
        trade, new_balance = _close_position(position, close, ts, reason, balance)
        return None, new_balance, trade
    return position, balance, None


def _simulate(df: pd.DataFrame) -> tuple[list[dict], list[float]]:
    sma_s    = sma(df, _SMA_PERIOD)
    rsi_s    = rsi(df, _RSI_PERIOD)
    balance  = _BALANCE
    trades:   list[dict]  = []
    equity:   list[float] = [balance]
    position: dict | None = None

    for i in range(_MIN_CANDLES, len(df)):
        close, sma_val, rsi_val = _bar_values(df, sma_s, rsi_s, i)
        if pd.isna(sma_val) or pd.isna(rsi_val):
            continue
        position, balance, trade = _process_bar(
            close, sma_val, rsi_val, int(df['ts'].iloc[i]), position, balance,
        )
        if trade:
            trades.append(trade)
            equity.append(balance)

    return trades, equity


# ── metrics ────────────────────────────────────────────────────────────────

def _max_drawdown(equity: list[float]) -> float:
    peak   = equity[0]
    max_dd = 0.0
    for value in equity:
        if value > peak:
            peak = value
        dd = (peak - value) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(returns: list[float]) -> float:
    """Per-trade Sharpe ratio (non-annualized)."""
    if len(returns) < 2:
        return 0.0
    n        = len(returns)
    mean_r   = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r    = math.sqrt(variance) if variance > 0 else 0.0
    return mean_r / std_r if std_r > 0 else 0.0


def _ts_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _trade_metrics(trades: list[dict], equity: list[float]) -> dict:
    wins        = [t for t in trades if t['result'] == 'WIN']
    total_pnl   = sum(t['pnl_usdt'] for t in trades)
    returns     = [t['pnl_pct'] / 100 for t in trades]
    best_trade  = max(trades, key=lambda t: t['pnl_usdt'])
    worst_trade = min(trades, key=lambda t: t['pnl_usdt'])
    return {
        'total_pnl_usdt':   round(total_pnl, 4),
        'total_pnl_pct':    round(total_pnl / _BALANCE * 100, 4),
        'num_wins':         len(wins),
        'num_losses':       len(trades) - len(wins),
        'win_rate_pct':     round(len(wins) / len(trades) * 100, 2),
        'max_drawdown_pct': round(_max_drawdown(equity) * 100, 4),
        'sharpe_ratio':     round(_sharpe(returns), 4),
        'best_trade_usdt':  round(best_trade['pnl_usdt'], 4),
        'worst_trade_usdt': round(worst_trade['pnl_usdt'], 4),
    }


def _compute_metrics(
    trades: list[dict],
    equity: list[float],
    df: pd.DataFrame,
) -> dict:
    data_from = _ts_to_iso(int(df['ts'].iloc[0]))
    data_to   = _ts_to_iso(int(df['ts'].iloc[-1]))
    report: dict = {
        'period':     {'from': data_from, 'to': data_to, 'candles': len(df)},
        'parameters': {'symbol': _SYMBOL, 'timeframe': _TIMEFRAME,
                       'rsi_threshold': _RSI_THRESHOLD, 'sma_period': _SMA_PERIOD,
                       'sl_pct': _SL_PCT, 'tp_pct': _TP_PCT, 'risk_pct': _RISK_PCT},
        'initial_balance': _BALANCE,
        'final_balance':   round(equity[-1], 4),
        'num_trades':      len(trades),
    }
    if trades:
        report.update(_trade_metrics(trades, equity))
        report['trades'] = trades
    else:
        report['note'] = 'no trades executed during this period'
    return report


# ── output ─────────────────────────────────────────────────────────────────

def _save_report(report: dict) -> None:
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    _REPORT_FILE.write_text(json.dumps(report, indent=2))
    logger.info('report_saved path=%s', _REPORT_FILE)


def _log_summary(report: dict) -> None:
    sep = '─' * 52
    logger.info(sep)
    logger.info('BACKTEST  %s  →  %s', report['period']['from'], report['period']['to'])
    logger.info('Candles : %d   |   Trades: %d', report['period']['candles'], report['num_trades'])
    logger.info(sep)
    if report.get('note'):
        logger.info('Note: %s', report['note'])
        logger.info(sep)
        return
    sign = '+' if report['total_pnl_usdt'] >= 0 else ''
    logger.info('PnL         : %s%.4f USDT  (%s%.2f%%)',
                sign, report['total_pnl_usdt'], sign, report['total_pnl_pct'])
    logger.info('Balance     : %.4f  →  %.4f USDT',
                report['initial_balance'], report['final_balance'])
    logger.info('Win rate    : %.1f%%  (%d W / %d L)',
                report['win_rate_pct'], report['num_wins'], report['num_losses'])
    logger.info('Max drawdown: %.4f%%', report['max_drawdown_pct'])
    logger.info('Sharpe ratio: %.4f  (per-trade, non-annualized)', report['sharpe_ratio'])
    logger.info('Best trade  : +%.4f USDT', report['best_trade_usdt'])
    logger.info('Worst trade :  %.4f USDT', report['worst_trade_usdt'])
    logger.info(sep)


# ── entry point ────────────────────────────────────────────────────────────

def run() -> None:
    logger.info('backtest_start symbol=%s timeframe=%s lookback_days=%d',
                _SYMBOL, _TIMEFRAME, _LOOKBACK_DAYS)
    exchange = _make_exchange()

    logger.info('downloading historical data from Binance testnet...')
    df = _fetch_historical(exchange)

    if len(df) < _MIN_CANDLES + 1:
        logger.error('insufficient_data candles=%d required_minimum=%d', len(df), _MIN_CANDLES + 1)
        return

    logger.info('simulating strategy on %d candles...', len(df))
    trades, equity = _simulate(df)
    logger.info('simulation_complete trades=%d', len(trades))

    report = _compute_metrics(trades, equity, df)
    _save_report(report)
    _log_summary(report)


if __name__ == '__main__':
    run()
