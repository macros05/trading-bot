"""Tests for backtest/tf_sim.py — synthetic bars only, never the pkl caches."""
import json

import pandas as pd
import pytest

from backtest.tf_sim import TfSimParams, resample_1m, simulate_tf, timeframe_ms
from config import SLIPPAGE

BASE_TS = 1_704_067_200_000  # 2024-01-01T00:00:00Z — hour-aligned bucket start
MINUTE_MS = 60_000
FIVE_MINUTES_MS = 300_000


def build_bars(bars: list[tuple[float, float, float, float]],
               step_ms: int = FIVE_MINUTES_MS, volume: float = 1.0) -> pd.DataFrame:
    opens, highs, lows, closes = zip(*bars)
    return pd.DataFrame({
        'ts': [BASE_TS + i * step_ms for i in range(len(bars))],
        'open': list(opens), 'high': list(highs), 'low': list(lows),
        'close': list(closes), 'volume': [float(volume)] * len(bars),
    })


def flat(price: float) -> tuple[float, float, float, float]:
    return (price, price, price, price)


def signal_at(*bar_ts_values: int):
    wanted = set(bar_ts_values)

    def signal_fn(df_tf: pd.DataFrame) -> pd.Series:
        return df_tf['ts'].isin(wanted)

    return signal_fn


def fixed_params(**overrides) -> TfSimParams:
    defaults: dict = dict(label='test', timeframe='5min', exit_mode='fixed',
                          sl_pct=0.02, tp_pct=0.02, max_hold_bars=0)
    defaults.update(overrides)
    return TfSimParams(**defaults)


class TestResample:

    def _one_minute_frame(self, num_rows: int) -> pd.DataFrame:
        bars = [(i + 1.0, i + 1.5, i + 0.5, i + 1.0) for i in range(num_rows)]
        return build_bars(bars, step_ms=MINUTE_MS)

    def test_aggregates_ohlcv(self):
        result = resample_1m(self._one_minute_frame(10), '5min')
        assert len(result) == 2
        first = result.iloc[0]
        assert first['open'] == 1.0
        assert first['high'] == 5.5
        assert first['low'] == 0.5
        assert first['close'] == 5.0
        assert first['volume'] == 5.0
        assert result.iloc[1]['close'] == 10.0

    def test_drops_incomplete_bucket(self):
        # 12 rows = 2 complete 5min buckets + 2 stray minutes that must vanish
        result = resample_1m(self._one_minute_frame(12), '5min')
        assert len(result) == 2

    def test_ts_is_bucket_start_in_ms(self):
        result = resample_1m(self._one_minute_frame(10), '5min')
        assert result['ts'].tolist() == [BASE_TS, BASE_TS + FIVE_MINUTES_MS]

    def test_one_hour_rule(self):
        assert timeframe_ms('1h') == 3_600_000
        assert len(resample_1m(self._one_minute_frame(61), '1h')) == 1

    def test_requires_ts_column(self):
        with pytest.raises(ValueError):
            resample_1m(pd.DataFrame({'close': [1.0]}), '5min')


class TestNoLookahead:

    def _staircase(self) -> pd.DataFrame:
        return build_bars([flat(100.0 + i) for i in range(6)])

    def test_entry_fills_at_next_bar_open(self):
        params = fixed_params(sl_pct=0.10, tp_pct=0.10, max_hold_bars=2,
                              signal_fn=signal_at(BASE_TS + 2 * FIVE_MINUTES_MS))
        result = simulate_tf(self._staircase(), params)
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        # signal on bar 2 close -> fill at bar 3 OPEN (103), never bar 2 prices
        assert trade['entry_price'] == pytest.approx(103.0 * (1 + SLIPPAGE))
        assert trade['entry_ts'] == BASE_TS + 3 * FIVE_MINUTES_MS
        assert trade['reason'] == 'time_exit'
        assert trade['exit_price'] == pytest.approx(104.0 * (1 - SLIPPAGE))
        assert trade['exit_ts'] == BASE_TS + 5 * FIVE_MINUTES_MS
        assert trade['duration_min'] == pytest.approx(10.0)

    def test_signal_on_last_bar_produces_no_trade(self):
        params = fixed_params(signal_fn=signal_at(BASE_TS + 5 * FIVE_MINUTES_MS))
        result = simulate_tf(self._staircase(), params)
        assert result['trades'] == []
        assert result['final_balance'] == params.balance


