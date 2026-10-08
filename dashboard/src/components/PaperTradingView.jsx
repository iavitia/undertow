import { useEffect, useState } from "react";
import { TOKENS, mono, sans } from "../tokens";
import { apiGet } from "../api";
import { Empty, SectionLabel, StatCard } from "./Shared";

function PctSign({ value }) {
  if (value == null) return <span style={{ color: TOKENS.dim }}>—</span>;
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : ""}{value}%</span>;
}

function UsdSign({ value }) {
  if (value == null) return <span style={{ color: TOKENS.dim }}>—</span>;
  const color = value > 0 ? TOKENS.current : value < 0 ? TOKENS.bad : TOKENS.dim;
  return <span style={{ color, fontFamily: mono }}>{value >= 0 ? "+" : "-"}${Math.abs(value).toFixed(2)}</span>;
}

const STATUS_STYLE = {
  open: { label: "open", color: TOKENS.undertow },
  pending_entry: { label: "pricing…", color: TOKENS.flare },
  resolved: { label: "resolved", color: TOKENS.dim },
  stopped_out: { label: "stopped out (retired)", color: TOKENS.dim },
  skipped_stale: { label: "skipped (stale)", color: TOKENS.bad },
  skipped_naked: { label: "skipped (naked, retired)", color: TOKENS.dim },
  skipped_weak_edge: { label: "skipped (weak edge)", color: TOKENS.flare },
};

function EdgeBadge({ t }) {
  if (t.edge_bucket_n == null) return <span style={{ color: TOKENS.dim, fontFamily: mono, fontSize: 10.5 }}>—</span>;
  const color = t.edge_bucket_wilson_low >= 65 ? TOKENS.current : t.edge_bucket_wilson_low >= 50 ? TOKENS.flare : TOKENS.bad;
  return (
    <span
      title={`This wallet's historical record specifically in ${t.asset} ${t.option_type} entered-via-${t.side} trades (not their overall record) -- recorded for later analysis, not used to filter this trade.`}
      style={{ fontFamily: mono, fontSize: 10.5, color }}
    >
      n={t.edge_bucket_n} · {t.edge_bucket_wilson_low}%
    </span>
  );
}

function WalletOutcomeBadge({ t }) {
  if (t.wallet_return_pct == null) {
    return <span style={{ color: TOKENS.dim, fontFamily: mono, fontSize: 10.5 }} title="The source wallet's own position hasn't closed or expired yet -- rechecked every tick.">—</span>;
  }
  const estTag = t.wallet_pnl_is_estimated ? " (est. at expiry)" : " (their own close)";
  return (
    <span
      title={`What the source wallet's own position actually did, no stop-loss, no protection${estTag}. Their exit: ${t.wallet_exit_price != null ? t.wallet_exit_price.toFixed(2) : "—"}.`}
      style={{ fontFamily: mono, fontSize: 10.5 }}
    >
      <PctSign value={t.wallet_return_pct} />
    </span>
  );
}

function TradeRow({ t }) {
  const status = STATUS_STYLE[t.fill_status] || { label: t.fill_status, color: TOKENS.dim };
  return (
    <div style={{ display: "grid", gridTemplateColumns: "130px 1fr 90px 90px 90px 90px 80px 80px 100px", padding: "9px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12, color: TOKENS.paper, alignItems: "center" }}>
      <span style={{ fontFamily: mono, color: TOKENS.undertow }}>{t.alias || t.source_wallet_address.slice(0, 10)}</span>
      <span>
        {t.asset} {t.option_type.toUpperCase()} {t.strike.toLocaleString()} · exp {t.expiry_date} · entered via {t.side}
      </span>
      <span style={{ fontFamily: mono, fontSize: 10.5, color: status.color, textTransform: "uppercase" }}>{status.label}</span>
      <span style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim }}>{t.entry_price != null ? t.entry_price.toFixed(2) : "—"}</span>
      <span style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim }}>{t.exit_price != null ? t.exit_price.toFixed(2) : "—"}</span>
      <EdgeBadge t={t} />
      <span>
        <PctSign value={t.return_pct} />
        {t.loss_capped && (
          <span title="This naked short's real settlement math was worse than -100% -- clamped to a -100% floor instead, the same max-loss shape as a bought option." style={{ fontFamily: mono, fontSize: 9.5, color: TOKENS.flare, marginLeft: 4 }}>
            capped
          </span>
        )}
      </span>
      <WalletOutcomeBadge t={t} />
      <span style={{ textAlign: "right" }}><UsdSign value={t.hypothetical_income_usd} /></span>
    </div>
  );
}

