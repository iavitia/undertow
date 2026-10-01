import { Bar, Brush, CartesianGrid, ComposedChart, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { TOKENS, mono, sans } from "../tokens";
import { eventFetchWindow, fmtDateTick, fmtDateTickShort, fmtDateTime, fmtUsd } from "../format";

const axisTick = { fill: TOKENS.dim, fontSize: 10, fontFamily: "IBM Plex Mono" };

function PriceTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: TOKENS.panel2, border: `1px solid ${TOKENS.hair}`, fontFamily: mono, fontSize: 11, padding: "6px 10px" }}>
      <div style={{ color: TOKENS.dim }}>{fmtDateTime(p.timestamp)}</div>
      <div style={{ color: TOKENS.paper }}>{fmtUsd(p.price)}</div>
      {p.volume != null && <div style={{ color: TOKENS.undertow }}>vol {fmtUsd(p.volume)}</div>}
    </div>
  );
}

// Full price history, inauguration -> now, with daily options notional
// volume as background bars and the currently selected event's window
// shaded so it's clear where the detail chart below zooms in.
// `key`'d by asset upstream: recharts doesn't reliably recompute axis
// domains on a data-prop change alone, so switching BTC<->ETH without a
// remount left the old asset's Y-axis scale on screen (confirmed by
// inspecting the rendered axis text -- data was refetched correctly, only
// the chart didn't redraw).
export function OverviewChart({ data, selectedEvent }) {
  if (!data.length) return null;
  const win = selectedEvent ? eventFetchWindow(selectedEvent) : null;

  return (
    <ResponsiveContainer width="100%" height={130}>
      <ComposedChart data={data} margin={{ top: 6, right: 20, left: 0, bottom: 0 }}>
        <XAxis
          dataKey="timestamp"
          type="number"
          domain={["dataMin", "dataMax"]}
          tick={axisTick}
          axisLine={{ stroke: TOKENS.hair }}
          tickLine={false}
          tickFormatter={fmtDateTick}
        />
        <YAxis yAxisId="price" tick={axisTick} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={56} tickFormatter={(v) => fmtUsd(v)} />
        <YAxis yAxisId="volume" orientation="right" tick={axisTick} axisLine={false} tickLine={false} domain={[0, (max) => max * 4]} width={56} tickFormatter={(v) => fmtUsd(v)} />
        <Tooltip content={<PriceTooltip />} />
        {win && <ReferenceArea yAxisId="price" x1={win.start} x2={win.end} fill={TOKENS.flare} fillOpacity={0.12} stroke={TOKENS.flare} strokeOpacity={0.35} />}
        <Bar yAxisId="volume" dataKey="volume" fill={TOKENS.undertow} opacity={0.35} isAnimationActive={false} />
        <Line yAxisId="price" type="monotone" dataKey="price" stroke={TOKENS.paper} strokeWidth={1.25} dot={false} isAnimationActive={false} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

function nearestIndex(data, ts) {
  const i = data.findIndex((d) => d.timestamp >= ts);
  return i < 0 ? data.length - 1 : i;
}

// Zoomed window around the selected event: entry date -> expiry, padded,
// with strike/entry/expiry marked, plus a Brush so the full fetched range
// (which now always covers every marker -- see eventFetchWindow) stays
// scrollable/zoomable instead of picking one fixed static window. `key`'d
// by event id upstream, same remount reasoning as OverviewChart.
// strike/expiry are optional -- the catalysts view reuses this chart for
// market-wide events that have an entry moment but no option strike.
//
// `markers` is an optional list of extra vertical lines for position
// lifecycle (entry/close/roll of specific trades tied to the event) --
// { ts, label, color }. Kept separate from the primary `event` marker
// (the announcement/catalyst moment) so the two concepts don't collide.
// `defaultZoom` ({ start, end } timestamps) sets the Brush's initial
// visible slice; the full data stays reachable by dragging it.
export function DetailChart({ data, event, entryLabel = "entry", markers = [], defaultZoom }) {
  if (!data.length || !event) return null;

  const rangeStart = data[0].timestamp;
  const rangeEnd = data[data.length - 1].timestamp;
  const inRange = (ts) => ts >= rangeStart && ts <= rangeEnd;

  const expiryTs = event.expiry != null ? event.expiry * 1000 : null;
  const expiryVisible = expiryTs != null && inRange(expiryTs);
  const hiddenNotes = [];
  if (expiryTs != null && !expiryVisible) {
    hiddenNotes.push(`expires ${new Date(expiryTs).toISOString().slice(0, 10)}${expiryTs > Date.now() ? " (in the future)" : ""}`);
  }
  const visibleMarkers = markers.filter((m) => inRange(m.ts));
  for (const m of markers) {
    if (!inRange(m.ts)) hiddenNotes.push(`${m.label} ${new Date(m.ts).toISOString().slice(0, 10)} (outside chart data)`);
  }

  let startIndex, endIndex;
  if (defaultZoom) {
    startIndex = nearestIndex(data, defaultZoom.start);
    endIndex = nearestIndex(data, defaultZoom.end);
  }

  return (
    <>
      <ResponsiveContainer width="100%" height={250}>
        <LineChart data={data} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={TOKENS.hair} strokeDasharray="2 4" vertical={false} />
          <XAxis
            dataKey="timestamp"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={axisTick}
            axisLine={{ stroke: TOKENS.hair }}
            tickLine={false}
            tickFormatter={fmtDateTickShort}
          />
          <YAxis tick={axisTick} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={56} tickFormatter={(v) => fmtUsd(v)} />
          {event.strike != null && (
            <ReferenceLine
              y={event.strike}
              stroke={TOKENS.flare}
              strokeDasharray="4 3"
              label={{ value: `strike ${event.strike}`, position: "insideTopRight", fill: TOKENS.flare, fontSize: 10, fontFamily: "IBM Plex Mono" }}
            />
          )}
          <ReferenceLine
            x={event.timestamp}
            stroke={TOKENS.undertow}
            strokeDasharray="3 3"
            label={{ value: entryLabel, position: "insideBottomLeft", fill: TOKENS.undertow, fontSize: 10, fontFamily: "IBM Plex Mono" }}
          />
          {expiryVisible && (
            <ReferenceLine
              x={expiryTs}
              stroke={TOKENS.dim}
              strokeDasharray="3 3"
              label={{ value: "expiry", position: "insideBottomRight", fill: TOKENS.dim, fontSize: 10, fontFamily: "IBM Plex Mono" }}
            />
          )}
          {visibleMarkers.map((m, i) => (
            <ReferenceLine
              key={i}
              x={m.ts}
              stroke={m.color}
              strokeDasharray="2 2"
              label={{ value: m.label, position: i % 2 === 0 ? "insideTopLeft" : "insideBottomLeft", fill: m.color, fontSize: 9.5, fontFamily: "IBM Plex Mono" }}
            />
          ))}
          <Tooltip content={<PriceTooltip />} />
          <Line type="monotone" dataKey="price" stroke={TOKENS.current} strokeWidth={2} dot={false} isAnimationActive={false} />
          <Brush
            dataKey="timestamp"
            height={26}
            stroke={TOKENS.undertow}
            fill={TOKENS.panel2}
            travellerWidth={8}
            startIndex={startIndex}
            endIndex={endIndex}
            tickFormatter={fmtDateTickShort}
          />
        </LineChart>
      </ResponsiveContainer>
      {hiddenNotes.length > 0 && (
        <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 4 }}>
          not shown (no price data that far out): {hiddenNotes.join(" · ")}
        </div>
      )}
    </>
  );
}
