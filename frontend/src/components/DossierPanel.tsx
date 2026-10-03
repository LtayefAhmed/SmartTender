import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type {
  GeneratedDocument,
  GenerationResult,
  TemplateKind,
} from "../api/types";
import { Badge, ErrorState, Loading } from "./ui";
import { useToast } from "./toast";

/**
 * The dossier: one document per retained profile.
 *
 * This closes the loop the platform was built for — a tender is detected,
 * profiles are ranked, a human retains some, and the files come out. Placed
 * inside the validated selection rather than on a page of its own, because
 * that is where the decision was taken and where a bid manager is standing
 * when they want the result.
 *
 * Two things are shown that a tidier screen would hide, and both are the point:
 *
 * · **What could not be filled.** A CV whose education section is empty says
 *   so here, in a list, rather than in a dossier a client opens. The platform
 *   naming its own gaps is worth more than a document that looks complete.
 *
 * · **The watermark state.** Every version is a draft until somebody approves
 *   it, and a draft that looks final is how a draft gets sent.
 */
export function DossierPanel({
  shortlistId,
  retained,
  locked,
}: {
  shortlistId: string;
  retained: number;
  locked: boolean;
}) {
  const client = useQueryClient();
  const toast = useToast();
  //: Off by default, and deliberately. The deterministic document copies what
  //: the CV says and can never be wrong about it; asking a model to reword a
  //: contractual file is a choice someone makes, not a default they inherit.
  const [adapt, setAdapt] = useState(false);
  const [kind, setKind] = useState<TemplateKind>("cv");

  const exportDossier = useMutation({
    mutationFn: () =>
      api.post<{ url: string; filename: string; pieces: number }>(
        `/shortlists/${shortlistId}/export`,
        {}
      ),
    onSuccess: (result) => {
      toast.ok(
        `Dossier exporté — ${result.pieces} pièce(s)`,
        "Lien signé à durée limitée, manifeste inclus dans l'archive."
      );
      window.open(result.url, "_blank", "noopener");
    },
    // The refusal is the useful message here: it names how many drafts are
    // still waiting, which is exactly what has to happen next.
    onError: (error: unknown) =>
      toast.err(
        "Export impossible",
        error instanceof Error ? error.message : "L'opération a échoué."
      ),
  });
  const isMatrix = kind === "matrice_conformite";
  // Only the CV adaptation is a toggle. The letter always consults the
  // model and always falls back to a plain, factual version when it
  // cannot — there is nothing for a checkbox to decide.
  const perProfileCv = kind === "cv";

  const documents = useQuery({
    queryKey: ["documents", shortlistId],
    queryFn: () =>
      api.get<{ total: number; items: GeneratedDocument[] }>(
        `/shortlists/${shortlistId}/documents`
      ),
    enabled: locked,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const generate = useMutation({
    mutationFn: () =>
      api.post<GenerationResult>(`/shortlists/${shortlistId}/documents`, {
        kind,
        // The matrix is deterministic by construction: a document whose
        // job is to say what is *missing* must not be written by
        // something that can smooth a gap into a sentence.
        adapt: kind === "cv" ? adapt : false,
      }),
    onSuccess: (result) => {
      client.invalidateQueries({ queryKey: ["documents", shortlistId] });
      toast.ok(
        `${result.produced.length} document(s) produit(s)`,
        `Gabarit ${result.template}.` +
          (result.refused.length
            ? ` ${result.refused.length} profil(s) sans document — voir la liste.`
            : "")
      );
      // Refusals are surfaced individually: "one profile produced nothing" is
      // a fact a bid manager has to act on, not a footnote in a count.
      for (const item of result.refused) {
        toast.err(`Aucun document pour ${item.label}`, item.reason);
      }
    },
    onError: (error: unknown) =>
      toast.err(
        "Génération impossible",
        error instanceof Error ? error.message : "L'opération a échoué."
      ),
  });

  if (!locked) {
    return (
      <div className="card">
        <div className="card-title">Dossier</div>
        <div className="tiny muted">
          Validez la sélection pour produire les documents. Un dossier ne se
          construit pas à partir d'une liste que personne n'a arrêtée.
        </div>
      </div>
    );
  }

  const items = documents.data?.items ?? [];

  return (
    <div className="card">
      <div className="row spread" style={{ gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: 13.5 }}>Dossier</div>
          <div className="tiny muted">
            Un CV au format du gabarit actif, par profil retenu. Chaque version
            porte la mention « en attente de validation » jusqu'à approbation.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button
            className="btn sm ghost"
            disabled={exportDossier.isPending}
            onClick={() => exportDossier.mutate()}
            title="Archive ZIP des pièces approuvées, avec son manifeste"
          >
            {exportDossier.isPending ? "…" : "⤓ Exporter le dossier"}
          </button>
          <select
            className="select"
            style={{ width: 210 }}
            value={kind}
            onChange={(event) => setKind(event.target.value as TemplateKind)}
          >
            <option value="cv">CV adaptés — un par profil</option>
            <option value="fiche_expert">Fiches experts — une par profil</option>
            <option value="lettre">Lettre d'accompagnement — une par dossier</option>
            <option value="matrice_conformite">
              Matrice de conformité — un par dossier
            </option>
          </select>
          <button
            className="btn sm primary"
            disabled={generate.isPending || retained === 0}
            onClick={() => generate.mutate()}
          >
            {generate.isPending
              ? "Production…"
              : isMatrix
                ? "Générer la matrice"
                : kind === "lettre"
                  ? "Générer la lettre"
                  : kind === "fiche_expert"
                    ? `Générer les ${retained} fiches`
                    : `Générer les ${retained} CV`}
          </button>
        </div>
      </div>

      {!perProfileCv ? (
        <div className="tiny muted" style={{ marginTop: 10 }}>
          {isMatrix ? (
            <>
              La matrice croise les exigences de l'appel d'offres avec ce que les
              profils retenus attestent.{" "}
              <strong>Aucun modèle n'intervient</strong> : un document dont le rôle
              est de dire ce qui manque ne doit pas être rédigé par quelque chose
              qui sait combler un trou par une phrase.
            </>
          ) : (
            <>
              La lettre ne peut citer que les compétences <strong>attestées</strong>{" "}
              par l'équipe retenue. Si le modèle affirme autre chose, le brouillon
              est refusé en entier et une lettre factuelle est produite à la place.
            </>
          )}
        </div>
      ) : (
      <label className="row tiny" style={{ gap: 8, marginTop: 10, cursor: "pointer" }}>
        <input
          type="checkbox"
          checked={adapt}
          onChange={(event) => setAdapt(event.target.checked)}
        />
        <span>
          <strong>Adapter les textes à l'appel d'offres</strong>
          <span className="muted">
            {" "}
            — reformule les missions dans le vocabulaire de l'AO et les
            reclasse par pertinence. Aucune compétence, date ou chiffre n'est
            ajouté : toute reformulation qui en introduit une est rejetée et
            le texte d'origine conservé.
          </span>
        </span>
      </label>
      )}

      {documents.isLoading && <Loading label="Lecture du dossier…" />}
      {documents.error && <ErrorState error={documents.error} />}

      {items.length > 0 && (
        <>
          <div className="divider" style={{ margin: "12px 0" }} />
          <div className="stack" style={{ gap: 8 }}>
            {items.map((item) => (
              <Row key={item.id} item={item} shortlistId={shortlistId} />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function Row({ item, shortlistId }: { item: GeneratedDocument; shortlistId: string }) {
  const client = useQueryClient();
  const toast = useToast();
  const approved = item.status === "approved";

  const decide = useMutation({
    mutationFn: (action: "approve" | "reject") =>
      api.post<{ status: string; indexed_passages?: number }>(
        `/documents/${item.id}/${action}`,
        {}
      ),
    onSuccess: (result, action) => {
      client.invalidateQueries({ queryKey: ["documents", shortlistId] });
      if (action === "approve") {
        toast.ok(
          "Document approuvé",
          `Filigrane retiré du fichier.` +
            (result.indexed_passages
              ? ` ${result.indexed_passages} passage(s) ajouté(s) au référentiel des réponses validées.`
              : "")
        );
      } else {
        toast.ok("Document renvoyé", "La version est conservée, marquée remplacée.");
      }
    },
    onError: (error: unknown) =>
      toast.err(
        "Décision non enregistrée",
        error instanceof Error ? error.message : "L'opération a échoué."
      ),
  });

  return (
    <div className="stack" style={{ gap: 4 }}>
      <div className="row spread" style={{ gap: 10 }}>
        <div className="row" style={{ gap: 8, minWidth: 0 }}>
          {approved ? (
            <Badge color="teal">approuvé</Badge>
          ) : (
            item.watermarked && <Badge color="amber">brouillon</Badge>
          )}
          {item.kind === "matrice_conformite" && <Badge color="violet">matrice</Badge>}
          {item.kind === "lettre" && <Badge color="rose">lettre</Badge>}
          {item.kind === "fiche_expert" && <Badge color="blue">fiche expert</Badge>}
          <span style={{ fontWeight: 600, fontSize: 13 }}>{item.label}</span>
          <span className="tiny muted mono">
            v{item.version} · {item.template ?? "gabarit inconnu"} ·{" "}
            {Math.round(item.size_bytes / 1024)} Ko
          </span>
        </div>
        <div className="row" style={{ gap: 6 }}>
          <button className="btn sm ghost" onClick={() => download(item.id)}>
            ↓ .docx
          </button>
          {!approved && item.status !== "superseded" && (
            <>
              <button
                className="btn sm teal"
                disabled={decide.isPending}
                onClick={() => decide.mutate("approve")}
                title="Retire le filigrane du fichier et verse le document au référentiel"
              >
                ✓ Approuver
              </button>
              <button
                className="btn sm ghost"
                disabled={decide.isPending}
                onClick={() => decide.mutate("reject")}
              >
                ✕ Renvoyer
              </button>
            </>
          )}
        </div>
      </div>

      {item.empty_fields.length > 0 && (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          {item.kind === "matrice_conformite" ? "Écarts relevés : " : "Sections non renseignées : "}
          {item.empty_fields.join(" · ")}
        </div>
      )}

      {/* Whether a model touched the wording. Stated on the row rather than
          buried, because it is the first thing a reviewer should know before
          reading a text that will be submitted under their name. */}
      {item.adaptation?.llm_used ? (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          ✦ Textes adaptés à l'AO — {item.adaptation.reformulated ?? 0} reformulé(s)
          {(item.adaptation.rejected ?? 0) > 0 && (
            <>
              , <strong>{item.adaptation.rejected} rejeté(s)</strong> par le contrôle
              {item.adaptation.invented_terms?.length
                ? ` (ajout refusé : ${item.adaptation.invented_terms.join(", ")})`
                : ""}
            </>
          )}
        </div>
      ) : (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          Textes repris tels quels du CV source.
        </div>
      )}

      {/* Sentences the reformulation kept but that rest on thin support in the
          source. Named rather than silently included: a document that looks
          uniformly confident is harder to review than one that points at its
          own weak spots. */}
      {/* The automatic review. Blocking findings never reach here — they are
          repaired before the file is stored — so what is shown is what a human
          still has to decide about. */}
      {item.qa && (item.qa.warnings ?? 0) > 0 && (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          Contrôle automatique : {item.qa.warnings} point(s) à vérifier
          {(item.qa.repaired_sections?.length ?? 0) > 0 && (
            <>
              {" · "}
              <strong>
                {item.qa.repaired_sections!.length} paragraphe(s) restauré(s)
              </strong>{" "}
              depuis le CV source
            </>
          )}
          <ul style={{ margin: "3px 0 0", paddingLeft: 16, lineHeight: 1.6 }}>
            {item.qa.findings
              ?.filter((f) => f.severity === "avertissement")
              .slice(0, 3)
              .map((f, index) => (
                <li key={index}>{f.message}</li>
              ))}
          </ul>
        </div>
      )}

      {approved && (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          ✓ Approuvé par {item.approved_by ?? "un opérateur"} — filigrane retiré du
          fichier, document versé au référentiel des réponses validées.
        </div>
      )}

      {(item.adaptation?.flagged?.length ?? 0) > 0 && (
        <div className="tiny muted" style={{ paddingLeft: 2 }}>
          À relire — appui faible dans le CV source :
          <ul style={{ margin: "3px 0 0", paddingLeft: 16, lineHeight: 1.6 }}>
            {item.adaptation.flagged!.map((sentence, index) => (
              <li key={index}>« {sentence} »</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

async function download(documentId: string) {
  try {
    const res = await api.get<{ url: string }>(`/documents/${documentId}/download`);
    window.open(res.url, "_blank", "noopener");
  } catch {
    window.alert("Ce document n'a pas pu être ouvert.");
  }
}
