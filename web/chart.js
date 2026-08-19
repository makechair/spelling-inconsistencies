/**
 * Lightweight Charts wrapper.
 *
 * Two behaviours matter beyond the defaults:
 *
 * - The chart is redrawn from whole history on load, then only the in-progress
 *   bar is updated. `update()` on the same timestamp replaces the candle, which
 *   is exactly the live-bar semantic the collector produces.
 * - Nothing is drawn for a minute with no trades. Lightweight Charts leaves a
 *   gap, which is the honest rendering: the spec forbids inventing movement.
 */

import { formatDate, formatDateWithYear, formatTime, onChange } from './timezone.js';
import { rangeAt, rangeSeries, rangeWindowsFor, sma } from './indicators.js';
import { save, view } from './viewstate.js';

function row(label, value) {
  const item = document.createElement('span');
  item.className = 'legend-item';
  if (label) {
    const key = document.createElement('span');
    key.className = 'legend-key';
    key.textContent = label;
    item.appendChild(key);
  }
  const val = document.createElement('span');
  val.className = 'legend-value';
  val.textContent = value;
  item.appendChild(val);
  return item;
}

const UP = '#26a69a';
const DOWN = '#ef5350';

// Periods and colours follow the convention Japanese broker terminals use, so
// the numbers line up with whatever the reader is comparing against.
const MOVING_AVERAGES = [
  { period: 5, color: '#4ea1ff' },
  { period: 25, color: '#d9a441' },
  { period: 75, color: '#c778dd' },
];

export class PriceChart {
  constructor(container, { persistView = true, showYear = false } = {}) {
    this.container = container;
    this.persistView = persistView;
    // A grid pane is a quarter of the width an expanded one has. The legend
    // wraps, so three more items there would push it down over the candles.
    this.expanded = persistView;
    this.showYear = showYear;
    this.chart = LightweightCharts.createChart(container, this.#options());
    this.candles = this.chart.addCandlestickSeries({
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    });
    this.volume = this.chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      // The overlay scale draws no axis, so its last-value badge is a number
      // with nothing to read it against -- and it lands on top of the price
      // badge. The legend below carries the figure instead.
      lastValueVisible: false,
      priceLineVisible: false,
    });
    this.chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    // Reserve the bottom quarter for volume. Without this the candles use the
    // full height and a low price prints straight through the bars, leaving
    // two unrelated series drawn over each other.
    this.chart.priceScale('right').applyOptions({
      scaleMargins: { top: 0.08, bottom: 0.24 },
    });

