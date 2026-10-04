import { Link, useLocation } from "react-router-dom";

/**
 * Where the current screen sits in the process.
 *
 * The sidebar says what exists; this says where you are and what comes next.
 * The distinction matters on a platform with a four-phase workflow that spans
 * days: someone who opens the app on a Tuesday to finish Friday's dossier has
 * no way, from a highlighted menu row alone, to tell whether they are early or
 * late in the chain.
 *
 * It is a *process* breadcrumb rather than a URL one. `/matching/recherche`
 * expanded as "matching › recherche" would restate the address bar; naming the
 * phase and showing the two either side of it tells the reader something they
 * did not already have.
 */
const PHASES = [
  { key: "detection", label: "Détection", href: "/tenders" },
  { key: "matching", label: "Matching", href: "/matching/recherche" },
  { key: "selection", label: "Sélection", href: "/dossiers" },
  { key: "dossier", label: "Dossier", href: "/dossiers" },
] as const;

/** Which phase a path belongs to. Unlisted paths — the dashboard, admin,
 *  notifications — are not phases of the process and show no trail at all,
 *  rather than being forced into one. */
const ROUTE_PHASE: { prefix: string; phase: string }[] = [
  { prefix: "/tenders", phase: "detection" },
  { prefix: "/scrape", phase: "detection" },
  { prefix: "/upload", phase: "detection" },
  { prefix: "/schedules", phase: "detection" },
  { prefix: "/sources", phase: "detection" },
  { prefix: "/matching", phase: "matching" },
  { prefix: "/dossiers", phase: "selection" },
];

export function Breadcrumb() {
  const { pathname } = useLocation();
  const match = ROUTE_PHASE.find((entry) => pathname.startsWith(entry.prefix));
  if (!match) return null;

  const current = PHASES.findIndex((phase) => phase.key === match.phase);

  return (
    <nav className="trail" aria-label="Progression dans le parcours">
      {PHASES.map((phase, index) => {
        const state =
          index < current ? "done" : index === current ? "current" : "ahead";
        return (
          <span key={phase.key} className="trail-item">
            {index > 0 && <i className="trail-sep" aria-hidden>›</i>}
            {state === "current" ? (
              // The current phase is not a link to itself.
              <strong className="trail-current" aria-current="step">
                {phase.label}
              </strong>
            ) : (
              <Link to={phase.href} className={`trail-link ${state}`}>
                {phase.label}
              </Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}
