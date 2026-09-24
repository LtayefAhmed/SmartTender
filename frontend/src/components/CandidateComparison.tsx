import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { JobMatchCandidate } from "../api/jobMatchTypes";

export function EvidenceViewer({ candidate, query, onClose }: { candidate: JobMatchCandidate; query: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [data, setData] = useState<{ pages: { page: number; method: string; image: string; boxes: number[][] }[]; partial: boolean }>();
  const [error, setError] = useState("");
  useEffect(() => { dialog.current?.showModal(); }, []);
  useEffect(() => {
    let active = true;
    setData(undefined); setError("");
    api.post<NonNullable<typeof data>>(`/cvs/${candidate.cv_id}/evidence`, { query })
      .then(result => { if (active) setData(result); })
      .catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, [candidate.cv_id, query]);
  return <dialog ref={dialog} className="candidate-dialog" onCancel={onClose} aria-label="Preuve dans le PDF">
    <button className="btn" onClick={onClose}>Fermer la preuve</button>
    <h2>{candidate.label}</h2><p>Texte recherché : <strong>{query}</strong></p>
    {!data && !error && <p role="status">Recherche des pages et des preuves…</p>}
    {error && <p role="alert">{error}</p>}
    {data?.partial && <p>Recherche limitée : 30 pages, 5 pages scannées et 3 pages de résultats maximum. Certaines pages peuvent ne pas avoir été examinées.</p>}
    {data && !data.pages.length && <p>Aucun emplacement exact retrouvé. Consultez le CV original ; une correspondance de classement ne garantit pas une citation exacte.</p>}
    {data?.pages.map(page => <section key={page.page}>
      <h3>Page {page.page}{page.method === "ocr" ? " — reconnaissance OCR, à vérifier" : ""}</h3>
      <div className="pdf-evidence-page">
        <img src={page.image} alt={`Page ${page.page} du CV ; texte recherché : ${query}`} />
        {page.boxes.map(([x, y, w, h], i) => <span aria-hidden="true" className="pdf-evidence-highlight" key={i}
          style={{ left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` }} />)}
      </div>
    </section>)}
  </dialog>;
}

export function CandidateComparison({ candidates, onClose, onEvidence }: {
  candidates: JobMatchCandidate[]; onClose: () => void; onEvidence: (candidate: JobMatchCandidate, query: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  const rows: { label: string; render: (c: JobMatchCandidate) => React.ReactNode }[] = [
    { label: "Conformité aux filtres", render: c => {
      const checks = c.filter_checks ?? [];
      const confirmed = checks.filter(check => check.status === "pass").length;
      const percentage = checks.length ? Math.round(confirmed / checks.length * 100) : null;
      return percentage === null ? <span className="muted">Aucun filtre sélectionné</span> : <div className="comparison-score">
        <strong>{percentage} %</strong><span>{confirmed}/{checks.length} critères confirmés</span>
        <progress max={checks.length} value={confirmed} aria-label="Proportion de critères confirmés" />
        <span className="tiny muted">{checks.filter(check => check.status === "unknown").length} à vérifier · {checks.filter(check => check.status === "fail").length} hors critères</span>
      </div>;
    } },
    { label: "Correspondance texte et technologies", render: c => <span className="comparison-match">{Math.round(c.score * 100)} %</span> },
    { label: "Compétences recherchées retrouvées", render: c => c.matched_technologies.length ? c.matched_technologies.map(skill =>
      <button key={skill} className="btn evidence-skill" onClick={() => onEvidence(c, skill)}>{skill} ↗</button>) : "Aucune confirmée" },
    { label: "Expérience", render: c => c.structured_profile?.experience_years != null ? `${c.structured_profile.experience_years} ans` : "Non vérifiée" },
    { label: "Diplômes", render: c => c.structured_profile?.education || "Non vérifiés" },
    { label: "Langues", render: c => c.structured_profile?.languages.join(", ") || "Non vérifiées" },
    { label: "Certifications", render: c => c.structured_profile?.certifications.join(", ") || "Non vérifiées" },
    { label: "Exigences manquantes / à vérifier", render: c => <>
      {c.missing_technologies.length > 0 && <p>Technologies non retrouvées : {c.missing_technologies.join(", ")}</p>}
      {c.filter_checks?.filter(check => check.status !== "pass").map((check, i) => <p key={i}><strong>{check.requested} — {check.status === "unknown" ? "À vérifier" : "Hors critères"}</strong><br />{check.reason}</p>)}
      {c.veto_reason && <p>{c.veto_reason}</p>}
      {!c.missing_technologies.length && !c.filter_checks?.some(check => check.status !== "pass") && !c.veto_reason && "Aucune signalée pour les critères évalués"}
    </> },
    { label: "Extraits justificatifs", render: c => {
      const quotes = [...new Set([...(c.filter_checks ?? []).flatMap(check => check.evidence), ...c.evidence.map(e => e.passage)])];
      return quotes.length ? <details className="comparison-excerpts"><summary>Voir les extraits ({quotes.length})</summary>{quotes.map((quote, i) => <blockquote key={i}><p>{quote}</p>{quote.length <= 600 && <button className="btn" onClick={() => onEvidence(c, quote)}>Localiser dans le PDF</button>}</blockquote>)}</details> : "Aucun extrait disponible";
    } },
  ];
  return <dialog ref={dialog} className="candidate-dialog comparison-dialog" onCancel={onClose} aria-label="Comparaison des candidats">
    <header className="comparison-header">
      <div><h2>Comparer {candidates.length} candidats</h2><p className="muted">Les preuves du CV, critère par critère.</p></div>
      <button className="btn" onClick={onClose}>Fermer la comparaison</button>
    </header>
    <div className="comparison-guide"><strong>Comment lire les scores ?</strong><p>Conformité = critères confirmés ÷ critères sélectionnés. Exemple : 3/5 = 60 %. Tous les critères doivent être confirmés pour valider les filtres. La correspondance texte et technologies est un score distinct, sans seuil de validation.</p></div>
    <div className="candidate-comparison-scroll" tabIndex={0} role="region" aria-label="Tableau comparatif défilant">
      <table className="candidate-comparison" style={{ minWidth: 180 + candidates.length * 250 }}><colgroup><col style={{ width: 180 }} />{candidates.map(c => <col key={c.cv_id} />)}</colgroup><thead><tr><th scope="col">Critère</th>{candidates.map((c, i) => <th scope="col" key={c.cv_id}><span className="comparison-number">Profil {i + 1}</span><span>{c.label}</span><span className={`comparison-status ${c.filter_status ?? "unknown"}`}>{!c.filter_checks?.length ? "Aucun filtre" : c.filter_status === "pass" ? "Filtres validés" : c.filter_status === "fail" ? "Non conforme" : "À vérifier"}</span></th>)}</tr></thead>
        <tbody>{rows.map(row => <tr key={row.label}><th scope="row">{row.label}</th>{candidates.map(c => <td key={c.cv_id}>{row.render(c)}</td>)}</tr>)}</tbody>
      </table>
    </div>
  </dialog>;
}
