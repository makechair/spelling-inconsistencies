/**
 * Remembered view: period, extended-hours toggle, and chart zoom.
 *
 * setBars() called fitContent() unconditionally, so every load, every symbol
 * switch and every period change threw away the zoom. Reading one-minute
 * detail means widening the bars each time, and the work was discarded by the
 * next click.
 *
 * Horizontal state is barSpacing (pixels per bar) and rightOffset. The vertical
 * axis is not stored: Lightweight Charts v4 exposes no way to read the range a
 * drag on the price scale produced -- there is setAutoScale but no
 * get/setVisibleRange for a price scale. It does not need to be. autoScale
 * fits the price axis to whatever is horizontally visible, so restoring the
 * horizontal zoom restores the vertical fit with it. A drag turns autoScale
 * off; double-clicking the price axis turns it back on.
 */

const STORAGE_KEY = 'usstocks.view';

const DEFAULTS = {
  days: 1,
  extended: true,
  barSpacing: null,   // null = fit the range on first load
  rightOffset: null,
  movingAverages: false,
};

function read() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    const stored = JSON.parse(raw);
    return { ...DEFAULTS, ...(stored && typeof stored === 'object' ? stored : {}) };
  } catch {
    // Corrupt entry, or storage unavailable. Defaults render; a view
    // preference is not worth failing the page over.
    return { ...DEFAULTS };
  }
}

let current = read();

export function view() {
  return current;
}

export function save(patch) {
  current = { ...current, ...patch };
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* session-only; the in-memory value still applies */
  }
}
