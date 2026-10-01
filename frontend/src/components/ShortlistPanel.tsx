import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Shortlist, ShortlistDecision, ShortlistEntry } from "../api/types";
import { DossierPanel } from "./DossierPanel";
import { Badge, ErrorState, Loading } from "./ui";
import { useToast } from "./toast";

/**
 * A frozen ranking, and the decisions taken on it.
 *
 * The preview beside this recomputes on every open, which is right for
 * browsing — a ranking is per tender and never absolute. This is the other
 * thing: a capture, kept with the question it answered, so that the documents
 * generated later correspond to something a person actually approved.
 *
 * Three behaviours here are deliberate and each looks like a bug until you know
 * why:
 *
 * · A vetoed profile can still be retained. The veto is a lexical rule over a
 *   technology list read out of a document; a bid manager who knows the
 *   consultant outranks it. The row keeps its "écarté" mark afterwards, so the
 *   override reads as an override rather than disappearing into agreement.
 *
 * · Nothing is decided in bulk. There is no "retain all" button, because the
 *   whole point of the capture is that a person looked at each name they are
 *   about to put in front of a client.
 *
 * · Validation does not clear the pending rows. They stay pending — "not
 *   selected" and "turned down" are different findings, and recording the
 *   second when only the first happened would put words in the validator's
 *   mouth.
 */
