import { useEffect, useState } from "react";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet } from "../api";
import { fmtDateTime, fmtUsd } from "../format";
import { NEWS_LOOKUPS_ENABLED } from "../featureFlags";
import { DetailChart } from "./PriceCharts";
import { Empty, SectionLabel } from "./Shared";

const VERDICT_STYLE = {
  strong_signal: { label: "SIGNAL FOUND", color: TOKENS.flare },
  mixed_signal: { label: "MIXED", color: TOKENS.undertow },
  no_signal: { label: "NO SIGNAL", color: TOKENS.dim },
};

function VerdictBadge({ verdict }) {
  const s = VERDICT_STYLE[verdict] || { label: verdict, color: TOKENS.dim };
  return (
    <span style={{ fontFamily: mono, fontSize: 9.5, color: s.color === TOKENS.dim ? TOKENS.dim : TOKENS.ink, background: s.color === TOKENS.dim ? "transparent" : s.color, border: s.color === TOKENS.dim ? `1px solid ${TOKENS.hair}` : "none", padding: "2px 6px", borderRadius: 3, fontWeight: 600 }}>
      {s.label}
    </span>
  );
}

const DAY_MS = 86400000;
const LIFECYCLE_STYLE = {
  closed: { label: "closed", color: TOKENS.current },
  rolled: { label: "rolled", color: TOKENS.undertow },
  open: { label: "still open", color: TOKENS.flare },
  expired: { label: "held to expiry", color: TOKENS.dim },
};

// Builds DetailChart's `markers` prop from the catalyst's grouped positions:
// entry + close/roll per position. Expiry markers are handled by
// DetailChart itself per-event, not here, since a catalyst can have
// several positions with different expiries.
function positionsToMarkers(positions) {
  const markers = [];
  for (const p of positions) {
    markers.push({ ts: p.entry_ts, label: `${p.wallet_alias} entry`, color: TOKENS.flare });
    if (p.close_ts) {
      const style = LIFECYCLE_STYLE[p.status] || LIFECYCLE_STYLE.closed;
      markers.push({ ts: p.close_ts, label: `${p.wallet_alias} ${style.label}`, color: style.color });
    }
  }
  return markers;
}

// Price-fetch window: wide enough to cover the announcement AND every
// position's entry/close/expiry, capped at "now". Previously this was a
// fixed announcement +/- 3 days, which clipped anything that happened
// outside that -- e.g. the tariff-crash position closed 2 weeks after the
// announcement, well outside a 3-day window, so its close marker (and the
// price action around it) was simply never fetched.
function catalystFetchWindow(moveTs, positions, paddingDays = 5) {
  const now = Date.now();
  const marks = [moveTs];
  for (const p of positions) {
    marks.push(p.entry_ts);
    if (p.close_ts) marks.push(p.close_ts);
    marks.push(Math.min(p.expiry * 1000, now));
  }
  return {
    start: Math.min(...marks) - paddingDays * DAY_MS,
    end: Math.min(Math.max(...marks) + paddingDays * DAY_MS, now),
  };
}