def ambiguous_bar_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """1m rows where TP is touched on minute 6 and SL on minute 7; the second
    5min bar therefore touches BOTH barriers."""
    rows = [flat(100.0)] * 5 + [
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 103.0, 100.0, 102.0),
        (100.0, 100.0, 95.0, 96.0),
        flat(96.0),
        flat(96.0),
    ]
    df_1m = build_bars(rows, step_ms=MINUTE_MS)
    return df_1m, resample_1m(df_1m, '5min')


class TestExitPaths:

    def test_conservative_same_bar_rule_sl_first(self):
        _, df_tf = ambiguous_bar_frames()
        params = fixed_params(signal_fn=signal_at(BASE_TS))
        result = simulate_tf(df_tf, params)  # no df_1m -> pessimistic rule
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['reason'] == 'stop_loss'
        expected_sl = 100.0 * (1 + SLIPPAGE) * (1 - 0.02)
        assert trade['exit_price'] == pytest.approx(expected_sl * (1 - SLIPPAGE))
        assert trade['exit_ts'] == BASE_TS + 2 * FIVE_MINUTES_MS

    def test_1m_precision_resolves_tp_first(self):
        df_1m, df_tf = ambiguous_bar_frames()
        params = fixed_params(signal_fn=signal_at(BASE_TS))
        result = simulate_tf(df_tf, params, df_1m=df_1m)
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['reason'] == 'take_profit'
        expected_tp = 100.0 * (1 + SLIPPAGE) * (1 + 0.02)
        assert trade['exit_price'] == pytest.approx(expected_tp * (1 - SLIPPAGE))
        # TP was hit inside the 7th minute, not at the 5min bar boundary
        assert trade['exit_ts'] == BASE_TS + 7 * MINUTE_MS

    def test_trailing_donchian_ratchet(self):
        bars = [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 101.0, 99.2, 100.5),
            (100.5, 101.5, 99.5, 101.0),
            (101.0, 102.0, 100.0, 101.5),
            (101.5, 102.5, 100.5, 102.0),
            (102.0, 103.0, 101.5, 102.5),
            (102.5, 104.0, 102.0, 103.5),
            (103.5, 104.0, 100.0, 101.0),
        ]
        params = TfSimParams(label='trail', timeframe='5min', exit_mode='trail',
                             trail_lookback=3,
                             signal_fn=signal_at(BASE_TS + 4 * FIVE_MINUTES_MS))
        result = simulate_tf(build_bars(bars), params)
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['reason'] == 'stop_loss'
        # initial stop 99.5 ratcheted to 100.0 then 100.5 before bar 7's drop
        assert trade['exit_price'] == pytest.approx(100.5 * (1 - SLIPPAGE))
        expected_entry = 102.0 * (1 + SLIPPAGE)
        expected_notional = 10_000.0 * 0.01 / ((expected_entry - 99.5) / expected_entry)
        assert trade['qty'] == pytest.approx(expected_notional / expected_entry)

    def test_atr_floor_scales_stop_and_target(self):
        # ATR is 0.02 (0.02%): raw 1.5x stop would sit at -0.03% and be hit by
        # bar 20's -0.2% low; the 0.3% floor must keep the trade alive until TP.
        bars = [(100.0, 100.01, 99.99, 100.0)] * 20 + [
            (100.0, 100.2, 99.8, 100.0),
            (100.0, 100.3, 99.8, 100.1),
            (100.1, 101.0, 99.9, 100.5),
        ]
        params = TfSimParams(label='atr-floor', timeframe='5min', exit_mode='atr',
                             atr_sl_multiplier=1.5, atr_tp_multiplier=3.0,
                             min_sl_pct=0.003,
                             signal_fn=signal_at(BASE_TS + 19 * FIVE_MINUTES_MS))
        result = simulate_tf(build_bars(bars), params)
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['reason'] == 'take_profit'
        entry_fill = 100.0 * (1 + SLIPPAGE)
        floored_sl_distance = 0.003 * entry_fill
        expected_tp = entry_fill + floored_sl_distance * (3.0 / 1.5)
        assert trade['exit_price'] == pytest.approx(expected_tp * (1 - SLIPPAGE))

    def test_triple_barrier_levels(self):
        bars = [
            (100.0, 100.6, 99.9, 100.0),
            (100.0, 100.6, 99.9, 100.5),
            (100.5, 100.6, 99.9, 100.0),
            (100.0, 100.6, 99.9, 100.5),
            (100.5, 100.6, 99.9, 100.0),
            (100.0, 100.3, 99.9, 100.2),
            (100.2, 101.5, 100.0, 101.0),
        ]
        df_tf = build_bars(bars)
        params = TfSimParams(label='tb', timeframe='5min',
                             exit_mode='triple_barrier', sigma_window=4,
                             tb_tp_sigma=1.5, tb_sl_sigma=1.0, max_hold_bars=96,
                             signal_fn=signal_at(BASE_TS + 4 * FIVE_MINUTES_MS))
        result = simulate_tf(df_tf, params)
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        assert trade['reason'] == 'take_profit'
        sigma = (df_tf['close'].pct_change()
                 .rolling(4, min_periods=4).std(ddof=0).iloc[4])
        expected_tp = 100.0 * (1 + SLIPPAGE) * (1 + 1.5 * sigma)
        assert trade['exit_price'] == pytest.approx(expected_tp * (1 - SLIPPAGE))


