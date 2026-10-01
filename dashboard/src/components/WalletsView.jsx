import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet } from "../api";
import { fmtDateTick, fmtDateTime, fmtUsd } from "../format";
import { NEWS_LOOKUPS_ENABLED } from "../featureFlags";
import { Empty, SectionLabel, StatCard } from "./Shared";

const ACTIVITY_STYLE = {
  deposit: { label: "deposit", color: TOKENS.current },
  withdrawal: { label: "withdrawal", color: TOKENS.flare },
  staking: { label: "staking", color: TOKENS.undertow },
  transfer: { label: "transfer", color: TOKENS.dim },
};

function PnlSign({ value }) {
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{fmtUsd(value)}</span>;
}

function PctSpan({ value }) {
  if (value == null) return <span style={{ color: TOKENS.dim }}>—</span>;
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{value}%</span>;
}

const TIER_COLOR = {
  high_confidence: TOKENS.current,
  validated: TOKENS.current,
  provisional: TOKENS.flare,
  qualified_but_faded: TOKENS.bad,
  does_not_qualify: TOKENS.bad,
  insufficient_data: TOKENS.dim,
};

const TIER_LABEL = {
  high_confidence: "high confidence",
  validated: "validated",
  provisional: "provisional",
  qualified_but_faded: "qualified, faded",
  does_not_qualify: "does not qualify",
  insufficient_data: "insufficient data",
};

const axisTick = { fill: TOKENS.dim, fontSize: 10, fontFamily: "IBM Plex Mono" };

function ValueTimelineTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div style={{ background: TOKENS.panel2, border: `1px solid ${TOKENS.hair}`, fontFamily: mono, fontSize: 11, padding: "6px 10px" }}>
      <div style={{ color: TOKENS.dim }}>{p.date}</div>
      <div style={{ color: TOKENS.current }}>P&L net fees: {fmtUsd(p.cumulative_pnl_net_fees)}</div>
      <div style={{ color: TOKENS.flare }}>USDC flow: {fmtUsd(p.cumulative_usdc_flow)}</div>
    </div>
  );
}

function ValueTimelineChart({ address }) {
  const [series, setSeries] = useState(null);

  useEffect(() => {
    setSeries(null);
    apiGet(`/api/wallets/${address}/value-timeline`).then((d) => setSeries(d.series));
  }, [address]);

  if (!series) return <Empty>loading… (first load pulls on-chain history if not already cached, can take ~15s)</Empty>;
  if (series.length === 0) return <Empty>no data</Empty>;

  return (
    <div>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={series} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={TOKENS.hair} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="timestamp" type="number" domain={["dataMin", "dataMax"]} tick={axisTick} axisLine={{ stroke: TOKENS.hair }} tickLine={false} tickFormatter={fmtDateTick} />
          <YAxis tick={axisTick} axisLine={false} tickLine={false} domain={["auto", "auto"]} width={64} tickFormatter={(v) => fmtUsd(v)} />
          <ReferenceLine y={0} stroke={TOKENS.hair} />
          <Tooltip content={<ValueTimelineTooltip />} />
          <Line type="stepAfter" dataKey="cumulative_pnl_net_fees" stroke={TOKENS.current} strokeWidth={2} dot={false} isAnimationActive={false} name="P&L net fees" />
          <Line type="stepAfter" dataKey="cumulative_usdc_flow" stroke={TOKENS.flare} strokeWidth={1.5} strokeDasharray="4 3" dot={false} isAnimationActive={false} name="USDC flow" />
        </LineChart>
      </ResponsiveContainer>
      <div style={{ display: "flex", gap: 16, fontFamily: mono, fontSize: 10.5, marginTop: 6 }}>
        <span style={{ color: TOKENS.current }}>— cumulative realized P&L, net of fees</span>
        <span style={{ color: TOKENS.flare }}>- - cumulative USDC bridge flow (deposits − withdrawals)</span>
      </div>
      <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 4 }}>
        Not a true mark-to-market net worth -- that needs historical option pricing for still-open positions and USD
        conversion for non-USDC collateral (WBTC, WstETH, HYPE, etc.), neither of which is available. This is trading
        outcome and cash movement, the two things we can actually compute.
      </div>
    </div>
  );
}

function LegRow({ l }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "130px 50px 70px 70px 70px 80px 90px 90px 1fr", padding: "6px 14px", fontFamily: mono, fontSize: 10.5, color: TOKENS.paper, borderTop: `1px solid ${TOKENS.hair}`, alignItems: "center" }}>
      <span style={{ color: TOKENS.dim }}>{fmtDateTime(l.timestamp)}</span>
      <span>{l.side}</span>
      <span>{l.size}</span>
      <span>{l.price}</span>
      <span>{l.mark_price != null ? l.mark_price.toFixed(2) : "—"}</span>
      <span>{l.liquidity_role ?? "—"}</span>
      <span>{fmtUsd(l.trade_fee)} fee</span>
      <span><PnlSign value={l.realized_pnl ?? 0} /></span>
      <span style={{ color: TOKENS.dim, wordBreak: "break-all" }}>{l.tx_hash ? `${l.tx_hash.slice(0, 12)}…` : "—"}</span>
    </div>
  );
}

