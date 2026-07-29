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

import { formatDate, formatTime, onChange } from './timezone.js';

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

export class PriceChart {
  constructor(container) {
    this.container = container;
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

    this.legend = document.createElement('div');
    this.legend.className = 'chart-legend';
    container.appendChild(this.legend);
    this.chart.subscribeCrosshairMove((param) => this.#renderLegend(param));

    this.lastTime = null;
    this.lastCandle = null;
    this.lastVolume = null;
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
        tickMarkFormatter: (time, tickMarkType) =>
          tickMarkType >= LightweightCharts.TickMarkType.DayOfMonth
            ? formatDate(time)
            : formatTime(time),
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      localization: {
        locale: navigator.language || 'en-US',
        timeFormatter: (time) => `${formatDate(time)} ${formatTime(time)}`,
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
    this.legend.replaceChildren(
      row('始', price(candle.open)),
      row('高', price(candle.high)),
      row('安', price(candle.low)),
      row('終', price(candle.close)),
      row('出来高', volume?.value == null ? '—' : volume.value.toLocaleString()),
      row('', time ? `${formatDate(time)} ${formatTime(time)}` : ''),
    );
  }

  #resize() {
    const { clientWidth, clientHeight } = this.container;
    if (clientWidth > 0 && clientHeight > 0) {
      this.chart.resize(clientWidth, clientHeight);
    }
  }

  /** Replace the whole series. `bars` come from /api/bars. */
  setBars(bars) {
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
    this.lastCandle = candles.length ? candles[candles.length - 1] : null;
    this.lastVolume = volumes.length ? volumes[volumes.length - 1] : null;
    this.#renderLegend(null);
    this.lastTime = bars.length ? bars[bars.length - 1].time : null;
    if (bars.length) this.chart.timeScale().fitContent();
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
    this.#renderLegend(null);
    this.lastTime = bar.time;
  }

  clear() {
    this.candles.setData([]);
    this.volume.setData([]);
    this.lastCandle = null;
    this.lastVolume = null;
    this.legend.textContent = '';
    this.lastTime = null;
  }
}