    this.maSeries = MOVING_AVERAGES.map(({ period, color }) => ({
      period,
      color,
      series: this.chart.addLineSeries({
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        // An average is a reading, not a level to snap the crosshair to.
        crosshairMarkerVisible: false,
      }),
    }));
    // Whitespace points make the requested calendar window part of the time
    // scale even when it begins on a weekend or outside market hours. Without
    // them fitContent() collapses (for example) a 7D pane to the timestamps on
    // which trades happened, so its axis can appear to cover only five days.
    this.rangeAnchors = this.chart.addLineSeries({
      // Keep the series itself active so its whitespace points participate in
      // fitContent(). `visible: false` also removes the series from time-scale
      // fitting in Lightweight Charts, which made 7D/1M/1Y collapse to the
      // same range when no trade existed exactly on a calendar boundary.
      visible: true,
      lineVisible: false,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    // Range readouts are derived per pane and drawn nowhere -- the legend
    // carries them. Each entry is { window, points } where points is keyed by
    // bar time, so the crosshair reads them the way it reads a series.
    this.ranges = [];
    this.periodWindow = null;
    this.periodSeconds = null;
    this.oldestLoadedTime = null;

    this.legend = document.createElement('div');
    this.legend.className = 'chart-legend';
    container.appendChild(this.legend);

    // The price scale shows bare numbers. Every instrument here is priced in
    // USD (the catalog import keeps only USD listings), so one static label is
    // enough -- prefixing each tick with a currency symbol would crowd an axis
    // that already carries two decimals.
    const unit = document.createElement('div');
    unit.className = 'chart-unit';
    unit.textContent = 'USD';
    container.appendChild(unit);
    this.chart.subscribeCrosshairMove((param) => this.#renderLegend(param));

    // Debounced: a single pinch or wheel gesture fires this many times, and
    // localStorage writes are synchronous.
    this.zoomSaveTimer = null;
    this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
      if (!this.persistView) return;
      clearTimeout(this.zoomSaveTimer);
      this.zoomSaveTimer = setTimeout(() => {
        const options = this.chart.timeScale().options();
        save({ barSpacing: options.barSpacing, rightOffset: options.rightOffset });
      }, 400);
    });

    this.showMovingAverages = view().movingAverages;
    this.setMovingAverages(this.showMovingAverages);

    this.lastTime = null;
    this.lastCandle = null;
    this.lastVolume = null;
    this.candleData = [];
    this.observer = new ResizeObserver(() => this.#resize());
    this.observer.observe(container);
    this.#resize();

    this.themeQuery = window.matchMedia('(prefers-color-scheme: light)');
    this.themeQuery.addEventListener('change', () => {
      this.chart.applyOptions(this.#options());
    });

    // Re-applying the options rebuilds both formatters, so the axis follows a
    // zone change without reloading the series.
    onChange(() => this.chart.applyOptions(this.#options()));
  }

  /**
   * Grid panes always fit their own period. The expanded pane restores and
   * records the same zoom controls the former single-chart view used.
   */
  setExpanded(expanded) {
    this.persistView = expanded;
    this.expanded = expanded;
    this.#renderLegend(null);
    if (this.periodWindow) {
      this.#applyPeriodWindow();
      return;
    }
    if (expanded) {
      const { barSpacing, rightOffset } = view();
      if (barSpacing == null) {
        this.chart.timeScale().fitContent();
      } else {
        this.chart.timeScale().applyOptions({
          barSpacing,
          ...(rightOffset == null ? {} : { rightOffset }),
        });
      }
    } else {
      this.chart.timeScale().fitContent();
    }
  }

  #applyPeriodWindow() {
    if (!this.periodWindow) return;
    const { from, to } = this.periodWindow;
    this.rangeAnchors.setData(from < to ? [{ time: from }, { time: to }] : [{ time: to }]);
    if (from < to) {
      // Each pane owns a different pair of calendar anchors. Fitting those
      // anchors is more reliable than setVisibleRange(), which clamps to the
      // nearest plotted candle and can erase weekend/holiday portions.
      this.chart.timeScale().fitContent();
    } else {
      this.chart.timeScale().fitContent();
    }
  }

  #options() {
    const light = window.matchMedia('(prefers-color-scheme: light)').matches;
    return {
      layout: {
        background: { color: 'transparent' },
        textColor: light ? '#656d76' : '#8b949e',
        fontSize: 11,
        // The library draws a TradingView logo in the pane by default. The
        // bundled licence is plain Apache 2.0, whose obligations attach to
        // distribution -- retaining the notices, which
        // web/vendor/LICENSE-lightweight-charts.txt does -- and not to what a
        // private single-user page renders. Turned off here, kept in the repo.
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: light ? '#eaeef2' : '#1f262e' },
        horzLines: { color: light ? '#eaeef2' : '#1f262e' },
      },
      rightPriceScale: { borderColor: light ? '#d0d7de' : '#2a313a' },
      timeScale: {
        borderColor: light ? '#d0d7de' : '#2a313a',
        timeVisible: true,
        secondsVisible: false,
        // Without a formatter the axis is UTC. Lightweight Charts reads the
        // UNIX seconds as UTC and renders them as-is; `locale` alone changes
        // only the wording. The day is shown at each boundary because an
        // intraday axis in market time crosses midnight in most zones.
        //
        // The comparison runs <=, not >=: TickMarkType is ordered coarse to
        // fine (Year 0, Month 1, DayOfMonth 2, Time 3, TimeWithSeconds 4), so
        // >= DayOfMonth catches the time marks as well and every tick on an
        // intraday chart renders as the same date.
        tickMarkFormatter: (time, tickMarkType) =>
          tickMarkType <= LightweightCharts.TickMarkType.DayOfMonth
            ? (this.showYear ? formatDateWithYear(time) : formatDate(time))
            : formatTime(time),
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      localization: {
        locale: navigator.language || 'en-US',
        timeFormatter: (time) =>
          `${this.showYear ? formatDateWithYear(time) : formatDate(time)} ${formatTime(time)}`,
      },
    };
  }

  /**
   * Values under the crosshair, falling back to the newest bar.
   *
   * The volume series has no axis of its own -- an overlay scale draws none --
   * so without this its bars are a shape with no magnitude, and there is
   * nothing on screen saying they are volume at all.
   */
  #renderLegend(param) {
    const candle = param?.seriesData?.get(this.candles) ?? this.lastCandle;
    const volume = param?.seriesData?.get(this.volume) ?? this.lastVolume;
    if (!candle) {
      this.legend.textContent = '';
      return;
    }
    const price = (value) =>
      value == null ? '—' : value.toLocaleString(undefined, {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      });
    const time = param?.time ?? candle.time;
    const maRows = this.showMovingAverages
      ? this.maSeries.map((ma) => {
          const point = param?.seriesData?.get(ma.series);
          const item = row(`MA${ma.period}`, price(point?.value));
          item.style.color = ma.color;
          return item;
        })
      : [];
    // Where this bar's close sits in the range behind it, one item per horizon
    // the pane can answer. Nothing is fetched and nothing is stored: the bars
    // already on screen are the whole input.
    // Compact panes show the longest horizon only; the whole ladder is worth
    // the width once a pane is expanded.
    const shownRanges = this.expanded ? this.ranges : this.ranges.slice(0, 1);
    const rangeRows = shownRanges.map(({ window, points }) => {
      const point = time == null ? null : points.get(time);
      const item = row(
        `レンジ${window.label}`,
        point ? `${point.value.toFixed(1)}%` : '—',
      );
      item.className += ' legend-range';
      item.title = point
        ? `過去${window.label}の高安 ${price(point.low)} 〜 ${price(point.high)}` +
          (point.percentile == null
            ? ''
            : `\nこの期間の足の ${point.percentile.toFixed(1)}% より高い`)
        : `過去${window.label}分の足がまだ揃っていない`;
      return item;
    });
    this.legend.replaceChildren(
      row('始', price(candle.open)),
      row('高', price(candle.high)),
      row('安', price(candle.low)),
      row('終', price(candle.close)),
      row('出来高', volume?.value == null ? '—' : volume.value.toLocaleString()),
      ...maRows,
      ...rangeRows,
      row('', time ? `${formatDate(time)} ${formatTime(time)}` : ''),
    );
  }

  setMovingAverages(visible) {
    this.showMovingAverages = visible;
    for (const ma of this.maSeries) {
      ma.series.applyOptions({ visible });
    }
    save({ movingAverages: visible });
    this.#renderLegend(null);
  }

  #resize() {
    const { clientWidth, clientHeight } = this.container;
    if (clientWidth > 0 && clientHeight > 0) {
      this.chart.resize(clientWidth, clientHeight);
    }
  }

