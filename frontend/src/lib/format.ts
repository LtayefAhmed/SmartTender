/** Small formatting helpers shared across pages. */

import type { RelevanceBand } from "../api/types";

export function fmtDate(iso: string | null | undefined, withTime = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  const date = d.toLocaleDateString("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  if (!withTime) return date;
  return `${date} ${d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })}`;
}

export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return "jamais";
  const diff = Date.now() - new Date(iso).getTime();
  const s = Math.round(diff / 1000);
  if (s < 60) return "à l'instant";
  const m = Math.round(s / 60);
  if (m < 60) return `il y a ${m} min`;
  const h = Math.round(m / 60);
  if (h < 24) return `il y a ${h} h`;
  const days = Math.round(h / 24);
  return `il y a ${days} j`;
}

export function fmtMoney(amount: string | number | null, currency: string | null): string {
  if (amount === null || amount === undefined) return "—";
  const n = typeof amount === "string" ? parseFloat(amount) : amount;
  if (isNaN(n)) return "—";
  return `${n.toLocaleString("fr-FR", { maximumFractionDigits: 0 })} ${currency ?? ""}`.trim();
}

export function fmtBytes(bytes: number | null | undefined): string {
  if (!bytes) return "—";
  const units = ["o", "Ko", "Mo", "Go"];
  let v = bytes;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export const BAND_COLOR: Record<RelevanceBand, string> = {
  highly_relevant: "teal",
  relevant: "amber",
  low_relevance: "gray",
  out_of_scope: "rose",
  unscored: "gray",
};
export const BAND_LABEL: Record<RelevanceBand, string> = {
  highly_relevant: "Très pertinent",
  relevant: "Pertinent",
  low_relevance: "Peu pertinent",
  out_of_scope: "Hors périmètre",
  unscored: "Non évalué",
};

export function healthColor(health: string): string {
  return (
    {
      healthy: "green",
      degraded: "amber",
      failing: "red",
      disabled: "gray",
      credentials_missing: "violet",
      unknown: "gray",
    }[health] ?? "gray"
  );
}

export function statusColor(status: string): string {
  return (
    {
      succeeded: "green",
      partial: "amber",
      failed: "red",
      running: "blue",
      pending: "gray",
      cancelled: "gray",
      timed_out: "red",
      completed: "green",
      scored: "teal",
      received: "gray",
      parsing: "blue",
      scoring: "blue",
      rejected: "red",
      duplicate: "violet",
    }[status] ?? "gray"
  );
}

/**
 * Turns a connector's machine-readable unavailability code into a sentence.
 *
 * These codes are contracts between the registry and the API — stable, greppable,
 * and meant for logs. Rendering them raw in the interface ("fixtures_unavailable")
 * asks the person reading to know the codebase, and makes a deliberately
 * inactive source look like a failure.
 */
export function unavailableLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  if (reason.startsWith("not_enabled_in_env:")) {
    return `Réservée à l'environnement ${reason.split(":")[1]}`;
  }
  const labels: Record<string, string> = {
    fixtures_unavailable: "Source de test — non embarquée dans l'image de production",
    credentials_missing: "Identifiants non configurés",
    disabled: "Désactivée manuellement",
    circuit_open: "Disjoncteur ouvert — trop d'échecs consécutifs",
  };
  return labels[reason] ?? reason;
}

/**
 * Whether a source is inactive by design rather than broken.
 *
 * Both look identical on a card — same grey badge, same zero counters — but one
 * is a setup step and the other is an incident. Only the second deserves the
 * error history to stay on screen.
 */
export function isInactiveByDesign(reason: string | null | undefined): boolean {
  if (!reason) return false;
  return (
    reason === "fixtures_unavailable" ||
    reason === "disabled" ||
    reason.startsWith("not_enabled_in_env:")
  );
}
