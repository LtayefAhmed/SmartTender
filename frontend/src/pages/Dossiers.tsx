import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ShortlistPage, ShortlistSummary } from "../api/types";
import { ShortlistPanel } from "../components/ShortlistPanel";
import { Badge, Empty, ErrorState, Loading } from "../components/ui";

/**
 * Every frozen selection, and the documents that came out of it.
 *
 * This page exists because the work had nowhere to live. A selection and its
 * dossier were reachable only from inside the matching modal of one tender,
 * so returning to a dossier approved yesterday meant finding the tender,
 * opening the modal, and **waiting for a full re-ranking** — ten seconds of
 * encoder and vector search — before a "Ouvrir" button appeared. The platform
 * charged a matching run to reach a document it had already produced.
 *
 * The selection is a durable object; it deserved a door of its own.
 *
 * The URL carries the open dossier (`?open=<id>`), so a row is a link
 * somebody can send to a colleague — which is what a bid manager does with
 * the thing they want a second opinion on.
 */
export function Dossiers() {
  const [params, setParams] = useSearchParams();
  const open = params.get("open");
  const [status, setStatus] = useState<string>("");

  const query = useQuery({
    queryKey: ["shortlists", "all"],
    queryFn: () => api.get<ShortlistPage>("/shortlists", { page_size: 100 }),
    refetchOnWindowFocus: false,
  });

  const items = (query.data?.items ?? []).filter(
    (item) => !status || item.status === status
  );

  if (open) {
    return (
      <div className="stack" style={{ gap: 16 }}>
        <button className="btn sm ghost" onClick={() => setParams({})}>
          ← Tous les dossiers
        </button>
        <ShortlistPanel shortlistId={open} onBack={() => setParams({})} />
      </div>
    );
  }

  return (
    <div className="stack" style={{ gap: 18 }}>
      <div>
        <h1>Dossiers</h1>
        <p className="muted" style={{ maxWidth: 760 }}>
          Chaque sélection figée, avec les décisions prises et les pièces
          produites. Reprenez un dossier là où vous l'avez laissé — sans
          relancer le rapprochement.
        </p>
      </div>

      <div className="row" style={{ gap: 8 }}>
        {[
          ["", "Tous"],
          ["validated", "Validés"],
          ["open", "En cours"],
        ].map(([value, label]) => (
          <button
            key={value}
            className={status === value ? "btn sm primary" : "btn sm ghost"}
            onClick={() => setStatus(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {query.isLoading && <Loading label="Lecture des dossiers…" />}
      {query.error && <ErrorState error={query.error} />}

      {query.data && items.length === 0 && (
        <Empty>
          Aucun dossier. Ouvrez un appel d'offres, cliquez sur ⧫ Profils, puis
          « Figer cette sélection ».
        </Empty>
      )}

      <div className="stack" style={{ gap: 10 }}>
        {items.map((item) => (
          <Row key={item.id} item={item} onOpen={() => setParams({ open: item.id })} />
        ))}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function Row({
  item,
  onOpen,
}: {
  item: ShortlistSummary & { documents?: number };
  onOpen: () => void;
}) {
  const validated = item.status === "validated";
  const retained = item.decisions?.retained ?? 0;
  const pending = item.decisions?.pending ?? 0;

  return (
    <button className="dossier" onClick={onOpen}>
      <div className="row spread" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 8, minWidth: 0 }}>
          <Badge color={validated ? "teal" : "amber"}>
            {validated ? "validé" : "en cours"}
          </Badge>
          <span className="dossier-title">
            {item.tender_title ?? item.label ?? "Appel d'offres supprimé"}
          </span>
        </div>
        <span className="tiny muted mono">
          {item.created_at ? new Date(item.created_at).toLocaleDateString("fr-FR") : ""}
        </span>
      </div>

      {/* The counts that decide what to do next. "Sans décision" is listed
          because it is the actionable one: a selection with twenty untouched
          rows was skimmed, not judged. */}
      <div className="dossier-facts">
        <span>
          <strong>{retained}</strong> retenu{retained > 1 ? "s" : ""}
        </span>
        {pending > 0 && (
          <span className="warn">
            <strong>{pending}</strong> sans décision
          </span>
        )}
        <span>
          <strong>{item.documents ?? 0}</strong> pièce
          {(item.documents ?? 0) > 1 ? "s" : ""}
        </span>
        <span className="muted">
          {item.vetoed_total} écarté{item.vetoed_total > 1 ? "s" : ""} par le verrou
        </span>
        {item.validated_by && (
          <span className="muted">validé par {item.validated_by}</span>
        )}
      </div>
    </button>
  );
}
