import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { JobMatchCandidate, JobMatchResult } from "../api/jobMatchTypes";
import { TopBar } from "../components/Layout";
import { Badge, Card, Empty, ErrorState, Loading, Meter, Spinner } from "../components/ui";
import { TagInput } from "../components/TagInput";
import { useToast } from "../components/toast";

type Mode = "paste" | "file";

//: Common certifications recruiters filter on. Not exhaustive — the field
//: still accepts free text, this only saves typing the frequent ones.
const CERTIFICATION_SUGGESTIONS = [
  "PMP",
  "Prince2",
  "ITIL",
  "Scrum Master",
  "PMI-ACP",
  "SAFe Agilist",
  "AWS Certified",
  "Azure Fundamentals",
  "Google Cloud Certified",
  "CISSP",
  "CISA",
  "TOGAF",
  "ISO 27001",
];

const LANGUAGE_SUGGESTIONS = ["Français", "Anglais", "Arabe", "Allemand", "Espagnol", "Italien"];

//: Drawn from backend/config/technologies.yaml, the vocabulary the matching
//: engine itself recognises — picking one here is picking a term the ranking
//: can actually act on.
const TECHNOLOGY_SUGGESTIONS = [
  "Java",
  "Python",
  "PHP",
  "TypeScript",
  "JavaScript",
  ".NET",
  "Symfony",
  "Angular",
  "React",
  "Vue.js",
  "Spring Boot",
  "Node.js",
  "Docker",
  "Kubernetes",
  "Terraform",
  "Jenkins",
  "GitLab",
  "Azure",
  "AWS",
  "Google Cloud",
  "PostgreSQL",
  "MongoDB",
  "Kafka",
  "SAP",
  "Salesforce",
];

