"""Long-only bar simulator for resampled timeframes (5min / 15min / 1h).

Execution model — documented to make the no-lookahead contract explicit:

* A signal evaluated on the CLOSE of bar t fills at the OPEN of bar t+1,
  long side only, with slippage applied against the trader.
* 'ts' is always the bucket START, so any event resolved on bar j is
  timestamped ts[j] + timeframe_ms (the bar's close time); 1m-precision
  exits are timestamped at the 1m bar's close time.
* Same-bar ambiguity: without df_1m, when a TF bar touches both SL and TP
  the STOP fires first (conservative). When df_1m is provided the
  underlying minutes decide which barrier was actually hit first (SL still
  wins inside a single ambiguous minute).
* A position still open when the data ends is force-closed at the last bar
  close ('end_of_data') so PnL accounting is always complete.

Costs mirror backtest/advanced.py: taker fee per side on notional plus
slippage per side on the fill price. Sizing is volatility-targeted
(risk_pct of balance at the stop distance) and capped at 1x balance (spot).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

import numpy as np
import pandas as pd

from config import SLIPPAGE, TAKER_FEE
from strategy.indicators import atr, donchian_low
from strategy.sessions import session_for_ts

logger = logging.getLogger(__name__)

SignalFn = Callable[[pd.DataFrame], pd.Series]

MINUTE_MS = 60_000
EXIT_MODES = ('fixed', 'atr', 'trail', 'triple_barrier')
_OHLCV_COLUMNS = ('ts', 'open', 'high', 'low', 'close', 'volume')


def timeframe_ms(rule: str) -> int:
    """Bar duration of a fixed-width pandas offset rule ('5min'/'15min'/'1h') in ms."""
    return int(pd.Timedelta(rule).total_seconds() * 1000)


def resample_1m(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate 1m OHLCV rows into *rule* bars; 'ts' becomes the bucket START.

    Integer bucket math instead of pandas datetime resampling keeps the
    epoch-ms contract independent of pandas datetime-resolution behavior.
    Buckets not fully covered by the source range (leading/trailing partials)
    are dropped so the simulator never acts on an incomplete bar.
    """
    if 'ts' not in df_1m.columns:
        raise ValueError("df_1m must contain a 'ts' column in ms since epoch")
    rule_ms = timeframe_ms(rule)
    if df_1m.empty:
        return pd.DataFrame({name: pd.Series(dtype='float64') for name in _OHLCV_COLUMNS})
    bucket = (df_1m['ts'] // rule_ms) * rule_ms
    bars = df_1m.groupby(bucket).agg(
        open=('open', 'first'), high=('high', 'max'), low=('low', 'min'),
        close=('close', 'last'), volume=('volume', 'sum'),
    )
    first_ts = int(df_1m['ts'].iloc[0])
    last_ts = int(df_1m['ts'].iloc[-1])
    bucket_ts = bars.index.to_numpy(dtype='int64')
    complete = (bucket_ts >= first_ts) & (bucket_ts + rule_ms <= last_ts + MINUTE_MS)
    out = bars.loc[complete].reset_index()
    out.columns = list(_OHLCV_COLUMNS)
    return out


@dataclass(frozen=True)
class TfSimParams:
    """Parameters for one long-only timeframe-simulator candidate."""
    label: str = 'tf-sim'
    timeframe: str = '5min'
    exit_mode: str = 'fixed'
    sl_pct: float = 0.01
    tp_pct: float = 0.02
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_tp_multiplier: float = 3.0
    min_sl_pct: float = 0.003
    trail_lookback: int = 10
    sigma_window: int = 20
    tb_tp_sigma: float = 1.5
    tb_sl_sigma: float = 1.0
    max_hold_bars: int = 0
    balance: float = 10_000.0
    risk_pct: float = 0.01
    taker_fee: float = TAKER_FEE
    slippage: float = SLIPPAGE
    signal_fn: SignalFn | None = None


@dataclass(frozen=True)
class _Arrays:
    ts: np.ndarray
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    tf_ms: int

    @property
    def size(self) -> int:
        return len(self.ts)

    @classmethod
    def from_frame(cls, df_tf: pd.DataFrame, timeframe: str) -> '_Arrays':
        return cls(
            ts=df_tf['ts'].to_numpy(dtype='int64'),
            opens=df_tf['open'].to_numpy(dtype='float64'),
            highs=df_tf['high'].to_numpy(dtype='float64'),
            lows=df_tf['low'].to_numpy(dtype='float64'),
            closes=df_tf['close'].to_numpy(dtype='float64'),
            tf_ms=timeframe_ms(timeframe),
        )


@dataclass(frozen=True)
class _MinuteArrays:
    ts: np.ndarray
    highs: np.ndarray
    lows: np.ndarray

    @classmethod
    def from_frame(cls, df_1m: pd.DataFrame) -> '_MinuteArrays':
        return cls(
            ts=df_1m['ts'].to_numpy(dtype='int64'),
            highs=df_1m['high'].to_numpy(dtype='float64'),
            lows=df_1m['low'].to_numpy(dtype='float64'),
        )

    def first_barrier_hit(self, start_ms: int, end_ms: int, sl_price: float,
                          tp_price: float | None) -> tuple[str, float, int] | None:
        """Walk the 1m rows of one TF bar; SL beats TP inside a single minute."""
        low = int(np.searchsorted(self.ts, start_ms, side='left'))
        high = int(np.searchsorted(self.ts, end_ms, side='left'))
        for k in range(low, high):
            if self.lows[k] <= sl_price:
                return 'stop_loss', sl_price, int(self.ts[k]) + MINUTE_MS
            if tp_price is not None and self.highs[k] >= tp_price:
                return 'take_profit', tp_price, int(self.ts[k]) + MINUTE_MS
        return None


@dataclass(frozen=True)
class _IndicatorLevels:
    atr_values: np.ndarray | None
    trail_floor: np.ndarray | None
    sigma_values: np.ndarray | None

    @classmethod
    def from_frame(cls, df_tf: pd.DataFrame, params: TfSimParams) -> '_IndicatorLevels':
        atr_values = (atr(df_tf, params.atr_period).to_numpy(dtype='float64')
                      if params.exit_mode == 'atr' else None)
        trail_floor = (donchian_low(df_tf, params.trail_lookback).to_numpy(dtype='float64')
                       if params.exit_mode == 'trail' else None)
        sigma_values = (df_tf['close'].pct_change()
                        .rolling(params.sigma_window, min_periods=params.sigma_window)
                        .std(ddof=0).to_numpy(dtype='float64')
                        if params.exit_mode == 'triple_barrier' else None)
        return cls(atr_values, trail_floor, sigma_values)


@dataclass
class _SimState:
    balance: float
    equity: list[float] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    fees_total: float = 0.0


def _validate_params(params: TfSimParams) -> None:
    if params.exit_mode not in EXIT_MODES:
        raise ValueError(f"unknown exit_mode '{params.exit_mode}', expected one of {EXIT_MODES}")
    if params.signal_fn is None:
        raise ValueError('params.signal_fn is required: simulate_tf has no built-in entry rule')


def _signal_array(df_tf: pd.DataFrame, params: TfSimParams) -> np.ndarray:
    raw = params.signal_fn(df_tf)
    if raw.dtype != bool:
        raw = raw.fillna(False)
    return np.asarray(raw, dtype=bool)


def _initial_barriers(params: TfSimParams, entry_fill: float, levels: _IndicatorLevels,
                      signal_index: int) -> tuple[float, float | None] | None:
    """SL/TP prices from the SIGNAL bar's indicator values; None = skip entry."""
    if params.exit_mode == 'fixed':
        return entry_fill * (1 - params.sl_pct), entry_fill * (1 + params.tp_pct)
    if params.exit_mode == 'atr':
        atr_value = float(levels.atr_values[signal_index])
        if not np.isfinite(atr_value) or atr_value <= 0:
            return None
        # the 0.3% floor protects against micro-stops; TP keeps the configured
        # reward:risk ratio even when the floor binds
        sl_distance = max(params.atr_sl_multiplier * atr_value, params.min_sl_pct * entry_fill)
        tp_distance = sl_distance * (params.atr_tp_multiplier / params.atr_sl_multiplier)
        return entry_fill - sl_distance, entry_fill + tp_distance
    if params.exit_mode == 'trail':
        floor_value = float(levels.trail_floor[signal_index])
        if not np.isfinite(floor_value) or floor_value >= entry_fill:
            return None
        return floor_value, None
    sigma_value = float(levels.sigma_values[signal_index])
    if not np.isfinite(sigma_value) or sigma_value <= 0:
        return None
    return (entry_fill * (1 - params.tb_sl_sigma * sigma_value),
            entry_fill * (1 + params.tb_tp_sigma * sigma_value))


def _first_barrier_hit(j: int, sl_price: float, tp_price: float | None, arrays: _Arrays,
                       minutes: _MinuteArrays | None) -> tuple[str, float, int] | None:
    if minutes is not None:
        return minutes.first_barrier_hit(int(arrays.ts[j]), int(arrays.ts[j]) + arrays.tf_ms,
                                         sl_price, tp_price)
    if arrays.lows[j] <= sl_price:
        return 'stop_loss', sl_price, int(arrays.ts[j]) + arrays.tf_ms
    if tp_price is not None and arrays.highs[j] >= tp_price:
        return 'take_profit', tp_price, int(arrays.ts[j]) + arrays.tf_ms
    return None


def _exit_event_on_bar(j: int, entry_index: int, sl_price: float, tp_price: float | None,
                       arrays: _Arrays, minutes: _MinuteArrays | None,
                       params: TfSimParams) -> tuple[str, float, int] | None:
    event = _first_barrier_hit(j, sl_price, tp_price, arrays, minutes)
    if (event is None and params.max_hold_bars > 0
            and (j - entry_index + 1) >= params.max_hold_bars):
        event = ('time_exit', float(arrays.closes[j]), int(arrays.ts[j]) + arrays.tf_ms)
    return event


def _book_trade(state: _SimState, params: TfSimParams, position: dict,
                reason: str, raw_exit_price: float, exit_ts: int) -> None:
    exit_fill = raw_exit_price * (1 - params.slippage)
    gross = (exit_fill - position['entry_fill']) * position['qty']
    fees = position['notional'] * 2 * params.taker_fee
    net_pnl = gross - fees
    state.fees_total += fees
    state.balance += net_pnl
    state.equity.append(state.balance)
    state.trades.append({
        'side':         'long',
        'entry_price':  position['entry_fill'],
        'exit_price':   exit_fill,
        'qty':          position['qty'],
        'pnl_usdt':     round(net_pnl, 4),
        'pnl_pct':      round(net_pnl / position['notional'] * 100, 4),
        'result':       'WIN' if net_pnl >= 0 else 'LOSS',
        'reason':       reason,
        'entry_ts':     position['entry_ts'],
        'exit_ts':      int(exit_ts),
        'duration_min': round((int(exit_ts) - position['entry_ts']) / MINUTE_MS, 1),
        'session':      session_for_ts(position['entry_ts']),
    })


def _open_and_run_trade(signal_index: int, arrays: _Arrays, levels: _IndicatorLevels,
                        minutes: _MinuteArrays | None, params: TfSimParams,
                        state: _SimState) -> int | None:
    """Fill at next bar OPEN, walk bars until an exit; returns the exit bar index."""
    entry_index = signal_index + 1
    entry_fill = float(arrays.opens[entry_index]) * (1 + params.slippage)
    barriers = _initial_barriers(params, entry_fill, levels, signal_index)
    if barriers is None:
        return None
    sl_price, tp_price = barriers
    effective_sl_pct = (entry_fill - sl_price) / entry_fill
    notional = min(params.risk_pct * state.balance / max(effective_sl_pct, 1e-9), state.balance)
    position = {'entry_fill': entry_fill, 'qty': notional / entry_fill,
                'notional': notional, 'entry_ts': int(arrays.ts[entry_index])}
    for j in range(entry_index, arrays.size):
        event = _exit_event_on_bar(j, entry_index, sl_price, tp_price, arrays, minutes, params)
        if event is not None:
            _book_trade(state, params, position, *event)
            return j
        if params.exit_mode == 'trail':
            floor_value = float(levels.trail_floor[j])
            if np.isfinite(floor_value):
                sl_price = max(sl_price, floor_value)
    last = arrays.size - 1
    _book_trade(state, params, position, 'end_of_data',
                float(arrays.closes[last]), int(arrays.ts[last]) + arrays.tf_ms)
    return arrays.size


def _by_side_view(trades: list[dict]) -> dict:
    wins = sum(1 for t in trades if t['result'] == 'WIN')
    count = len(trades)
    return {
        'long': {
            'trades': count,
            'wins': wins,
            'win_rate_pct': round(wins / count * 100, 2) if count else 0.0,
            'pnl': round(sum(t['pnl_usdt'] for t in trades), 4),
        },
        # long-only by design: shorts lost -80%..-99% across all 2024-26 relaxations
        'short': {'trades': 0, 'wins': 0, 'win_rate_pct': 0.0, 'pnl': 0.0},
    }


def _serializable_params(params: TfSimParams) -> dict:
    values = asdict(params)
    values.pop('signal_fn')
    return values


def _build_result(df_tf: pd.DataFrame, params: TfSimParams, state: _SimState) -> dict:
    def iso(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()

    candles = int(len(df_tf))
    return {
        'label':         params.label,
        'trades':        state.trades,
        'equity':        state.equity,
        'final_balance': round(state.balance, 4),
        'total_pnl':     round(state.balance - params.balance, 4),
        'total_fees':    round(state.fees_total, 4),
        'by_side':       _by_side_view(state.trades),
        'params':        _serializable_params(params),
        'period': {
            'from':    iso(int(df_tf['ts'].iloc[0])) if candles else None,
            'to':      iso(int(df_tf['ts'].iloc[-1])) if candles else None,
            'candles': candles,
        },
    }


def simulate_tf(df_tf: pd.DataFrame, params: TfSimParams,
                df_1m: pd.DataFrame | None = None) -> dict:
    """Run the long-only simulator on already-resampled *df_tf* bars.

    When *df_1m* is given, intrabar barrier resolution walks the underlying
    minutes (accurate path); otherwise the conservative same-bar SL-first
    rule applies. Result shape matches backtest/v7_full.py's simulate_v7,
    plus an always-present 'by_side' view with an empty short bucket.
    """
    _validate_params(params)
    arrays = _Arrays.from_frame(df_tf, params.timeframe)
    levels = _IndicatorLevels.from_frame(df_tf, params)
    minutes = _MinuteArrays.from_frame(df_1m) if df_1m is not None else None
    signals = _signal_array(df_tf, params)
    state = _SimState(balance=params.balance, equity=[params.balance])
    index = 0
    while index < arrays.size - 1 and state.balance > 0:
        if not signals[index]:
            index += 1
            continue
        exit_index = _open_and_run_trade(index, arrays, levels, minutes, params, state)
        index = exit_index if exit_index is not None else index + 1
    return _build_result(df_tf, params, state)
