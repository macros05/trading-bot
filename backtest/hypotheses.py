"""Strategy hypothesis families on higher timeframes (AUDIT.md C2 #1).

The live 1m strategy yields ~8 trades/year — statistically hopeless against
the >=100-trades promotion gate. Each family here targets 5min/15min/1h bars
to raise trade frequency while honoring the constraints burned in by past
failures: long-only, fees+slippage always on, ATR exits computed on the
signal timeframe (never on 1m bars) with a 0.3% stop floor.

Each family exposes a name, its timeframe rule, a bounded candidate grid
(label + TfSimParams) and ``build_simulate_fn()`` returning a generic
``simulate_fn(df_1m_slice, params) -> result`` compatible with the
walk-forward engine in backtest/wfa.py: the 1m slice is resampled to the
family timeframe and exits are resolved with 1m precision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from backtest.tf_sim import TfSimParams, resample_1m, simulate_tf, timeframe_ms
from strategy.indicators import (
    atr_percentile, bollinger_lower, donchian_high, ema, rsi, volume_sma,
)

logger = logging.getLogger(__name__)

SignalFn = Callable[[pd.DataFrame], pd.Series]
SimulateFn = Callable[[pd.DataFrame, TfSimParams], dict]

RSI_MR_TIMEFRAMES = ('5min', '15min')
RSI_MR_THRESHOLDS = (25.0, 30.0, 35.0)
RSI_MR_FIXED_EXITS = ((0.010, 0.020), (0.015, 0.030))
RSI_MR_MAX_HOLD_BARS = 48
DONCHIAN_CHANNELS = (20, 55)
DONCHIAN_TRAIL_LOOKBACK = 10
VOLUME_CONFIRM_FACTOR = 1.5
# 90 days of 1h bars for the ATR-percentile regime filter
REGIME_WINDOW_BARS = 90 * 24
BB_RSI_THRESHOLDS = (30.0, 35.0)
BB_RSI_BARRIERS = ((1.5, 1.0), (2.0, 1.5))
BB_RSI_MAX_HOLD_BARS = 96
MIN_SL_PCT_FLOOR = 0.003


@dataclass(frozen=True)
class HypothesisFamily:
    """One hypothesis family: a named, capped grid of long-only candidates."""
    name: str
    timeframe: str
    candidates: tuple[TfSimParams, ...]

    def build_simulate_fn(self) -> SimulateFn:
        """Generic simulate_fn(df_1m, params) for the walk-forward engine."""
        timeframe = self.timeframe

        def simulate_fn(df_1m: pd.DataFrame, params: TfSimParams) -> dict:
            df_tf = resample_1m(df_1m, timeframe)
            return simulate_tf(df_tf, params, df_1m=df_1m)

        return simulate_fn


def hourly_ema_trend_flag(df_tf: pd.DataFrame, period: int = 200) -> pd.Series:
    """Boolean 1h-trend flag (close > EMA(period) on 1h closes) on the tf grid.

    Each hourly value is only 'known' at its bucket END; lookup uses the tf
    bar START, so a bar can never see the hour it lives in — strictly no
    lookahead, at the cost of being one hour conservative. False while the
    EMA is undefined, so the filter fails closed during warmup.
    """
    hour_ms = timeframe_ms('1h')
    if df_tf.empty:
        return pd.Series(np.zeros(0, dtype=bool), index=df_tf.index)
    bucket = (df_tf['ts'] // hour_ms) * hour_ms
    hourly_close = df_tf.groupby(bucket)['close'].last()
    hourly_ema = ema(hourly_close, period)
    bullish = ((hourly_close > hourly_ema) & hourly_ema.notna()).to_numpy()
    known_at = hourly_close.index.to_numpy(dtype='int64') + hour_ms
    positions = np.searchsorted(known_at, df_tf['ts'].to_numpy(dtype='int64'),
                                side='right') - 1
    flag = np.where(positions >= 0, bullish[np.maximum(positions, 0)], False)
    return pd.Series(flag, index=df_tf.index)


def build_rsi_mr_signal(threshold: float, use_trend_filter: bool,
                        rsi_period: int = 14) -> SignalFn:
    """RSI mean-reversion entry: RSI(period) < threshold, optional 1h EMA200 trend gate."""
    def signal_fn(df_tf: pd.DataFrame) -> pd.Series:
        entries = rsi(df_tf, rsi_period) < threshold
        if use_trend_filter:
            entries &= hourly_ema_trend_flag(df_tf)
        return entries

    return signal_fn


def build_donchian_signal(channel_period: int, use_volume_confirm: bool,
                          use_regime_filter: bool) -> SignalFn:
    """Donchian breakout entry: close > prior channel high, optional confirms."""
    def signal_fn(df_tf: pd.DataFrame) -> pd.Series:
        # shift(1): a bar must break the PRIOR channel, never its own high
        entries = df_tf['close'] > donchian_high(df_tf, channel_period).shift(1)
        if use_volume_confirm:
            entries &= df_tf['volume'] > VOLUME_CONFIRM_FACTOR * volume_sma(df_tf, 20)
        if use_regime_filter:
            entries &= atr_percentile(df_tf, 14, REGIME_WINDOW_BARS) > 0.5
        return entries

    return signal_fn


def build_bb_rsi_signal(rsi_threshold: float, bb_period: int = 20,
                        bb_num_std: float = 2.0) -> SignalFn:
    """Bollinger + RSI dip entry: close below the lower band AND RSI(14) < threshold."""
    def signal_fn(df_tf: pd.DataFrame) -> pd.Series:
        below_band = df_tf['close'] < bollinger_lower(df_tf, bb_period, bb_num_std)
        return below_band & (rsi(df_tf, 14) < rsi_threshold)

    return signal_fn


def family_rsi_mr(tf: str) -> HypothesisFamily:
    """RSI(14) mean-reversion on 5min or 15min bars; 18 candidates (cap 24)."""
    if tf not in RSI_MR_TIMEFRAMES:
        raise ValueError(f"family_rsi_mr supports {RSI_MR_TIMEFRAMES}, got '{tf}'")
    candidates: list[TfSimParams] = []
    for threshold in RSI_MR_THRESHOLDS:
        for use_trend_filter in (False, True):
            signal_fn = build_rsi_mr_signal(threshold, use_trend_filter)
            trend_tag = 'trend' if use_trend_filter else 'notrend'
            prefix = f'rsi_mr_{tf}_th{threshold:g}_{trend_tag}'
            for sl_pct, tp_pct in RSI_MR_FIXED_EXITS:
                candidates.append(TfSimParams(
                    label=f'{prefix}_fixed_sl{sl_pct * 100:g}_tp{tp_pct * 100:g}',
                    timeframe=tf, exit_mode='fixed', sl_pct=sl_pct, tp_pct=tp_pct,
                    max_hold_bars=RSI_MR_MAX_HOLD_BARS, signal_fn=signal_fn,
                ))
            candidates.append(TfSimParams(
                label=f'{prefix}_atr_sl1.5_tp3.0',
                timeframe=tf, exit_mode='atr',
                atr_sl_multiplier=1.5, atr_tp_multiplier=3.0,
                min_sl_pct=MIN_SL_PCT_FLOOR,
                max_hold_bars=RSI_MR_MAX_HOLD_BARS, signal_fn=signal_fn,
            ))
    return HypothesisFamily(name=f'rsi_mr_{tf}', timeframe=tf,
                            candidates=tuple(candidates))


def family_donchian_breakout() -> HypothesisFamily:
    """Donchian breakout on 1h bars; 16 candidates (cap 16)."""
    candidates: list[TfSimParams] = []
    for channel_period in DONCHIAN_CHANNELS:
        for use_volume_confirm in (False, True):
            for use_regime_filter in (False, True):
                signal_fn = build_donchian_signal(
                    channel_period, use_volume_confirm, use_regime_filter)
                volume_tag = 'vol' if use_volume_confirm else 'novol'
                regime_tag = 'regime' if use_regime_filter else 'noregime'
                prefix = f'donchian_1h_n{channel_period}_{volume_tag}_{regime_tag}'
                candidates.append(TfSimParams(
                    label=f'{prefix}_trail{DONCHIAN_TRAIL_LOOKBACK}',
                    timeframe='1h', exit_mode='trail',
                    trail_lookback=DONCHIAN_TRAIL_LOOKBACK, signal_fn=signal_fn,
                ))
                candidates.append(TfSimParams(
                    label=f'{prefix}_atr_sl2.0_tp3.0',
                    timeframe='1h', exit_mode='atr',
                    atr_sl_multiplier=2.0, atr_tp_multiplier=3.0,
                    min_sl_pct=MIN_SL_PCT_FLOOR, signal_fn=signal_fn,
                ))
    return HypothesisFamily(name='donchian_breakout_1h', timeframe='1h',
                            candidates=tuple(candidates))


def family_bb_rsi() -> HypothesisFamily:
    """Bollinger(20,2)+RSI dip-buy on 15min bars, triple-barrier exits; 4 candidates (cap 8)."""
    candidates: list[TfSimParams] = []
    for rsi_threshold in BB_RSI_THRESHOLDS:
        signal_fn = build_bb_rsi_signal(rsi_threshold)
        for tb_tp_sigma, tb_sl_sigma in BB_RSI_BARRIERS:
            candidates.append(TfSimParams(
                label=(f'bb_rsi_15min_th{rsi_threshold:g}'
                       f'_tb_tp{tb_tp_sigma:g}_sl{tb_sl_sigma:g}'),
                timeframe='15min', exit_mode='triple_barrier',
                sigma_window=20, tb_tp_sigma=tb_tp_sigma, tb_sl_sigma=tb_sl_sigma,
                max_hold_bars=BB_RSI_MAX_HOLD_BARS, signal_fn=signal_fn,
            ))
    return HypothesisFamily(name='bb_rsi_15min', timeframe='15min',
                            candidates=tuple(candidates))


def all_families() -> list[HypothesisFamily]:
    """Every hypothesis family for the WFA runner: 18+18+16+4 = 56 candidates."""
    return [
        family_rsi_mr('5min'),
        family_rsi_mr('15min'),
        family_donchian_breakout(),
        family_bb_rsi(),
    ]
