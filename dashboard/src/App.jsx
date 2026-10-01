import { useEffect, useState } from "react";
import { Routes, Route, Navigate, useNavigate } from "react-router-dom";
import { TOKENS, FONTS, mono, sans } from "./tokens";
import { apiGet, apiPost } from "./api";
import { Tab } from "./components/Shared";
import { SignalsView } from "./components/SignalsView";
import { WalletsView } from "./components/WalletsView";
import { LeaderboardView } from "./components/LeaderboardView";
import { PaperTradingView } from "./components/PaperTradingView";
import { LiveFeedView } from "./components/LiveFeedView";
// CatalystsView is dormant, not deleted -- same precedent as
// NEWS_LOOKUPS_ENABLED in featureFlags.js. Its code, /api/news,
// /api/trump-posts, and trump_posts_cache are all untouched; this project
// just isn't investing further there for now. Re-add the import/tab below
// to bring it back.

export default function App() {
  const [meta, setMeta] = useState(null);
  const [assetFilter, setAssetFilter] = useState("ALL");
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  // feature a wallet found via the leaderboard (so it's pinned/searchable
  // with a note attached, same mechanism as manually-curated deep-dive
  // subjects), then navigate to its URL directly -- no more passing a
  // "jump to this address" flag down through props and racing it against
  // the wallet list's own default-selection effect (see conversation: that
  // caused two separate real bugs earlier). The URL IS the selection now.
  async function deepDive(address) {
    await apiPost(`/api/wallets/${address}/feature`, { note: "Surfaced via leaderboard for a closer look." });
    navigate(`/wallets/${address}`);
  }

  useEffect(() => {
    apiGet("/api/meta").then(setMeta);
  }, []);

  useEffect(() => {
    setLoading(true);
    const qs = assetFilter === "ALL" ? "?limit=2000" : `?asset=${assetFilter}&limit=2000`;
    apiGet(`/api/events${qs}`)
      .then(setEvents)
      .finally(() => setLoading(false));
  }, [assetFilter]);

  return (
    <div style={{ background: TOKENS.ink, minHeight: "100vh", color: TOKENS.paper, fontFamily: sans }}>
      <style>{FONTS}</style>

      <div style={{ borderBottom: `1px solid ${TOKENS.hair}`, padding: "16px 22px 0", display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 20 }}>
          <span style={{ fontFamily: mono, fontWeight: 700, fontSize: 20, letterSpacing: "0.02em" }}>UNDERTOW</span>
          <div style={{ display: "flex" }}>
            <Tab label="SIGNALS" to="/signals" />
            <Tab label="WALLETS" to="/wallets" />
            <Tab label="LEADERBOARD" to="/leaderboard" />
            <Tab label="PAPER TRADING" to="/paper-trading" />
            <Tab label="LIVE FEED" to="/live-feed" />
          </div>
        </div>
        <div style={{ fontFamily: mono, fontSize: 11, color: TOKENS.dim, display: "flex", alignItems: "center", gap: 6, paddingBottom: 12 }}>
          <span style={{ width: 6, height: 6, borderRadius: "50%", background: TOKENS.current, display: "inline-block" }} />
          {meta ? `data ${meta.date_range.start} → ${meta.date_range.end}` : "loading…"}
        </div>
      </div>

      <Routes>
        <Route path="/" element={<Navigate to="/signals" replace />} />
        <Route
          path="/signals"
          element={<SignalsView events={events} assetFilter={assetFilter} setAssetFilter={setAssetFilter} loading={loading} />}
        />
        <Route path="/wallets" element={<WalletsView />} />
        <Route path="/wallets/:address" element={<WalletsView />} />
        <Route path="/leaderboard" element={<LeaderboardView onDeepDive={deepDive} />} />
        <Route path="/paper-trading" element={<PaperTradingView />} />
        <Route path="/live-feed" element={<LiveFeedView />} />
        <Route path="*" element={<Navigate to="/signals" replace />} />
      </Routes>
    </div>
  );
}
