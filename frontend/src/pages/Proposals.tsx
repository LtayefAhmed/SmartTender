import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Page, TenderSummary } from "../api/types";
import { TopBar } from "../components/Layout";
import { Card, Empty, ErrorState, Loading } from "../components/ui";
import { Combobox } from "../components/Combobox";
import { ProposalFlow, type ProposalFlowStep } from "../components/ProposalFlow";
import { ProposalSections } from "../components/ProposalSections";
import { useToast } from "../components/toast";

//: Demo pipeline state — a live wire-up later swaps this constant for polled
//: data from a real run, without touching ProposalFlow itself.
const DEMO_STEPS: ProposalFlowStep[] = [
  {
    key: "ao_analysis",
    label: "Analyse de l'AO",
    description: "Lecture du dossier, extraction des exigences",
    icon: "◫",
    status: "succeeded",
  },
  {
    key: "cv_matching",
    label: "Rapprochement des CVs",
    description: "Recherche sémantique dans la base de CVs",
    icon: "⧉",
    status: "running",
  },
  {
    key: "drafting",
    label: "Rédaction de la proposition",
    description: "Génération des sections du mémoire technique",
    icon: "◈",
    status: "pending",
  },
];

export function Proposals() {
  const toast = useToast();
  const [selected, setSelected] = useState<string[]>([]);
  const [showPreview, setShowPreview] = useState(false);

  const tenders = useQuery({
    queryKey: ["tenders", "picker"],
    queryFn: () => api.get<Page<TenderSummary>>("/tenders", { page_size: 50 }),
  });

  // Combobox is a multi-select over a preloaded vocabulary; used here in
  // effectively-single-select mode — picking a new tender replaces rather
  // than adds, since a proposal is drafted for exactly one AO at a time.
  const options = useMemo(
    () =>
      (tenders.data?.items ?? []).map((t) => ({
        name: t.title,
        hint: t.buyer ?? t.reference ?? undefined,
      })),
    [tenders.data]
  );

  function onChange(names: string[]) {
    setSelected(names.slice(-1));
  }

  function generate() {
    if (!selected.length) return;
    setShowPreview(true);
    toast.ok("Aperçu affiché", "Interface de prévisualisation — non connectée au backend pour l'instant.");
  }

  return (
    <>
      <TopBar
        title="Génération de proposition"
        sub="Aperçu de l'interface — la génération automatique arrive dans une prochaine phase."
      />
      <div className="content stack">
        <Card title="Choisir un appel d'offres">
          {tenders.isLoading ? (
            <Loading />
          ) : tenders.error ? (
            <ErrorState error={tenders.error} />
          ) : !options.length ? (
            <Empty icon="▤">Aucun appel d'offres disponible pour l'instant.</Empty>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              <Combobox
                value={selected}
                onChange={onChange}
                options={options}
                placeholder="Rechercher un appel d'offres…"
                allowFreeText={false}
              />
              <div className="row" style={{ justifyContent: "flex-end" }}>
                <button className="btn" disabled={!selected.length} onClick={generate}>
                  Générer la proposition
                </button>
              </div>
            </div>
          )}
        </Card>

        {showPreview && (
          <>
            <Card title="Structure de l'agent">
              <ProposalFlow steps={DEMO_STEPS} />
            </Card>

            <ProposalSections />
          </>
        )}
      </div>
    </>
  );
}