function WatchlistPanel() {
  const [watchlist, setWatchlist] = useState(null);

  useEffect(() => {
    apiGet("/api/watchlist").then(setWatchlist);
  }, []);

  if (!watchlist) return <Empty>loading…</Empty>;
  const active = watchlist.filter((w) => w.is_active);

  return (
    <div>
      <SectionLabel>Watchlist · {active.length} active</SectionLabel>
      <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
        {active.map((w) => (
          <div key={w.address} style={{ padding: "10px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <span style={{ fontFamily: mono, color: TOKENS.undertow, fontWeight: 600 }}>{w.alias}</span>
              <span style={{ fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase" }}>{w.source}</span>
            </div>
            <div style={{ fontFamily: sans, fontSize: 11, color: TOKENS.dim, marginTop: 3 }}>{w.added_reason}</div>
          </div>
        ))}
        {active.length === 0 && <Empty>watchlist is empty -- run scripts/sync_watchlist.py</Empty>}
      </div>
    </div>
  );
}

export function PaperTradingView() {
  const [summary, setSummary] = useState(null);
  const [trades, setTrades] = useState(null);
  const [statusFilter, setStatusFilter] = useState(null);

  useEffect(() => {
    apiGet("/api/paper-trades/summary").then(setSummary);
  }, []);

  useEffect(() => {
    setTrades(null);
    const qs = statusFilter ? `?status=${statusFilter}` : "";
    apiGet(`/api/paper-trades${qs}`).then(setTrades);
  }, [statusFilter]);

  return (
    <div style={{ padding: "20px 24px" }}>
      <div style={{ fontFamily: mono, fontSize: 20, fontWeight: 700 }}>Paper trading</div>
      <div style={{ fontFamily: sans, fontSize: 12, color: TOKENS.dim, marginTop: 4, maxWidth: 680 }}>
        For every watchlisted wallet's first trade in a new instrument, simulates a copier entering at the next
        available market print from another wallet (not their own fill), settling at expiry the same way the
        backtest that validated this wallet was scored -- no real order placed, no account, no credentials. This is
        the actual forward-looking test of whether the watchlist works, updated automatically as
        scripts/run_watchlist_agent.py runs.
      </div>
      <div style={{ fontFamily: sans, fontSize: 11, color: TOKENS.dim, marginTop: 6, maxWidth: 680 }}>
        A wallet's record is evidence of skill, not instructions to inherit wholesale -- our system decides how much
        risk to actually take on. Naked shorts (unbounded loss if the market moves against the strike -- see
        conversation: every one of the worst losses found in cedar-ridge-e9, iron-grove-22, and others was exactly
        this) are mirrored, not skipped, and held to expiry exactly like every other trade -- <b>not</b> closed
        early by a live poll. A first version of this system tried exactly that (a live stop closing a position once
        its mark reached 1.10x the premium collected), but two days of real production data showed a 15-minute poll
        can't reliably catch a short-dated, high-gamma naked short before its mark gaps well past any threshold --
        48 stopped-out trades averaged -63% realized loss, not the intended -10%, and every one where the source
        wallet's own unprotected outcome was already checkable would have been a win if left alone. Replaced with a
        settlement-time floor instead: a naked short's realized loss is capped at 100% of the premium collected --
        <b>loss capped</b> -- the same max-loss shape as a bought option, modeling a margined position getting
        liquidated the instant its posted margin is exhausted rather than us trying to catch that moment on a poll.
        Live mark price and Greeks are still recorded every tick for every open naked short (observability only, not
        used to close anything). A wallet's buy-side trades are already naturally capped at 100% of premium paid.
        <b>Skipped (weak edge)</b>: the wallet has n≥5 history specifically in this (asset, option type, entry side)
        combo and its Wilson lower bound is below 50% -- worse than a coin flip in something it's actually done
        enough of to judge; this one still isn't opened at all. Resolved trades (including any loss-capped ones) and
        the historical stopped-out trades from before this change both count toward the win-rate/return/income
        figures below -- excluding either would hide real losses and make the track record look artificially rosy.
      </div>
      <div style={{ fontFamily: sans, fontSize: 11, color: TOKENS.dim, marginTop: 6, maxWidth: 680 }}>
        <b>Income</b> figures are hypothetical, not real premium totals -- trades are copied at their source
        wallet's own contract size, so raw entry prices range from $5 to $500+ and aren't comparable to sum. Every
        trade is instead normalized to a flat hypothetical stake, so income = return % × stake, apples to apples
        across every trade regardless of the original premium. Shown at both $10 (the cap already planned for real
        Phase 4 execution) and $5 (real minimum-order research puts the median trade's actual minimum tradeable
        size around $2.50 on the current watchlist -- a smaller live budget would likely size closer to each
        instrument's own minimum than a flat $10, so $5 is a closer preview of what that looks like).
      </div>

      {!summary ? (
        <Empty>loading…</Empty>
      ) : (
        <>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 18 }}>
            <StatCard label="Resolved" value={summary.resolved_count} />
            <StatCard label="Loss capped" value={summary.loss_capped_count} accent={TOKENS.flare} />
            <StatCard label="Stopped out (retired)" value={summary.stopped_out_count} />
            <StatCard label="Win rate" value={summary.win_rate != null ? `${summary.win_rate}%` : "—"} accent={summary.win_rate >= 50 ? TOKENS.current : TOKENS.bad} />
            <StatCard label="Avg return/trade" value={summary.total_return_pct != null ? `${summary.total_return_pct}%` : "—"} accent={summary.total_return_pct >= 0 ? TOKENS.current : TOKENS.bad} />
            <StatCard
              label={`Income ($${summary.hypothetical_stake_usd}/trade)`}
              value={summary.total_hypothetical_income_usd != null ? `${summary.total_hypothetical_income_usd >= 0 ? "+" : "-"}$${Math.abs(summary.total_hypothetical_income_usd).toFixed(2)}` : "—"}
              accent={summary.total_hypothetical_income_usd >= 0 ? TOKENS.current : TOKENS.bad}
            />
            <StatCard
              label={`Income ($${summary.hypothetical_stake_usd_small}/trade)`}
              value={summary.total_hypothetical_income_usd_small != null ? `${summary.total_hypothetical_income_usd_small >= 0 ? "+" : "-"}$${Math.abs(summary.total_hypothetical_income_usd_small).toFixed(2)}` : "—"}
              accent={summary.total_hypothetical_income_usd_small >= 0 ? TOKENS.current : TOKENS.bad}
            />
            <StatCard label="Open" value={summary.open_count} accent={TOKENS.undertow} />
            <StatCard label="Pricing" value={summary.pending_count} accent={TOKENS.flare} />
            <StatCard label="Skipped (stale)" value={summary.skipped_stale_count} />
            <StatCard label="Skipped (weak edge)" value={summary.skipped_weak_edge_count} accent={TOKENS.flare} />
          </div>

          <div style={{ marginTop: 20 }}>
            <SectionLabel>Us vs. the source wallets</SectionLabel>
            <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginBottom: 10, maxWidth: 680 }}>
              Two different sample sizes, not a bug: <b>"Us"</b> is every trade we've completed (resolved, naked
              shorts included -- held to expiry with a 100%-loss floor rather than closed early) -- our side is
              fully known the moment that happens. <b>"Source wallets"</b> is whatever subset of those same trades
              also has the wallet's own outcome known. Going forward these should track closely, since we no longer
              resolve early -- by the time our own position hits expiry, the wallet's own outcome is normally
              already determined too (either they closed it themselves earlier, or the same expiry-settlement
              estimate applies to both sides at once). The remaining gap here is mostly the 48 historical
              <b>stopped out</b> trades from an earlier live-poll stop-loss engine (since retired) that really did
              resolve on our side well before the wallet's own unprotected position reached its real expiry -- see
              "wallet's own" in the table below for the trade-by-trade version of this same comparison.
            </div>
            <div style={{ display: "flex", gap: 20, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.undertow, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                  Us (naked shorts capped at 100% loss) · n={summary.wallet_comparison.our_n}
                </div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <StatCard label="Win rate" value={summary.wallet_comparison.our_win_rate != null ? `${summary.wallet_comparison.our_win_rate}%` : "—"} accent={summary.wallet_comparison.our_win_rate >= 50 ? TOKENS.current : TOKENS.bad} />
                  <StatCard label="Avg return/trade" value={summary.wallet_comparison.our_total_return_pct != null ? `${summary.wallet_comparison.our_total_return_pct}%` : "—"} accent={summary.wallet_comparison.our_total_return_pct >= 0 ? TOKENS.current : TOKENS.bad} />
                  <StatCard
                    label="Income ($10/trade)"
                    value={summary.wallet_comparison.our_total_hypothetical_income_usd != null ? `${summary.wallet_comparison.our_total_hypothetical_income_usd >= 0 ? "+" : "-"}$${Math.abs(summary.wallet_comparison.our_total_hypothetical_income_usd).toFixed(2)}` : "—"}
                    accent={summary.wallet_comparison.our_total_hypothetical_income_usd >= 0 ? TOKENS.current : TOKENS.bad}
                  />
                  <StatCard
                    label="Income ($5/trade)"
                    value={summary.wallet_comparison.our_total_hypothetical_income_usd_small != null ? `${summary.wallet_comparison.our_total_hypothetical_income_usd_small >= 0 ? "+" : "-"}$${Math.abs(summary.wallet_comparison.our_total_hypothetical_income_usd_small).toFixed(2)}` : "—"}
                    accent={summary.wallet_comparison.our_total_hypothetical_income_usd_small >= 0 ? TOKENS.current : TOKENS.bad}
                  />
                </div>
              </div>
              <div>
                <div style={{ fontFamily: mono, fontSize: 10, color: TOKENS.flare, textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 6 }}>
                  Source wallets (no protection) · n={summary.wallet_comparison.wallet_known_n}
                </div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <StatCard label="Win rate" value={summary.wallet_comparison.wallet_win_rate != null ? `${summary.wallet_comparison.wallet_win_rate}%` : "—"} accent={summary.wallet_comparison.wallet_win_rate >= 50 ? TOKENS.current : TOKENS.bad} />
                  <StatCard label="Avg return/trade" value={summary.wallet_comparison.wallet_total_return_pct != null ? `${summary.wallet_comparison.wallet_total_return_pct}%` : "—"} accent={summary.wallet_comparison.wallet_total_return_pct >= 0 ? TOKENS.current : TOKENS.bad} />
                  <StatCard
                    label="Income ($10/trade)"
                    value={summary.wallet_comparison.wallet_total_hypothetical_income_usd != null ? `${summary.wallet_comparison.wallet_total_hypothetical_income_usd >= 0 ? "+" : "-"}$${Math.abs(summary.wallet_comparison.wallet_total_hypothetical_income_usd).toFixed(2)}` : "—"}
                    accent={summary.wallet_comparison.wallet_total_hypothetical_income_usd >= 0 ? TOKENS.current : TOKENS.bad}
                  />
                  <StatCard
                    label="Income ($5/trade)"
                    value={summary.wallet_comparison.wallet_total_hypothetical_income_usd_small != null ? `${summary.wallet_comparison.wallet_total_hypothetical_income_usd_small >= 0 ? "+" : "-"}$${Math.abs(summary.wallet_comparison.wallet_total_hypothetical_income_usd_small).toFixed(2)}` : "—"}
                    accent={summary.wallet_comparison.wallet_total_hypothetical_income_usd_small >= 0 ? TOKENS.current : TOKENS.bad}
                  />
                </div>
              </div>
            </div>
          </div>

          {summary.by_wallet.length > 0 && (
            <div style={{ marginTop: 20 }}>
              <SectionLabel>By source wallet</SectionLabel>
              <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
                <div style={{ display: "grid", gridTemplateColumns: "160px 90px 90px 90px 90px 1fr", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                  <span>wallet</span><span>completed</span><span>win rate</span><span>avg return/trade</span><span>income ($10)</span><span style={{ textAlign: "right" }}>income ($5)</span>
                </div>
                {summary.by_wallet.map((w) => (
                  <div key={w.address} style={{ display: "grid", gridTemplateColumns: "160px 90px 90px 90px 90px 1fr", padding: "9px 14px", borderTop: `1px solid ${TOKENS.hair}`, fontFamily: sans, fontSize: 12 }}>
                    <span style={{ fontFamily: mono, color: TOKENS.undertow }}>{w.alias}</span>
                    <span style={{ color: TOKENS.dim }}>{w.resolved}</span>
                    <span style={{ color: w.win_rate >= 50 ? TOKENS.current : TOKENS.bad }}>{w.win_rate}%</span>
                    <span><PctSign value={w.total_return_pct} /></span>
                    <span><UsdSign value={w.hypothetical_income_usd} /></span>
                    <span style={{ textAlign: "right" }}><UsdSign value={w.hypothetical_income_usd_small} /></span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div style={{ marginTop: 20 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <SectionLabel>All paper trades</SectionLabel>
              <div style={{ display: "flex", border: `1px solid ${TOKENS.hair}`, borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
                {[
                  { label: "all", value: null },
                  { label: "open", value: "open" },
                  { label: "resolved", value: "resolved" },
                  { label: "stopped out (retired)", value: "stopped_out" },
                  { label: "pricing", value: "pending_entry" },
                  { label: "skipped (weak edge)", value: "skipped_weak_edge" },
                  { label: "skipped (naked, retired)", value: "skipped_naked" },
                ].map((o) => (
                  <button
                    key={o.label}
                    onClick={() => setStatusFilter(o.value)}
                    style={{ fontFamily: mono, fontSize: 10.5, padding: "5px 10px", background: statusFilter === o.value ? TOKENS.panel2 : "transparent", color: statusFilter === o.value ? TOKENS.paper : TOKENS.dim, border: "none", cursor: "pointer" }}
                  >
                    {o.label}
                  </button>
                ))}
              </div>
            </div>
            <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginBottom: 6 }}>
              "edge" = this wallet's historical win rate specifically in that (asset, option type, entry side) combo,
              not their overall record -- e.g. a wallet great at selling ETH puts but weaker buying BTC calls would
              show a lower number on the latter. Shown on every opened trade for context; a combo with n≥5 history
              and Wilson below 50% is skipped before it ever gets here (see "skipped (weak edge)" above) -- an
              untested combo still gets opened, since no evidence isn't the same as proven-bad.
            </div>
            <div style={{ fontFamily: sans, fontSize: 10.5, color: TOKENS.dim, marginBottom: 6 }}>
              "wallet's own" = what the source wallet's own position actually closed at -- their real close if they
              exited it themselves, or the same expiry-settlement estimate used everywhere else if they never did
              (hover for which, and their exact exit price). No stop-loss, no protection -- this is the direct,
              trade-by-trade comparison of our outcome against what actually happened to the wallet holding the
              exact same position unprotected. Blank means their position hasn't closed or expired yet.
            </div>
            <div style={{ border: `1px solid ${TOKENS.hair}`, borderRadius: 6, overflow: "hidden" }}>
              <div style={{ display: "grid", gridTemplateColumns: "130px 1fr 90px 90px 90px 90px 80px 80px 100px", padding: "8px 14px", background: TOKENS.panel2, fontFamily: mono, fontSize: 10, color: TOKENS.dim, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                <span>wallet</span><span>position</span><span>status</span><span>entry</span><span>exit</span><span>edge</span><span>return</span><span>wallet's own</span><span style={{ textAlign: "right" }}>income ($10/trade)</span>
              </div>
              {!trades && <Empty>loading…</Empty>}
              {trades?.length === 0 && <Empty>no paper trades yet</Empty>}
              {trades?.map((t) => <TradeRow key={t.id} t={t} />)}
            </div>
          </div>

          <div style={{ marginTop: 24 }}>
            <WatchlistPanel />
          </div>
        </>
      )}
    </div>
  );
}
