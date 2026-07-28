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
    });
    this.chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    this.lastTime = null;
    this.observer = new ResizeObserver(() => this.#resize());
    this.observer.observe(container);
    this.#resize();

    this.themeQuery = window.matchMedia('(prefers-color-scheme: light)');
    this.themeQuery.addEventListener('change', () => {
      this.chart.applyOptions(this.#options());
    });
  }

  #options() {
    const light = window.matchMedia('(prefers-color-scheme: light)').matches;
    return {
      layout: {
        background: { color: 'transparent' },
        textColor: light ? '#656d76' : '#8b949e',
        fontSize: 11,
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
      },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      localization: {
        // Bars are stored UTC; the axis shows the viewer's local clock, which
        // for the intended user is JST and for the market is ET. The quote
        // panel spells the timezone out to avoid ambiguity.
        locale: navigator.language || 'en-US',
      },
    };
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
    this.lastTime = bar.time;
  }

  clear() {
    this.candles.setData([]);
    this.volume.setData([]);
    this.lastTime = null;
  }
}
