'use strict';

const UI = (() => {

  // ── helpers ───────────────────────────────────────────────────────────────

  function $(id) { return document.getElementById(id); }

  function fmt(v, d = 2) {
    return Number(v).toLocaleString('en-US', {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function fmtSigned(v, d = 2) {
    const n = Number(v);
    return (n >= 0 ? '+' : '') + fmt(n, d);
  }

  function safe(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  // ── header ────────────────────────────────────────────────────────────────

  let _lastPrice = null;

  function renderHeader(close, symbol, isLive) {
    const priceEl  = $('hdr-btc-price');
    const symEl    = $('hdr-symbol');
    const badge    = $('hdr-status-badge');
    const dot      = $('hdr-dot');

    if (priceEl && close != null) {
      const prev = _lastPrice;
      priceEl.textContent = `$${fmt(close)}`;
      if (prev !== null && close !== prev) {
        priceEl.classList.remove('flash-up', 'flash-down');
        void priceEl.offsetWidth;
        priceEl.classList.add(close > prev ? 'flash-up' : 'flash-down');
      }
      _lastPrice = close;
    }

    if (symEl) symEl.textContent = symbol || 'BTC/USDT';

    if (dot)   dot.style.background  = isLive ? '#4ade80' : '#f87171';

    if (badge) {
      badge.textContent = isLive ? 'RUNNING' : 'STOPPED';
      badge.className = isLive
        ? 'text-[10px] font-bold tracking-widest px-3 py-1 rounded-full border text-green-400 border-green-400/40 bg-green-400/10'
        : 'text-[10px] font-bold tracking-widest px-3 py-1 rounded-full border text-red-400 border-red-400/40 bg-red-400/10';
    }
  }

  function renderLastTick(lastTickMs) {
    const el = $('hdr-lasttick');
    if (!el) return;
    if (!lastTickMs) { el.textContent = '—'; return; }
    const s = Math.floor((Date.now() - lastTickMs) / 1000);
    el.textContent = s < 60 ? `${s}s ago` : `${Math.floor(s / 60)}m ago`;
  }

  // ── stats cards ───────────────────────────────────────────────────────────

  function renderStats({ balance, pnlTotal, tradesToday, winRate, state }) {
    const balEl = $('stat-balance');
    if (balEl) balEl.textContent = `$${fmt(balance)}`;

    const pnlEl = $('stat-pnl');
    if (pnlEl) {
      pnlEl.textContent = fmtSigned(pnlTotal) + ' USDT';
      pnlEl.className   = `text-2xl font-bold tracking-tight tabular-nums ${
        Number(pnlTotal) >= 0 ? 'text-green-400' : 'text-red-400'
      }`;
    }

    const trEl = $('stat-trades');
    if (trEl) trEl.textContent = String(tradesToday);

    const wrEl   = $('stat-winrate');
    const wrFill = $('stat-winrate-fill');
    if (wrEl) {
      wrEl.textContent = `${Number(winRate).toFixed(1)}%`;
      wrEl.className   = `text-2xl font-bold tracking-tight tabular-nums ${
        winRate >= 50 ? 'text-green-400' : 'text-amber-400'
      }`;
    }
    if (wrFill) {
      wrFill.style.width      = `${Math.min(100, winRate)}%`;
      wrFill.style.background = winRate >= 50 ? '#4ade80' : '#fbbf24';
    }

    _renderStateCard(state);
  }

  function _renderStateCard(state) {
    const el = $('stat-state');
    if (!el) return;
    const map = {
      WAITING_SIGNAL: ['text-cyan-400 border-cyan-400/40 bg-cyan-400/10',   'WAITING SIGNAL'],
      IN_POSITION:    ['text-amber-400 border-amber-400/40 bg-amber-400/10', 'IN POSITION'],
      ORDER_PENDING:  ['text-amber-400 border-amber-400/40 bg-amber-400/10', 'ORDER PENDING'],
      ERROR_COOLDOWN: ['text-red-400 border-red-400/40 bg-red-400/10',       'ERROR COOLDOWN'],
    };
    const [cls, label] = map[state] || ['text-slate-500 border-slate-700/40 bg-slate-700/10', state || '—'];
    el.innerHTML = `<span class="text-[10px] font-bold tracking-widest px-3 py-1.5 rounded-lg border ${cls}">${label}</span>`;
  }

  // ── RSI gauge (SVG semicircle) ────────────────────────────────────────────

  function renderRsi(rsi) {
    const container = $('rsi-gauge');
    if (!container) return;

    const v  = Math.min(100, Math.max(0, parseFloat(rsi) || 0));
    const CX = 80, CY = 76, R = 58;

    function arcPath(deg0, deg1) {
      const r0 = Math.PI - (deg0 / 180) * Math.PI;
      const r1 = Math.PI - (deg1 / 180) * Math.PI;
      return `M ${CX + R * Math.cos(r0)} ${CY - R * Math.sin(r0)} ` +
             `A ${R} ${R} 0 0 1 ${CX + R * Math.cos(r1)} ${CY - R * Math.sin(r1)}`;
    }

    const needleDeg = v * 1.8;  // 0–100 → 0–180°
    const nr = Math.PI - (needleDeg / 180) * Math.PI;
    const nx = CX + (R - 6) * Math.cos(nr);
    const ny = CY - (R - 6) * Math.sin(nr);

    let fillColor = '#4ade80';
    if (v > 70)      fillColor = '#f87171';
    else if (v > 55) fillColor = '#fbbf24';
    else if (v > 30) fillColor = '#64748b';

    container.innerHTML = `
      <svg viewBox="0 0 160 88" class="w-full">
        <!-- bg arc -->
        <path d="${arcPath(0, 180)}" fill="none" stroke="#1e293b" stroke-width="10" stroke-linecap="round"/>
        <!-- oversold zone 0–30 -->
        <path d="${arcPath(0, 54)}"  fill="none" stroke="#4ade8033" stroke-width="10" stroke-linecap="round"/>
        <!-- neutral zone 30–70 -->
        <path d="${arcPath(54, 126)}" fill="none" stroke="#33415533" stroke-width="10" stroke-linecap="round"/>
        <!-- overbought zone 70–100 -->
        <path d="${arcPath(126, 180)}" fill="none" stroke="#f8717133" stroke-width="10" stroke-linecap="round"/>
        <!-- value fill -->
        ${v > 0 ? `<path d="${arcPath(0, needleDeg)}" fill="none" stroke="${fillColor}aa" stroke-width="6" stroke-linecap="round"/>` : ''}
        <!-- needle tip -->
        <circle cx="${nx}" cy="${ny}" r="5" fill="${fillColor}" filter="url(#glow)"/>
        <!-- glow filter -->
        <defs>
          <filter id="glow"><feGaussianBlur stdDeviation="2.5" result="blur"/>
            <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
          </filter>
        </defs>
        <!-- value label -->
        <text x="${CX}" y="${CY + 4}" text-anchor="middle"
              fill="${fillColor}" font-size="22" font-family="'JetBrains Mono',monospace"
              font-weight="600" letter-spacing="-1">${v.toFixed(1)}</text>
        <!-- zone ticks -->
        <text x="10"  y="82" fill="#4ade8055" font-size="7" font-family="monospace">0</text>
        <text x="70"  y="18" fill="#64748b66" font-size="7" font-family="monospace">50</text>
        <text x="142" y="82" fill="#f8717155" font-size="7" font-family="monospace">100</text>
      </svg>`;
  }

  // ── SMA panel ─────────────────────────────────────────────────────────────

  function renderSma(close, sma) {
    const valEl = $('sma-value');
    const subEl = $('sma-sub');
    const relEl = $('sma-relation');
    if (!valEl) return;

    const c = parseFloat(close), s = parseFloat(sma);
    valEl.textContent = `$${fmt(s)}`;

    if (!isNaN(c) && !isNaN(s)) {
      const above = c > s;
      const pct   = ((Math.abs(c - s) / s) * 100).toFixed(2);
      if (subEl) {
        subEl.textContent = above ? `↑ ${pct}% above SMA` : `↓ ${pct}% below SMA`;
        subEl.className   = `text-xs mt-1 font-medium ${above ? 'text-green-400' : 'text-red-400'}`;
      }
      if (relEl) {
        relEl.textContent = above ? 'ABOVE' : 'BELOW';
        relEl.className   = `text-[10px] font-bold tracking-widest px-2 py-1 rounded border ${
          above
            ? 'text-green-400 border-green-400/30 bg-green-400/10'
            : 'text-red-400 border-red-400/30 bg-red-400/10'
        }`;
      }
    }
  }

  // ── active position ───────────────────────────────────────────────────────

  function renderPosition(position, currentPrice, unrealized) {
    const emptyEl   = $('pos-empty');
    const contentEl = $('pos-content');
    if (!emptyEl || !contentEl) return;

    if (!position) {
      emptyEl.style.display   = 'block';
      contentEl.style.display = 'none';
      return;
    }

    emptyEl.style.display   = 'none';
    contentEl.style.display = 'block';

    const entry   = $('pos-entry');
    const qty     = $('pos-qty');
    const current = $('pos-current');
    const pnlEl   = $('pos-pnl');
    const pnlPct  = $('pos-pnl-pct');

    if (entry)   entry.textContent   = `$${fmt(position.entry)}`;
    if (qty)     qty.textContent     = `${Number(position.qty).toFixed(6)} BTC`;
    if (current && currentPrice) current.textContent = `$${fmt(currentPrice)}`;

    if (unrealized && pnlEl) {
      const { usdt, pct } = unrealized;
      const cls = Number(usdt) >= 0 ? 'text-green-400' : 'text-red-400';
      pnlEl.textContent = `${fmtSigned(usdt, 4)} USDT`;
      pnlEl.className   = `text-xl font-bold tabular-nums ${cls}`;
      if (pnlPct) {
        pnlPct.textContent = `${fmtSigned(pct, 2)}%`;
        pnlPct.className   = `text-xs ${cls}`;
      }
    }
  }

  // ── trades table ──────────────────────────────────────────────────────────

  function renderTrades(trades) {
    const tbody   = $('trades-tbody');
    const emptyEl = $('trades-empty');
    const summary = $('trades-summary');
    if (!tbody) return;

    if (!trades || trades.length === 0) {
      if (emptyEl)  emptyEl.style.display  = 'block';
      if (summary)  summary.classList.add('hidden');
      tbody.innerHTML = '';
      return;
    }

    if (emptyEl) emptyEl.style.display = 'none';
    if (summary) summary.classList.remove('hidden');

    const last10 = [...trades].slice(-10).reverse();
    tbody.innerHTML = last10.map(t => {
      const win      = t.result === 'WIN';
      const pnlCls   = win ? 'text-green-400' : 'text-red-400';
      const badgeCls = win
        ? 'text-green-400 border-green-400/40 bg-green-400/10'
        : 'text-red-400 border-red-400/40 bg-red-400/10';
      const date = (t.exit_time || t.timestamp || '').slice(0, 16).replace('T', ' ') || '—';
      return `<tr class="border-b border-slate-800/50 hover:bg-slate-800/20 transition-colors">
        <td class="py-2.5 px-3 text-slate-500 text-xs tabular-nums">${safe(date)}</td>
        <td class="py-2.5 px-3 text-slate-300 tabular-nums">${fmt(t.entry_price)}</td>
        <td class="py-2.5 px-3 text-slate-300 tabular-nums">${fmt(t.exit_price)}</td>
        <td class="py-2.5 px-3 ${pnlCls} font-medium tabular-nums">${fmtSigned(t.pnl_usdt, 4)}</td>
        <td class="py-2.5 px-3">
          <span class="text-[10px] font-bold tracking-widest px-2 py-0.5 rounded border ${badgeCls}">
            ${safe(t.result)}
          </span>
        </td>
      </tr>`;
    }).join('');

    // summary bar
    const wins     = trades.filter(t => t.result === 'WIN').length;
    const totalPnl = trades.reduce((s, t) => s + (t.pnl_usdt || 0), 0);
    const wr       = trades.length > 0 ? (wins / trades.length * 100).toFixed(1) : '0.0';
    const sumWr    = $('sum-winrate');
    const sumPnl   = $('sum-pnl');
    const sumCnt   = $('sum-count');
    if (sumWr)  sumWr.textContent  = `${wr}% (${wins}/${trades.length})`;
    if (sumCnt) sumCnt.textContent = trades.length;
    if (sumPnl) {
      sumPnl.textContent  = `${fmtSigned(totalPnl, 4)} USDT`;
      sumPnl.className    = `${Number(totalPnl) >= 0 ? 'text-green-400' : 'text-red-400'}`;
    }
  }

  // ── log panel ─────────────────────────────────────────────────────────────

  const RE_LEVEL = /\s(INFO|WARNING|ERROR|DEBUG)\s/;

  function renderLogs(lines) {
    const container = $('log-lines');
    if (!container) return;

    const last20      = lines.filter(l => l.trim()).slice(-20);
    const atBottom    = container.scrollHeight - container.clientHeight <= container.scrollTop + 20;

    container.innerHTML = last20.map(line => {
      const m     = line.match(RE_LEVEL);
      const level = m ? m[1] : 'INFO';
      const cls = {
        WARNING: 'text-amber-400',
        ERROR:   'text-red-400',
        DEBUG:   'text-slate-700',
      }[level] || 'text-slate-500';
      return `<div class="text-[11px] leading-relaxed whitespace-pre-wrap break-all ${cls}">${safe(line)}</div>`;
    }).join('');

    if (atBottom) container.scrollTop = container.scrollHeight;
  }

  // ── public ────────────────────────────────────────────────────────────────

  return {
    renderHeader,
    renderLastTick,
    renderStats,
    renderRsi,
    renderSma,
    renderPosition,
    renderTrades,
    renderLogs,
  };

})();
