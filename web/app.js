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

const PERIODS = [
  { days: 1, label: '1D', interval: '1m', intervalLabel: '1分足' },
  { days: 7, label: '7D', interval: '5m', intervalLabel: '5分足' },
  { days: 30, label: '1M', interval: '1d', intervalLabel: '日足' },
  { days: 365, label: '1Y', interval: '1d', intervalLabel: '日足' },
];
const PERIOD_DAYS = new Set(PERIODS.map(({ days }) => days));
const MAX_PERIOD_DAYS = PERIODS.at(-1).days;
const rememberedDays = Number(view().days);

const state = {
  symbols: [],          // watchlist entries
  selected: null,       // active ticker
  // Period and the extended-hours toggle are restored, not defaulted: they are
  // part of "how I look at this", the same as the zoom.
  days: PERIOD_DAYS.has(rememberedDays) ? rememberedDays : 1,
  chartMode: 'grid',
  extended: view().extended,
  barsPayloads: new Map(),
  periodNotes: new Map(),
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
  // When the collector last completed a fetch, per symbol. Read alongside the
  // newest bar this separates "nothing traded" from "nothing is running",
  // which the last-received time alone cannot express.
  checkedAt: new Map(),
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
  quoteChecked: document.getElementById('quote-checked'),
  connDot: document.getElementById('conn-dot'),
  connLabel: document.getElementById('conn-label'),
  tz: document.getElementById('tz-select'),
  chartGrid: document.getElementById('chart-grid'),
  chartNote: document.getElementById('chart-note'),
  gridViewButton: document.getElementById('grid-view-button'),
  footerStatus: document.getElementById('footer-status'),
  exportLink: document.getElementById('export-link'),
  extendedToggle: document.getElementById('extended-toggle'),
  maToggle: document.getElementById('ma-toggle'),
};

const charts = new Map(
  PERIODS.map(({ days }) => [
    days,
    new PriceChart(document.getElementById(`chart-${days}`), { persistView: false }),
  ]),
);
const chartPanels = new Map(
  PERIODS.map(({ days }) => [
    days,
    document.querySelector(`[data-chart-days="${days}"]`),
  ]),
);
const chartSummaries = new Map(
  PERIODS.map(({ days }) => [
    days,
    document.getElementById(`chart-summary-${days}`),
  ]),
);

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
    state.barsPayloads.clear();
    for (const chart of charts.values()) chart.clear();
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
  state.barsPayloads.clear();
  renderWatchlist();
  el.quoteSymbol.textContent = symbol;
  el.quoteName.textContent = state.names.get(symbol) || '';
  updateExportLink();
  await loadBars();
  renderQuote();
}

async function loadBars({ quiet = false } = {}) {
  if (!state.selected) return;
  if (!quiet) el.chartNote.textContent = '読み込み中…';
  const symbol = state.selected;
  try {
    const session = state.extended ? 'all' : 'regular';
    // Aggregate before returning data. This keeps a 1Y chart to roughly 252
    // daily rows instead of truncating a huge 1-minute response at 20,000.
    const loaded = await Promise.all(PERIODS.map(async (period) => [
      period.days,
      await api(
        `/api/bars/${symbol}?days=${period.days + 7}` +
        `&interval=${period.interval}&session=${session}`,
      ),
    ]));
    if (state.selected !== symbol) return;
    state.barsPayloads = new Map(loaded);
    state.checkedAt.set(symbol, state.barsPayloads.get(1)?.checked_at || null);
    renderLoadedBars();
    renderQuote();
  } catch (error) {
    if (state.selected !== symbol) return;
    el.chartNote.textContent = `読み込みに失敗しました: ${error.message}`;
  }
}

