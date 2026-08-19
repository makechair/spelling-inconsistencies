/**
 * Chart overlays computed from bars already on the page.
 *
 * Nothing here touches the network. The series the chart is drawing is the
 * whole input, so adding an indicator costs no provider request and spends
 * none of the hourly REST allowance (docs/spec-review.md A-2).
 */

/**
 * Simple moving average of `period` bars.
 *
 * Averaged over bars, not over minutes -- and with this data those differ.
 * Minutes in which nothing traded are not stored (spec-review A-5), so on a
 * thin listing an average of 75 bars can span several hours of wall clock,
 * while on a liquid one it is close to 75 minutes. That is the honest
 * treatment: the alternative is to carry the last price into the empty minutes
 * and average that, which invents the very prices the collector refuses to
 * record.
 *
 * Emits nothing until `period` bars exist, rather than averaging a shorter
 * window, so the first points are not quietly computed differently from the
 * rest.
 */
export function sma(bars, period) {
  if (!Array.isArray(bars) || period < 1 || bars.length < period) return [];
  const points = [];
  let total = 0;
  for (let index = 0; index < bars.length; index += 1) {
    total += bars[index].close;
    if (index >= period) total -= bars[index - period].close;
    if (index >= period - 1) {
      points.push({ time: bars[index].time, value: total / period });
    }
  }
  return points;
}

const HOUR = 3600;
const DAY = 86400;

/**
 * Trailing calendar windows the range readout can be measured over, longest
 * first.
 *
 * Calendar spans rather than bar counts, unlike the moving averages above.
 * Four panes draw the same symbol at four intervals, and "25 bars" means
 * twenty-five minutes on one and half a year on another. A range is read
 * against the calendar -- nobody asks where a price sits in its last 25 bars
 * -- so the window is a duration and it means the same thing on every pane.
 */
export const RANGE_WINDOWS = [
  { label: '10年', seconds: 3650 * DAY },
  { label: '5年', seconds: 1825 * DAY },
  { label: '3年', seconds: 1095 * DAY },
  { label: '1年', seconds: 365 * DAY },
  { label: '6ヶ月', seconds: 182 * DAY },
  { label: '3ヶ月', seconds: 91 * DAY },
  { label: '1ヶ月', seconds: 30 * DAY },
  { label: '1週間', seconds: 7 * DAY },
  { label: '1日', seconds: DAY },
  { label: '4時間', seconds: 4 * HOUR },
  { label: '1時間', seconds: HOUR },
];

/**
 * The windows a pane can actually answer, longest first, at most `limit`.
 *
 * A window is offered only if the loaded history is at least twice as long as
 * it, so a reading exists over most of the pane rather than only at its right
 * edge. The pane's own period therefore chooses the horizons: the minute pane
 * reads hours, the weekly pane reads years, and neither needs a control.
 */
export function rangeWindowsFor(bars, limit = 3) {
  if (!Array.isArray(bars) || bars.length < 2) return [];
  const span = bars[bars.length - 1].time - bars[0].time;
  return RANGE_WINDOWS.filter((window) => window.seconds * 2 <= span).slice(0, limit);
}

/**
 * Where one bar's close sits in the high-low range of the window behind it.
 *
 * This is the stochastic %K taken over a long lookback: nought at the window's
 * low, a hundred at its high. `percentile` is the share of the other bars in
 * the window that closed below this one, which reads the whole distribution
 * instead of its two ends. Both are returned because they disagree in a way
 * that is worth seeing: the range is fixed by exactly two bars, so one spike
 * widens it and every bar after it reads as high in it, while the percentile
 * is unmoved.
 *
 * The range uses each bar's high and low -- what the candles actually show --
 * and the percentile uses closes, because a wick is a price that was touched
 * and a close is a price that was settled at.
 *
 * Strictly trailing: only bars at or before `index` are read, so scrubbing the
 * crosshair back through the chart shows what was knowable then rather than a
 * range built partly out of that bar's future.
 */
export function rangeAt(bars, index, windowSeconds) {
  const current = bars?.[index];
  if (!current) return null;
  const floor = current.time - windowSeconds;
  let start = index;
  while (start > 0 && bars[start - 1].time > floor) start -= 1;
  // The window has to be filled by data, not by the start of the series. A
  // "1年レンジ" measured over three months is a three-month range wearing the
  // wrong label, and it leaves the real high outside the window -- so it makes
  // an expensive price read as mid-range, which is the worse direction to err.
  if (start === 0 && bars[0].time > floor) return null;
  // And it has to be filled in the middle, not only at its edges. A market
  // closes: on the minute pane the bar just after an overnight gap has an
  // hour of wall clock behind it and one bar in it, and a "1時間レンジ" built
  // from a single candle is that candle's own wick, labelled as an hour.
  if (current.time - bars[start].time < windowSeconds / 2) return null;
  let high = -Infinity;
  let low = Infinity;
  let below = 0;
  for (let j = start; j <= index; j += 1) {
    if (bars[j].high > high) high = bars[j].high;
    if (bars[j].low < low) low = bars[j].low;
    if (j !== index && bars[j].close < current.close) below += 1;
  }
  if (!(high > low)) return null;
  const others = index - start;
  return {
    time: current.time,
    value: (100 * (current.close - low)) / (high - low),
    percentile: others > 0 ? (100 * below) / others : null,
    high,
    low,
  };
}

/** `rangeAt` for every bar, keyed by time. Bars without a full window are absent. */
export function rangeSeries(bars, windowSeconds) {
  const points = new Map();
  if (!Array.isArray(bars)) return points;
  for (let index = 0; index < bars.length; index += 1) {
    const point = rangeAt(bars, index, windowSeconds);
    if (point) points.set(point.time, point);
  }
  return points;
}
