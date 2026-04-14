"""Macro-level market filter combining funding rate and news sentiment.

Combines two signals:
  - Funding rate (Binance Futures public API) → bearish | bullish | neutral
  - News sentiment (CoinDesk RSS + Gemini API, cached 15 min) → positive | negative | neutral

get_mode() returns:
  AGGRESSIVE  — bearish funding + positive sentiment (mean-reversion bounce setup)
  NO_TRADE    — bearish funding + negative sentiment (double-negative, stay out)
  NORMAL      — all other combinations
"""

import json
import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────

_FUNDING_URL  = 'https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1'
_RSS_URL      = 'https://feeds.feedburner.com/CoinDesk'
_GEMINI_MODEL = 'gemini-1.5-flash'
_GEMINI_URL   = (
    f'https://generativelanguage.googleapis.com/v1beta/models/'
    f'{_GEMINI_MODEL}:generateContent'
)
_NEWS_LIMIT  = 5
_CACHE_TTL   = 900  # 15 minutes

FUNDING_BEARISH_THRESHOLD: float =  0.001
FUNDING_BULLISH_THRESHOLD: float = -0.001

AGGRESSIVE = 'AGGRESSIVE'
NO_TRADE   = 'NO_TRADE'
NORMAL     = 'NORMAL'


# ── state ──────────────────────────────────────────────────────────────────

@dataclass
class MacroState:
    funding_signal:       str    # 'bearish' | 'bullish' | 'neutral'
    sentiment:            str    # 'positive' | 'negative' | 'neutral'
    sentiment_confidence: float
    fetched_at:           float  # unix timestamp


# ── pure helpers ───────────────────────────────────────────────────────────

def _funding_signal_from(rate: float) -> str:
    if rate > FUNDING_BEARISH_THRESHOLD:
        return 'bearish'
    if rate < FUNDING_BULLISH_THRESHOLD:
        return 'bullish'
    return 'neutral'


def _mode_from(funding: str, sentiment: str) -> str:
    if funding == 'bearish' and sentiment == 'positive':
        return AGGRESSIVE
    if funding == 'bearish' and sentiment == 'negative':
        return NO_TRADE
    return NORMAL


# ── async fetchers ─────────────────────────────────────────────────────────

async def _fetch_funding_signal(session: aiohttp.ClientSession) -> str:
    """Fetch latest BTC funding rate from Binance Futures (no auth required)."""
    async with session.get(_FUNDING_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
        data = await r.json()
    rate   = float(data[0]['fundingRate'])
    signal = _funding_signal_from(rate)
    logger.info('funding_rate=%.6f signal=%s', rate, signal)
    return signal


async def _fetch_headlines(session: aiohttp.ClientSession) -> list[str]:
    """Fetch the latest _NEWS_LIMIT titles from CoinDesk RSS feed."""
    async with session.get(_RSS_URL, timeout=aiohttp.ClientTimeout(total=10)) as r:
        text = await r.text()
    root  = ET.fromstring(text)
    items = root.findall('.//item')[:_NEWS_LIMIT]
    return [item.findtext('title', default='') for item in items]


async def _call_gemini(
    session: aiohttp.ClientSession,
    api_key: str,
    headlines: list[str],
) -> tuple[str, float]:
    """Ask Gemini to classify market sentiment from headlines."""
    prompt = (
        'Based on these crypto headlines, reply ONLY with valid JSON — '
        'no markdown, no extra text: '
        '{"sentiment":"positive"|"negative"|"neutral","confidence":0.0-1.0}\n\n'
        + '\n'.join(f'- {h}' for h in headlines)
    )
    url  = f'{_GEMINI_URL}?key={api_key}'
    body = {'contents': [{'parts': [{'text': prompt}]}]}
    async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json()
    raw = data['candidates'][0]['content']['parts'][0]['text'].strip()
    raw = raw.removeprefix('```json').removeprefix('```').removesuffix('```').strip()
    try:
        parsed = json.loads(raw)
        return str(parsed.get('sentiment', 'neutral')), float(parsed.get('confidence', 0.5))
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning('gemini_parse_failed raw=%.80s', raw)
        return 'neutral', 0.0


# ── MacroFilter ────────────────────────────────────────────────────────────

class MacroFilter:
    """Evaluates and caches the macro trading mode every _CACHE_TTL seconds."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._api_key = api_key
        self._session = session
        self._state:  MacroState | None = None

    def _is_cache_valid(self) -> bool:
        return (self._state is not None
                and time.time() - self._state.fetched_at < _CACHE_TTL)

    async def _refresh(self) -> None:
        funding         = await _fetch_funding_signal(self._session)
        headlines       = await _fetch_headlines(self._session)
        sentiment, conf = await _call_gemini(self._session, self._api_key, headlines)
        self._state     = MacroState(
            funding_signal=funding,
            sentiment=sentiment,
            sentiment_confidence=conf,
            fetched_at=time.time(),
        )
        logger.info('macro_refresh funding=%s sentiment=%s confidence=%.2f',
                    funding, sentiment, conf)

    async def get_mode(self) -> str:
        """Return current macro mode, refreshing from APIs when cache expires."""
        if not self._is_cache_valid():
            try:
                await self._refresh()
            except Exception as exc:
                logger.warning('macro_refresh_failed error=%s returning=NORMAL', exc)
                if self._state is None:
                    self._state = MacroState('neutral', 'neutral', 0.0, 0.0)
        mode = _mode_from(self._state.funding_signal, self._state.sentiment)
        logger.info(
            'macro_mode=%s funding=%s sentiment=%s confidence=%.2f',
            mode, self._state.funding_signal, self._state.sentiment,
            self._state.sentiment_confidence,
        )
        return mode