function ExpandablePosition({ p, address }) {
  const [open, setOpen] = useState(false);
  const [legs, setLegs] = useState(null);

  function toggle() {
    setOpen((o) => !o);
    if (!legs) apiGet(`/api/wallets/${address}/positions/${encodeURIComponent(p.instrument)}/legs`).then(setLegs);
  }

  return (
    <div style={{ borderTop: `1px solid ${TOKENS.hair}` }}>
      <button onClick={toggle} style={{ width: "100%", textAlign: "left", background: "transparent", border: "none", cursor: "pointer", padding: "9px 14px", display: "grid", gridTemplateColumns: "20px 100px 1fr 70px 90px", fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
        <span style={{ fontFamily: mono, color: TOKENS.dim }}>{open ? "▾" : "▸"}</span>
        <span style={{ fontFamily: mono, color: TOKENS.dim }}>{p.entry_date}</span>
        <span>
          {p.asset} {p.option_type.toUpperCase()} {p.strike.toLocaleString()} · exp {p.expiry_date} · entered via {p.entry_side}
        </span>
        <span style={{ fontFamily: mono, color: TOKENS.dim }}>{p.leg_count} fills</span>
        <span style={{ textAlign: "right" }}>
          <PnlSign value={p.net_pnl} />
          {p.pnl_is_estimated && <span title="No closing trade for this position -- P&L estimated from intrinsic value at expiry, not an on-chain settlement figure" style={{ color: TOKENS.flare, marginLeft: 3 }}>*</span>}
        </span>
      </button>
      {open && (
        <div style={{ background: TOKENS.panel2, paddingBottom: 4 }}>
          <div style={{ display: "grid", gridTemplateColumns: "130px 50px 70px 70px 70px 80px 90px 90px 1fr", padding: "6px 14px", fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, textTransform: "uppercase" }}>
            <span>time</span><span>side</span><span>size</span><span>price</span><span>mark</span><span>role</span><span>fee</span><span>pnl</span><span>tx</span>
          </div>
          {!legs ? <Empty>loading…</Empty> : legs.map((l) => <LegRow key={l.id} l={l} />)}
        </div>
      )}
    </div>
  );
}

function RatingsSection({ address }) {
  const [ratings, setRatings] = useState(null);

  useEffect(() => {
    setRatings(null);
    apiGet(`/api/wallets/${address}/ratings`).then(setRatings);
  }, [address]);

  if (!ratings) return <Empty>loading ratings…</Empty>;
  const { own_record, copy_backtest, qualification, edge_profile } = ratings;

  return (
    <div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "12px 14px" }}>
          <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim, textTransform: "uppercase" }}>own trading record</div>
          <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 2, marginBottom: 8 }}>
            This wallet's own realized P&L, trading at its own size and timing -- says nothing about whether copying it would work.
          </div>
          {!own_record ? <Empty>not yet computed</Empty> : (
            <div style={{ fontFamily: mono, fontSize: 13 }}>
              {own_record.resolved} resolved · <span style={{ color: own_record.win_rate >= 50 ? TOKENS.current : TOKENS.bad }}>{own_record.win_rate}% win</span> · <PnlSign value={own_record.net_of_fees} /> net of fees
              {own_record.is_likely_bot && <span style={{ color: TOKENS.flare, marginLeft: 6, fontSize: 10 }}>· possible MM</span>}
            </div>
          )}
        </div>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "12px 14px" }}>
          <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim, textTransform: "uppercase" }}>copy-trade backtest</div>
          <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 2, marginBottom: 8 }}>
            If a copier had mirrored every open at the next available market print (not this wallet's own fill) -- would it have been profitable?
          </div>
          {!copy_backtest ? <Empty>not enough copy-eligible history</Empty> : (
            <div style={{ fontFamily: mono, fontSize: 13 }}>
              n={copy_backtest.copy_positions} · wilson {copy_backtest.copy_win_rate_wilson_low}% · <PctSpan value={copy_backtest.copy_total_return_pct} /> total
              <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 4 }}>
                best <PctSpan value={copy_backtest.copy_best_return_pct} /> · worst <PctSpan value={copy_backtest.copy_worst_return_pct} />
              </div>
            </div>
          )}
        </div>
      </div>

      <div style={{ marginTop: 16, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "12px 14px" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim, textTransform: "uppercase" }}>no-look-ahead qualification</div>
          {qualification && <span style={{ fontFamily: mono, fontSize: 11, color: TIER_COLOR[qualification.tier], fontWeight: 700 }}>{TIER_LABEL[qualification.tier] ?? qualification.tier}</span>}
        </div>
        <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 2, marginBottom: 8 }}>
          Walks this wallet's resolved copy-trade history chronologically, using only data available at each point --
          the first time the cumulative Wilson lower bound clears 60% and cumulative return is positive is the
          qualification point. Everything after that is a genuine forward test, not a second look at the same data.
        </div>
        {!qualification ? (
          <Empty>not yet computed -- add to watchlist to qualify on demand, or wait for the next batch run</Empty>
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, fontFamily: mono, fontSize: 12 }}>
              <div>
                <div style={{ color: TOKENS.dim, fontSize: 10, textTransform: "uppercase" }}>pre-qualification (n={qualification.pre_qualification_n ?? "—"})</div>
                <div>{qualification.pre_qualification_win_rate ?? "—"}% win · wilson {qualification.pre_qualification_wilson_low ?? "—"}% · <PctSpan value={qualification.pre_qualification_total_return_pct} /></div>
              </div>
              <div>
                <div style={{ color: TOKENS.dim, fontSize: 10, textTransform: "uppercase" }}>forward, post-qualification (n={qualification.forward_n ?? "—"})</div>
                <div>{qualification.forward_win_rate ?? "—"}% win · wilson {qualification.forward_wilson_low ?? "—"}% · <PctSpan value={qualification.forward_total_return_pct} /></div>
              </div>
            </div>
            <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 8 }}>
              total n={qualification.total_n} · qualified at trade #{qualification.qualified_at_n ?? "—"} · risk discipline {qualification.risk_discipline_score ?? "—"}/100 · rfq {qualification.rfq_pct ?? "—"}% · sizing cv {qualification.sizing_cv ?? "—"}
            </div>
          </>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <SectionLabel>Edge by trade type (asset · option type · entry side)</SectionLabel>
        <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginBottom: 6 }}>
          This wallet's copy-backtest record broken out by exact trade type instead of one blended number -- edge is
          usually concentrated in a specific kind of trade, not uniform across everything a wallet does.
        </div>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
          <div style={{ display: "grid", gridTemplateColumns: "70px 70px 70px 60px 90px 1fr", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>asset</span><span>type</span><span>side</span><span>n</span><span>wilson</span><span>median return</span>
          </div>
          {edge_profile.length === 0 && <Empty>no buckets with enough history yet (n≥5 required)</Empty>}
          {edge_profile.map((b, i) => (
            <div key={i} style={{ display: "grid", gridTemplateColumns: "70px 70px 70px 60px 90px 1fr", padding: "8px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: mono, fontSize: 11.5 }}>
              <span style={{ color: TOKENS.flare }}>{b.asset}</span>
              <span>{b.option_type}</span>
              <span>{b.entry_side}</span>
              <span style={{ color: TOKENS.dim }}>{b.n}</span>
              <span style={{ color: b.wilson_low >= 65 ? TOKENS.current : b.wilson_low >= 50 ? TOKENS.flare : TOKENS.bad }}>{b.wilson_low}%</span>
              <span><PctSpan value={b.median_return_pct} /></span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const HISTORY_SORTS = {
  date_desc: { label: "newest first", cmp: (a, b) => b.entry_ts - a.entry_ts },
  date_asc: { label: "oldest first", cmp: (a, b) => a.entry_ts - b.entry_ts },
  pnl_desc: { label: "best P&L first", cmp: (a, b) => b.net_pnl - a.net_pnl },
  pnl_asc: { label: "worst P&L first", cmp: (a, b) => a.net_pnl - b.net_pnl },
  notional_desc: { label: "largest notional first", cmp: (a, b) => b.avg_notional - a.avg_notional },
};

function PositionsHistorySection({ positions, address }) {
  const [assetFilter, setAssetFilter] = useState("ALL");
  const [typeFilter, setTypeFilter] = useState("ALL");
  const [sideFilter, setSideFilter] = useState("ALL");
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [sort, setSort] = useState("date_desc");

  const filtered = positions.filter((p) => {
    if (assetFilter !== "ALL" && p.asset !== assetFilter) return false;
    if (typeFilter !== "ALL" && p.option_type !== typeFilter) return false;
    if (sideFilter !== "ALL" && p.entry_side !== sideFilter) return false;
    if (statusFilter === "OPEN" && p.is_resolved) return false;
    if (statusFilter === "RESOLVED" && !p.is_resolved) return false;
    return true;
  });
  const sorted = [...filtered].sort(HISTORY_SORTS[sort].cmp);

  const selectStyle = { fontFamily: mono, fontSize: 10.5, padding: "5px 8px", background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <SectionLabel>Complete trade history ({sorted.length} of {positions.length}) — click a row to expand every fill</SectionLabel>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
          <select value={assetFilter} onChange={(e) => setAssetFilter(e.target.value)} style={selectStyle}>
            <option value="ALL">all assets</option>
            <option value="BTC">BTC</option>
            <option value="ETH">ETH</option>
          </select>
          <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} style={selectStyle}>
            <option value="ALL">calls + puts</option>
            <option value="call">calls only</option>
            <option value="put">puts only</option>
          </select>
          <select value={sideFilter} onChange={(e) => setSideFilter(e.target.value)} style={selectStyle}>
            <option value="ALL">buy + sell</option>
            <option value="buy">entered via buy</option>
            <option value="sell">entered via sell</option>
          </select>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} style={selectStyle}>
            <option value="ALL">open + resolved</option>
            <option value="OPEN">open only</option>
            <option value="RESOLVED">resolved only</option>
          </select>
          <select value={sort} onChange={(e) => setSort(e.target.value)} style={selectStyle}>
            {Object.entries(HISTORY_SORTS).map(([key, s]) => (
              <option key={key} value={key}>{s.label}</option>
            ))}
          </select>
        </div>
      </div>
      <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden", maxHeight: 560, overflowY: "auto" }}>
        {sorted.map((p) => <ExpandablePosition key={p.instrument} p={p} address={address} />)}
        {sorted.length === 0 && <Empty>no positions match these filters</Empty>}
      </div>
    </div>
  );
}

function PortfolioSection({ address }) {
  const [portfolio, setPortfolio] = useState(null);

  useEffect(() => {
    setPortfolio(null);
    apiGet(`/api/wallets/${address}/portfolio`).then(setPortfolio);
  }, [address]);

  if (!portfolio) return <Empty>loading portfolio…</Empty>;

  const sideStats = portfolio.by_entry_side;

  return (
    <div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <StatCard label="Positions" value={portfolio.position_count} />
        <StatCard label="Win rate" value={portfolio.win_rate != null ? `${portfolio.win_rate}%` : "—"} accent={portfolio.win_rate >= 50 ? TOKENS.current : TOKENS.bad} />
        <StatCard label="Wins / Losses" value={`${portfolio.win_count} / ${portfolio.loss_count}`} />
        <StatCard label="Gross realized P&L" value={fmtUsd(portfolio.total_realized_pnl)} accent={portfolio.total_realized_pnl >= 0 ? TOKENS.current : TOKENS.bad} />
        <StatCard label="Fees paid" value={fmtUsd(portfolio.total_fees_paid)} />
        <StatCard label="Net of fees" value={fmtUsd(portfolio.net_of_fees)} accent={portfolio.net_of_fees >= 0 ? TOKENS.current : TOKENS.bad} />
      </div>
      <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 8 }}>
        {portfolio.open_or_flat_count} of {portfolio.position_count} positions still open, not yet expired (excluded from win rate) ·
        avg win <PnlSign value={portfolio.avg_win ?? 0} /> · avg loss <PnlSign value={portfolio.avg_loss ?? 0} />
        {portfolio.estimated_pnl_count > 0 && (
          <> · <span style={{ color: TOKENS.flare }}>*</span> {portfolio.estimated_pnl_count} expired position{portfolio.estimated_pnl_count === 1 ? "" : "s"} had no closing trade -- P&L estimated from intrinsic value at expiry, not a real settlement figure</>
        )}
      </div>

      <div style={{ marginTop: 16 }}>
        <SectionLabel>Value over time</SectionLabel>
        <ValueTimelineChart address={address} />
      </div>

      {sideStats && (sideStats.buy.resolved > 0 || sideStats.sell.resolved > 0) && (
        <div style={{ marginTop: 16 }}>
          <SectionLabel>Success by entry side (no genuine spot buy/sell exists on-chain for these wallets -- see note below)</SectionLabel>
          <div style={{ display: "flex", gap: 10 }}>
            <div style={{ flex: 1, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "10px 14px" }}>
              <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase" }}>opened by buying ({sideStats.buy.resolved} resolved)</div>
              <div style={{ fontFamily: mono, fontSize: 16, marginTop: 4 }}>{sideStats.buy.win_rate != null ? `${sideStats.buy.win_rate}% win rate` : "—"} · <PnlSign value={sideStats.buy.net_pnl} /></div>
            </div>
            <div style={{ flex: 1, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "10px 14px" }}>
              <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase" }}>opened by selling/writing ({sideStats.sell.resolved} resolved)</div>
              <div style={{ fontFamily: mono, fontSize: 16, marginTop: 4 }}>{sideStats.sell.win_rate != null ? `${sideStats.sell.win_rate}% win rate` : "—"} · <PnlSign value={sideStats.sell.net_pnl} /></div>
            </div>
          </div>
          <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 6 }}>
            Checked this and two other top wallets directly on Derive Chain's explorer: no DEX/spot swap activity exists for any of
            them -- everything off-options is bridging collateral or DRV staking. So "buy/sell success" is read here as which side
            opened the position (a position mixes a buy and sell leg to open/close, so leg-level win rate isn't meaningful).
          </div>
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <PositionsHistorySection positions={portfolio.positions} address={address} />
      </div>
    </div>
  );
}

function OnchainActivitySection({ address }) {
  const [activity, setActivity] = useState(null);

  useEffect(() => {
    setActivity(null);
    apiGet(`/api/wallets/${address}/onchain-activity`).then(setActivity);
  }, [address]);

  if (!activity) return <Empty>loading on-chain activity… (first pull can take ~15s, paginating Derive Chain's explorer)</Empty>;

  const bridgeIn = activity.bridge_activity.filter((a) => a.activity_type === "deposit");
  const bridgeOut = activity.bridge_activity.filter((a) => a.activity_type === "withdrawal");

  return (
    <div>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <StatCard label="Bridge deposits" value={bridgeIn.length} accent={TOKENS.current} />
        <StatCard label="Bridge withdrawals" value={bridgeOut.length} accent={TOKENS.flare} />
        <StatCard label="Staking events" value={activity.staking_activity.length} accent={TOKENS.undertow} />
        <StatCard label="Trade-settlement txs" value={activity.internal_trade_settlement_count} />
      </div>
      <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginTop: 8 }}>
        {activity.internal_trade_settlement_count} of {activity.total_count} on-chain events are ERC-4337 trade-settlement mechanics
        (margin/premium movement already reflected in the options data above) -- not shown below to avoid double-counting. No
        spot/DEX swap activity found for this wallet either -- everything off-options is bridging or staking.
      </div>

      <div style={{ marginTop: 14 }}>
        <SectionLabel>Bridge deposits / withdrawals ({activity.bridge_activity.length})</SectionLabel>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden", maxHeight: 260, overflowY: "auto" }}>
          {activity.bridge_activity.map((a) => (
            <div key={a.id} style={{ display: "grid", gridTemplateColumns: "150px 90px 1fr", padding: "8px 14px", fontFamily: sans, fontSize: 12, color: TOKENS.paper, borderTop: `1px solid ${TOKENS.hair}` }}>
              <span style={{ fontFamily: mono, color: TOKENS.dim }}>{a.date}</span>
              <span style={{ fontFamily: mono, color: ACTIVITY_STYLE[a.activity_type]?.color, textTransform: "uppercase", fontSize: 10.5 }}>
                {a.direction === "in" ? "↓" : "↑"} {ACTIVITY_STYLE[a.activity_type]?.label}
              </span>
              <span>
                {a.amount.toLocaleString(undefined, { maximumFractionDigits: 2 })} {a.asset}
                {a.counterparty_label && <span style={{ color: TOKENS.dim }}> · {a.counterparty_label}</span>}
              </span>
            </div>
          ))}
          {activity.bridge_activity.length === 0 && <Empty>none found</Empty>}
        </div>
      </div>
    </div>
  );
}

function TimelineCheck({ position, asset }) {
  const [open, setOpen] = useState(false);
  const [news, setNews] = useState(null);
  const [posts, setPosts] = useState(null);

  function toggle() {
    setOpen((o) => !o);
    if (!news && NEWS_LOOKUPS_ENABLED) {
      apiGet(`/api/news?asset=${asset}&date=${position.entry_date}&window_days=2`).then(setNews);
      const d = new Date(position.entry_ts);
      const start = new Date(d.getTime() - 2 * 86400000).toISOString().slice(0, 10);
      const end = new Date(d.getTime() + 2 * 86400000).toISOString().slice(0, 10);
      apiGet(`/api/trump-posts?start_date=${start}&end_date=${end}`).then(setPosts);
    }
  }

  return (
    <div style={{ borderTop: `1px solid ${TOKENS.hair}` }}>
      <button onClick={toggle} style={{ width: "100%", textAlign: "left", background: "transparent", border: "none", cursor: "pointer", padding: "10px 14px", display: "flex", justifyContent: "space-between", fontFamily: sans, fontSize: 12, color: TOKENS.paper }}>
        <span>
          <span style={{ fontFamily: mono, color: TOKENS.dim, marginRight: 6 }}>{open ? "▾" : "▸"}</span>
          {position.entry_date} · {position.asset} {position.option_type.toUpperCase()} {position.strike.toLocaleString()}
        </span>
        <PnlSign value={position.net_pnl ?? 0} />
      </button>
      {open && (
        <div style={{ padding: "4px 14px 14px 30px", background: TOKENS.panel2 }}>
          {!NEWS_LOOKUPS_ENABLED ? (
            <Empty>news / Trump-post lookups turned off for now (slow, no caching yet) -- see featureFlags.js</Empty>
          ) : (
            <>
              <div style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, textTransform: "uppercase", marginTop: 8 }}>news, ±2 days</div>
              {!news ? <Empty>loading…</Empty> : news.items.slice(0, 4).map((it, i) => (
                <a key={i} href={it.link} target="_blank" rel="noreferrer" style={{ display: "block", fontFamily: sans, fontSize: 11.5, color: TOKENS.paper, padding: "5px 0", textDecoration: "none" }}>
                  {it.title} <span style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.dim }}>{it.source}</span>
                </a>
              ))}
              <div style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, textTransform: "uppercase", marginTop: 8 }}>trump posts, ±2 days (first load ~30s)</div>
              {!posts ? <Empty>loading…</Empty> : posts.items.filter((p) => p.is_crypto_related).slice(0, 4).map((p, i) => (
                <div key={i} style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.paper, padding: "5px 0" }}>
                  <span style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.flare }}>{fmtDateTime(new Date(p.date).getTime())}</span> {p.text.slice(0, 180)}
                </div>
              ))}
              {posts && posts.items.filter((p) => p.is_crypto_related).length === 0 && <Empty>no crypto-related posts in window</Empty>}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function TimelineSection({ address }) {
  const [portfolio, setPortfolio] = useState(null);

  useEffect(() => {
    setPortfolio(null);
    apiGet(`/api/wallets/${address}/portfolio`).then(setPortfolio);
  }, [address]);

  if (!portfolio) return <Empty>loading…</Empty>;

  const candidates = [...portfolio.best_positions.slice(0, 2), ...portfolio.worst_positions.slice(0, 2)];

  return (
    <div>
      <div style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.dim, marginBottom: 8 }}>
        Checking news/Trump posts around this wallet's biggest wins and losses -- click a row to look. Manual review only, same as elsewhere: this isn't an automated matcher.
      </div>
      <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
        {candidates.map((p, i) => <TimelineCheck key={i} position={p} asset={p.asset} />)}
        {candidates.length === 0 && <Empty>no resolved positions to check yet</Empty>}
      </div>
    </div>
  );
}