function renderLoadedBars() {
  if (!state.barsPayloads.size || !state.selected) return;

  state.periodNotes.clear();
  for (const { days, label, intervalLabel } of PERIODS) {
    const payload = state.barsPayloads.get(days);
    const visible = payload?.bars || [];
    const latestTime = visible.at(-1)?.time ?? null;
    const oldestTime = visible[0]?.time ?? null;
    const periodSeconds = days * 86400;
    const desiredFrom = latestTime == null ? null : latestTime - periodSeconds;
    // Anchor every pane at the newest stored bar rather than wall-clock time.
    // A weekend must not empty the 1D pane. If history does not reach the
    // requested boundary, start at the oldest actual record as requested.
    const visibleFrom =
      desiredFrom == null || oldestTime == null
        ? null
        : Math.max(desiredFrom, oldestTime);
    const bars = visibleFrom == null
      ? []
      : visible.filter((bar) => bar.time >= visibleFrom && bar.time <= latestTime);
    charts.get(days).setBars(bars, {
      visibleFrom,
      visibleTo: latestTime,
      periodSeconds,
      oldestTime,
    });

    // The provider is named only when more than one appears in the period.
    // IEX and SIP volume are not comparable, so a mixed range must not be
    // blended silently (spec 5.2).
    const sources = [...new Set(bars.map((bar) => bar.source))];
    const notes = [`${intervalLabel} ${bars.length.toLocaleString()}本`];
    if (sources.length > 1) notes.push(`提供元が混在: ${sources.join(' / ')}`);
    const clipped =
      payload.truncated && desiredFrom != null && oldestTime > desiredFrom;
    if (clipped) notes.push('件数上限で期間先頭を省略');
    if (!bars.length) notes.push('データなし');
    state.periodNotes.set(days, notes.join(' · '));
    chartSummaries.get(days).textContent =
      `${intervalLabel} · ${bars.length.toLocaleString()}本${clipped ? ' · 上限' : ''}`;
    chartPanels.get(days).dataset.periodLabel = label;
  }

  const oneDayBars = state.barsPayloads.get(1)?.bars || [];
  if (oneDayBars.length) {
    state.lastBar.set(state.selected, oneDayBars[oneDayBars.length - 1]);
  } else {
    state.lastBar.delete(state.selected);
  }
  renderChartNote();
}

function renderChartNote() {
  if (state.chartMode === 'grid') {
    el.chartNote.textContent = '期間別に適切な時間足へ集約 · 選択すると拡大します';
    return;
  }
  el.chartNote.textContent = state.periodNotes.get(state.days) || '';
}

function setChartMode(mode, days = state.days) {
  state.chartMode = mode;
  if (mode === 'expanded') {
    state.days = PERIOD_DAYS.has(days) ? days : 1;
    saveView({ days: state.days });
  }
  el.chartGrid.classList.toggle('expanded', mode === 'expanded');
  el.gridViewButton.hidden = mode !== 'expanded';
  for (const [periodDays, panel] of chartPanels) {
    const active = mode === 'expanded' && periodDays === state.days;
    panel.classList.toggle('active', active);
    charts.get(periodDays).setExpanded(active);
  }
  document.querySelectorAll('.range-bar button[data-days]').forEach((button) => {
    button.classList.toggle(
      'active',
      mode === 'expanded' && Number(button.dataset.days) === state.days,
    );
  });
  updateExportLink();
  renderChartNote();
}

/**
 * Draw the minutes the collector has stored since the last time we looked.
 *
 * The chart used to be drawn once per selection and then left to the SSE
 * stream, which was correct while the provider websocket delivered trades.
 * It no longer does on either free tier (docs/spec-review.md A-6), so the
 * collector refreshes bars over REST instead -- into `market.db`, where
 * nothing was telling the browser about them. Between two page loads the
 * chart simply stopped, however often the collector polled.
 *
 * This is a read of the local database, not a provider call: it spends none of
 * the 50 calls/hour, so the interval is set by how soon a finished minute
 * should appear rather than by budget.
 *
 * It is also what keeps the collector aimed here. `/api/bars` is what stamps
 * `last_viewed_at`, and the foreground poll only covers a symbol requested
 * within `viewer_idle_seconds` (300s) -- so without a repeating request, a
 * chart left open would drop out of the foreground after five minutes and go
 * back to the half-hourly sweep.
 */
const TAIL_REFRESH_MS = 30_000;

function upsertCachedBar(bar) {
  const cached = state.barsPayloads.get(1)?.bars;
  if (!cached || !bar) return;
  const last = cached[cached.length - 1];
  if (last?.time === bar.time) {
    cached[cached.length - 1] = bar;
  } else if (!last || bar.time > last.time) {
    cached.push(bar);
  }
}

function updateChartsWithBar(bar) {
  upsertCachedBar(bar);
  if (!bar || (!state.extended && bar.session !== 'regular')) return;
  // The other panes are server-aggregated 15-minute/daily bars. Adding a raw
  // minute directly would corrupt their scale, so the live stream updates the
  // 1D pane only; symbol/period reloads refresh every aggregate.
  charts.get(1).updateBar(bar);
}

