import { NavLink } from "react-router-dom";
import { TOKENS, mono, sans } from "../tokens";

// `end` matches react-router's NavLink prop: pass true for a tab whose
// path is a prefix of another tab's path (e.g. "/wallets" is a prefix of
// "/wallets/:address", but they're different tabs) so it only lights up
// on an exact match, not on every nested route underneath it.
export function Tab({ label, to, end }) {
  return (
    <NavLink
      to={to}
      end={end}
      style={({ isActive }) => ({
        fontFamily: mono,
        fontSize: 12,
        letterSpacing: "0.06em",
        padding: "8px 16px",
        background: "transparent",
        border: "none",
        borderBottom: `2px solid ${isActive ? TOKENS.flare : "transparent"}`,
        color: isActive ? TOKENS.paper : TOKENS.dim,
        cursor: "pointer",
        textDecoration: "none",
        display: "inline-block",
      })}
    >
      {label}
    </NavLink>
  );
}

export function StatCard({ label, value, accent }) {
  return (
    <div style={{ background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "10px 14px", flex: 1 }}>
      <div style={{ fontFamily: mono, fontSize: 10, letterSpacing: "0.08em", color: TOKENS.dim, textTransform: "uppercase" }}>{label}</div>
      <div style={{ fontFamily: mono, fontSize: 18, fontWeight: 600, color: accent || TOKENS.paper, marginTop: 4 }}>{value}</div>
    </div>
  );
}

export function Panel({ title, children, style }) {
  return (
    <div style={{ background: TOKENS.panel, border: `1px solid ${TOKENS.hair}`, borderRadius: 6, padding: "14px", ...style }}>
      {title && (
        <div style={{ fontFamily: mono, fontSize: 11, letterSpacing: "0.1em", color: TOKENS.dim, textTransform: "uppercase", marginBottom: 10 }}>
          {title}
        </div>
      )}
      {children}
    </div>
  );
}

export function SectionLabel({ children }) {
  return (
    <div style={{ fontFamily: mono, fontSize: 11, letterSpacing: "0.1em", color: TOKENS.dim, textTransform: "uppercase", marginBottom: 8 }}>
      {children}
    </div>
  );
}

export function Empty({ children }) {
  return <div style={{ fontFamily: sans, fontSize: 12.5, color: TOKENS.dim, padding: "10px 0" }}>{children}</div>;
}
