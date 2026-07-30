/** Small shared building blocks: badges, tiles, meters, empty states, spinners. */
import type { ReactNode } from "react";

export function Badge({ color, children }: { color: string; children: ReactNode }) {
  return <span className={`badge ${color}`}>{children}</span>;
}

export function Dot({ color }: { color: string }) {
  const map: Record<string, string> = {
    green: "var(--green)",
    amber: "var(--amber)",
    red: "var(--red)",
    blue: "var(--blue)",
    teal: "var(--teal)",
    rose: "var(--rose)",
    violet: "var(--violet)",
    gray: "var(--muted-2)",
  };
  return <span className="dot" style={{ background: map[color] ?? "var(--muted-2)" }} />;
}

export function StatTile({
  label,
  value,
  foot,
  tone = "blue",
}: {
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  tone?: string;
}) {
  return (
    <div className={`stat ${tone}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {foot && <div className="foot">{foot}</div>}
    </div>
  );
}

export function Card({
  title,
  hint,
  children,
  className = "",
}: {
  title?: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={`card ${className}`}>
      {title && (
        <div className="card-title">
          {title}
          {hint && <span className="hint">{hint}</span>}
        </div>
      )}
      {children}
    </div>
  );
}

export function Meter({ value, color = "var(--blue)" }: { value: number; color?: string }) {
  return (
    <div className="meter">
      <span style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%`, background: color }} />
    </div>
  );
}

export function Empty({ icon = "◇", children }: { icon?: string; children: ReactNode }) {
  return (
    <div className="empty">
      <div className="big">{icon}</div>
      {children}
    </div>
  );
}

export function Spinner() {
  return <span className="spin" />;
}

export function Loading({ label = "Chargement…" }: { label?: string }) {
  return (
    <div className="empty">
      <Spinner />
      <div className="mt tiny muted">{label}</div>
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="empty">
      <div className="big" style={{ color: "var(--red)" }}>
        ⚠
      </div>
      <div className="muted">{msg}</div>
    </div>
  );
}
