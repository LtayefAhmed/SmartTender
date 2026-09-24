import { useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import type { JobMatchCandidate, JobMatchResult } from "../api/jobMatchTypes";
import { TopBar } from "../components/Layout";
import { Badge, Card, Empty, ErrorState, Loading, Meter, Spinner } from "../components/ui";
import { TagInput } from "../components/TagInput";
import { useToast } from "../components/toast";
import { CandidateComparison, EvidenceViewer } from "../components/CandidateComparison";

type Mode = "paste" | "file" | "linkedin";

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
  const [linkedinUrl, setLinkedinUrl] = useState("");
  const [linkedinBusy, setLinkedinBusy] = useState(false);
  const [linkedinError, setLinkedinError] = useState<string | null>(null);

  async function importLinkedin() {
    setLinkedinBusy(true);
    setLinkedinError(null);
    try {
      const form = new FormData();
      form.append("url", linkedinUrl.trim());
      const imported = await api.upload<{ text: string }>("/job-match/import-linkedin", form);
      setJobText(imported.text);
      setJobFile(null);
      setMode("paste");
      toast.ok("Publication importée", "Vérifiez le texte avant de lancer la recherche.");
    } catch (e) {
      setLinkedinError((e as Error).message);
    } finally {
      setLinkedinBusy(false);
    }
  }

  const [ageMin, setAgeMin] = useState("");
  const [ageMax, setAgeMax] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [certifications, setCertifications] = useState<string[]>([]);
  const [education, setEducation] = useState<string[]>([]);
  const [languages, setLanguages] = useState<string[]>([]);
  const [technologies, setTechnologies] = useState<string[]>([]);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<JobMatchResult | null>(null);
  const [showUnconfirmed, setShowUnconfirmed] = useState(true);
  const [selected, setSelected] = useState<string[]>([]);
  const [comparing, setComparing] = useState(false);
  const [evidence, setEvidence] = useState<{ candidate: JobMatchCandidate; query: string } | null>(null);
  const openEvidence = (candidate: JobMatchCandidate, query: string) => setEvidence({ candidate, query });
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

  const canSubmit = (mode === "paste" ? jobText.trim().length > 0 : mode === "file" && jobFile !== null) && !busy && !linkedinBusy;

  async function submit() {
    setBusy(true);
    setError(null);
    setResult(null);
    setSelected([]);
    setComparing(false);
    setEvidence(null);
    try {
      const form = new FormData();
      form.append("background", "true");
      form.append("limit", "0");
      if (mode === "file" && jobFile) form.append("file", jobFile);
      else form.append("text", jobText);
      if (ageMin) form.append("age_min", ageMin);
      if (ageMax) form.append("age_max", ageMax);
      if (minExperience) form.append("min_experience_years", minExperience);
      if (certifications.length) form.append("certifications", certifications.join(","));
      if (education.length) form.append("education", education.join(","));
      if (languages.length) form.append("languages", languages.join(","));
      if (technologies.length) form.append("technologies", technologies.join(","));

      const job = await api.upload<{ task_id: string }>("/job-match", form);
      const deadline = Date.now() + 20 * 60 * 1000;
      let res: JobMatchResult | undefined;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const progress = await api.get<{ status: string; result?: JobMatchResult }>(
          `/job-match/${job.task_id}`
        );
        if (progress.status === "completed" && progress.result) {
          res = progress.result;
          break;
        }
      }
      if (!res) throw new Error("La recherche prend trop de temps. Veuillez réessayer plus tard.");
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
      <div className="content grid job-match-layout" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card title="Fiche de poste">
            <div className="row tiny" style={{ gap: 14, marginBottom: 10, flexWrap: "wrap" }}>
              <label className="row" style={{ gap: 6, cursor: "pointer" }}>
                <input
                  type="radio"
                  disabled={linkedinBusy}
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
                  disabled={linkedinBusy}
                  onChange={() => {
                    setMode("file");
                    setJobText("");
                  }}
                />
                Importer un fichier
              </label>
              <label className="row" style={{ gap: 6, cursor: "pointer" }}>
                <input type="radio" checked={mode === "linkedin"} disabled={linkedinBusy}
                  onChange={() => setMode("linkedin")} />
                Lien LinkedIn
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
            ) : mode === "linkedin" ? (
              <div className="stack">
                <label className="field">
                  <span>Lien de l'offre ou de la publication</span>
                  <input className="input" type="url" value={linkedinUrl} disabled={linkedinBusy}
                    placeholder="https://www.linkedin.com/jobs/view/..."
                    onChange={(e) => { setLinkedinUrl(e.target.value); setLinkedinError(null); }} />
                </label>
                <div className="tiny muted">Le texte public sera importé pour vérification. Si une connexion est demandée, copiez le texte depuis LinkedIn.</div>
                <button className="btn" onClick={importLinkedin} disabled={!linkedinUrl.trim() || linkedinBusy || busy}>
                  {linkedinBusy ? <Spinner /> : "Importer la publication"}
                </button>
                {linkedinError && <div role="alert" className="tiny">
                  {linkedinError}
                  <button className="btn mt" onClick={() => setMode("paste")}>Coller le texte</button>
                </div>}
              </div>
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
            <p className="tiny muted">Toutes les langues et certifications sélectionnées sont requises. Pour les diplômes, une des options suffit. Une information absente reste « À vérifier ». Les profils confirmés sont affichés en premier.</p>
            <div className="row" style={{ gap: 10 }}>
              <div className="field" style={{ flex: 1 }}>
                <label>Âge min</label>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={ageMin}
                  max={120}
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
                  max={120}
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
                  max={60}
                  onChange={(e) => setMinExperience(e.target.value)}
                />
              </div>
            </div>

            <div className="field mt">
              <label>Certifications (toutes requises)</label>
              <TagInput
                value={certifications}
                onChange={setCertifications}
                placeholder="PMP, ITIL…"
                suggestions={CERTIFICATION_SUGGESTIONS}
              />
            </div>
            <div className="field mt">
              <label>Diplômes acceptés (au moins un)</label>
              <TagInput value={education} onChange={setEducation} placeholder="Master, Ingénieur…" />
            </div>
            <div className="field mt">
              <label>Langues (toutes requises)</label>
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
            <Loading label="Analyse des CV en cours… Cela peut prendre plusieurs minutes." />
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
                <span>{result.kept_total} profils aux filtres validés</span>
                <span>{result.unverified_total ?? 0} à vérifier</span>
                <span>{result.filtered_total} non conformes</span>
                <span>{result.candidates.filter(c => showUnconfirmed || !c.filtered_out).length} affichés sur {result.total_candidates ?? result.candidates.length}</span>
                {result.required_technologies.length > 0 && (
                  <span>Technologies : {result.required_technologies.join(", ")}</span>
                )}
              </div>
              <p className="tiny muted">Tous les CV sont affichés par défaut. Classement : filtres validés, puis à vérifier, puis non conformes ; dans chaque groupe, proportion de critères confirmés, puis score de correspondance décroissant. Il n'y a aucun seuil de score pour valider les filtres : chaque filtre sélectionné doit être confirmé par le CV. Une information absente reste à vérifier.</p>
              <label className="row tiny" style={{ gap: 8 }}>
                <input type="checkbox" checked={showUnconfirmed} onChange={e => setShowUnconfirmed(e.target.checked)} />
                Afficher aussi les profils à vérifier et non conformes
              </label>
              <div className="row" style={{ flexWrap: "wrap" }}>
                <button className="btn" disabled={selected.length < 2} onClick={() => setComparing(true)}>Comparer ({selected.length}/4)</button>
                <button className="btn" disabled={!selected.length} onClick={() => setSelected([])}>Effacer la sélection</button>
                <span className="tiny muted">Sélectionnez 2 à 4 profils.</span>
              </div>
              {comparing && <CandidateComparison candidates={result.candidates.filter(c => selected.includes(c.cv_id))} onClose={() => setComparing(false)} onEvidence={openEvidence} />}
              {evidence && <EvidenceViewer candidate={evidence.candidate} query={evidence.query} onClose={() => setEvidence(null)} />}
              {!showUnconfirmed && !result.candidates.some(c => !c.filtered_out) && <Empty>
                Aucun profil ne confirme tous les critères demandés. Activez l'option ci-dessus pour examiner les informations manquantes.
              </Empty>}
              <div className="stack" style={{ gap: 10 }}>
                {result.candidates.filter(c => showUnconfirmed || !c.filtered_out).map((c) => (
                  <div key={c.cv_id}>
                    <label className="row tiny"><input type="checkbox" checked={selected.includes(c.cv_id)} disabled={selected.length >= 4 && !selected.includes(c.cv_id)} onChange={() => setSelected(ids => ids.includes(c.cv_id) ? ids.filter(id => id !== c.cv_id) : ids.length < 4 ? [...ids, c.cv_id] : ids)} />Comparer {c.label}</label>
                    <CandidateCard candidate={c} onEvidence={query => openEvidence(c, query)} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function CandidateCard({ candidate: c, onEvidence }: { candidate: JobMatchCandidate; onEvidence: (query: string) => void }) {
  const [document, setDocument] = useState<{ url: string; content_type: string } | null>(null);
  const [opening, setOpening] = useState(false);
  const [documentError, setDocumentError] = useState<string | null>(null);
  async function viewCv() {
    if (document) { setDocument(null); return; }
    setOpening(true);
    setDocumentError(null);
    try {
      setDocument(await api.get<{ url: string; content_type: string }>(`/cvs/${c.cv_id}/download`));
    } catch (e) {
      setDocumentError((e as Error).message);
    } finally { setOpening(false); }
  }
  const tone =
    c.vetoed || c.filter_status === "fail" ? "red" : c.filtered_out ? "amber" : "teal";
  const statusLabel = c.vetoed
    ? "Écarté"
    : c.filter_status === "unknown" ? "À vérifier"
    : c.filter_status === "fail" ? "Non conforme"
    : c.filtered_out
    ? "À vérifier"
    : "Retenu";

  return (
    <div className="card job-match-candidate" style={{ padding: 12, borderLeft: `3px solid var(--${tone})` }}>
      <div className="row spread">
        <span style={{ fontWeight: 600 }}>{c.label}</span>
        <Badge color={tone}>{statusLabel}</Badge>
      </div>
      {c.headline && <div className="tiny muted">{c.headline}</div>}
      <button className="btn mt" onClick={viewCv} disabled={opening}>
        {opening ? "Ouverture…" : document ? "Fermer le CV" : "Voir le CV original"}
      </button>
      {documentError && <div role="alert" className="tiny mt">{documentError}</div>}
      {document && <div className="mt">
        <a href={document.url} target="_blank" rel="noopener noreferrer">Ouvrir / télécharger le CV</a>
        {document.content_type === "application/pdf" ?
          <iframe title={`CV : ${c.label}`} src={document.url} style={{ width: "100%", height: 600, border: "1px solid var(--border)", marginTop: 8 }} /> :
          <div className="tiny muted mt">Téléchargez ce document pour le consulter dans votre lecteur DOCX.</div>}
      </div>}
      <div className="row" style={{ gap: 8, marginTop: 6, alignItems: "center" }}>
        <span className="tiny muted">Correspondance :</span>
        <Meter value={c.score} />
        <span className="tiny mono">{(c.score * 100).toFixed(0)}%</span>
      </div>
      {((c.vetoed && c.veto_reason) || (c.filtered_out && !c.filter_checks?.length && c.filtered_reason)) &&
        <p className="tiny muted">{c.veto_reason || c.filtered_reason}</p>}

      {c.explanation && <div className="tiny mt">
        <strong>Pourquoi ce classement ? Rang {c.explanation.rank} sur {c.explanation.total_candidates}</strong>
        <div>Similarité du texte : {(c.explanation.text_similarity * 100).toFixed(1)} / 100
          {" · "}poids {(c.explanation.text_weight * 100).toFixed(0)} %</div>
        {c.explanation.technology_coverage !== null && <div>
          Technologies demandées retrouvées : {c.matched_technologies.length} / {c.matched_technologies.length + c.missing_technologies.length}
          {" · "}poids {(c.explanation.technology_weight * 100).toFixed(0)} %
        </div>}
        <details className="muted mt">
          <summary>Comment lire le score ?</summary>
          <p>Le score compare les CV ; ce n'est pas une probabilité de réussite. Les profils qui confirment les filtres passent en premier. Les informations absentes restent à vérifier.</p>
        </details>
        {!c.explanation.text_available && <div role="status">Texte du CV indisponible : consultez le document original.</div>}
      </div>}

      <p className="tiny"><strong>Conformité aux filtres : </strong>{c.filter_checks?.length
        ? `${c.filter_checks.filter(check => check.status === "pass").length}/${c.filter_checks.length} confirmés · ${c.filter_checks.filter(check => check.status === "unknown").length} à vérifier · ${c.filter_checks.filter(check => check.status === "fail").length} hors critères`
        : "Aucun filtre à vérifier"}</p>
      {!!c.filter_checks?.length && <div className="stack mt" style={{ gap: 8 }}>
        <strong className="tiny">Vérification des critères dans le CV</strong>
        {c.filter_checks.map((check, index) => <div key={index} className="tiny" style={{ background: "var(--panel-2)", padding: 8, borderRadius: 6 }}>
          <div className="row spread">
            <strong>{check.requested}</strong>
            <Badge color={check.status === "pass" ? "teal" : check.status === "fail" ? "red" : "amber"}>
              {check.status === "pass" ? "Confirmé dans le CV" : check.status === "fail" ? "Hors critères" : "À vérifier"}
            </Badge>
          </div>
          {check.observed != null && <div>{check.criterion === "age" ? "Âge relevé" : "Durée relevée"} : {check.observed} ans</div>}
          <div>{check.reason}</div>
          {check.evidence.map((quote, i) => <blockquote key={i} style={{ margin: "6px 0", paddingLeft: 8, borderLeft: "2px solid var(--muted)" }}>{quote}</blockquote>)}
        </div>)}
      </div>}

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
          {c.matched_technologies.map(skill => <button className="btn evidence-skill" key={skill} onClick={() => onEvidence(skill)} title="Localiser la preuve dans le PDF">{skill} ↗</button>)}
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