  /** Replace the whole series. `bars` come from /api/bars. */
  setBars(
    bars,
    { visibleFrom = null, visibleTo = null, periodSeconds = null, oldestTime = null } = {},
  ) {
    const candles = bars.map((bar) => ({
      time: bar.time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    const volumes = bars.map((bar) => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? 'rgba(38,166,154,.5)' : 'rgba(239,83,80,.5)',
    }));
    this.candles.setData(candles);
    this.volume.setData(volumes);
    // Kept so the live bar can extend the averages without refetching. Only
    // the trailing window is needed, but holding the array is simpler than
    // maintaining three ring buffers.
    this.candleData = candles;
    for (const ma of this.maSeries) {
      ma.series.setData(sma(candles, ma.period));
    }
    // Recomputed from the bars in hand rather than fetched, and recomputed
    // here rather than per crosshair move: scrubbing must not rescan the pane
    // once per pixel.
    this.ranges = rangeWindowsFor(candles).map((window) => ({
      window,
      points: rangeSeries(candles, window.seconds),
    }));
    this.lastCandle = candles.length ? candles[candles.length - 1] : null;
    this.lastVolume = volumes.length ? volumes[volumes.length - 1] : null;
    this.#renderLegend(null);
    this.lastTime = bars.length ? bars[bars.length - 1].time : null;
    this.periodSeconds = periodSeconds;
    this.oldestLoadedTime = oldestTime;
    this.periodWindow =
      visibleFrom != null && visibleTo != null
        ? { from: visibleFrom, to: visibleTo }
        : null;
    if (!bars.length) {
      this.rangeAnchors.setData([]);
      return;
    }

    if (this.periodWindow) {
      this.#applyPeriodWindow();
      return;
    }

    // fitContent() unconditionally was what discarded the zoom on every load,
    // symbol switch and period change. Fit only when there is nothing
    // remembered; otherwise put the bars back at the width they were left at.
    const { barSpacing, rightOffset } = view();
    if (!this.persistView || barSpacing == null) {
      this.chart.timeScale().fitContent();
    } else {
      this.chart.timeScale().applyOptions({
        barSpacing,
        ...(rightOffset == null ? {} : { rightOffset }),
      });
    }
  }

  /**
   * Apply the in-progress bar. Ignores bars older than what is drawn, because
   * an out-of-order update would make Lightweight Charts throw and would also
   * mean rewriting settled history from a live feed.
   */
  updateBar(bar) {
    if (!bar) return;
    if (this.lastTime !== null && bar.time < this.lastTime) return;
    this.candles.update({
      time: bar.time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    });
    this.volume.update({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? 'rgba(38,166,154,.5)' : 'rgba(239,83,80,.5)',
    });
    this.lastCandle = { time: bar.time, open: bar.open, high: bar.high, low: bar.low, close: bar.close };
    this.lastVolume = { time: bar.time, value: bar.volume };

    // Grid panes keep their named calendar width as live bars arrive. An
    // expanded pane is left alone after the initial range so a user's manual
    // zoom is not reset by the 30-second refresh.
    if (
      !this.persistView &&
      this.periodWindow &&
      this.periodSeconds != null &&
      bar.time > this.periodWindow.to
    ) {
      this.periodWindow = {
        from: Math.max(
          this.oldestLoadedTime ?? bar.time,
          bar.time - this.periodSeconds,
        ),
        to: bar.time,
      };
      this.#applyPeriodWindow();
    }

    // Without this the averages freeze at the last completed bar while the
    // candle beside them keeps moving, which reads as a stalled indicator.
    // Only the newest point changes, so each series is updated rather than
    // rebuilt.
    if (this.candleData) {
      const last = this.candleData[this.candleData.length - 1];
      if (last && last.time === bar.time) {
        this.candleData[this.candleData.length - 1] = this.lastCandle;
      } else {
        this.candleData.push(this.lastCandle);
      }
      for (const ma of this.maSeries) {
        const window = this.candleData.slice(-ma.period);
        if (window.length === ma.period) {
          ma.series.update({
            time: bar.time,
            value: window.reduce((total, item) => total + item.close, 0) / ma.period,
          });
        }
      }
      // Only the newest bar moved, so only its point is recomputed. Rebuilding
      // every point on each tick would rescan the pane once a second.
      const newest = this.candleData.length - 1;
      for (const range of this.ranges) {
        const point = rangeAt(this.candleData, newest, range.window.seconds);
        if (point) range.points.set(point.time, point);
      }
    }
    this.#renderLegend(null);
    this.lastTime = bar.time;
  }

  clear() {
    this.candles.setData([]);
    this.volume.setData([]);
    this.candleData = [];
    for (const ma of this.maSeries) ma.series.setData([]);
    this.ranges = [];
    this.lastCandle = null;
    this.lastVolume = null;
    this.legend.textContent = '';
    this.lastTime = null;
  }
}
