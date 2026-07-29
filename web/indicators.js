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