function MarketMakerBadge({ style }) {
  return (
    <span
      title="Many small fills per position or very high daily trade frequency -- pattern matches automated market-making, not a discretionary bet. See scripts/build_leaderboard.py for the heuristic."
      style={{ fontFamily: mono, fontSize: 9, color: TOKENS.flare, border: `1px solid ${TOKENS.flare}`, padding: "1px 5px", borderRadius: 3, fontWeight: 600, ...style }}
    >
      possible MM
    </span>
  );
}

const PAGE_SIZE = 50;

function FilterRow({ children }) {
  return <div style={{ display: "flex", gap: 6, marginTop: 8 }}>{children}</div>;
}

function SearchSidebar({ addressParam, onSelect }) {
  const [q, setQ] = useState("");
  const [assetFilter, setAssetFilter] = useState("ALL");
  const [sort, setSort] = useState("notional");
  const [minTrades, setMinTrades] = useState("");
  const [minWinRate, setMinWinRate] = useState("");
  const [excludeBots, setExcludeBots] = useState(true);
  const [featuredOnly, setFeaturedOnly] = useState(false);
  const [offset, setOffset] = useState(0);
  const [results, setResults] = useState(null);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    setOffset(0);
  }, [q, assetFilter, sort, minTrades, minWinRate, excludeBots, featuredOnly]);

  useEffect(() => {
    // debounced -- q changes on every keystroke, the rest change rarely,
    // but one shared debounce is simpler than splitting the effect and the
    // extra ~250ms is imperceptible for a filter toggle.
    const handle = setTimeout(() => {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (assetFilter !== "ALL") params.set("asset", assetFilter);
      params.set("sort", sort);
      if (minTrades) params.set("min_trades", minTrades);
      if (minWinRate) params.set("min_win_rate", minWinRate);
      params.set("exclude_bots", excludeBots);
      if (featuredOnly) params.set("featured_only", "true");
      params.set("limit", PAGE_SIZE);
      params.set("offset", offset);
      setResults(null);
      apiGet(`/api/wallets/search?${params.toString()}`).then((d) => {
        setResults(d.results);
        setTotal(d.total);
      });
    }, 250);
    return () => clearTimeout(handle);
  }, [q, assetFilter, sort, minTrades, minWinRate, excludeBots, featuredOnly, offset]);

  const inputStyle = { fontFamily: mono, fontSize: 11.5, padding: "6px 8px", background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, width: "100%" };
  const toggleBtnStyle = (active) => ({ fontFamily: mono, fontSize: 10.5, padding: "5px 8px", background: active ? TOKENS.panel2 : "transparent", color: active ? TOKENS.paper : TOKENS.dim, border: `1px solid ${TOKENS.hair}`, borderRadius: 4, cursor: "pointer", flex: 1 });

  return (
    <div style={{ borderRight: `1px solid ${TOKENS.hair}`, display: "flex", flexDirection: "column", maxHeight: "calc(100vh - 60px)" }}>
      <div style={{ padding: "12px 16px", borderBottom: `1px solid ${TOKENS.hair}` }}>
        <input
          type="text"
          placeholder="search address or alias…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={inputStyle}
        />
        <FilterRow>
          {["ALL", "BTC", "ETH"].map((a) => (
            <button key={a} onClick={() => setAssetFilter(a)} style={toggleBtnStyle(assetFilter === a)}>{a}</button>
          ))}
        </FilterRow>
        <FilterRow>
          <select value={sort} onChange={(e) => setSort(e.target.value)} style={{ ...inputStyle, flex: 1 }}>
            <option value="notional">sort: avg notional</option>
            <option value="win_rate">sort: win rate</option>
            <option value="pnl">sort: net P&L</option>
            <option value="trades">sort: trade count</option>
          </select>
        </FilterRow>
        <FilterRow>
          <input type="number" placeholder="min trades" value={minTrades} onChange={(e) => setMinTrades(e.target.value)} style={inputStyle} />
          <input type="number" placeholder="min win %" value={minWinRate} onChange={(e) => setMinWinRate(e.target.value)} style={inputStyle} />
        </FilterRow>
        <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 8, fontFamily: sans, fontSize: 11, color: TOKENS.dim, cursor: "pointer" }}>
          <input type="checkbox" checked={excludeBots} onChange={(e) => setExcludeBots(e.target.checked)} />
          exclude possible market makers
        </label>
        <label style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 4, fontFamily: sans, fontSize: 11, color: TOKENS.dim, cursor: "pointer" }}>
          <input type="checkbox" checked={featuredOnly} onChange={(e) => setFeaturedOnly(e.target.checked)} />
          featured only
        </label>
        <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, marginTop: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
          {total.toLocaleString()} wallet{total === 1 ? "" : "s"} match
        </div>
      </div>

      <div style={{ flex: 1, overflowY: "auto" }}>
        {!results && <Empty>loading…</Empty>}
        {results?.length === 0 && <Empty>no wallets match these filters</Empty>}
        {results?.map((w) => (
          <button
            key={w.address}
            onClick={() => onSelect(w.address)}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              background: w.address === addressParam ? TOKENS.panel2 : "transparent",
              border: "none",
              borderLeft: `2px solid ${w.address === addressParam ? TOKENS.undertow : "transparent"}`,
              borderBottom: `1px solid ${TOKENS.hair}`,
              padding: "10px 16px",
              cursor: "pointer",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
              {w.is_featured && <span style={{ fontFamily: mono, fontSize: 9, color: TOKENS.ink, background: TOKENS.flare, padding: "1px 5px", borderRadius: 3, fontWeight: 600 }}>★ FEATURED</span>}
              <div style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: TOKENS.undertow }}>{w.alias}</div>
              {w.is_likely_bot && <MarketMakerBadge />}
            </div>
            <div style={{ fontFamily: sans, fontSize: 11, color: TOKENS.dim, marginTop: 4 }}>
              {w.positions ?? 0} trades · {w.win_rate != null ? `${w.win_rate}% win` : "no resolved yet"}
            </div>
            <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.flare, marginTop: 2 }}>
              {w.avg_notional_usd != null ? `avg ${fmtUsd(w.avg_notional_usd)}` : "—"}
            </div>
          </button>
        ))}
      </div>

      {total > PAGE_SIZE && (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 16px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: mono, fontSize: 10.5, color: TOKENS.dim }}>
          <button disabled={offset === 0} onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))} style={{ background: "transparent", border: "none", color: offset === 0 ? TOKENS.hair : TOKENS.paper, cursor: offset === 0 ? "default" : "pointer" }}>
            ← prev
          </button>
          <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total.toLocaleString()}</span>
          <button disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset((o) => o + PAGE_SIZE)} style={{ background: "transparent", border: "none", color: offset + PAGE_SIZE >= total ? TOKENS.hair : TOKENS.paper, cursor: offset + PAGE_SIZE >= total ? "default" : "pointer" }}>
            next →
          </button>
        </div>
      )}
    </div>
  );
}

