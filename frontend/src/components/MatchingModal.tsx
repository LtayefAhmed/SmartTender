import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { CandidateMatch, MatchResult } from "../api/types";
import { Badge, ErrorState, Loading } from "./ui";

/**
 * CV matching, given the screen it deserves.
 *
 * The chart is one stacked horizontal bar per candidate, and the choice of
 * form is the whole design. The three weighted signals sum *exactly* to the
 * score, so a stack answers both questions at once: the bar's length is the
 * ranking, and its segments are the reason. Two separate charts — one for
 * rank, one for breakdown — would make a reader join them by eye.
 *
 * Colour follows the shared chart tokens, which were validated per mode with
 * the palette checker rather than picked: lightness band, chroma floor,
 * colour-vision separation (worst adjacent pair ΔE 13.6), and contrast against
 * the surface. Dark mode carries its own re-validated steps; flipping the
 * light ones lands outside the darker, narrower band.
 *
 * Vetoed candidates are listed apart rather than drawn as zero-length bars. A
 * bar of length zero communicates nothing, and the reason for the veto is the
 * useful part.
 */
export function MatchingModal({
  tenderId,
  title,
  onClose,
}: {
  tenderId: string;
  title: string;
  onClose: () => void;
}) {
  const [expanded, setExpanded] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["candidates", tenderId],
    queryFn: () => api.get<MatchResult>(`/tenders/${tenderId}/candidates`, { limit: 20 }),
    refetchOnWindowFocus: false,
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const data = query.data;
  const ranked = (data?.candidates ?? []).filter((c) => !c.vetoed);
  const vetoed = (data?.candidates ?? []).filter((c) => c.vetoed);

  return (
    <div className="modal-scrim" onClick={onClose}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <div className="tiny muted mono">PROFILS CORRESPONDANTS</div>
            <h2 style={{ fontSize: 17, marginTop: 3 }}>{title}</h2>
          </div>
          <button className="btn sm ghost" style={{ marginLeft: "auto" }} onClick={onClose}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          {query.isLoading && <Loading label="Analyse des exigences et comparaison des profils…" />}
          {query.error && <ErrorState error={query.error} />}

          {data && data.status !== "ok" && (
            <div className="empty">
              <div className="big">◇</div>
              {data.message ?? "Cet appel d'offres n'a pas de texte exploitable."}
            </div>
          )}

          {data && data.status === "ok" && (
            <div className="stack" style={{ gap: 20 }}>
              <Summary data={data} />

              {data.structured_requirements && (
                <Requirements structured={data.structured_requirements} />
              )}

              {ranked.length > 0 ? (
                <div className="card">
                  <div className="card-title">
                    Score par profil
                    <span className="hint">
                      longueur = score · segments = origine du score
                    </span>
                  </div>
                  <Chart candidates={ranked} />
                </div>
              ) : (
                <div className="card">
                  <div className="empty" style={{ padding: "28px 20px" }}>
                    Aucun profil ne franchit le verrou technologique.
                  </div>
                </div>
              )}

              {ranked.map((candidate, rank) => (
                <Profile
                  key={candidate.cv_id}
                  rank={rank + 1}
                  candidate={candidate}
                  open={expanded === candidate.cv_id}
                  onToggle={() =>
                    setExpanded(expanded === candidate.cv_id ? null : candidate.cv_id)
                  }
                  requirements={data.requirements}
                />
              ))}

              {vetoed.length > 0 && <Vetoed candidates={vetoed} />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * Open a candidate's PDF through a short-lived signed link.
 *
 * The API never streams the bytes: it returns a URL and the browser fetches the
 * object store directly, so a 20 MB CV never occupies a request handler.
 */
async function openCvPdf(cvId: string) {
  try {
    const res = await api.get<{ url: string }>(`/cvs/${cvId}/download`);
    window.open(res.url, "_blank", "noopener");
  } catch {
    window.alert("Ce CV n'a pas pu être ouvert.");
  }
}

/* -------------------------------------------------------------------------- */
function Summary({ data }: { data: MatchResult }) {
  return (
    <div className="grid cols-4">
      {/* Totals, not the size of the slice below. */}
      <Tile
        label="Profils retenus"
        value={data.kept_total}
        foot="au-dessus du verrou"
        tone="teal"
      />
      <Tile label="Écartés" value={data.vetoed_total} foot="technologies insuffisantes" />
      <Tile label="Exigences lues" value={data.requirements.length} foot="passages du dossier" />
      <Tile
        label="Technologies"
        value={data.required_technologies.length}
        foot="nommées par l'offre"
        tone="amber"
      />
    </div>
  );
}

function Tile({
  label,
  value,
  foot,
  tone = "blue",
}: {
  label: string;
  value: number;
  foot: string;
  tone?: string;
}) {
  return (
    <div className={`stat ${tone}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      <div className="foot">{foot}</div>
    </div>
  );
}

/**
 * What the tender demands, read out of its own requirement passages.
 *
 * Shown above the ranking because it is the question the scores answer. A
 * shortlist without its criteria beside it invites the reader to assume the
 * criteria were the right ones.
 */
function Requirements({
  structured,
}: {
  structured: NonNullable<MatchResult["structured_requirements"]>;
}) {
  const groups: [string, string[]][] = [
    ["Technologies", structured.technologies],
    ["Profils recherchés", structured.profils],
    ["Certifications", structured.certifications],
    ["Langues", structured.langues],
  ];
  const filled = groups.filter(([, values]) => values.length > 0);
  if (!filled.length && !structured.exigences.length) return null;

  return (
    <div className="card">
      <div className="card-title">
        Ce que l'offre exige
        <span className="hint">lu dans le dossier</span>
      </div>

      <div className="stack" style={{ gap: 12 }}>
        {structured.experience_min_annees != null && (
          <div className="tiny">
            <span className="muted">Expérience minimale · </span>
            <strong>{structured.experience_min_annees} ans</strong>
          </div>
        )}

        {filled.map(([label, values]) => (
          <div key={label}>
            <div className="tiny muted" style={{ marginBottom: 5 }}>
              {label}
            </div>
            <div className="row wrap" style={{ gap: 5 }}>
              {values.map((value) => (
                <Badge key={value} color="blue">
                  {value}
                </Badge>
              ))}
            </div>
          </div>
        ))}

        {structured.exigences.length > 0 && (
          <div>
            <div className="tiny muted" style={{ marginBottom: 5 }}>
              Obligations relevées
            </div>
            <ul className="tiny muted" style={{ paddingLeft: 16, lineHeight: 1.7 }}>
              {structured.exigences.slice(0, 8).map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
const SERIES = [
  { key: "similarity" as const, label: "Similarité", weight: 0.45, token: "var(--chart-1)" },
  { key: "coverage" as const, label: "Couverture", weight: 0.35, token: "var(--chart-2)" },
  { key: "technology_ratio" as const, label: "Technologies", weight: 0.2, token: "var(--chart-3)" },
];

function Chart({ candidates }: { candidates: CandidateMatch[] }) {
  const [hover, setHover] = useState<string | null>(null);
  // Scaled to the best score rather than to 1.0: every bar would otherwise sit
  // in the left third and the ranking would be unreadable.
  const ceiling = Math.max(...candidates.map((c) => c.score), 0.1);

  return (
    <>
      {/* A legend is present because there are three series — identity must
          never rest on colour-matching alone. */}
      <div className="chart-legend">
        {SERIES.map((series) => (
          <span key={series.key}>
            <i style={{ background: series.token }} />
            {series.label}
            <span className="muted">· {(series.weight * 100).toFixed(0)} %</span>
          </span>
        ))}
      </div>

      {candidates.map((candidate) => {
        let offset = 0;
        return (
          <div
            className="chart-row"
            key={candidate.cv_id}
            onMouseEnter={() => setHover(candidate.cv_id)}
            onMouseLeave={() => setHover(null)}
          >
            <div className="chart-label" title={candidate.filename}>
              {candidate.label}
            </div>
            <div
              className="chart-track"
              style={{ opacity: hover && hover !== candidate.cv_id ? 0.55 : 1 }}
            >
              {SERIES.map((series) => {
                const width = (candidate[series.key] * series.weight * 100) / ceiling;
                const left = offset;
                offset += width;
                return (
                  <div
                    key={series.key}
                    className="chart-seg"
                    style={{ left: `${left}%`, width: `${width}%`, background: series.token }}
                    // A title attribute rather than a floating tooltip: it
                    // works on keyboard focus and in a screen reader, and the
                    // value it shows is also printed beside the bar and in the
                    // profile card below — a tooltip must never be the only
                    // way to read a number.
                    title={`${series.label} ${(candidate[series.key] * 100).toFixed(0)} % → ${(
                      candidate[series.key] * series.weight
                    ).toFixed(3)}`}
                  />
                );
              })}
            </div>
            <div className="chart-value">{candidate.score.toFixed(2)}</div>
          </div>
        );
      })}
    </>
  );
}

/* -------------------------------------------------------------------------- */
function Profile({
  rank,
  candidate,
  open,
  onToggle,
  requirements,
}: {
  rank: number;
  candidate: CandidateMatch;
  open: boolean;
  onToggle: () => void;
  requirements: MatchResult["requirements"];
}) {
  return (
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <div
        className="row spread"
        style={{ padding: "14px 18px", cursor: "pointer" }}
        onClick={onToggle}
      >
        <div className="row" style={{ minWidth: 0, gap: 12 }}>
          <span className="mono muted" style={{ fontSize: 12, width: 20 }}>
            {rank}
          </span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13.5 }}>{candidate.label}</div>
            <div className="tiny muted">
              {/* The headline only when it is not already the label — an
                  anonymised CV would otherwise print its job title twice. */}
              {candidate.display_name && candidate.headline
                ? `${candidate.headline} · `
                : ""}
              {candidate.matched_technologies.length} technologies attestées ·{" "}
              {candidate.evidence.length} extraits probants
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 10 }}>
          {/* stopPropagation: the row expands the evidence, this opens the
              document. A shortlist you cannot read the CVs from is a list of
              scores, not a decision aid. */}
          <button
            className="btn sm ghost"
            title={`Ouvrir ${candidate.filename}`}
            onClick={(event) => {
              event.stopPropagation();
              openCvPdf(candidate.cv_id);
            }}
          >
            ↓ PDF
          </button>
          <Badge color={candidate.score >= 0.6 ? "teal" : "blue"}>
            {candidate.score.toFixed(2)}
          </Badge>
          <span className="tiny muted">{open ? "▴" : "▾"}</span>
        </div>
      </div>

      {open && (
        <div style={{ padding: "0 18px 18px" }}>
          <div className="divider" style={{ margin: "0 0 14px" }} />

          <div className="grid cols-3" style={{ gap: 14, marginBottom: 16 }}>
            {SERIES.map((series) => (
              <div key={series.key}>
                <div className="tiny muted">
                  {series.label} · pondéré {(series.weight * 100).toFixed(0)} %
                </div>
                <div className="row" style={{ gap: 8, marginTop: 4 }}>
                  <div className="chart-track" style={{ flex: 1, height: 6 }}>
                    <div
                      className="chart-seg"
                      style={{
                        left: 0,
                        width: `${candidate[series.key] * 100}%`,
                        background: series.token,
                        borderRight: 0,
                      }}
                    />
                  </div>
                  <span className="mono tiny">
                    {(candidate[series.key] * 100).toFixed(0)} %
                  </span>
                </div>
              </div>
            ))}
          </div>

          {candidate.matched_technologies.length > 0 && (
            <div className="mb">
              <div className="tiny muted mb" style={{ marginBottom: 6 }}>
                Technologies exigées et attestées
              </div>
              <div className="row wrap" style={{ gap: 5 }}>
                {candidate.matched_technologies.map((tech) => (
                  <Badge key={tech} color="teal">
                    {tech}
                  </Badge>
                ))}
              </div>
            </div>
          )}

          {/* The passages, beside the requirement each one answered. This is
              the proof the specification asks for: a ranking a bid manager
              cannot audit is one they are right to distrust. */}
          <div className="tiny muted" style={{ marginBottom: 6 }}>
            Extraits du CV, en regard de l'exigence à laquelle ils répondent
          </div>
          <div className="stack" style={{ gap: 10 }}>
            {candidate.evidence.map((item, index) => {
              const requirement = requirements.find((r) => r.position === item.requirement);
              return (
                <div key={index} className="tiny" style={{ display: "grid", gap: 4 }}>
                  <div className="row" style={{ gap: 8 }}>
                    <span className="mono" style={{ color: "var(--chart-1)" }}>
                      {item.score.toFixed(2)}
                    </span>
                    <span className="muted" style={{ minWidth: 0 }}>
                      {requirement ? requirement.text.slice(0, 130) + "…" : "exigence"}
                    </span>
                  </div>
                  <div
                    style={{
                      borderLeft: "2px solid var(--line)",
                      paddingLeft: 10,
                      color: "var(--ink)",
                    }}
                  >
                    {item.passage}
                  </div>
                </div>
              );
            })}
          </div>

          {candidate.missing_technologies.length > 0 && (
            <div className="mt">
              <div className="tiny muted" style={{ marginBottom: 6 }}>
                Exigées mais non attestées
              </div>
              <div className="row wrap" style={{ gap: 5 }}>
                {candidate.missing_technologies.slice(0, 14).map((tech) => (
                  <Badge key={tech} color="gray">
                    {tech}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function Vetoed({ candidates }: { candidates: CandidateMatch[] }) {
  return (
    <div className="card">
      <div className="card-title">
        Écartés par le verrou
        <span className="hint">{candidates.length} exemples</span>
      </div>
      <div className="tiny muted mb">
        Ces profils n'attestent pas assez des technologies nommées par l'offre. Le
        score est ramené à zéro quelle que soit la similarité du texte — un CV bien
        écrit sans les compétences exigées ne doit pas séduire le classement.
      </div>
      {/* The arithmetic, per profile. "Score 0" without it is the kind of
          output that makes people stop trusting a tool, and the rule scales
          with the tender so a single shared sentence would be wrong. */}
      <div className="stack" style={{ gap: 6 }}>
        {candidates.map((candidate) => (
          <div key={candidate.cv_id} className="row" style={{ gap: 10 }}>
            <Badge color="gray">{candidate.label}</Badge>
            <span className="tiny muted">{candidate.veto_reason}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
