/**
 * Frontend controller.
 *
 * Notable behaviours, all traceable to the spec or to gaps found reviewing it:
 *
 * - The SSE stream only carries symbols that changed, so the UI never animates
 *   a price that did not move (spec 3.3).
 * - "Last update" is computed from the last *trade* received, not from the last
 *   frame, so a quiet market and a dead feed look different (spec 3.2).
 * - When the stream drops, /api/ping decides whether the Cloudflare Access
 *   session expired (reload to re-authenticate) or the server is down (retry
 *   with backoff). Without this the page silently stops updating after an
 *   Access session times out (docs/spec-review.md B-6).
 */

import { PriceChart } from './chart.js';
import { formatTime, onChange, selected, setZone, zoneLabel } from './timezone.js';
import { save as saveView, view } from './viewstate.js';

const state = {
  symbols: [],          // watchlist entries
  selected: null,       // active ticker
  // Period and the extended-hours toggle are restored, not defaulted: they are
  // part of "how I look at this", the same as the zoom.
  days: view().days,
  extended: view().extended,
  live: new Map(),      // ticker -> latest LiveOut
  names: new Map(),
  eventSource: null,
  // Which symbols the collector actually subscribed to. With the cap at 10
  // it is realistic to watch more than that, and a silently unsubscribed
  // symbol would just look like a stuck price.
  subscribed: null,
  // Newest bar of the loaded range, per symbol. Outside market hours the
  // collector publishes no live snapshot, so this is the only price there is.
  lastBar: new Map(),
  retryDelay: 1000,
  lastEventAt: 0,
};

const el = {
  search: document.getElementById('search-input'),
  results: document.getElementById('search-results'),
  watchlist: document.getElementById('watchlist'),
  watchlistHint: document.getElementById('watchlist-hint'),
  quoteSymbol: document.getElementById('quote-symbol'),
  quoteName: document.getElementById('quote-name'),
  quoteLast: document.getElementById('quote-last'),
  quoteChange: document.getElementById('quote-change'),
  quoteSession: document.getElementById('quote-session'),
  quoteUpdated: document.getElementById('quote-updated'),
  connDot: document.getElementById('conn-dot'),
  connLabel: document.getElementById('conn-label'),
  tz: document.getElementById('tz-select'),
  chartNote: document.getElementById('chart-note'),
  footerStatus: document.getElementById('footer-status'),
  exportLink: document.getElementById('export-link'),
  extendedToggle: document.getElementById('extended-toggle'),
  maToggle: document.getElementById('ma-toggle'),
};

const chart = new PriceChart(document.getElementById('chart'));

const SESSION_LABEL = {
  pre: 'プレマーケット',
  regular: '通常取引',
  post: 'アフターマーケット',
  closed: '時間外（市場閉場）',
};

/* ------------------------------------------------------------------ utils */

const fmtPrice = (value) =>
  value === null || value === undefined
    ? '—'
    : value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const fmtSigned = (value, digits = 2) =>
  value === null || value === undefined
    ? ''
    : `${value >= 0 ? '+' : ''}${value.toLocaleString(undefined, {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      })}`;

