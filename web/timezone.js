/**
 * One timezone for everything on screen.
 *
 * Before this, the chart axis and the "last received" clock disagreed and
 * neither said so. Lightweight Charts reads UNIX seconds as UTC and renders the
 * axis in UTC unless given a formatter -- passing `locale` does not change the
 * zone -- while the quote panel used toLocaleTimeString(), i.e. the browser's
 * zone. So a bar at 20:00 on the axis was 05:00 the next morning in the field
 * below it, and the reader had no way to tell.
 *
 * The default is market time. A US session's landmarks -- 09:30, 16:00, and the
 * 20:00 extended close -- are only recognisable in ET; in any other zone the
 * data appears to start and stop at arbitrary times.
 */

const STORAGE_KEY = 'usstocks.timezone';
const DEFAULT_ZONE = 'America/New_York';

const LABELS = {
  'America/New_York': 'ET',
  'Asia/Tokyo': 'JST',
  UTC: 'UTC',
};

let current = load();
const listeners = new Set();

function load() {
  try {
    return window.localStorage.getItem(STORAGE_KEY) || DEFAULT_ZONE;
  } catch {
    // Private browsing and file:// both throw here. A preference that cannot
    // be stored is not a reason to fail to render.
    return DEFAULT_ZONE;
  }
}

/** The IANA zone to format in, resolving the "browser" choice. */
export function zone() {
  return current === 'local'
    ? Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
    : current;
}

/** Short label for the chosen zone, for display next to a time. */
export function zoneLabel() {
  const resolved = zone();
  return LABELS[resolved] || resolved.split('/').pop().replace('_', ' ');
}

export function setZone(value) {
  current = value;
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* preference is session-only; formatting still follows the choice */
  }
  for (const listener of listeners) listener();
}

export function selected() {
  return current;
}

export function onChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const formatters = new Map();

function formatter(options) {
  const key = `${zone()}|${JSON.stringify(options)}`;
  let existing = formatters.get(key);
  if (!existing) {
    // Intl.DateTimeFormat construction is the expensive part and the axis
    // formats a tick per label on every redraw.
    existing = new Intl.DateTimeFormat('en-GB', { ...options, timeZone: zone() });
    formatters.set(key, existing);
  }
  return existing;
}

onChange(() => formatters.clear());

/** HH:MM in the chosen zone. `value` is a Date or UNIX seconds. */
export function formatTime(value, { seconds = false } = {}) {
  const date = value instanceof Date ? value : new Date(value * 1000);
  return formatter({
    hour: '2-digit',
    minute: '2-digit',
    ...(seconds ? { second: '2-digit' } : {}),
    hour12: false,
  }).format(date);
}

/** DD MMM in the chosen zone, for axis labels that cross a day. */
export function formatDate(value) {
  const date = value instanceof Date ? value : new Date(value * 1000);
  return formatter({ day: '2-digit', month: 'short' }).format(date);
}