export function JobMatch() {
  const toast = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<Mode>("paste");
  const [jobText, setJobText] = useState("");
  const [jobFile, setJobFile] = useState<File | null>(null);

  const [ageMin, setAgeMin] = useState("");
  const [ageMax, setAgeMax] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [certifications, setCertifications] = useState<string[]>([]);
  const [education, setEducation] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [technologies, setTechnologies] = useState<string[]>([]);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<JobMatchResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  function pickFile(f: File | null) {
    setJobFile(f);
    if (f) {
      setMode("file");
      setJobText("");
    }
  }

  function pastText(v: string) {
    setJobText(v);
    if (v) {
      setMode("paste");
      setJobFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  const canSubmit = (mode === "paste" ? jobText.trim().length > 0 : jobFile !== null) && !busy;

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const form = new FormData();
      if (mode === "file" && jobFile) form.append("file", jobFile);
      else form.append("text", jobText);
      if (ageMin) form.append("age_min", ageMin);
      if (ageMax) form.append("age_max", ageMax);
      if (minExperience) form.append("min_experience_years", minExperience);
      if (certifications.length) form.append("certifications", certifications.join(","));
      if (education.length) form.append("education", education.join(","));
      if (languages.length) form.append("languages", languages.join(","));
      if (technologies.length) form.append("technologies", technologies.join(","));

      const res = await api.upload<JobMatchResult>("/job-match", form);
      setResult(res);
      if (!res.candidates.length) {
        toast.ok("Recherche terminée", "Aucun candidat trouvé pour cette fiche de poste.");
      }
    } catch (e) {
      setError(e);
      toast.err("Recherche impossible", (e as ApiError).message ?? String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <TopBar
        title="Recherche de CVs par fiche de poste"
        sub="Collez ou importez une fiche de poste, filtrez, et retrouvez les meilleurs profils déjà importés."
      />
      <div className="content grid cols-2" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card title="Fiche de poste">
            <div className="row tiny" style={{ gap: 14, marginBottom: 10 }}>
              <label className="row" style={{ gap: 6, cursor: "pointer" }}>
                <input
                  type="radio"
                  checked={mode === "paste"}
                  onChange={() => {
                    setMode("paste");
                    setJobFile(null);
                    if (fileInputRef.current) fileInputRef.current.value = "";
                  }}
                />
                Coller le texte
              </label>
              <label className="row" style={{ gap: 6, cursor: "pointer" }}>
                <input
                  type="radio"
                  checked={mode === "file"}
                  onChange={() => {
                    setMode("file");
                    setJobText("");
                  }}
                />
                Importer un fichier
              </label>
            </div>

            {mode === "paste" ? (
              <textarea
                className="input"
                style={{ minHeight: 180, resize: "vertical" }}
                placeholder="Collez ici le texte de la fiche de poste…"
                value={jobText}
                onChange={(e) => pastText(e.target.value)}
              />
            ) : (
              <div
                className="dropzone"
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  const f = e.dataTransfer.files?.[0];
                  if (f) pickFile(f);
                }}
              >
                <div className="big">⧫</div>
                <div style={{ fontWeight: 600 }}>
                  {jobFile ? jobFile.name : "Glissez-déposez une fiche de poste"}
                </div>
                <div className="tiny muted mt">ou cliquez pour parcourir · PDF, DOCX</div>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf,.docx"
                  hidden
                  onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
                />
              </div>
            )}
          </Card>

          <Card title="Filtres">
            <div className="row" style={{ gap: 10 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>Âge min</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={ageMin}
                  onChange={(e) => setAgeMin(e.target.value)}
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Âge max</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={ageMax}
                  onChange={(e) => setAgeMax(e.target.value)}
                />
              </div>
              <div className="field" style={{ flex: 1 }}>
                <label>Expérience min (ans)</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={minExperience}
                  onChange={(e) => setMinExperience(e.target.value)}
                />
              </div>
            </div>

            <div className="field mt">
              <label>Certifications</label>
              <TagInput
                value={certifications}
                onChange={setCertifications}
                placeholder="PMP, ITIL…"
                suggestions={CERTIFICATION_SUGGESTIONS}
              />
            </div>
            <div className="field mt">
              <label>Niveau d'études</label>
              <TagInput value={education} onChange={setEducation} placeholder="Master, Ingénieur…" />
            </div>
            <div className="field mt">
              <label>Langues</label>
              <TagInput
                value={languages}
                onChange={setLanguages}
                placeholder="Français, Anglais…"
                suggestions={LANGUAGE_SUGGESTIONS}
              />
            </div>
            <div className="field mt">
              <label>Technologies</label>
              <TagInput
                value={technologies}
                onChange={setTechnologies}
                placeholder="Docker, Symfony…"
                suggestions={TECHNOLOGY_SUGGESTIONS}
              />
            </div>
          </Card>

          <button className="btn" disabled={!canSubmit} onClick={submit}>
            {busy ? <Spinner /> : "Rechercher les meilleurs profils"}
          </button>
        </div>

        <Card
          title="Résultats"
          hint={result ? `${result.candidates.length}` : undefined}
        >
          {busy ? (
            <Loading label="Recherche en cours…" />
          ) : error ? (
            <ErrorState error={error} />
          ) : !result ? (
            <Empty icon="◎">Renseignez une fiche de poste pour lancer la recherche.</Empty>
          ) : result.status === "no_text" ? (
            <Empty icon="⚠">{result.message ?? "Aucun texte exploitable."}</Empty>
          ) : !result.candidates.length ? (
            <Empty icon="⧫">Aucun candidat trouvé pour cette fiche de poste.</Empty>
          ) : (
            <div className="stack">
              <div className="row tiny muted" style={{ gap: 12, flexWrap: "wrap" }}>
                <span>{result.kept_total} retenus</span>
                <span>{result.vetoed_total} écartés</span>
                <span>{result.filtered_total} filtrés</span>
                {result.required_technologies.length > 0 && (
                  <span>Technologies : {result.required_technologies.join(", ")}</span>
                )}
              </div>
              <div className="stack" style={{ gap: 10 }}>
                {result.candidates.map((c) => (
                  <CandidateCard key={c.cv_id} candidate={c} />
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function CandidateCard({ candidate: c }: { candidate: JobMatchCandidate }) {
  const tone =
    c.vetoed ? "red" : c.filtered_out ? "amber" : "teal";
  const statusLabel = c.vetoed
    ? c.veto_reason ?? "Écarté"
    : c.filtered_out
    ? c.filtered_reason ?? "Filtré"
    : "Retenu";

  return (
    <div className="card" style={{ padding: 12, borderLeft: `3px solid var(--${tone})` }}>
      <div className="row spread">
        <span style={{ fontWeight: 600 }}>{c.label}</span>
        <Badge color={tone}>{statusLabel}</Badge>
      </div>
      {c.headline && <div className="tiny muted">{c.headline}</div>}
      <div className="row" style={{ gap: 8, marginTop: 6, alignItems: "center" }}>
        <Meter value={c.score} />
        <span className="tiny mono">{(c.score * 100).toFixed(0)}%</span>
      </div>

      {c.structured_profile && (
        <div className="tiny muted mt">
          {[
            c.structured_profile.age != null ? `${c.structured_profile.age} ans` : null,
            c.structured_profile.experience_years != null
              ? `${c.structured_profile.experience_years} ans d'expérience`
              : null,
            c.structured_profile.education,
          ]
            .filter(Boolean)
            .join(" · ")}
          {c.structured_profile.certifications.length > 0 && (
            <div>Certifications : {c.structured_profile.certifications.join(", ")}</div>
          )}
          {c.structured_profile.languages.length > 0 && (
            <div>Langues : {c.structured_profile.languages.join(", ")}</div>
          )}
        </div>
      )}

      {c.matched_technologies.length > 0 && (
        <div className="tiny mt">
          <span className="muted">Technologies trouvées : </span>
          {c.matched_technologies.join(", ")}
        </div>
      )}
      {c.missing_technologies.length > 0 && (
        <div className="tiny">
          <span className="muted">Manquantes : </span>
          {c.missing_technologies.join(", ")}
        </div>
      )}

      {c.evidence.length > 0 && (
        <details className="mt">
          <summary className="tiny muted" style={{ cursor: "pointer" }}>
            Preuves ({c.evidence.length})
          </summary>
          <div className="stack" style={{ gap: 6, marginTop: 6 }}>
            {c.evidence.map((e, i) => (
              <div key={i} className="tiny" style={{ background: "var(--panel-2)", padding: 8, borderRadius: 6 }}>
                {e.passage}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
