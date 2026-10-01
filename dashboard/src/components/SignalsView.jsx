import { useEffect, useState } from "react";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet, apiPost } from "../api";
import { eventDefaultZoom, eventFetchWindow, fmtDateTime, fmtUsd } from "../format";
import { NEWS_LOOKUPS_ENABLED } from "../featureFlags";
import { EventRow } from "./EventRow";
import { OverviewChart, DetailChart } from "./PriceCharts";
import { Empty, SectionLabel } from "./Shared";

const LIFECYCLE_STYLE = {
  closed: { label: "closed", color: TOKENS.current },
  rolled: { label: "rolled", color: TOKENS.undertow },
};

export function SignalsView({ events, assetFilter, setAssetFilter, loading }) {
  const [selectedId, setSelectedId] = useState(null);
  const [fullPrices, setFullPrices] = useState([]);
  const [zoomPrices, setZoomPrices] = useState([]);
  const [lifecycle, setLifecycle] = useState(null);
  const [walletDetail, setWalletDetail] = useState(null);
  const [annotations, setAnnotations] = useState([]);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [news, setNews] = useState(null);

  const selected = events.find((e) => e.id === selectedId) || events[0];

  // keep selection valid as the asset filter changes the underlying list
  useEffect(() => {
    if (events.length && !events.find((e) => e.id === selectedId)) {
      setSelectedId(events[0].id);
    }
  }, [events, selectedId]);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;

    Promise.all([
      apiGet(`/api/prices?asset=${selected.asset}&granularity=daily`),
      apiGet(`/api/daily_signals?asset=${selected.asset}`),
    ]).then(([priceData, signals]) => {
      if (cancelled) return;
      const volumeByDay = new Map(signals.map((s) => [s.day, s.total_notional_usd]));
      setFullPrices(priceData.map((p) => ({ ...p, volume: volumeByDay.get(p.bucket) ?? 0 })));
    });

    setLifecycle(null);
    apiGet(`/api/events/${selected.id}`).then((full) => {
      if (cancelled) return;
      setLifecycle(full);
      const win = eventFetchWindow(selected, { closeTs: full.close_ts });
      apiGet(`/api/prices?asset=${selected.asset}&granularity=hourly&from_ts=${win.start}&to_ts=${win.end}`).then((d) => {
        if (!cancelled) setZoomPrices(d);
      });
    });

    if (selected.wallet_address) {
      setWalletDetail(null);
      apiGet(`/api/wallets/${selected.wallet_address}`).then((d) => {
        if (!cancelled) setWalletDetail(d);
      });
    } else {
      setWalletDetail(null);
    }

    apiGet(`/api/annotations?event_id=${selected.id}`).then((d) => {
      if (!cancelled) setAnnotations(d);
    });
    setNote("");

    setNews(null);
    if (NEWS_LOOKUPS_ENABLED) {
      apiGet(`/api/news?asset=${selected.asset}&date=${selected.date}&window_days=3`).then((d) => {
        if (!cancelled) setNews(d);
      });
    }

    return () => {
      cancelled = true;
    };
  }, [selected?.id]);

  async function saveNote() {
    if (!note.trim() || !selected) return;
    setSaving(true);
    try {
      await apiPost("/api/annotations", { related_event_id: selected.id, note });
      const fresh = await apiGet(`/api/annotations?event_id=${selected.id}`);
      setAnnotations(fresh);
      setNote("");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <div style={{ padding: "12px 22px 0", display: "flex", justifyContent: "flex-end" }}>
        <div style={{ display: "flex", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, overflow: "hidden" }}>
          {["ALL", "BTC", "ETH"].map((a) => (
            <button
              key={a}
              onClick={() => setAssetFilter(a)}
              style={{ fontFamily: mono, fontSize: 11, padding: "6px 12px", background: assetFilter === a ? TOKENS.panel2 : "transparent", color: assetFilter === a ? TOKENS.paper : TOKENS.dim, border: "none", cursor: "pointer" }}
            >
              {a}
            </button>
          ))}
        </div>
      </div>

      <div style={{ padding: "14px 22px 6px" }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 6 }}>
          <div style={{ fontFamily: mono, fontSize: 11, letterSpacing: "0.14em", color: TOKENS.dim, textTransform: "uppercase" }}>
            {selected ? `${selected.asset} price + daily options volume · inauguration to now` : "price history"}
          </div>
          <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.flare }}>{events.length} flagged</div>
        </div>
        <OverviewChart key={selected?.asset} data={fullPrices} selectedEvent={selected} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "290px 1fr 280px", borderTop: `1px solid ${TOKENS.hair}` }}>
        <div style={{ borderRight: `1px solid ${TOKENS.hair}`, maxHeight: 640, overflowY: "auto" }}>
          <div style={{ padding: "12px 16px", fontFamily: mono, fontSize: 11, letterSpacing: "0.1em", color: TOKENS.dim, textTransform: "uppercase", borderBottom: `1px solid ${TOKENS.hair}` }}>
            Flagged events
          </div>
          {loading && <Empty>loading…</Empty>}
          {!loading && events.length === 0 && <Empty>none for this filter</Empty>}
          {events.map((ev) => (
            <EventRow key={ev.id} ev={ev} active={selected && ev.id === selected.id} onClick={() => setSelectedId(ev.id)} />
          ))}
        </div>

        {selected ? (
          <div style={{ padding: "18px 24px", borderRight: `1px solid ${TOKENS.hair}` }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 6 }}>
              <div>
                <div style={{ fontFamily: mono, fontSize: 22, fontWeight: 700 }}>
                  {selected.asset} {selected.option_type.toUpperCase()} · ${selected.strike.toLocaleString()}
                </div>
                <div style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.dim, marginTop: 4 }}>
                  {selected.side} · opened {fmtDateTime(selected.timestamp)} · expires {selected.expiry_date}
                </div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim }}>notional</div>
                <div style={{ fontFamily: mono, fontSize: 16, color: TOKENS.flare, fontWeight: 600 }}>{fmtUsd(selected.notional_usd)}</div>
              </div>
            </div>

            <div style={{ marginTop: 14, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "14px 10px 4px" }}>
              <DetailChart
                key={selected.id}
                data={zoomPrices}
                event={selected}
                defaultZoom={eventDefaultZoom(selected)}
                markers={
                  lifecycle?.close_ts
                    ? [{
                        ts: lifecycle.close_ts,
                        label: LIFECYCLE_STYLE[lifecycle.status]?.label || "closed",
                        color: LIFECYCLE_STYLE[lifecycle.status]?.color || TOKENS.current,
                      }]
                    : []
                }
              />
            </div>
            {lifecycle && (
              <div style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim, marginTop: 4 }}>
                position: {lifecycle.status === "closed" || lifecycle.status === "rolled"
                  ? `${LIFECYCLE_STYLE[lifecycle.status]?.label} ${fmtDateTime(lifecycle.close_ts)}${lifecycle.roll_to_instrument ? ` → ${lifecycle.roll_to_instrument}` : ""}`
                  : lifecycle.status === "expired" ? "held to expiry, no closing trade in our data" : "still open per our data"}
              </div>
            )}

            <div style={{ marginTop: 18 }}>
              <SectionLabel>News lookup · {selected.date} ± 3 days</SectionLabel>
              <div style={{ background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "4px 14px", maxHeight: 180, overflowY: "auto" }}>
                {!NEWS_LOOKUPS_ENABLED && <Empty>news lookups turned off for now (slow, no caching yet) -- see featureFlags.js</Empty>}
                {NEWS_LOOKUPS_ENABLED && !news && <Empty>loading…</Empty>}
                {news?.items.length === 0 && <Empty>no headlines found for this window</Empty>}
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
              <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, marginTop: 6 }}>
                manual review only — pulled from Google News, not auto-matched to this event
              </div>
            </div>

            <div style={{ marginTop: 18 }}>
              <SectionLabel>Annotation</SectionLabel>
              {annotations.map((a) => (
                <div key={a.id} style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.paper, padding: "8px 10px", background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, marginBottom: 8 }}>
                  {a.note}
                </div>
              ))}
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="worth noting? outcome, direction, anything odd about sizing…"
                style={{ width: "100%", minHeight: 64, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, color: TOKENS.paper, fontFamily: sans, fontSize: 12.5, padding: 10, resize: "vertical", boxSizing: "border-box" }}
              />
              <button
                onClick={saveNote}
                disabled={saving || !note.trim()}
                style={{ marginTop: 8, fontFamily: mono, fontSize: 11, padding: "6px 14px", background: "transparent", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: note.trim() ? "pointer" : "default", opacity: note.trim() ? 1 : 0.5 }}
              >
                save
              </button>
            </div>
          </div>
        ) : (
          <div style={{ padding: 24 }}>
            <Empty>select an event</Empty>
          </div>
        )}

        <div style={{ padding: "18px 18px" }}>
          <SectionLabel>Wallet</SectionLabel>
          {!selected?.wallet_address ? (
            <Empty>no wallet data for this event</Empty>
          ) : !walletDetail ? (
            <Empty>loading…</Empty>
          ) : (
            <>
              <div style={{ fontFamily: mono, fontSize: 15, fontWeight: 700, color: TOKENS.undertow }}>{walletDetail.alias}</div>
              <div style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim, wordBreak: "break-all", marginTop: 2 }}>{walletDetail.address}</div>
              <div style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.dim, marginTop: 4 }}>
                {walletDetail.label || "unlabeled"} · first seen {walletDetail.first_seen}
              </div>
              <div style={{ marginTop: 16, fontFamily: mono, fontSize: 10.5, letterSpacing: "0.08em", color: TOKENS.dim, textTransform: "uppercase" }}>
                Flagged activity, this wallet ({walletDetail.events.length} of {walletDetail.total_event_count} trades)
              </div>
              <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 8, maxHeight: 420, overflowY: "auto" }}>
                {walletDetail.events.map((a) => (
                  <div key={a.id} style={{ borderLeft: `2px solid ${TOKENS.flare}`, paddingLeft: 10 }}>
                    <div style={{ fontFamily: mono, fontSize: 11.5, color: TOKENS.paper }}>
                      {a.asset} {a.option_type.toUpperCase()} {a.strike.toLocaleString()} · {a.side}
                    </div>
                    <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim }}>
                      {a.date} · {fmtUsd(a.notional_usd)}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  );
}