async function refreshTail() {
  if (!state.selected) return;
  // A hidden tab is not being read. Letting it go on stamping `last_viewed_at`
  // would point the allowance at a chart nobody is looking at.
  if (document.hidden) return;

  const symbol = state.selected;
  const last = state.lastBar.get(symbol);
  if (!last) {
    // Nothing is drawn, so there is no tail to extend -- and this is exactly
    // the case that needs the request most, since a symbol with no bars yet
    // has to be marked as watched before the collector will fetch any.
    await loadBars({ quiet: true });
    return;
  }

  try {
    // Inclusive of the newest bar we hold: a minute that was still open when we
    // read it comes back revised, and updateBar() rewrites it in place.
    const start = new Date(last.time * 1000).toISOString();
    const payload = await api(
      `/api/bars/${symbol}?start=${encodeURIComponent(start)}`
    );
    if (state.selected !== symbol) return;  // switched while the request was out
    // Recorded before the early return below: a poll that found nothing is
    // exactly when this field has to move, since the bar timestamp cannot.
    state.checkedAt.set(symbol, payload.checked_at || null);
    for (const bar of payload.bars) updateChartsWithBar(bar);
    const visibleBars = state.extended
      ? payload.bars
      : payload.bars.filter((bar) => bar.session === 'regular');
    if (!visibleBars.length) {
      renderQuote();
      return;
    }
    state.lastBar.set(symbol, visibleBars[visibleBars.length - 1]);
    renderQuote();
  } catch {
    // Transient; the next tick retries. api() already handles an expired
    // Access session by reloading the page.
  }
}

setInterval(() => {
  refreshTail();
}, TAIL_REFRESH_MS);

// Coming back to the tab should not mean waiting out the interval to see what
// was missed while it was hidden.
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) refreshTail();
});

document.querySelectorAll('.range-bar button[data-days]').forEach((button) => {
  button.addEventListener('click', () => {
    setChartMode('expanded', Number(button.dataset.days));
  });
});

for (const [days, panel] of chartPanels) {
  panel.addEventListener('click', () => {
    if (state.chartMode === 'grid') setChartMode('expanded', days);
  });
}

el.gridViewButton.addEventListener('click', () => setChartMode('grid'));

el.extendedToggle.addEventListener('change', () => {
  state.extended = el.extendedToggle.checked;
  saveView({ extended: state.extended });
  loadBars();
});

// The remembered period is used on the first expansion, while the initial
// screen itself is always the four-pane overview.
setChartMode('grid');
el.extendedToggle.checked = state.extended;

el.maToggle.checked = view().movingAverages;
el.maToggle.addEventListener('change', () => {
  for (const chart of charts.values()) {
    chart.setMovingAverages(el.maToggle.checked);
  }
});

function updateExportLink() {
  if (!state.selected) return;
  const days = state.chartMode === 'grid' ? MAX_PERIOD_DAYS : state.days;
  el.exportLink.href = `/api/export/csv?symbols=${state.selected}&days=${days}`;
  el.exportLink.title = `${state.selected} の${state.chartMode === 'grid' ? '1Y' : `${days}日`}分足をCSVで取得`;
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
      updateChartsWithBar(entry.bar);
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

/**
 * "最終確認" — when the collector last finished a fetch for this symbol.
 *
 * The last-received time answers "when did this last trade", which cannot
 * distinguish an after-hours session in which nothing printed from a collector
 * that died at the close: both freeze. This one moves on every completed fetch
 * including the ones that returned nothing, so the two cases read differently.
 *
 * It legitimately stops while the market is shut, because the poll loop stops
 * too, so that case is labelled rather than flagged.
 */
function renderChecked() {
  const checked = state.checkedAt.get(state.selected);
  if (!checked) {
    el.quoteChecked.textContent = '—';
    el.quoteChecked.className = '';
    el.quoteChecked.title = 'この銘柄はまだ一度も取得されていません';
    return;
  }
  const ageSeconds = Math.round((Date.now() - new Date(checked).getTime()) / 1000);
  const live = state.live.get(state.selected);
  const bar = state.lastBar.get(state.selected);
  const closed = (live?.session || bar?.session) === 'closed';

  el.quoteChecked.textContent =
    ageSeconds > 120 ? `${fmtClock(checked)} (${fmtAge(ageSeconds)}前)` : fmtClock(checked);
  // Two poll intervals of silence during a session that should be producing
  // them is the collector having stopped, not a quiet market.
  const overdue = !closed && ageSeconds > 600;
  el.quoteChecked.className = overdue ? 'stale' : '';
  el.quoteChecked.title = overdue
    ? '取引時間中にも関わらず取得が止まっています。collectorのログを確認してください'
    : closed
      ? '市場が閉じている間は取得を停止します'
      : '約定が無くてもこの時刻は進みます。進んでいるなら収集は正常です';
}

function fmtAge(seconds) {
  if (seconds < 3600) return `${Math.round(seconds / 60)}分`;
  return `${Math.floor(seconds / 3600)}時間${Math.round((seconds % 3600) / 60)}分`;
}

function renderQuote() {
  renderChecked();
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