function ExpandableTransaction({ ev }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(null);

  function toggle() {
    setOpen((o) => !o);
    if (!full) apiGet(`/api/events/${ev.id}`).then(setFull);
  }

  const lifecycle = full ? LIFECYCLE_STYLE[full.status] : null;

  return (
    <div style={{ borderTop: `1px solid ${TOKENS.hair}` }}>
      <button
        onClick={toggle}
        style={{ width: "100%", textAlign: "left", background: "transparent", border: "none", cursor: "pointer", padding: "10px 14px", display: "flex", justifyContent: "space-between", fontFamily: sans, fontSize: 12, color: TOKENS.paper }}
      >
        <span>
          <span style={{ fontFamily: mono, color: TOKENS.dim, marginRight: 6 }}>{open ? "▾" : "▸"}</span>
          <span style={{ fontFamily: mono, color: TOKENS.undertow }}>{ev.wallet_alias}</span> ·{" "}
          {ev.asset} {ev.option_type.toUpperCase()} {ev.strike.toLocaleString()} · {ev.side} · {ev.date}
        </span>
        <span style={{ fontFamily: mono }}>{fmtUsd(ev.notional_usd)}</span>
      </button>
      {open && (
        <div style={{ padding: "4px 14px 14px 30px", background: TOKENS.panel2 }}>
          {!full ? (
            <Empty>loading…</Empty>
          ) : (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px 24px", fontFamily: mono, fontSize: 11.5 }}>
              <Field label="instrument" value={full.instrument} />
              <Field label="expiry" value={full.expiry_date} />
              <Field label="side" value={full.side} />
              <Field label="size (contracts)" value={full.size} />
              <Field label="premium (price)" value={full.price} />
              <Field label="notional (usd)" value={fmtUsd(full.notional_usd)} />
              <Field label="index price at execution" value={fmtUsd(full.index_price_usd)} />
              <Field label="mark price" value={full.mark_price ?? "—"} />
              <Field label="liquidity role" value={full.liquidity_role ?? "—"} />
              <Field label="rfq" value={full.rfq_id ? "yes (block-negotiated)" : "no (order book)"} />
              <Field label="trade fee" value={full.trade_fee ?? "—"} />
              <Field label="realized pnl" value={full.realized_pnl ?? "—"} />
              <Field label="tx status" value={full.tx_status ?? "—"} />
              <Field label="tx hash" value={full.tx_hash ? `${full.tx_hash.slice(0, 14)}…` : "—"} />
              <Field label="wallet address" value={full.wallet_address} />
              <Field label="flagged as" value={full.flag_reasons.join(", ") || "not flagged"} />
              <Field
                label="position lifecycle"
                value={
                  full.status === "closed" || full.status === "rolled"
                    ? `${lifecycle.label} ${fmtDateTime(full.close_ts)}${full.roll_to_instrument ? ` → ${full.roll_to_instrument}` : ""}`
                    : lifecycle.label
                }
              />
              <div style={{ gridColumn: "1 / -1", fontFamily: sans, fontSize: 10, color: TOKENS.dim, marginTop: 4 }}>
                no per-trade greeks (delta/IV/etc) available -- Derive only exposes those on the live ticker snapshot, not on historical trade records
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }) {
  return (
    <div>
      <span style={{ color: TOKENS.dim, textTransform: "uppercase", fontSize: 9.5, letterSpacing: "0.05em" }}>{label}</span>
      <div style={{ color: TOKENS.paper, wordBreak: "break-all" }}>{String(value)}</div>
    </div>
  );
}

export function CatalystsView() {
  const [catalysts, setCatalysts] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [prices, setPrices] = useState([]);
  const [news, setNews] = useState(null);
  const [trumpPosts, setTrumpPosts] = useState(null);

  useEffect(() => {
    apiGet("/api/catalysts").then((d) => {
      setCatalysts(d);
      if (d.length) setSelectedId(d[0].id);
    });
  }, []);

  const selected = catalysts.find((c) => c.id === selectedId);

  useEffect(() => {
    if (!selectedId) return;
    setDetail(null);
    setNews(null);
    setTrumpPosts(null);
    apiGet(`/api/catalysts/${selectedId}`).then(async (d) => {
      setDetail(d);
      const moveTs = new Date(d.confirmed_move_utc).getTime();
      const fetchWin = catalystFetchWindow(moveTs, d.positions);
      const priceData = await apiGet(
        `/api/prices?asset=${d.asset}&granularity=hourly&from_ts=${fetchWin.start}&to_ts=${fetchWin.end}`
      );
      setPrices(priceData);

      if (NEWS_LOOKUPS_ENABLED) {
        const newsData = await apiGet(`/api/news?asset=${d.asset}&date=${d.confirmed_move_date}&window_days=3`);
        setNews(newsData);

        const startDate = new Date(moveTs - 3 * DAY_MS).toISOString().slice(0, 10);
        const endDate = new Date(moveTs + 3 * DAY_MS).toISOString().slice(0, 10);
        const postsData = await apiGet(`/api/trump-posts?start_date=${startDate}&end_date=${endDate}`);
        setTrumpPosts(postsData);
      }
    });
  }, [selectedId]);

  const chartEvent = detail ? { timestamp: new Date(detail.confirmed_move_utc).getTime() } : null;
  const chartMarkers = detail ? positionsToMarkers(detail.positions) : [];
  const chartDefaultZoom = chartEvent
    ? { start: chartEvent.timestamp - 3 * DAY_MS, end: Math.min(chartEvent.timestamp + 3 * DAY_MS, Date.now()) }
    : null;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "300px 1fr" }}>
      <div style={{ borderRight: `1px solid ${TOKENS.hair}` }}>
        <div style={{ padding: "12px 16px", fontFamily: mono, fontSize: 11, letterSpacing: "0.1em", color: TOKENS.dim, textTransform: "uppercase", borderBottom: `1px solid ${TOKENS.hair}` }}>
          Trump-announcement catalysts · {catalysts.length}
        </div>
        {catalysts.map((c) => (
          <button
            key={c.id}
            onClick={() => setSelectedId(c.id)}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              background: c.id === selectedId ? TOKENS.panel2 : "transparent",
              border: "none",
              borderLeft: `2px solid ${c.id === selectedId ? TOKENS.flare : "transparent"}`,
              borderBottom: `1px solid ${TOKENS.hair}`,
              padding: "12px 16px",
              cursor: "pointer",
            }}
          >
            <div style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: TOKENS.paper }}>{c.label}</div>
            <div style={{ marginTop: 6 }}>
              <VerdictBadge verdict={c.verdict} />
            </div>
          </button>
        ))}
      </div>

      {!selected || !detail ? (
        <div style={{ padding: 24 }}>
          <Empty>loading…</Empty>
        </div>
      ) : (
        <div style={{ padding: "20px 24px" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 16 }}>
            <div>
              <div style={{ fontFamily: mono, fontSize: 20, fontWeight: 700 }}>{detail.label}</div>
              <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, marginTop: 4 }}>
                confirmed move {fmtDateTime(new Date(detail.confirmed_move_utc).getTime())}
              </div>
            </div>
            <VerdictBadge verdict={detail.verdict} />
          </div>

          <div style={{ marginTop: 16 }}>
            <SectionLabel>Claim</SectionLabel>
            <div style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.dim, lineHeight: 1.5 }}>{detail.claim}</div>
          </div>

          <div style={{ marginTop: 14 }}>
            <SectionLabel>What our data actually shows</SectionLabel>
            <div style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.paper, lineHeight: 1.5 }}>{detail.confirmed_note}</div>
          </div>

          <div style={{ marginTop: 14, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "14px 10px 4px" }}>
            {prices.length > 0 && chartEvent ? (
              <DetailChart key={selectedId} data={prices} event={chartEvent} entryLabel="announcement" markers={chartMarkers} defaultZoom={chartDefaultZoom} />
            ) : (
              <Empty>loading price data…</Empty>
            )}
          </div>

          <div style={{ marginTop: 18 }}>
            <SectionLabel>Verdict</SectionLabel>
            <div style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.paper, lineHeight: 1.5, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "12px 14px" }}>
              {detail.verdict_note}
            </div>
          </div>

          {detail.events.length > 0 && (
            <div style={{ marginTop: 18 }}>
              <SectionLabel>Related transactions ({detail.events.length}) — click to expand</SectionLabel>
              <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
                {detail.events.map((ev) => (
                  <ExpandableTransaction key={ev.id} ev={ev} />
                ))}
              </div>
            </div>
          )}

          <div style={{ marginTop: 18 }}>
            <SectionLabel>News lookup · {detail.confirmed_move_date} ± 3 days</SectionLabel>
            <div style={{ background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "4px 14px", maxHeight: 220, overflowY: "auto" }}>
              {!NEWS_LOOKUPS_ENABLED && <Empty>news lookups turned off for now (slow, no caching yet) -- see featureFlags.js</Empty>}
              {NEWS_LOOKUPS_ENABLED && !news && <Empty>loading…</Empty>}
              {news?.items.map((it, i) => (
                <a
                  key={i}
                  href={it.link}
                  target="_blank"
                  rel="noreferrer"
                  style={{ display: "block", fontFamily: sans, fontSize: 12.5, color: TOKENS.paper, padding: "8px 0", borderBottom: i < news.items.length - 1 ? `1px solid ${TOKENS.hair}` : "none", textDecoration: "none" }}
                >
                  {it.title}
                  <span style={{ fontFamily: mono, fontSize: 10, color: it.source_type === "policy" ? TOKENS.undertow : TOKENS.dim, marginLeft: 6 }}>
                    {it.source}
                    {it.source_type === "policy" ? " · capitol hill" : ""}
                  </span>
                </a>
              ))}
            </div>
          </div>

          <div style={{ marginTop: 18 }}>
            <SectionLabel>Trump's Truth Social posts · {detail.confirmed_move_date} ± 3 days (via Roll Call Factbase)</SectionLabel>
            <div style={{ background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "4px 14px", maxHeight: 260, overflowY: "auto" }}>
              {!NEWS_LOOKUPS_ENABLED && <Empty>Trump-post lookups turned off for now (slow, first fetch of a window can take ~30s) -- see featureFlags.js</Empty>}
              {NEWS_LOOKUPS_ENABLED && !trumpPosts && <Empty>loading… (first load of a window can take ~30s -- the archive has no server-side date filter, so this paginates to find it; cached after that)</Empty>}
              {trumpPosts?.items.length === 0 && <Empty>no posts found for this window</Empty>}
              {trumpPosts?.items.map((p, i) => (
                <a
                  key={i}
                  href={p.post_url}
                  target="_blank"
                  rel="noreferrer"
                  style={{
                    display: "block",
                    fontFamily: sans,
                    fontSize: 12.5,
                    color: p.is_crypto_related ? TOKENS.paper : TOKENS.dim,
                    padding: "8px 10px",
                    marginTop: 6,
                    marginBottom: 2,
                    borderRadius: 4,
                    background: p.is_crypto_related ? TOKENS.panel2 : "transparent",
                    borderLeft: p.is_crypto_related ? `2px solid ${TOKENS.flare}` : `2px solid transparent`,
                    textDecoration: "none",
                    lineHeight: 1.4,
                  }}
                >
                  <span style={{ fontFamily: mono, fontSize: 10, color: p.is_crypto_related ? TOKENS.flare : TOKENS.dim }}>
                    {fmtDateTime(new Date(p.date).getTime())}
                  </span>
                  <div>{p.text.length > 260 ? p.text.slice(0, 260) + "…" : p.text}</div>
                </a>
              ))}
            </div>
            <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, marginTop: 6 }}>
              full unfiltered archive of everything Trump posted in this window -- highlighted ones match a simple crypto/tariff keyword check, not curated
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
