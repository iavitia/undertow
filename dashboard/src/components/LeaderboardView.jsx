import { useEffect, useState } from "react";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet } from "../api";
import { fmtUsd } from "../format";
import { Empty, SectionLabel } from "./Shared";

function PnlSign({ value }) {
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{fmtUsd(value)}</span>;
}

function PctSign({ value }) {
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{value}%</span>;
}

function Toggle({ options, value, onChange }) {
  return (
    <div style={{ display: "flex", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, overflow: "hidden" }}>
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          style={{ fontFamily: mono, fontSize: 11, padding: "6px 12px", background: value === o.value ? TOKENS.panel2 : "transparent", color: value === o.value ? TOKENS.paper : TOKENS.dim, border: "none", cursor: "pointer" }}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function OwnPerformancePanel({ onDeepDive }) {
  const [sort, setSort] = useState("pnl");
  const [minResolved, setMinResolved] = useState(5);
  const [includeBots, setIncludeBots] = useState(false);
  const [rows, setRows] = useState(null);
  const [diving, setDiving] = useState(null);

  useEffect(() => {
    setRows(null);
    apiGet(`/api/leaderboard?sort=${sort}&min_resolved=${minResolved}&include_bots=${includeBots}&limit=30`).then(setRows);
  }, [sort, minResolved, includeBots]);

  async function handleDeepDive(address) {
    setDiving(address);
    await onDeepDive(address);
    setDiving(null);
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, maxWidth: 640 }}>
          Ranked by realized P&L across every options position ever traded (not just flagged ones), net of fees.
          For expired positions with no closing trade, P&L is estimated from intrinsic value at expiry (nearest
          available index price used as a stand-in for the real settlement price) rather than left at zero --
          see a wallet's portfolio for which of its positions are estimated this way. Automated market-makers
          (thousands of small fills, P&L that just scales with volume) are excluded by default -- toggle to
          include them.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Toggle
            options={[{ label: "top profit", value: "pnl" }, { label: "top win rate", value: "win_rate" }]}
            value={sort}
            onChange={setSort}
          />
          <Toggle
            options={[{ label: "min 5 trades", value: 5 }, { label: "min 20 trades", value: 20 }, { label: "min 50 trades", value: 50 }]}
            value={minResolved}
            onChange={setMinResolved}
          />
          <Toggle
            options={[{ label: "exclude bots", value: false }, { label: "include bots", value: true }]}
            value={includeBots}
            onChange={setIncludeBots}
          />
        </div>
      </div>

      <div style={{ marginTop: 20 }}>
        <SectionLabel>
          {sort === "pnl" ? "Ranked by net profit" : "Ranked by win rate"} · {minResolved}+ resolved positions
        </SectionLabel>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
          <div style={{ display: "grid", gridTemplateColumns: "160px 90px 90px 90px 1fr 100px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>wallet</span>
            <span>net P&L</span>
            <span>win rate</span>
            <span>resolved</span>
            <span>pattern</span>
            <span style={{ textAlign: "right" }}>action</span>
          </div>
          {!rows && <Empty>loading…</Empty>}
          {rows?.length === 0 && <Empty>no wallets meet this filter</Empty>}
          {rows?.map((w) => (
            <div key={w.address} style={{ display: "grid", gridTemplateColumns: "160px 90px 90px 90px 1fr 100px", padding: "10px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
              <span style={{ fontFamily: mono, color: TOKENS.undertow }}>{w.alias}</span>
              <span><PnlSign value={w.net_of_fees} /></span>
              <span style={{ color: w.win_rate >= 50 ? TOKENS.current : TOKENS.bad }}>{w.win_rate}%</span>
              <span style={{ color: TOKENS.dim }}>{w.resolved}</span>
              <span style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim }}>
                {w.call_pct}% call · {w.buy_pct}% buy · {w.btc_pct}% btc
                {w.is_likely_bot && <span style={{ color: TOKENS.flare, marginLeft: 6 }}>· bot-like</span>}
              </span>
              <span style={{ textAlign: "right" }}>
                <button
                  onClick={() => handleDeepDive(w.address)}
                  disabled={diving === w.address}
                  style={{ fontFamily: mono, fontSize: 10.5, padding: "5px 10px", background: "transparent", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: "pointer" }}
                >
                  {diving === w.address ? "opening…" : "deep dive →"}
                </button>
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

const BACKTEST_SORT_LABELS = {
  wilson: "Wilson-adjusted win rate (recommended)",
  win_rate: "raw win rate",
  median_return: "median return per trade",
  avg_return: "average return per trade",
  total_return: "total return (sum across trades)",
};

function CopyBacktestPanel({ onDeepDive }) {
  const [sort, setSort] = useState("wilson");
  const [minPositions, setMinPositions] = useState(20);
  const [includeBots, setIncludeBots] = useState(false);
  const [rows, setRows] = useState(null);
  const [diving, setDiving] = useState(null);

  useEffect(() => {
    setRows(null);
    apiGet(`/api/copy-backtest?sort=${sort}&min_positions=${minPositions}&include_bots=${includeBots}&limit=30`).then(setRows);
  }, [sort, minPositions, includeBots]);

  async function handleDeepDive(address) {
    setDiving(address);
    await onDeepDive(address);
    setDiving(null);
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, maxWidth: 680 }}>
          For every position a wallet opened, simulates a copier entering at the next available market print from
          another wallet (not this wallet's own fill) instead -- a different question than "was this wallet
          profitable," since a wallet's own edge doesn't automatically transfer to someone copying it late. Positions
          with no other-wallet print within 24h are excluded as not realistically copyable, not counted as losses.
          <br /><br />
          <span style={{ color: TOKENS.flare }}>Win rate alone is misleading here</span> -- of wallets with a 70%+
          win rate and 20+ sampled positions, about 1 in 4 are still net-negative overall: naked-premium sellers with
          a high hit rate and one catastrophic print (same shape as brass-basin-71). That's why the default sort is
          the Wilson lower bound, not raw win rate, and why return columns sit right next to it. Returns are
          %-of-premium and can blow up on cheap deep-OTM options -- median is the more robust read than average.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Toggle
            options={[
              { label: "wilson win rate", value: "wilson" },
              { label: "raw win rate", value: "win_rate" },
              { label: "median return", value: "median_return" },
              { label: "total return", value: "total_return" },
            ]}
            value={sort}
            onChange={setSort}
          />
          <Toggle
            options={[{ label: "min 10", value: 10 }, { label: "min 20", value: 20 }, { label: "min 50", value: 50 }]}
            value={minPositions}
            onChange={setMinPositions}
          />
          <Toggle
            options={[{ label: "exclude bots", value: false }, { label: "include bots", value: true }]}
            value={includeBots}
            onChange={setIncludeBots}
          />
        </div>
      </div>

      <div style={{ marginTop: 20 }}>
        <SectionLabel>
          Ranked by {BACKTEST_SORT_LABELS[sort]} · {minPositions}+ copy-eligible positions
        </SectionLabel>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
          <div style={{ display: "grid", gridTemplateColumns: "150px 70px 90px 90px 90px 90px 1fr 90px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>wallet</span>
            <span>n</span>
            <span>win rate</span>
            <span>wilson low</span>
            <span>median ret</span>
            <span>avg ret</span>
            <span>total ret</span>
            <span style={{ textAlign: "right" }}>action</span>
          </div>
          {!rows && <Empty>loading…</Empty>}
          {rows?.length === 0 && <Empty>no wallets meet this filter</Empty>}
          {rows?.map((w) => (
            <div key={w.address} style={{ display: "grid", gridTemplateColumns: "150px 70px 90px 90px 90px 90px 1fr 90px", padding: "10px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
              <span style={{ fontFamily: mono, color: TOKENS.undertow }}>
                {w.alias}
                {w.is_likely_bot && <span style={{ color: TOKENS.flare, marginLeft: 4, fontSize: 9.5 }}>· bot-like</span>}
              </span>
              <span style={{ color: TOKENS.dim }}>{w.copy_positions}</span>
              <span style={{ color: w.copy_win_rate >= 50 ? TOKENS.current : TOKENS.bad }}>{w.copy_win_rate}%</span>
              <span style={{ fontFamily: mono, fontSize: 11 }}>{w.copy_win_rate_wilson_low}%</span>
              <span><PctSign value={w.copy_median_return_pct} /></span>
              <span><PctSign value={w.copy_avg_return_pct} /></span>
              <span><PctSign value={w.copy_total_return_pct} /></span>
              <span style={{ textAlign: "right" }}>
                <button
                  onClick={() => handleDeepDive(w.address)}
                  disabled={diving === w.address}
                  style={{ fontFamily: mono, fontSize: 10.5, padding: "5px 10px", background: "transparent", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: "pointer" }}
                >
                  {diving === w.address ? "opening…" : "deep dive →"}
                </button>
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
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

function CopyCandidatesPanel({ onDeepDive }) {
  const [tier, setTier] = useState("");
  const [sort, setSort] = useState("forward_wilson_low");
  const [rows, setRows] = useState(null);
  const [diving, setDiving] = useState(null);

  useEffect(() => {
    setRows(null);
    apiGet(`/api/wallet-qualification?${tier ? `tier=${tier}&` : ""}sort=${sort}&limit=60`).then(setRows);
  }, [tier, sort]);

  async function handleDeepDive(address) {
    setDiving(address);
    await onDeepDive(address);
    setDiving(null);
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
        <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, maxWidth: 680 }}>
          Each wallet is walked chronologically through its own resolved copy-trade history, using only data
          available at each point (no look-ahead). The first point where the cumulative Wilson lower bound
          clears 60% <b>and</b> cumulative return is positive is the wallet's <em>qualification point</em> --
          everything after that is a genuine forward test, not a second look at the same data. A wallet with
          too little history is <span style={{ color: TIER_COLOR.insufficient_data }}>insufficient data</span>;
          one that never clears the bar is <span style={{ color: TIER_COLOR.does_not_qualify }}>does not qualify</span> (see
          brass-basin-71: excellent win rate, never both Wilson≥60% <b>and</b> net positive at once); one that
          qualified but whose forward record didn't hold up is <span style={{ color: TIER_COLOR.qualified_but_faded }}>qualified, faded</span>.
        </div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Toggle
            options={[
              { label: "forward wilson", value: "forward_wilson_low" },
              { label: "forward return", value: "forward_total_return_pct" },
              { label: "total n", value: "total_n" },
              { label: "risk discipline", value: "risk_discipline_score" },
            ]}
            value={sort}
            onChange={setSort}
          />
          <Toggle
            options={[
              { label: "all tiers", value: "" },
              { label: "validated+", value: "validated" },
              { label: "high confidence", value: "high_confidence" },
              { label: "provisional", value: "provisional" },
            ]}
            value={tier}
            onChange={setTier}
          />
        </div>
      </div>

      <div style={{ marginTop: 20 }}>
        <SectionLabel>
          {tier ? TIER_LABEL[tier] : "All qualified/qualifying wallets"} · ranked by {sort === "forward_wilson_low" ? "forward Wilson lower bound" : sort === "forward_total_return_pct" ? "forward total return" : sort === "total_n" ? "total resolved positions" : "risk discipline score"}
        </SectionLabel>
        <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
          <div style={{ display: "grid", gridTemplateColumns: "140px 120px 70px 110px 110px 90px 1fr 90px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            <span>wallet</span>
            <span>tier</span>
            <span>qual @n</span>
            <span>total n</span>
            <span>forward</span>
            <span>fwd ret</span>
            <span>risk / rfq / cv</span>
            <span style={{ textAlign: "right" }}>action</span>
          </div>
          {!rows && <Empty>loading…</Empty>}
          {rows?.length === 0 && <Empty>no wallets meet this filter</Empty>}
          {rows?.map((w) => (
            <div key={w.address} style={{ display: "grid", gridTemplateColumns: "140px 120px 70px 110px 110px 90px 1fr 90px", padding: "10px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
              <span style={{ fontFamily: mono, color: TOKENS.undertow }}>
                {w.alias}
                {w.is_likely_bot && <span style={{ color: TOKENS.flare, marginLeft: 4, fontSize: 9.5 }}>· bot</span>}
              </span>
              <span style={{ color: TIER_COLOR[w.tier], fontFamily: mono, fontSize: 10.5 }}>{TIER_LABEL[w.tier] ?? w.tier}</span>
              <span style={{ color: TOKENS.dim, fontFamily: mono, fontSize: 10.5 }}>{w.qualified_at_n ?? "—"}</span>
              <span style={{ color: TOKENS.dim }}>{w.total_n}</span>
              <span style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim }}>
                {w.forward_n ? `n=${w.forward_n} · ${w.forward_win_rate}% win · wilson ${w.forward_wilson_low}%` : "—"}
              </span>
              <span>{w.forward_total_return_pct != null ? <PctSign value={w.forward_total_return_pct} /> : "—"}</span>
              <span style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.dim }}>
                {w.risk_discipline_score ?? "—"} · rfq {w.rfq_pct ?? "—"}% · cv {w.sizing_cv ?? "—"}
              </span>
              <span style={{ textAlign: "right" }}>
                <button
                  onClick={() => handleDeepDive(w.address)}
                  disabled={diving === w.address}
                  style={{ fontFamily: mono, fontSize: 10.5, padding: "5px 10px", background: "transparent", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, color: TOKENS.paper, cursor: "pointer" }}
                >
                  {diving === w.address ? "opening…" : "deep dive →"}
                </button>
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function LeaderboardView({ onDeepDive }) {
  const [mode, setMode] = useState("own");

  return (
    <div style={{ padding: "20px 24px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
        <div style={{ fontFamily: mono, fontSize: 20, fontWeight: 700 }}>
          {mode === "own" ? "Top winning wallets" : mode === "backtest" ? "Copy-trade backtest" : "Validated copy candidates"}
        </div>
        <Toggle
          options={[
            { label: "wallet performance", value: "own" },
            { label: "copy-trade backtest", value: "backtest" },
            { label: "validated candidates", value: "candidates" },
          ]}
          value={mode}
          onChange={setMode}
        />
      </div>
      <div style={{ marginTop: 12 }}>
        {mode === "own" && <OwnPerformancePanel onDeepDive={onDeepDive} />}
        {mode === "backtest" && <CopyBacktestPanel onDeepDive={onDeepDive} />}
        {mode === "candidates" && <CopyCandidatesPanel onDeepDive={onDeepDive} />}
      </div>
    </div>
  );
}
