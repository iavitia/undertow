import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet } from "../api";
import { fmtDateTime, fmtUsd } from "../format";
import { Empty, SectionLabel, StatCard } from "./Shared";

function PctSign({ value }) {
  if (value == null) return <span style={{ color: TOKENS.dim }}>—</span>;
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{value}%</span>;
}

function ScoreCell({ n, wilsonLow, title }) {
  if (n == null) return <span title={title} style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.hair }}>no data</span>;
  const color = wilsonLow >= 65 ? TOKENS.current : wilsonLow >= 50 ? TOKENS.flare : TOKENS.bad;
  return <span title={title} style={{ fontFamily: mono, fontSize: 10.5, color }}>n={n} · {wilsonLow}%</span>;
}

function PipelineStatus() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    apiGet("/api/pipeline-status").then(setStatus);
    const interval = setInterval(() => apiGet("/api/pipeline-status").then(setStatus), 30000);
    return () => clearInterval(interval);
  }, []);

  if (!status) return <Empty>loading…</Empty>;
  const fresh = status.minutes_since_ingest != null && status.minutes_since_ingest < 20;
  const lastRun = status.recent_runs[0];

  return (
    <div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <StatCard
          label="Last ingest"
          value={status.minutes_since_ingest != null ? `${status.minutes_since_ingest}m ago` : "never"}
          accent={fresh ? TOKENS.current : TOKENS.bad}
        />
        <StatCard label="Total events (all-time)" value={status.total_events.toLocaleString()} />
        <StatCard label="Total wallets seen" value={status.total_wallets.toLocaleString()} />
        {lastRun && (
          <StatCard
            label="Last tick"
            value={`+${lastRun.ingested_count} events`}
            accent={TOKENS.undertow}
          />
        )}
      </div>
      {!fresh && status.minutes_since_ingest != null && (
        <div style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.bad, marginTop: 8 }}>
          Last successful ingest was {status.minutes_since_ingest} minutes ago -- the scheduled task (every 15 min)
          may not be running. Check `schtasks /query /tn UndertowWatchlistAgent /v`.
        </div>
      )}

      {status.recent_runs.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <SectionLabel>Recent agent ticks</SectionLabel>
          <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden", maxHeight: 220, overflowY: "auto" }}>
            <div style={{ display: "grid", gridTemplateColumns: "160px 100px 100px 100px 100px", padding: "7px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
              <span>time</span><span>ingested</span><span>opened</span><span>advanced</span><span>resolved</span>
            </div>
            {status.recent_runs.map((r, i) => (
              <div key={i} style={{ display: "grid", gridTemplateColumns: "160px 100px 100px 100px 100px", padding: "6px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: mono, fontSize: 11, color: TOKENS.paper }}>
                <span style={{ color: TOKENS.dim }}>{fmtDateTime(r.run_ts)}</span>
                <span>{r.ingested_count}</span>
                <span style={{ color: r.opened_count > 0 ? TOKENS.current : TOKENS.dim }}>{r.opened_count}</span>
                <span>{r.advanced_count}</span>
                <span>{r.resolved_count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function TradeRow({ t }) {
  const navigate = useNavigate();
  return (
    <div style={{ display: "grid", gridTemplateColumns: "130px 150px 1fr 90px 130px 130px 90px", padding: "8px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
      <span style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim }}>{fmtDateTime(t.timestamp)}</span>
      <span>
        <button
          onClick={() => navigate(`/wallets/${t.wallet_address}`)}
          style={{ fontFamily: mono, fontSize: 12, color: TOKENS.undertow, background: "transparent", border: "none", cursor: "pointer", padding: 0, textAlign: "left" }}
        >
          {t.alias}
        </button>
        {t.on_watchlist && <span style={{ fontFamily: mono, fontSize: 9, color: TOKENS.ink, background: TOKENS.flare, padding: "1px 4px", borderRadius: 3, marginLeft: 5 }}>WATCH</span>}
      </span>
      <span>
        {t.asset} {t.option_type.toUpperCase()} {t.strike.toLocaleString()} · exp {t.expiry_date} · {t.side}
        {t.is_rfq && <span style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, marginLeft: 6 }}>RFQ</span>}
      </span>
      <span style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim }}>{fmtUsd(t.notional_usd)}</span>
      <ScoreCell n={t.copy_positions} wilsonLow={t.copy_win_rate_wilson_low} title={`Full-history copy-backtest Wilson win rate (n=${t.copy_positions}), total return ${t.copy_total_return_pct}%`} />
      <ScoreCell n={t.edge_n} wilsonLow={t.edge_wilson_low} title={`This wallet's Wilson win rate specifically in ${t.asset} ${t.option_type} entered-via-${t.side} trades (n=${t.edge_n}), median return ${t.edge_median_return_pct}%`} />
      <span style={{ fontFamily: mono, fontSize: 10.5 }}>
        {t.passes_screen == null ? (
          <span style={{ color: TOKENS.hair }}>no data</span>
        ) : t.passes_screen ? (
          <span style={{ color: TOKENS.current }}>✓ validated</span>
        ) : (
          <span style={{ color: TOKENS.dim }}>screened, failed</span>
        )}
      </span>
    </div>
  );
}

export function LiveFeedView() {
  const [trades, setTrades] = useState([]);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const PAGE_SIZE = 40;

  function loadMore(reset) {
    setLoading(true);
    const nextOffset = reset ? 0 : offset;
    apiGet(`/api/recent-trades?limit=${PAGE_SIZE}&offset=${nextOffset}`).then((d) => {
      setTrades(reset ? d : (prev) => [...prev, ...d]);
      setOffset(nextOffset + PAGE_SIZE);
      setLoading(false);
    });
  }

  useEffect(() => {
    loadMore(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{ padding: "20px 24px" }}>
      <div style={{ fontFamily: mono, fontSize: 20, fontWeight: 700 }}>Live feed</div>
      <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, marginTop: 4, maxWidth: 700 }}>
        Every trade market-wide (not just watchlist wallets) as it lands in the database, scored with whatever we
        currently have for that wallet. Most wallets won't have all of it -- full-history copy-backtest Wilson
        covers 7,436 wallets, the trade-level bucket confidence only 3,025, and the walk-forward-validated screen
        only 375 (23 passing) -- "no data" just means that wallet hasn't traded enough for that particular check
        yet, not that it's bad.
      </div>

      <div style={{ marginTop: 20 }}>
        <PipelineStatus />
      </div>

      <div style={{ marginTop: 24 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <SectionLabel>Recent trades, newest first</SectionLabel>
          <button
            onClick={() => loadMore(true)}
            style={{ fontFamily: mono, fontSize: 10.5, padding: "5px 10px", background: "transparent", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: "pointer", marginBottom: 8 }}
          >
            ↻ refresh to newest
          </button>
        </div>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
          <div style={{ display: "grid", gridTemplateColumns: "130px 150px 1fr 90px 130px 130px 90px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>time</span><span>wallet</span><span>position</span><span>notional</span><span>full wilson</span><span>trade-edge wilson</span><span>walk-forward</span>
          </div>
          {trades.length === 0 && !loading && <Empty>no trades yet</Empty>}
          {trades.map((t) => <TradeRow key={t.id} t={t} />)}
        </div>
        <div style={{ textAlign: "center", marginTop: 12 }}>
          <button
            onClick={() => loadMore(false)}
            disabled={loading}
            style={{ fontFamily: mono, fontSize: 11, padding: "8px 16px", background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: loading ? "default" : "pointer" }}
          >
            {loading ? "loading…" : "load more"}
          </button>
        </div>
      </div>
    </div>
  );
}
