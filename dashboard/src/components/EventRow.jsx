import { TOKENS, mono, sans } from "../tokens";
import { fmtUsd } from "../format";

const REASON_STYLE = {
  large_trade: { label: "SIZE", color: TOKENS.flare },
  wallet_size_jump: { label: "JUMP", color: TOKENS.undertow },
};

function ReasonBadge({ reason }) {
  const s = REASON_STYLE[reason] || { label: reason, color: TOKENS.dim };
  return (
    <span
      style={{
        fontFamily: mono,
        fontSize: 9,
        color: TOKENS.ink,
        background: s.color,
        padding: "2px 5px",
        borderRadius: 3,
        fontWeight: 600,
      }}
    >
      {s.label}
    </span>
  );
}

export function EventRow({ ev, active, onClick }) {
  const detail = ev.anomaly_score != null ? `z ${ev.anomaly_score.toFixed(2)}` : `${ev.wallet_jump_ratio.toFixed(1)}x usual`;
  return (
    <button
      onClick={onClick}
      style={{
        display: "block",
        width: "100%",
        textAlign: "left",
        background: active ? TOKENS.panel2 : "transparent",
        border: "none",
        borderLeft: `2px solid ${active ? TOKENS.flare : "transparent"}`,
        borderBottom: `1px solid ${TOKENS.hair}`,
        padding: "12px 16px",
        cursor: "pointer",
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <span style={{ fontFamily: mono, fontSize: 13, fontWeight: 600, color: TOKENS.paper }}>
          {ev.asset} {ev.option_type.toUpperCase()} {ev.strike.toLocaleString()}
        </span>
        <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
          {ev.flag_reasons.map((r) => (
            <ReasonBadge key={r} reason={r} />
          ))}
        </div>
      </div>
      <div style={{ fontFamily: sans, fontSize: 11.5, color: TOKENS.dim, marginTop: 4 }}>
        {ev.date} · {ev.side} · {detail} · {fmtUsd(ev.notional_usd)}
      </div>
      {ev.wallet_alias && (
        <div style={{ fontFamily: mono, fontSize: 10.5, color: TOKENS.undertow, marginTop: 3 }}>{ev.wallet_alias}</div>
      )}
    </button>
  );
}
