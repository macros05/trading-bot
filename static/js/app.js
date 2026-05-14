'use strict';

// ── Regex patterns ────────────────────────────────────────────────────────────
const RE_TICK       = /tick\s+symbol=(\S+)\s+close=([\d.]+)\s+sma\d+=([.\d]+)\s+rsi\d+=([\d.]+)(?:\s+vol_ratio=[.\d]+)?\s+state=(\S+)/;
const RE_TRADE_OPEN = /paper_trade_open\s+entry_price=([\d.]+)\s+qty=([\d.]+)/;
const RE_UNREALIZED = /unrealized_pnl=([-\d.]+)\s+unrealized_pnl_pct=([-\d.]+)/;
const RE_TRADE_CLOSE = /paper_trade_close/;
const RE_TS         = /^(\d{4}-\d{2}-\d{2}\s[\d:,]+)/;

// ── App state ─────────────────────────────────────────────────────────────────
const INITIAL_BALANCE = 10000;
const POLL_MS         = 5000;
const STALE_MS        = 2 * 60 * 1000;

let lastTickMs     = null;
let activePosition = null;
let lastUnrealized = null;
let lastClose      = null;
let lastSma        = null;
let lastRsi        = null;
let lastSymbol     = 'BTC/USDT';
let lastBotState   = null;

// ── Log parsing ───────────────────────────────────────────────────────────────
function processLogText(text) {
  const lines = text.split('\n');

  const tickLines = lines.filter(l => l.includes(' tick ') && RE_TICK.test(l));
  if (tickLines.length > 0) {
    const last = tickLines[tickLines.length - 1];
    const m    = last.match(RE_TICK);
    const tsM  = last.match(RE_TS);
    if (m) {
      lastSymbol   = m[1];
      lastClose    = parseFloat(m[2]);
      lastSma      = parseFloat(m[3]);
      lastRsi      = parseFloat(m[4]);
      lastBotState = m[5];
      if (tsM) lastTickMs = new Date(tsM[1].replace(',', '.')).getTime();
    }
  }

  let pos = null;
  lines.forEach(line => {
    if (RE_TRADE_OPEN.test(line)) {
      const m = line.match(RE_TRADE_OPEN);
      if (m) pos = { entry: parseFloat(m[1]), qty: parseFloat(m[2]) };
    }
    if (RE_TRADE_CLOSE.test(line)) pos = null;
  });
  activePosition = pos;

  const pnlLines = lines.filter(l => RE_UNREALIZED.test(l));
  if (pnlLines.length > 0) {
    const m = pnlLines[pnlLines.length - 1].match(RE_UNREALIZED);
    if (m) lastUnrealized = { usdt: parseFloat(m[1]), pct: parseFloat(m[2]) };
  }
  if (!activePosition) lastUnrealized = null;

  return lines;
}

// ── Poll ──────────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const [logText, trades] = await Promise.all([API.logs(), API.trades()]);

    const lines  = processLogText(logText);
    const isLive = lastTickMs !== null && (Date.now() - lastTickMs) < STALE_MS;

    UI.renderHeader(lastClose, lastSymbol, isLive);

    const wins        = trades.filter(t => t.result === 'WIN').length;
    const totalPnl    = trades.reduce((s, t) => s + (t.pnl_usdt || 0), 0);
    const today       = new Date().toISOString().slice(0, 10);
    const tradesToday = trades.filter(t => (t.exit_time || t.timestamp || '').slice(0, 10) === today).length;
    const winRate     = trades.length > 0 ? (wins / trades.length) * 100 : 0;

    UI.renderStats({
      balance:     INITIAL_BALANCE + totalPnl,
      pnlTotal:    totalPnl,
      tradesToday: tradesToday,
      winRate:     winRate,
      state:       lastBotState,
    });

    if (lastRsi != null) UI.renderRsi(lastRsi);
    if (lastSma != null) UI.renderSma(lastClose, lastSma);

    UI.renderPosition(activePosition, lastClose, lastUnrealized);
    UI.renderTrades(trades);
    UI.renderLogs(lines);

  } catch (err) {
    console.error('[trading-bot] poll error:', err);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
poll();
setInterval(poll, POLL_MS);
setInterval(() => UI.renderLastTick(lastTickMs), 1000);
