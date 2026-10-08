export const DAY_MS = 86400000;

export function fmtUsd(n) {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${(n / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

export function fmtDateTick(ts) {
  const d = new Date(ts);
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function fmtDateTickShort(ts) {
  const d = new Date(ts);
  return `${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

export function fmtDateTime(ts) {
  const d = new Date(ts);
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

// Arizona doesn't observe DST, so it's always UTC-7 (same as MST)
// year-round -- using the IANA zone instead of a hardcoded offset so
// this stays correct rather than silently drifting if that ever changed.
const AZ_PARTS = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/Phoenix",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function fmtDateTimeAZ(ts) {
  const parts = Object.fromEntries(AZ_PARTS.formatToParts(new Date(ts)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute} MST`;
}

// Fetch window: wide enough to cover every marker that will be drawn
// (entry, expiry, and optionally a close/roll date), capped at "now" since
// there's no price data for the future. Previously this fetched
// entry-14d..expiry+14d with no cap -- for any event with expiry still in
// the future, that meant requesting data past "now", which the API can't
// return, so the expiry marker ended up outside the actual fetched range
// and the chart's auto x-domain (based on the data it actually got) put
// everything at "weird positions". A `closeTs` can be passed in (from
// position lifecycle) so the close marker is guaranteed to fall inside the
// fetched range too, rather than being clipped off-chart.
export function eventFetchWindow(ev, { paddingDays = 5, closeTs = null } = {}) {
  const now = Date.now();
  const markers = [ev.timestamp, ev.expiry * 1000];
  if (closeTs) markers.push(closeTs);
  const start = Math.min(...markers) - paddingDays * DAY_MS;
  const end = Math.min(Math.max(...markers) + paddingDays * DAY_MS, now);
  return { start, end };
}

// Default zoom sub-range for the chart's Brush -- tighter than the fetch
// window so the interesting action (around entry) isn't squashed by a
// multi-month span out to expiry. The user can drag the Brush to see the
// rest; this just picks a sane starting view.
export function eventDefaultZoom(ev, { paddingDays = 4 } = {}) {
  const now = Date.now();
  return {
    start: ev.timestamp - paddingDays * DAY_MS,
    end: Math.min(ev.timestamp + paddingDays * DAY_MS, now),
  };
}
