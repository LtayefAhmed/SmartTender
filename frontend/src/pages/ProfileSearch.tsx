import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  Facet,
  JobDescriptionReading,
  ProfileFacets,
  ProfileHit,
  ProfileSearchResult,
} from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Empty, ErrorState, Loading, Meter, Spinner } from "../components/ui";
import { useToast } from "../components/toast";

/**
 * Searching the CV base directly, with no tender involved.
 *
 * Two rules shape this screen, and both come from a measurement rather than a
 * preference.
 *
 * **Technologies exclude, everything else ranks.** A recruiter who ticked Java
 * and Spring meant both. But over 344 CVs only 21% state a language at all, so
 * filtering hard on one would reject a candidate for a silence rather than for
 * an absence — and the base would look empty when it is merely quiet.
 *
 * **Every suggestion carries its count.** The options come from what this
 * organisation's CVs actually contain, not from a fixed vocabulary. Offering
 * "Kubernetes" when nobody holds it produces an empty result and no
 * explanation; offering "Kubernetes (2)" says what to expect before clicking.
 */
export function ProfileSearch() {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const [text, setText] = useState("");
  const [technologies, setTechnologies] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [certifications, setCertifications] = useState<string[]>([]);
  const [educationMin, setEducationMin] = useState<number | null>(null);
  const [reading, setReading] = useState<JobDescriptionReading | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ProfileSearchResult | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const facets = useQuery({
    queryKey: ["profile-facets"],
    queryFn: () => api.get<ProfileFacets>("/profiles/facets"),
    staleTime: 5 * 60 * 1000,
  });

  const empty =
    !text.trim() && !technologies.length && !languages.length && !certifications.length
    && educationMin === null;

  async function run() {
    if (empty) return;
    setBusy(true);
    try {
      setResult(
        await api.post<ProfileSearchResult>("/profiles/search", {
          text,
          technologies,
          languages,
          certifications,
          education_min: educationMin,
          limit: 25,
        }),
      );
    } catch (e) {
      toast.err("Recherche impossible", (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function readJobDescription(file: File) {
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const found = await api.upload<JobDescriptionReading>("/profiles/job-description", form);
      setReading(found);
      // Pre-filled, never applied: the recruiter sees what was understood and
      // corrects it before searching. A search running on an unseen
      // interpretation gives an answer nobody can question.
      setTechnologies(found.technologies);
      setLanguages(found.languages);
      setCertifications(found.certifications);
      setEducationMin(found.education_min);
      toast.ok("Fiche de poste lue", "Vérifiez les critères proposés avant de lancer.");
    } catch (e) {
      toast.err("Lecture impossible", (e as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function reset() {
    setText("");
    setTechnologies([]);
    setLanguages([]);
    setCertifications([]);
    setEducationMin(null);
    setReading(null);
    setResult(null);
  }

  return (
    <>
      <TopBar
        title="Recherche de profils"
        sub="Interroger la base de CVs directement, sans passer par un appel d'offres."
      />

      <div className="content grid cols-2" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card title="Décrire le besoin">
            <textarea
              className="input"
              rows={4}
              placeholder="Développeur Java expérimenté pour une mission de tierce maintenance applicative…"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="row mt" style={{ gap: 8 }}>
              <button className="btn sm" onClick={() => fileRef.current?.click()} disabled={busy}>
                📄 Depuis une fiche de poste
              </button>
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,.docx"
                hidden
                onChange={(e) => e.target.files?.[0] && readJobDescription(e.target.files[0])}
              />
              {reading && (
                <span className="tiny muted">
                  {reading.filename}
                  {reading.llm_used ? " · lue par le modèle" : " · lue par le lexique"}
                </span>
              )}
            </div>
          </Card>

          {facets.isLoading && <Card><Loading /></Card>}
          {facets.error && <Card><ErrorState error={facets.error} /></Card>}

          {facets.data && (
            <>
              <FacetPicker
                title="Technologies"
                hint="obligatoires — toutes exigées"
                options={facets.data.technologies}
                selected={technologies}
                onToggle={(v) =>
                  setTechnologies((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v]))
                }
                tone="teal"
              />

              <Card title="Niveau d'études" hint="souhaité">
                <div className="row wrap" style={{ gap: 6 }}>
                  {facets.data.education.map((option) => (
                    <button
                      key={option.value}
                      className={`chip ${educationMin === option.value ? "" : ""}`}
                      onClick={() =>
                        setEducationMin(educationMin === option.value ? null : option.value)
                      }
                      style={{
                        cursor: "pointer",
                        borderColor:
                          educationMin === option.value ? "var(--teal)" : "var(--line)",
                        color: educationMin === option.value ? "var(--teal)" : "var(--ink)",
                      }}
                    >
                      {option.label}
                      <span className="muted"> {option.count}</span>
                    </button>
                  ))}
                </div>
              </Card>

              <FacetPicker
                title="Langues"
                hint="souhaitées — 21 % des CVs en mentionnent une"
                options={facets.data.languages}
                selected={languages}
                onToggle={(v) =>
                  setLanguages((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v]))
                }
              />

              <FacetPicker
                title="Certifications"
                hint="souhaitées"
                options={facets.data.certifications}
                selected={certifications}
                onToggle={(v) =>
                  setCertifications((p) => (p.includes(v) ? p.filter((x) => x !== v) : [...p, v]))
                }
              />
            </>
          )}

          <div className="row" style={{ gap: 8 }}>
            <button className="btn primary" onClick={run} disabled={busy || empty}>
              {busy ? <Spinner /> : "⧫"} Rechercher
            </button>
            <button className="btn sm ghost" onClick={reset} disabled={busy}>
              Réinitialiser
            </button>
            {facets.data && (
              <span className="tiny muted">{facets.data.profiles} profils dans la base</span>
            )}
          </div>
        </div>

        <div className="stack">
          {!result && !busy && (
            <Card>
              <Empty icon="⧫">
                Décrivez un besoin, ou choisissez des critères. Les technologies
                sélectionnées sont obligatoires ; les autres critères font monter au
                classement sans exclure personne.
              </Empty>
            </Card>
          )}

          {busy && !result && <Card><Loading label="Comparaison des profils…" /></Card>}

          {result && (
            <Card
              title="Profils trouvés"
              hint={
                <span className="tiny muted">
                  {/* The total before the limit: twenty out of two hundred and
                      twenty out of twenty are different answers, and the user
                      is deciding whether to narrow. */}
                  {result.results.length} affichés sur {result.total}
                </span>
              }
            >
              {/* Without a described need, the score is a proximity to a bare
                  list of keywords, not a grade. Measured: a one-word query
                  against 1 200-character passages scores around 0.25 even for
                  a perfect match, and a recruiter reading 0.26 beside a valid
                  Java developer concludes the tool is wrong. The order still
                  means something — how central the skill is to the CV — so it
                  is the number that needs the caveat, not the ranking. */}
              {!result.query.text.trim() && result.results.length > 0 && (
                <div className="tiny muted mb">
                  Tous ces profils attestent les technologies demandées. Le classement
                  reflète la place qu'elles occupent dans le CV — décrivez le besoin
                  en toutes lettres pour obtenir un score comparable.
                </div>
              )}

              {result.results.length === 0 ? (
                <Empty icon="◇">
                  Aucun profil n'atteste toutes les technologies demandées.
                  Retirez-en une pour élargir.
                </Empty>
              ) : (
                <div className="stack" style={{ gap: 10 }}>
                  {result.results.map((hit, rank) => (
                    <Hit
                      key={hit.cv_id}
                      rank={rank + 1}
                      hit={hit}
                      open={open === hit.cv_id}
                      onToggle={() => setOpen(open === hit.cv_id ? null : hit.cv_id)}
                    />
                  ))}
                </div>
              )}
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

/* -------------------------------------------------------------------------- */
function FacetPicker({
  title,
  hint,
  options,
  selected,
  onToggle,
  tone = "blue",
}: {
  title: string;
  hint: string;
  options: Facet[];
  selected: string[];
  onToggle: (value: string) => void;
  tone?: string;
}) {
  const [all, setAll] = useState(false);
  // Twelve is enough to recognise the base; the rest is one click away. A wall
  // of ninety options is a wall, not a choice.
  const shown = all ? options : options.slice(0, 12);

  if (!options.length) {
    return (
      <Card title={title} hint={hint}>
        <div className="tiny muted">Aucun profil de la base n'en mentionne.</div>
      </Card>
    );
  }

  return (
    <Card title={title} hint={hint}>
      <div className="row wrap" style={{ gap: 6 }}>
        {shown.map((option) => {
          const active = selected.includes(option.value);
          return (
            <button
              key={option.value}
              className="chip"
              onClick={() => onToggle(option.value)}
              style={{
                cursor: "pointer",
                borderColor: active ? `var(--${tone})` : "var(--line)",
                color: active ? `var(--${tone})` : "var(--ink)",
                fontWeight: active ? 600 : 400,
              }}
            >
              {option.value}
              {/* The count is the point: a filter that returns nothing teaches
                  a user to distrust the screen. */}
              <span className="muted"> {option.count}</span>
            </button>
          );
        })}
        {options.length > 12 && (
          <button className="btn sm ghost" onClick={() => setAll(!all)}>
            {all ? "moins" : `+ ${options.length - 12}`}
          </button>
        )}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
function Hit({
  rank,
  hit,
  open,
  onToggle,
}: {
  rank: number;
  hit: ProfileHit;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div
      className="card"
      style={{
        padding: 0,
        overflow: "hidden",
        borderLeft: `3px solid ${hit.score >= 0.6 ? "var(--teal)" : "var(--blue)"}`,
      }}
    >
      <div
        className="row spread"
        style={{ padding: "12px 16px", cursor: "pointer" }}
        onClick={onToggle}
      >
        <div className="row" style={{ minWidth: 0, gap: 12 }}>
          <span className="mono muted" style={{ fontSize: 12, width: 18 }}>
            {rank}
          </span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13 }}>{hit.label}</div>
            <div className="tiny muted">
              {hit.education_label ?? "niveau non précisé"}
              {hit.matched_languages.length > 0 && ` · ${hit.matched_languages.join(", ")}`}
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 10 }}>
          <button
            className="btn sm ghost"
            title={`Ouvrir ${hit.filename}`}
            onClick={(event) => {
              event.stopPropagation();
              openCv(hit.cv_id);
            }}
          >
            ↓ PDF
          </button>
          <Badge color={hit.score >= 0.6 ? "teal" : "blue"}>{hit.score.toFixed(2)}</Badge>
          <span className="tiny muted">{open ? "▴" : "▾"}</span>
        </div>
      </div>

      <div style={{ padding: "0 16px 12px" }}>
        <div className="row" style={{ gap: 5, flexWrap: "wrap" }}>
          {hit.matched_technologies.map((tech) => (
            <Badge key={tech} color="teal">
              {tech}
            </Badge>
          ))}
          {/* Named, not hidden: a criterion the recruiter asked for and this
              profile does not state is exactly what they need to see. */}
          {hit.missing_languages.map((lang) => (
            <Badge key={lang} color="gray">
              {lang} non mentionné
            </Badge>
          ))}
          {!hit.meets_education && <Badge color="gray">niveau inférieur au souhait</Badge>}
        </div>

        {open && (
          <div className="mt">
            <div className="tiny muted" style={{ marginBottom: 4 }}>
              Similarité au besoin décrit
            </div>
            <Meter value={hit.similarity} color="var(--chart-1)" />
            <div className="tiny muted mt" style={{ marginBottom: 6 }}>
              Extraits du CV
            </div>
            <div className="stack" style={{ gap: 8 }}>
              {hit.evidence.map((item, index) => (
                <div
                  key={index}
                  className="tiny"
                  style={{ borderLeft: "2px solid var(--line)", paddingLeft: 10 }}
                >
                  <span className="mono" style={{ color: "var(--chart-1)" }}>
                    {item.score.toFixed(2)}
                  </span>
                  <div style={{ marginTop: 2 }}>{item.passage}</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

async function openCv(cvId: string) {
  try {
    const res = await api.get<{ url: string }>(`/cvs/${cvId}/download`);
    window.open(res.url, "_blank", "noopener");
  } catch {
    window.alert("Ce CV n'a pas pu être ouvert.");
  }
}
