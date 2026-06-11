"""Tests for backtest/hypotheses.py and the new indicators — synthetic data only."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest.hypotheses import (
    all_families, build_bb_rsi_signal, build_donchian_signal,
    build_rsi_mr_signal, family_bb_rsi, family_donchian_breakout,
    family_rsi_mr, hourly_ema_trend_flag,
)
from backtest.tf_sim import EXIT_MODES, resample_1m
from strategy.indicators import (
    atr_percentile, bollinger_lower, donchian_high, donchian_low,
)

BASE_TS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
MINUTE_MS = 60_000
HOUR_MS = 3_600_000


def frame_from_closes(closes: np.ndarray, step_ms: int, volume=1.0,
                      highs: np.ndarray | None = None,
                      lows: np.ndarray | None = None) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame({
        'ts': BASE_TS + np.arange(len(closes)) * step_ms,
        'open': opens,
        'high': highs if highs is not None else np.maximum(opens, closes) * 1.0002,
        'low': lows if lows is not None else np.minimum(opens, closes) * 0.9998,
        'close': closes,
        'volume': np.broadcast_to(np.asarray(volume, dtype=float), len(closes)).copy(),
    })


def random_walk_1m(minutes: int, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.001, minutes)))
    return frame_from_closes(closes, MINUTE_MS, volume=rng.uniform(1.0, 10.0, minutes))


class TestNewIndicators:

    def _ohlc(self) -> pd.DataFrame:
        closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        return frame_from_closes(closes, HOUR_MS,
                                 highs=closes + 0.5, lows=closes - 0.5)

    def test_donchian_high(self):
        series = donchian_high(self._ohlc(), 3)
        assert series.iloc[:2].isna().all()
        assert series.iloc[2] == 3.5
        assert series.iloc[-1] == 6.5

    def test_donchian_low(self):
        series = donchian_low(self._ohlc(), 3)
        assert series.iloc[:2].isna().all()
        assert series.iloc[2] == 0.5
        assert series.iloc[-1] == 3.5

    def test_bollinger_lower_flat_series_equals_close(self):
        df = frame_from_closes(np.full(25, 100.0), HOUR_MS)
        series = bollinger_lower(df, 20, 2.0)
        assert series.iloc[:19].isna().all()
        assert series.iloc[-1] == pytest.approx(100.0)

    def test_bollinger_lower_sits_below_mean(self):
        closes = 100.0 + np.sin(np.arange(40))
        series = bollinger_lower(frame_from_closes(closes, HOUR_MS), 20, 2.0)
        rolling_mean = pd.Series(closes).rolling(20).mean()
        assert (series.iloc[19:] < rolling_mean.iloc[19:]).all()

    def test_atr_percentile_rises_with_expanding_ranges(self):
        n = 40
        closes = np.full(n, 100.0)
        spread = 0.1 + 0.05 * np.arange(n)
        df = frame_from_closes(closes, HOUR_MS, highs=closes + spread, lows=closes - spread)
        series = atr_percentile(df, period=3, window=10)
        defined = series.dropna()
        assert not defined.empty
        assert ((defined > 0.0) & (defined <= 1.0)).all()
        # strictly expanding true range -> current ATR is the max of any window
        assert series.iloc[-1] == pytest.approx(1.0)


class TestSignals:

    def test_rsi_mr_fires_on_oversold(self):
        closes = 100.0 * np.exp(np.cumsum(np.full(40, -0.002)))
        df_tf = frame_from_closes(closes, 5 * MINUTE_MS)
        signal = build_rsi_mr_signal(35.0, use_trend_filter=False)(df_tf)
        assert bool(signal.iloc[-1])
        assert not signal.iloc[:14].any()  # RSI undefined during warmup

    def test_rsi_mr_trend_filter_fails_closed_without_ema_history(self):
        closes = 100.0 * np.exp(np.cumsum(np.full(40, -0.002)))
        df_tf = frame_from_closes(closes, 5 * MINUTE_MS)
        signal = build_rsi_mr_signal(35.0, use_trend_filter=True)(df_tf)
        assert not signal.any()  # EMA200 on 1h needs months of history

    def test_hourly_ema_trend_flag_no_lookahead(self):
        # 8 hours of rising 5min closes; period=3 so the EMA defines quickly
        closes = 100.0 + 0.01 * np.arange(8 * 12)
        df_tf = frame_from_closes(closes, 5 * MINUTE_MS)
        flag = hourly_ema_trend_flag(df_tf, period=3)
        assert flag.dtype == bool
        # bars starting before 3h can never see a defined EMA value; the bar
        # starting exactly at 3h is the first that may use hour 2's close
        assert not flag.iloc[:3 * 12].any()
        assert bool(flag.iloc[3 * 12])
        assert bool(flag.iloc[-1])

    def test_hourly_ema_trend_flag_false_in_downtrend(self):
        closes = 100.0 - 0.01 * np.arange(8 * 12)
        df_tf = frame_from_closes(closes, 5 * MINUTE_MS)
        assert not hourly_ema_trend_flag(df_tf, period=3).any()

    def _breakout_frame(self, last_volume: float = 1.0) -> pd.DataFrame:
        closes = np.concatenate([np.full(25, 100.0), [102.0]])
        highs = np.concatenate([np.full(25, 101.0), [102.5]])
        lows = closes - 1.0
        volume = np.concatenate([np.full(25, 1.0), [last_volume]])
        return frame_from_closes(closes, HOUR_MS, volume=volume, highs=highs, lows=lows)

    def test_donchian_fires_only_on_breakout(self):
        signal = build_donchian_signal(20, False, False)(self._breakout_frame())
        assert bool(signal.iloc[-1])
        assert not signal.iloc[:-1].any()

    def test_donchian_volume_confirm_gates_entry(self):
        quiet = build_donchian_signal(20, True, False)(self._breakout_frame(last_volume=1.0))
        assert not quiet.any()
        loud = build_donchian_signal(20, True, False)(self._breakout_frame(last_volume=5.0))
        assert bool(loud.iloc[-1])

    def test_donchian_regime_filter_fails_closed_during_warmup(self):
        # 26 bars << REGIME_WINDOW_BARS -> percentile NaN -> no signals
        signal = build_donchian_signal(20, False, True)(self._breakout_frame())
        assert not signal.any()

    def test_bb_rsi_fires_on_crash_bar_only(self):
        closes = np.concatenate([np.full(30, 100.0), [90.0]])
        df_tf = frame_from_closes(closes, 15 * MINUTE_MS)
        signal = build_bb_rsi_signal(30.0)(df_tf)
        assert bool(signal.iloc[-1])
        assert not signal.iloc[:-1].any()


class TestFamilies:

    @pytest.mark.parametrize('family,cap,expected', [
        (family_rsi_mr('5min'), 24, 18),
        (family_rsi_mr('15min'), 24, 18),
        (family_donchian_breakout(), 16, 16),
        (family_bb_rsi(), 8, 4),
    ], ids=['rsi_mr_5min', 'rsi_mr_15min', 'donchian_1h', 'bb_rsi_15min'])
    def test_combo_caps(self, family, cap, expected):
        assert len(family.candidates) == expected
        assert len(family.candidates) <= cap

    def test_rsi_mr_rejects_unknown_timeframe(self):
        with pytest.raises(ValueError):
            family_rsi_mr('1m')

    def test_labels_unique_across_all_families(self):
        labels = [c.label for f in all_families() for c in f.candidates]
        assert len(labels) == 56
        assert len(set(labels)) == len(labels)

    def test_candidates_are_wfa_compatible(self):
        for family in all_families():
            for candidate in family.candidates:
                assert candidate.timeframe == family.timeframe
                assert candidate.signal_fn is not None
                assert candidate.exit_mode in EXIT_MODES
                assert candidate.risk_pct == 0.01
                # wfa.py carries the compounding balance via dataclasses.replace
                assert replace(candidate, balance=5_000.0).balance == 5_000.0

    def test_every_candidate_simulates_clean_and_long_only(self):
        df_1m = random_walk_1m(2 * 1440)
        for family in all_families():
            simulate_fn = family.build_simulate_fn()
            for candidate in family.candidates:
                result = simulate_fn(df_1m, candidate)
                assert isinstance(result['trades'], list), candidate.label
                assert all(t['side'] == 'long' for t in result['trades'])
                assert result['by_side']['short'] == {
                    'trades': 0, 'wins': 0, 'win_rate_pct': 0.0, 'pnl': 0.0,
                }

    def test_simulate_fn_resamples_to_family_timeframe(self):
        df_1m = random_walk_1m(6 * 60)
        family = family_bb_rsi()
        result = family.build_simulate_fn()(df_1m, family.candidates[0])
        assert result['period']['candles'] == len(resample_1m(df_1m, '15min'))

    def test_rsi_mr_produces_long_trades_on_dip(self):
        down = 100.0 * np.exp(np.cumsum(np.full(360, -0.0006)))
        up = down[-1] * np.exp(np.cumsum(np.full(360, 0.0006)))
        df_1m = frame_from_closes(np.concatenate([down, up]), MINUTE_MS)
        family = family_rsi_mr('5min')
        candidate = next(c for c in family.candidates
                         if 'th35' in c.label and 'notrend' in c.label
                         and c.exit_mode == 'fixed')
        result = family.build_simulate_fn()(df_1m, candidate)
        assert len(result['trades']) > 0
        assert all(t['side'] == 'long' for t in result['trades'])
        assert result['by_side']['long']['trades'] == len(result['trades'])
        assert result['by_side']['short']['trades'] == 0