export function WalletsView() {
  const { address: addressParam } = useParams();
  const navigate = useNavigate();
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    if (!addressParam) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    apiGet(`/api/wallets/${addressParam}`).then((d) => {
      if (!cancelled) setDetail(d);
    });
    return () => {
      cancelled = true;
    };
  }, [addressParam]);

  const flaggedCount = detail?.events?.length ?? 0;
  const flaggedNotional = detail?.events ? detail.events.reduce((sum, e) => sum + (e.notional_usd || 0), 0) : 0;

  return (
    <div style={{ display: "grid", gridTemplateColumns: "320px 1fr" }}>
      <SearchSidebar addressParam={addressParam} onSelect={(address) => navigate(`/wallets/${address}`)} />

      <div style={{ padding: "20px 24px" }}>
        {!addressParam ? (
          <Empty>search for a wallet, or pick one from the list, to see its full deep-dive.</Empty>
        ) : !detail ? (
          <Empty>loading…</Empty>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {detail.featured && <span style={{ fontFamily: mono, fontSize: 10, color: TOKENS.ink, background: TOKENS.flare, padding: "2px 6px", borderRadius: 3, fontWeight: 600 }}>★ FEATURED</span>}
              <div style={{ fontFamily: mono, fontSize: 20, fontWeight: 700, color: TOKENS.undertow }}>{detail.alias}</div>
              {detail.is_likely_market_maker && <MarketMakerBadge style={{ fontSize: 10, padding: "2px 6px" }} />}
            </div>
            {detail.is_likely_market_maker && (
              <div style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.dim, marginTop: 6 }}>
                Flagged as a possible market maker: {detail.legs_per_position} legs/position, {detail.legs_per_day} legs/day
                (thresholds are 8 and 15 -- see {"scripts/build_leaderboard.py"}). Below numbers still reflect all of this
                wallet's real trades, just worth reading with that in mind.
              </div>
            )}
            {detail.featured_note && (
              <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.paper, marginTop: 6, background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "8px 12px" }}>
                {detail.featured_note}
              </div>
            )}
            <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim, wordBreak: "break-all", marginTop: 8 }}>{detail.address}</div>
            <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, marginTop: 4 }}>
              {detail.chain} · {detail.label || "unlabeled"} · first options trade {detail.first_seen}
            </div>

            <div style={{ display: "flex", gap: 10, marginTop: 18, flexWrap: "wrap" }}>
              <StatCard label="Flagged events" value={flaggedCount} />
              <StatCard label="Flagged notional" value={fmtUsd(flaggedNotional)} accent={TOKENS.flare} />
              <StatCard label="Total trades" value={detail.total_event_count} accent={TOKENS.current} />
            </div>

            <div style={{ marginTop: 24 }}>
              <SectionLabel>Ratings</SectionLabel>
              <RatingsSection address={detail.address} />
            </div>

            <div style={{ marginTop: 24 }}>
              <SectionLabel>Portfolio analysis (all trades, not just flagged)</SectionLabel>
              <PortfolioSection address={detail.address} />
            </div>

            <div style={{ marginTop: 24 }}>
              <SectionLabel>On-chain activity (Derive Chain, beyond options trades)</SectionLabel>
              <OnchainActivitySection address={detail.address} />
            </div>

            <div style={{ marginTop: 24 }}>
              <SectionLabel>Timeline alignment</SectionLabel>
              <TimelineSection address={detail.address} />
            </div>

            <div style={{ marginTop: 24 }}>
              <SectionLabel>Flagged transaction history</SectionLabel>
              <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
                <div style={{ display: "grid", gridTemplateColumns: "160px 90px 1fr 100px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                  <span>date</span>
                  <span>asset</span>
                  <span>detail</span>
                  <span style={{ textAlign: "right" }}>notional</span>
                </div>
                <div style={{ maxHeight: 480, overflowY: "auto" }}>
                {detail.events.map((a) => (
                  <div
                    key={a.id}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "160px 90px 1fr 100px",
                      padding: "10px 14px",
                      borderTop: `1px solid ${TOKENS.hair}`,
                      fontFamily: sans,
                      fontSize: 12,
                      color: TOKENS.paper,
                      alignItems: "center",
                    }}
                  >
                    <span style={{ fontFamily: mono, color: TOKENS.dim }}>{a.date}</span>
                    <span style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.flare }}>{a.asset}</span>
                    <span>
                      {a.option_type.toUpperCase()} {a.strike.toLocaleString()} · {a.side} · exp {a.expiry_date}
                      <span style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.dim, marginLeft: 8 }}>
                        {a.flag_reasons.map((r) => (r === "large_trade" ? "size" : "jump")).join("+")}
                      </span>
                    </span>
                    <span style={{ textAlign: "right", fontFamily: mono }}>{fmtUsd(a.notional_usd)}</span>
                  </div>
                ))}
                {detail.events.length === 0 && <Empty>no flagged trades</Empty>}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
