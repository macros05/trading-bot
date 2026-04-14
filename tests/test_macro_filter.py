"""
Tests for core/macro_filter.py.

Mocks all external calls (_fetch_funding_signal, _fetch_headlines, _call_gemini)
so no network traffic is required.

Run from project root:
    python -m unittest tests.test_macro_filter
"""
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.macro_filter import (
    AGGRESSIVE,
    FUNDING_BEARISH_THRESHOLD,
    FUNDING_BULLISH_THRESHOLD,
    NORMAL,
    NO_TRADE,
    MacroFilter,
    MacroState,
    _funding_signal_from,
    _mode_from,
)


# ---------------------------------------------------------------------------
# _funding_signal_from — pure function
# ---------------------------------------------------------------------------

class TestFundingSignalFrom(unittest.TestCase):

    def test_bearish_above_threshold(self):
        self.assertEqual(_funding_signal_from(FUNDING_BEARISH_THRESHOLD + 0.0001), 'bearish')

    def test_bullish_below_threshold(self):
        self.assertEqual(_funding_signal_from(FUNDING_BULLISH_THRESHOLD - 0.0001), 'bullish')

    def test_neutral_within_range(self):
        self.assertEqual(_funding_signal_from(0.0), 'neutral')

    def test_boundary_bearish_is_neutral(self):
        # strictly greater-than, so exact threshold = neutral
        self.assertEqual(_funding_signal_from(FUNDING_BEARISH_THRESHOLD), 'neutral')

    def test_boundary_bullish_is_neutral(self):
        self.assertEqual(_funding_signal_from(FUNDING_BULLISH_THRESHOLD), 'neutral')

    def test_large_positive_rate_is_bearish(self):
        self.assertEqual(_funding_signal_from(0.01), 'bearish')

    def test_large_negative_rate_is_bullish(self):
        self.assertEqual(_funding_signal_from(-0.01), 'bullish')


# ---------------------------------------------------------------------------
# _mode_from — pure function
# ---------------------------------------------------------------------------

class TestModeFrom(unittest.TestCase):

    def test_aggressive_bearish_funding_positive_sentiment(self):
        self.assertEqual(_mode_from('bearish', 'positive'), AGGRESSIVE)

    def test_no_trade_bearish_funding_negative_sentiment(self):
        self.assertEqual(_mode_from('bearish', 'negative'), NO_TRADE)

    def test_normal_neutral_funding_any_sentiment(self):
        self.assertEqual(_mode_from('neutral', 'positive'),  NORMAL)
        self.assertEqual(_mode_from('neutral', 'negative'),  NORMAL)
        self.assertEqual(_mode_from('neutral', 'neutral'),   NORMAL)

    def test_normal_bullish_funding(self):
        self.assertEqual(_mode_from('bullish', 'positive'),  NORMAL)
        self.assertEqual(_mode_from('bullish', 'negative'),  NORMAL)

    def test_normal_bearish_funding_neutral_sentiment(self):
        self.assertEqual(_mode_from('bearish', 'neutral'), NORMAL)


# ---------------------------------------------------------------------------
# MacroFilter.get_mode — async, patches the three fetcher functions
# ---------------------------------------------------------------------------

def _patch_fetchers(funding: str, sentiment: str, confidence: float = 0.8):
    """Context manager helper: patches all three I/O functions."""
    return (
        patch('core.macro_filter._fetch_funding_signal',
              new=AsyncMock(return_value=funding)),
        patch('core.macro_filter._fetch_headlines',
              new=AsyncMock(return_value=['headline one', 'headline two'])),
        patch('core.macro_filter._call_gemini',
              new=AsyncMock(return_value=(sentiment, confidence))),
    )


class TestMacroFilterGetMode(unittest.IsolatedAsyncioTestCase):

    def _make_filter(self) -> MacroFilter:
        return MacroFilter(api_key='test_key', session=MagicMock())

    async def test_aggressive_mode(self):
        mf = self._make_filter()
        p1, p2, p3 = _patch_fetchers('bearish', 'positive')
        with p1, p2, p3:
            mode = await mf.get_mode()
        self.assertEqual(mode, AGGRESSIVE)

    async def test_no_trade_mode(self):
        mf = self._make_filter()
        p1, p2, p3 = _patch_fetchers('bearish', 'negative')
        with p1, p2, p3:
            mode = await mf.get_mode()
        self.assertEqual(mode, NO_TRADE)

    async def test_normal_mode_neutral_funding(self):
        mf = self._make_filter()
        p1, p2, p3 = _patch_fetchers('neutral', 'positive')
        with p1, p2, p3:
            mode = await mf.get_mode()
        self.assertEqual(mode, NORMAL)

    async def test_normal_mode_bullish_funding(self):
        mf = self._make_filter()
        p1, p2, p3 = _patch_fetchers('bullish', 'negative')
        with p1, p2, p3:
            mode = await mf.get_mode()
        self.assertEqual(mode, NORMAL)

    async def test_cache_prevents_second_refresh(self):
        """Second call within TTL must not call the fetchers again."""
        mf = self._make_filter()
        mock_funding = AsyncMock(return_value='neutral')
        mock_headlines = AsyncMock(return_value=['h1'])
        mock_gemini = AsyncMock(return_value=('neutral', 0.5))
        with patch('core.macro_filter._fetch_funding_signal', mock_funding), \
             patch('core.macro_filter._fetch_headlines', mock_headlines), \
             patch('core.macro_filter._call_gemini', mock_gemini):
            await mf.get_mode()
            await mf.get_mode()
        mock_funding.assert_awaited_once()

    async def test_cache_expires_and_refreshes(self):
        """After TTL expires the fetchers are called again."""
        mf = self._make_filter()
        mock_funding = AsyncMock(return_value='neutral')
        mock_headlines = AsyncMock(return_value=['h1'])
        mock_gemini = AsyncMock(return_value=('neutral', 0.5))
        with patch('core.macro_filter._fetch_funding_signal', mock_funding), \
             patch('core.macro_filter._fetch_headlines', mock_headlines), \
             patch('core.macro_filter._call_gemini', mock_gemini):
            await mf.get_mode()
            # Force cache to expire
            mf._state = MacroState('neutral', 'neutral', 0.5, fetched_at=0.0)
            await mf.get_mode()
        self.assertEqual(mock_funding.await_count, 2)

    async def test_fallback_to_normal_when_refresh_fails_no_state(self):
        """If refresh raises and there is no prior state, return NORMAL."""
        mf = self._make_filter()
        with patch('core.macro_filter._fetch_funding_signal',
                   AsyncMock(side_effect=Exception('network error'))):
            mode = await mf.get_mode()
        self.assertEqual(mode, NORMAL)

    async def test_prior_state_preserved_on_refresh_failure(self):
        """If refresh raises but a valid old state exists, reuse it."""
        mf = self._make_filter()
        # Seed a stale bearish/positive state
        mf._state = MacroState('bearish', 'positive', 0.9, fetched_at=0.0)
        with patch('core.macro_filter._fetch_funding_signal',
                   AsyncMock(side_effect=Exception('timeout'))):
            mode = await mf.get_mode()
        self.assertEqual(mode, AGGRESSIVE)


if __name__ == '__main__':
    unittest.main()