function fmtClock(iso) {
  if (!iso) return '—';
  // Same zone as the chart axis, and labelled. These two used to disagree
  // silently: the axis rendered UTC while this used the browser's zone.
  return `${formatTime(new Date(iso), { seconds: true })} ${zoneLabel()}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options });
  if (response.status === 401 || response.status === 403) {
    // The Access session lapsed; a reload takes the user back through Google.
    window.location.reload();
    throw new Error('unauthenticated');
  }
  if (!response.ok) {
    throw new Error(`${path} -> ${response.status}`);
  }
  return response.json();
}

/* -------------------------------------------------------------- watchlist */

async function loadWatchlist() {
  state.symbols = await api('/api/symbols?watched_only=true');
  state.symbols.forEach((entry) => {
    if (entry.name) state.names.set(entry.symbol, entry.name);
  });
  renderWatchlist();
  el.watchlistHint.hidden = state.symbols.length > 0;

  if (!state.selected && state.symbols.length) {
    await select(state.symbols[0].symbol);
  } else if (!state.symbols.length) {
    state.selected = null;
    chart.clear();
  }
  connectStream();
}

function renderWatchlist() {
  el.watchlist.replaceChildren(
    ...state.symbols.map((entry) => {
      const live = state.live.get(entry.symbol);
      const item = document.createElement('li');
      item.className = entry.symbol === state.selected ? 'active' : '';
      item.dataset.symbol = entry.symbol;

      const symbol = document.createElement('span');
      symbol.className = 'wl-symbol';
      symbol.textContent = entry.symbol;
      if (entry.supported === false) {
        symbol.title = entry.note || 'この銘柄はデータ提供元で未サポートです';
        symbol.textContent += ' ⚠';
      } else if (state.subscribed && !state.subscribed.has(entry.symbol)) {
        item.classList.add('unsubscribed');
        symbol.title = '購読上限を超えているため、この銘柄はリアルタイム更新されません';
        symbol.textContent += ' ⏸';
      }

      const price = document.createElement('span');
      price.className = 'wl-price';
      price.textContent = live ? fmtPrice(live.last_price) : '—';

      const change = document.createElement('span');
      change.className = `wl-change ${changeClass(live?.change_pct)}`;
      change.textContent = live?.change_pct != null ? `${fmtSigned(live.change_pct)}%` : '';

      const remove = document.createElement('button');
      remove.className = 'remove';
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = `${entry.symbol} を購読解除（履歴は保持されます）`;
      remove.addEventListener('click', async (event) => {
        event.stopPropagation();
        await api(`/api/symbols/${entry.symbol}`, { method: 'DELETE' });
        if (state.selected === entry.symbol) state.selected = null;
        await loadWatchlist();
      });

      item.append(symbol, price, change, remove);
      item.addEventListener('click', () => select(entry.symbol));
      return item;
    }),
  );
}

const changeClass = (value) => (value == null ? '' : value >= 0 ? 'up' : 'down');

/* ----------------------------------------------------------------- search */

let searchTimer = null;

/* Every uncached search spends one of 50 REST calls per hour, shared with the
 * collector's backfill (docs/spec-review.md A-2). Two letters and a longer
 * pause cut the number of distinct prefixes a lookup produces: a single letter
 * matches most of the universe and is never the query the user meant. */
const SEARCH_MIN_LENGTH = 2;
const SEARCH_DEBOUNCE_MS = 400;

el.search.addEventListener('input', () => {
  clearTimeout(searchTimer);
  const query = el.search.value.trim();
  if (query.length < SEARCH_MIN_LENGTH) {
    el.results.hidden = true;
    return;
  }
  searchTimer = setTimeout(() => runSearch(query), SEARCH_DEBOUNCE_MS);
});

el.search.addEventListener('blur', () => {
  // Delay so a click on a result still registers.
  setTimeout(() => { el.results.hidden = true; }, 150);
});

async function runSearch(query) {
  let results = [];
  try {
    results = await api(`/api/symbols/search?q=${encodeURIComponent(query)}`);
  } catch (error) {
    console.warn('search failed', error);
    return;
  }
  el.results.replaceChildren(
    ...results.map((entry) => {
      const item = document.createElement('li');
      const ticker = document.createElement('span');
      ticker.className = 'ticker';
      ticker.textContent = entry.symbol;
      const name = document.createElement('span');
      name.className = 'co';
      name.textContent = entry.name || '';
      item.append(ticker, name);
      if (entry.supported === false) {
        const flag = document.createElement('span');
        flag.className = 'unsupported';
        flag.textContent = '未サポート';
        item.append(flag);
      }
      item.addEventListener('mousedown', (event) => {
        event.preventDefault();
        addSymbol(entry);
      });
      return item;
    }),
  );
  el.results.hidden = results.length === 0;
}

async function addSymbol(entry) {
  el.results.hidden = true;
  el.search.value = '';
  await api(`/api/symbols/${entry.symbol}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      symbol: entry.symbol,
      name: entry.name,
      exchange: entry.exchange,
      asset_type: entry.asset_type,
      is_watched: true,
    }),
  });
  if (entry.name) state.names.set(entry.symbol, entry.name);
  await loadWatchlist();
  await select(entry.symbol);
}

/* ------------------------------------------------------------------ chart */

async function select(symbol) {
  state.selected = symbol;
  renderWatchlist();
  el.quoteSymbol.textContent = symbol;
  el.quoteName.textContent = state.names.get(symbol) || '';
  updateExportLink();
  await loadBars();
  renderQuote();
}

