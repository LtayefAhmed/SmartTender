import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";

/**
 * The four phases of the product, with live numbers.
 *
 * The dashboard described ingestion and nothing else, so anyone opening
 * SmartTender for the first time concluded it was a scraper. This is the
 * screen that says what the platform actually does, in the order it does it:
 * a tender is detected, profiles are ranked against it, a human selects, and
 * documents come out.
 *
 * **Why no WebGL.** The depth here is CSS — layered shadows, a perspective
 * container and `translateZ` on hover. A three.js scene would add some six
 * hundred kilobytes and GPU load to a page of tables, on machines that have
 * already run out of memory running this stack. The illusion is cheaper than
 * the dependency and reads better on an operator tool: a card that lifts when
 * you reach for it says "this is clickable", where a rotating cube says
 * nothing at all.
 *
 * **Why the numbers count up.** Not decoration: a counter that animates from
 * zero makes the eye land on the figure, and the figures are the argument.
 * The motion respects `prefers-reduced-motion`, where it resolves instantly.
 */
interface Phase {
  key: string;
  label: string;
  total: number;
  done: number;
  done_label: string;
  href: string;
}

const TINT: Record<string, string> = {
  detection: "var(--chart-1)",
  matching: "var(--chart-2)",
  selection: "var(--chart-3)",
  dossier: "var(--teal)",
};

const CAPTION: Record<string, string> = {
  detection: "Portails surveillés en continu",
  matching: "Profils rapprochés des exigences",
  selection: "Figées et verrouillées par un humain",
  dossier: "CV, matrices, lettres produits",
};

export function Journey() {
  const query = useQuery({
    queryKey: ["journey"],
    queryFn: () => api.get<{ phases: Phase[] }>("/stats/journey"),
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  });

  const phases = query.data?.phases ?? [];

  return (
    <section className="journey" aria-label="Parcours de la plateforme">
      <div className="journey-head">
        <h2>De l'appel d'offres au dossier</h2>
        <p>
          Quatre phases, un verrou humain avant chaque document qui sort de la
          plateforme.
        </p>
      </div>

      <div className="journey-track">
        {phases.map((phase, index) => (
          <PhaseCard key={phase.key} phase={phase} index={index} />
        ))}
      </div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
function PhaseCard({ phase, index }: { phase: Phase; index: number }) {
  const ratio = phase.total > 0 ? Math.min(1, phase.done / phase.total) : 0;

  return (
    <>
      {index > 0 && <Connector index={index} />}
      <Link
        to={phase.href}
        className="phase"
        style={
          {
            "--tint": TINT[phase.key] ?? "var(--blue)",
            // Staggered so the row assembles left to right, which is the
            // direction the process runs in.
            animationDelay: `${index * 110}ms`,
          } as React.CSSProperties
        }
      >
        <div className="phase-step">{String(index + 1).padStart(2, "0")}</div>
        <div className="phase-label">{phase.label}</div>

        <div className="phase-total">
          <Counter value={phase.total} delay={index * 110} />
        </div>

        <div className="phase-done">
          <span className="mono">{phase.done}</span> {phase.done_label}
        </div>

        {/* The bar is the honest part: it is `done / total`, so a phase where
            most of the work has not advanced reads as unfinished rather than
            as a large number. */}
        <div className="phase-bar" aria-hidden>
          <i style={{ width: `${Math.round(ratio * 100)}%` }} />
        </div>

        <div className="phase-caption">{CAPTION[phase.key] ?? ""}</div>
      </Link>
    </>
  );
}

/**
 * The arrow between two phases.
 *
 * Drawn rather than typed: a dash travelling along the path says the work
 * flows, and a character cannot. It is `aria-hidden` because it carries no
 * information a screen reader needs — the order of the cards already does.
 */
function Connector({ index }: { index: number }) {
  return (
    <svg className="journey-link" viewBox="0 0 48 12" aria-hidden focusable="false">
      <line x1="2" y1="6" x2="40" y2="6" className="journey-rail" />
      <line
        x1="2"
        y1="6"
        x2="40"
        y2="6"
        className="journey-pulse"
        style={{ animationDelay: `${index * 320}ms` }}
      />
      <path d="M38 2.5 L45 6 L38 9.5 Z" className="journey-head-arrow" />
    </svg>
  );
}

/**
 * A figure that counts up to its value.
 *
 * Resolves instantly when the reader asked for less motion, and whenever the
 * tab is hidden — an animation nobody is watching is a frame budget nobody
 * gets back.
 */
function Counter({ value, delay = 0 }: { value: number; delay?: number }) {
  const [shown, setShown] = useState(0);
  const frame = useRef<number>();

  useEffect(() => {
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced || document.hidden || value <= 0) {
      setShown(value);
      return;
    }

    const duration = 780;
    let start: number | null = null;

    const tick = (now: number) => {
      if (start === null) start = now;
      const elapsed = now - start - delay;
      if (elapsed < 0) {
        frame.current = requestAnimationFrame(tick);
        return;
      }
      const progress = Math.min(1, elapsed / duration);
      // Ease-out cubic: fast at first, settling at the end — the shape a
      // number reaching its value should have.
      const eased = 1 - Math.pow(1 - progress, 3);
      setShown(Math.round(value * eased));
      if (progress < 1) frame.current = requestAnimationFrame(tick);
    };

    frame.current = requestAnimationFrame(tick);
    return () => {
      if (frame.current) cancelAnimationFrame(frame.current);
    };
  }, [value, delay]);

  return <>{shown.toLocaleString("fr-FR")}</>;
}