export function ShortlistPanel({
  shortlistId,
  onBack,
}: {
  shortlistId: string;
  onBack: () => void;
}) {
  const client = useQueryClient();
  const toast = useToast();

  const query = useQuery({
    queryKey: ["shortlist", shortlistId],
    queryFn: () => api.get<Shortlist>(`/shortlists/${shortlistId}`),
    refetchOnWindowFocus: false,
    retry: false,
  });

  const decide = useMutation({
    mutationFn: (vars: { entryId: string; decision: ShortlistDecision; note?: string | null }) =>
      api.patch<Shortlist>(`/shortlists/${shortlistId}/entries/${vars.entryId}`, {
        decision: vars.decision,
        note: vars.note ?? null,
      }),
    onSuccess: (data) => client.setQueryData(["shortlist", shortlistId], data),
    onError: (error: unknown) => toast.err("Décision non enregistrée", errorText(error)),
  });

  const validate = useMutation({
    mutationFn: () => api.post<Shortlist>(`/shortlists/${shortlistId}/validate`, {}),
    onSuccess: (data) => {
      client.setQueryData(["shortlist", shortlistId], data);
      client.invalidateQueries({ queryKey: ["shortlists"] });
      toast.ok("Sélection validée", "Elle ne bougera plus : les documents produits s'y référeront.");
    },
    onError: (error: unknown) => toast.err("Validation refusée", errorText(error)),
  });

  if (query.isLoading) return <Loading label="Ouverture de la sélection…" />;
  if (query.error) return <ErrorState error={query.error} />;
  const data = query.data;
  if (!data) return null;

  const locked = data.status === "validated";
  const retained = data.decisions.retained ?? 0;
  const pending = data.decisions.pending ?? 0;

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row spread" style={{ gap: 12 }}>
        <button className="btn sm ghost" onClick={onBack}>
          ← Aperçu du classement
        </button>
        <div className="row" style={{ gap: 10, marginLeft: "auto" }}>
          {locked ? (
            <Badge color="teal">Validée</Badge>
          ) : (
            <button
              className="btn sm primary"
              disabled={validate.isPending}
              onClick={() => validate.mutate()}
            >
              Valider la sélection
            </button>
          )}
        </div>
      </div>

      {locked && (
        <div className="card" style={{ borderColor: "var(--teal)" }}>
          <div className="tiny">
            <strong>Sélection verrouillée</strong> — figée par{" "}
            {data.validated_by ?? "un opérateur"}
            {data.validated_at ? ` le ${formatDate(data.validated_at)}` : ""}. Les décisions
            ne sont plus modifiables : les documents produits à partir d'ici doivent
            correspondre à ce qui a été approuvé. Pour changer d'avis, figez une nouvelle
            sélection.
          </div>
        </div>
      )}

      <div className="grid cols-4">
        <div className="stat teal">
          <div className="label">Retenus</div>
          <div className="value">{retained}</div>
          <div className="foot">entreront dans le dossier</div>
        </div>
        <div className="stat">
          <div className="label">Écartés</div>
          <div className="value">{data.decisions.rejected ?? 0}</div>
          <div className="foot">refusés explicitement</div>
        </div>
        <div className="stat blue">
          <div className="label">Sans décision</div>
          <div className="value">{pending}</div>
          <div className="foot">ni vus ni jugés</div>
        </div>
        <div className="stat amber">
          <div className="label">Verrou technologique</div>
          <div className="value">{data.vetoed_total}</div>
          <div className="foot">sur l'ensemble des profils</div>
        </div>
      </div>

      {retained === 0 && locked && (
        <div className="card">
          <div className="tiny muted">
            Aucun profil retenu. C'est une conclusion valable — « nous avons regardé et
            personne ne convient » — et elle est enregistrée comme telle. Aucun document
            ne pourra être généré depuis cette sélection.
          </div>
        </div>
      )}

      {/* The dossier sits directly under the counters: once a selection is
          validated, producing the documents is the next thing anybody wants,
          and burying it under twenty candidate rows would hide the step the
          whole module exists for. */}
      <DossierPanel shortlistId={shortlistId} retained={retained} locked={locked} />

      <div className="stack" style={{ gap: 8 }}>
        {data.entries.map((entry) => (
          <Row
            key={entry.id}
            entry={entry}
            locked={locked}
            busy={decide.isPending}
            onDecide={(decision, note) =>
              decide.mutate({ entryId: entry.id, decision, note })
            }
          />
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function Row({
  entry,
  locked,
  busy,
  onDecide,
}: {
  entry: ShortlistEntry;
  locked: boolean;
  busy: boolean;
  onDecide: (decision: ShortlistDecision, note: string | null) => void;
}) {
  const [note, setNote] = useState(entry.decision_note ?? "");
  const decided = entry.decision !== "pending";

  return (
    <div
      className="card"
      style={{
        padding: "12px 16px",
        // The retained rows are the deliverable; the rest is context.
        opacity: entry.decision === "rejected" ? 0.6 : 1,
        borderColor:
          entry.decision === "retained" ? "var(--teal)" : undefined,
      }}
    >
      <div className="row spread" style={{ gap: 12 }}>
        <div className="row" style={{ minWidth: 0, gap: 12 }}>
          <span className="mono muted" style={{ fontSize: 12, width: 20 }}>
            {entry.rank}
          </span>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 600, fontSize: 13.5 }}>{entry.label}</div>
            <div className="tiny muted">
              {entry.matched_technologies.length} technologies attestées
              {entry.vetoed && entry.veto_reason ? ` · ${entry.veto_reason}` : ""}
            </div>
          </div>
        </div>

        <div className="row" style={{ gap: 8 }}>
          {entry.vetoed && <Badge color="gray">écarté par le verrou</Badge>}
          <Badge color={entry.score >= 0.6 ? "teal" : "blue"}>{entry.score.toFixed(2)}</Badge>
          {entry.cv_id && (
            <button
              className="btn sm ghost"
              title={entry.filename ?? undefined}
              onClick={() => openCvPdf(entry.cv_id!)}
            >
              ↓ PDF
            </button>
          )}
          {!locked && (
            <>
              <button
                className={entry.decision === "retained" ? "btn sm teal" : "btn sm ghost"}
                disabled={busy}
                onClick={() => onDecide("retained", note || null)}
              >
                ✓ Retenir
              </button>
              <button
                className={entry.decision === "rejected" ? "btn sm danger" : "btn sm ghost"}
                disabled={busy}
                onClick={() => onDecide("rejected", note || null)}
              >
                ✕ Écarter
              </button>
            </>
          )}
        </div>
      </div>

      {/* The reason, requested and never required. A rejection with a motive is
          labelled data for the learning loop; a mandatory field would only fill
          the column with dots. */}
      {decided && !locked && (
        <input
          className="input"
          style={{ marginTop: 10, fontSize: 12 }}
          placeholder={
            entry.vetoed && entry.decision === "retained"
              ? "Pourquoi ce profil malgré le verrou ? (recommandé)"
              : "Motif de la décision (facultatif)"
          }
          value={note}
          onChange={(event) => setNote(event.target.value)}
          onBlur={() => {
            if ((entry.decision_note ?? "") !== note) {
              onDecide(entry.decision, note || null);
            }
          }}
        />
      )}

      {decided && locked && entry.decision_note && (
        <div className="tiny muted" style={{ marginTop: 8 }}>
          « {entry.decision_note} » — {entry.decided_by ?? "opérateur"}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
async function openCvPdf(cvId: string) {
  try {
    const res = await api.get<{ url: string }>(`/cvs/${cvId}/download`);
    window.open(res.url, "_blank", "noopener");
  } catch {
    window.alert("Ce CV n'a pas pu être ouvert.");
  }
}

function formatDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString("fr-FR");
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "L'opération a échoué.";
}