async function loadBars() {
  if (!state.selected) return;
  el.chartNote.textContent = '読み込み中…';
  try {
    const payload = await api(`/api/bars/${state.selected}?days=${state.days}`);
    const bars = state.extended
      ? payload.bars
      : payload.bars.filter((bar) => bar.session === 'regular');
    chart.setBars(bars);
    if (bars.length) {
      state.lastBar.set(state.selected, bars[bars.length - 1]);
    } else {
      state.lastBar.delete(state.selected);
    }
    renderQuote();

    // The provider is named only when more than one appears in the range.
    // Spec 5.2 forbids blending providers silently, and that is the case worth
    // interrupting for -- IEX-only volume is not comparable with consolidated
    // volume, so a mixed range has a step in it that needs explaining. Naming
    // the single expected provider on every load says nothing and buries the
    // one time it matters.
    const sources = [...new Set(bars.map((bar) => bar.source))];
    const notes = [`${bars.length.toLocaleString()} 本`];
    if (sources.length > 1) {
      notes.push(`提供元が混在: ${sources.join(' / ')}`);
    }
    if (payload.truncated) notes.push('件数上限で切り詰めました');
    if (!bars.length) notes.push('この期間のデータがありません');
    el.chartNote.textContent = notes.join(' · ');
  } catch (error) {
    el.chartNote.textContent = `読み込みに失敗しました: ${error.message}`;
  }
}

document.querySelectorAll('.range-bar button[data-days]').forEach((button) => {
  button.addEventListener('click', async () => {
    document.querySelectorAll('.range-bar button[data-days]')
      .forEach((other) => other.classList.toggle('active', other === button));
    state.days = Number(button.dataset.days);
    saveView({ days: state.days });
    updateExportLink();
    await loadBars();
  });
});

el.extendedToggle.addEventListener('change', async () => {
  state.extended = el.extendedToggle.checked;
  saveView({ extended: state.extended });
  await loadBars();
});

// Reflect the restored view in the controls before the first load, so the
// highlighted period button and the checkbox match what is drawn.
document.querySelectorAll('.range-bar button[data-days]').forEach((button) => {
  button.classList.toggle('active', Number(button.dataset.days) === state.days);
});
el.extendedToggle.checked = state.extended;

el.maToggle.checked = view().movingAverages;
el.maToggle.addEventListener('change', () => {
  chart.setMovingAverages(el.maToggle.checked);
});

function updateExportLink() {
  if (!state.selected) return;
  el.exportLink.href = `/api/export/csv?symbols=${state.selected}&days=${state.days}`;
  el.exportLink.title = `${state.selected} の1分足をCSVで取得`;
}

/* ------------------------------------------------------------------- live */

function connectStream() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
  if (!state.symbols.length) {
    setConnection('idle', '銘柄未登録');
    return;
  }

  const tickers = state.symbols.map((entry) => entry.symbol).join(',');
  const source = new EventSource(`/api/live?symbols=${encodeURIComponent(tickers)}`);
  state.eventSource = source;

  source.addEventListener('snapshot', (event) => {
    state.retryDelay = 1000;
    applyUpdates(JSON.parse(event.data));
  });
  source.addEventListener('update', (event) => applyUpdates(JSON.parse(event.data)));
  source.addEventListener('heartbeat', () => { state.lastEventAt = Date.now(); });
  source.addEventListener('status', (event) => applyStatus(JSON.parse(event.data)));

  source.onerror = async () => {
    source.close();
    state.eventSource = null;
    setConnection('down', '再接続中…');
    // Distinguish "session expired" from "server unreachable": api() reloads
    // the page on 401/403, which is what sends the user back through Google.
    try {
      await api('/api/ping');
    } catch {
      /* the reload or the retry below handles it */
    }
    setTimeout(connectStream, state.retryDelay);
    state.retryDelay = Math.min(state.retryDelay * 2, 30000);
  };
}

function applyUpdates(payload) {
  state.lastEventAt = Date.now();
  for (const entry of payload.symbols || []) {
    state.live.set(entry.symbol, entry);
    if (entry.symbol === state.selected && entry.bar) {
      if (state.extended || entry.bar.session === 'regular') {
        chart.updateBar(entry.bar);
      }
    }
  }
  renderWatchlist();
  renderQuote();
}