class TestCostsAndResultShape:

    def _cost_run(self) -> dict:
        params = fixed_params(sl_pct=0.05, tp_pct=0.05, max_hold_bars=2,
                              signal_fn=signal_at(BASE_TS + FIVE_MINUTES_MS))
        return simulate_tf(build_bars([flat(100.0)] * 6), params)

    def test_round_trip_costs_30_bps_of_notional(self):
        result = self._cost_run()
        assert len(result['trades']) == 1
        trade = result['trades'][0]
        notional = 10_000.0 * 0.01 / 0.05
        # 2 x 0.10% fee + 2 x 0.05% slippage on a flat price = ~ -0.30%
        assert trade['pnl_usdt'] / notional == pytest.approx(-0.003, abs=1e-4)
        assert trade['pnl_pct'] == pytest.approx(-0.3, abs=0.01)
        assert result['total_fees'] == pytest.approx(notional * 0.002)

    def test_long_only_short_bucket_always_present(self):
        result = self._cost_run()
        assert all(t['side'] == 'long' for t in result['trades'])
        assert result['by_side']['short'] == {
            'trades': 0, 'wins': 0, 'win_rate_pct': 0.0, 'pnl': 0.0,
        }
        assert result['by_side']['long']['trades'] == 1

    def test_no_signal_produces_clean_empty_result(self):
        params = fixed_params(signal_fn=lambda df: pd.Series(False, index=df.index))
        result = simulate_tf(build_bars([flat(100.0)] * 5), params)
        assert result['trades'] == []
        assert result['final_balance'] == params.balance
        assert result['by_side']['short']['trades'] == 0

    def test_empty_frame_is_handled(self):
        empty = pd.DataFrame({'ts': [], 'open': [], 'high': [], 'low': [],
                              'close': [], 'volume': []})
        result = simulate_tf(empty, fixed_params(signal_fn=signal_at()))
        assert result['trades'] == []
        assert result['period']['candles'] == 0

    def test_result_is_json_serializable(self):
        json.dumps(self._cost_run())

    def test_missing_signal_fn_raises(self):
        with pytest.raises(ValueError):
            simulate_tf(build_bars([flat(100.0)] * 3), TfSimParams())

    def test_unknown_exit_mode_raises(self):
        params = fixed_params(exit_mode='martingale', signal_fn=signal_at())
        with pytest.raises(ValueError):
            simulate_tf(build_bars([flat(100.0)] * 3), params)