function applyStatus(status) {
  if (Array.isArray(status.subscribed_symbols)) {
    const next = new Set(status.subscribed_symbols);
    const changed =
      !state.subscribed ||
      state.subscribed.size !== next.size ||
      [...next].some((symbol) => !state.subscribed.has(symbol));
    state.subscribed = next;
    if (changed) renderWatchlist();
  }
  if (!status.connected) {
    setConnection('down', status.last_error ? '収集停止' : '未接続');
  } else {
    // The provider name is not shown here for the same reason the chart note
    // stopped printing it: on a single-source install it is the same word every
    // time. A mixed range still says so under the chart, where it matters.
    setConnection('live', '接続中');
  }
  const parts = [];
  if (status.subscribed_symbols) {
    const watched = state.symbols.length;
    const live = status.subscribed_symbols.length;
    parts.push(live < watched ? `購読 ${live}/${watched} 銘柄（上限）` : `購読 ${live} 銘柄`);
  }
  if (status.bytes_received_month != null) {
    parts.push(`月間受信 ${(status.bytes_received_month / 1e6).toFixed(1)} MB`);
  }
  if (status.rest_calls_hour != null) parts.push(`REST ${status.rest_calls_hour}/時`);
  if (status.reconnect_count) parts.push(`再接続 ${status.reconnect_count} 回`);
  el.footerStatus.textContent = parts.join(' · ');
}

function renderQuote() {
  const live = state.live.get(state.selected);
  if (!live) {
    // No live snapshot: the market is closed, or nothing has traded since the
    // collector started. Fall back to the newest bar on the chart rather than
    // showing a dash -- the price is on screen either way, and a blank field
    // next to a drawn candle reads as a fault.
    //
    // Marked as a close, not a last price. Spec 3.2 asks that a quiet market
    // and a dead feed look different, and that holds for the panel as much as
    // for the connection dot: presenting a stale figure as live would erase
    // exactly the distinction the timestamp beside it exists to make.
    const bar = state.lastBar.get(state.selected);
    el.quoteLast.textContent = bar ? fmtPrice(bar.close) : '—';
    el.quoteLast.classList.toggle('historical', Boolean(bar));
    el.quoteLast.title = bar ? 'ライブ更新なし。チャート最終足の終値' : '';
    el.quoteChange.textContent = '';
    el.quoteSession.textContent = bar
      ? `${SESSION_LABEL[bar.session] || bar.session}（最終足）`
      : '—';
    el.quoteUpdated.textContent = bar ? fmtClock(bar.time * 1000) : '—';
    return;
  }

  el.quoteLast.classList.remove('historical');
  el.quoteLast.title = '';

  el.quoteLast.textContent = fmtPrice(live.last_price);
  el.quoteChange.className = `change ${changeClass(live.change)}`;
  el.quoteChange.textContent =
    live.change == null ? '' : `${fmtSigned(live.change)} (${fmtSigned(live.change_pct)}%)`;
  el.quoteSession.textContent = SESSION_LABEL[live.session] || live.session;

  // Staleness comes from the last trade, so an idle market reads as idle
  // rather than as a broken feed (spec 3.2).
  const stale = live.staleness_seconds != null && live.staleness_seconds > 120;
  el.quoteUpdated.textContent = live.last_trade_at
    ? `${fmtClock(live.last_trade_at)}${stale ? ` (${Math.round(live.staleness_seconds)}秒前)` : ''}`
    : '—';
  el.quoteUpdated.className = stale && live.session !== 'closed' ? 'stale' : '';
}

function setConnection(kind, label) {
  el.connDot.className = `dot ${kind === 'idle' ? '' : kind}`;
  el.connLabel.textContent = label;
}

/* --------------------------------------------------------------- timezone */

el.tz.value = selected();
el.tz.addEventListener('change', () => setZone(el.tz.value));

// The chart re-applies its own formatters; this repaints the panel fields that
// carry a time, so the whole screen changes zone together rather than in two
// steps.
onChange(() => {
  renderQuote();
  renderWatchlist();
});

/* ------------------------------------------------------------------- boot */

// If no event of any kind arrives for a while the pipe is wedged even though
// EventSource still thinks it is open; the heartbeat makes this detectable.
setInterval(() => {
  if (!state.eventSource) return;
  if (state.lastEventAt && Date.now() - state.lastEventAt > 45000) {
    setConnection('stale', '受信が途絶えています');
  }
}, 5000);

loadWatchlist().catch((error) => {
  el.chartNote.textContent = `初期化に失敗しました: ${error.message}`;
});
