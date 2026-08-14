import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { CompletenessStats, DashboardStats, Page, TenderSummary, Source } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Meter, StatTile, Loading, ErrorState, Dot } from "../components/ui";
import { BAND_COLOR, BAND_LABEL, fmtDate, healthColor } from "../lib/format";
import type { RelevanceBand } from "../api/types";

const SOURCE_TONE = ["var(--teal)", "var(--blue)", "var(--amber)", "var(--violet)", "var(--rose)"];

/**
 * "What are we missing?" — the question no other panel answers.
 *
 * Three real losses were found by hand rather than by any failure: a document
 * cap that dropped the règlement de consultation while keeping the forms,
 * archives stored but never opened, and links inside a publication that
 * nothing followed. None of them raised an error, so none of them appeared
 * anywhere. A corpus feeding CV matching cannot afford invisible gaps.
 */
function CompletenessCard() {
  const q = useQuery({
    queryKey: ["completeness"],
    queryFn: () => api.get<CompletenessStats>("/tenders/stats/completeness"),
    refetchInterval: 30000,
  });

  if (q.isLoading) return <Card title="Complétude du corpus"><Loading /></Card>;
  if (q.error) return <Card title="Complétude du corpus"><ErrorState error={q.error} /></Card>;
  const c = q.data!;

  const gaps = [
    {
      label: "Sans aucune pièce stockée",
      value: c.tenders_without_stored_document,
      hint: "l'avis seul, sans cahier des charges",
    },
    {
      label: "Sans texte exploitable",
      value: c.tenders_without_text,
      hint: "ni publication ni pièce lisible",
    },
    {
      // A publication states the object; only a dossier states the required
      // skills. The two must not be counted as one.
      label: "Sans dossier lu",
      value: c.tenders_in_scope - c.tenders_with_dossier_text,
      hint: "publication seule — insuffisant pour matcher des CV",
    },
    {
      label: "Texte trop court",
      value: c.tenders_with_thin_text,
      hint: `moins de ${c.thin_text_threshold_chars.toLocaleString("fr-FR")} caractères`,
    },
    {
      // The quietest loss of all: the tender looks complete, the character
      // count looks impressive, and the tail of the dossier is simply gone.
      label: "Texte tronqué",
      value: c.tenders_with_truncated_text,
      hint: `plafond de ${c.text_cap_chars.toLocaleString("fr-FR")} caractères atteint`,
    },
  ];
  const worst = Math.max(1, ...gaps.map((g) => g.value));

  return (
    <Card
      title="Complétude du corpus"
      hint={<span className="tiny muted">{c.tenders_in_scope} offres retenues</span>}
    >
      <div className="grid cols-2">
        <div className="stack" style={{ gap: 12 }}>
          {gaps.map((gap) => (
            <div key={gap.label} className="bar-row" style={{ marginBottom: 0 }}>
              <div className="bl">
                {gap.label}
                <div className="tiny muted">{gap.hint}</div>
              </div>
              <div className="bt">
                <Meter
                  value={gap.value / worst}
                  color={gap.value === 0 ? "var(--teal)" : "var(--amber)"}
                />
              </div>
              <div className="bv">{gap.value}</div>
            </div>
          ))}
        </div>

        <div className="stack" style={{ gap: 10 }}>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
            {Object.entries(c.documents_by_status).map(([status, count]) => (
              <Badge key={status} color={status === "stored" ? "teal" : status === "failed" ? "rose" : "gray"}>
                {count} {status}
              </Badge>
            ))}
            {Object.keys(c.documents_by_status).length === 0 && (
              <span className="muted tiny">Aucune pièce jointe collectée.</span>
            )}
          </div>
          {/* Grouped by reason so a systematic cause — an expired signature, a
              portal demanding a login — reads as one number instead of many. */}
          {c.document_failures.map((failure) => (
            <div key={failure.reason} className="row tiny">
              <Badge color="rose">{failure.count}</Badge>
              <span className="muted" style={{ marginLeft: 8 }}>{failure.reason}</span>
            </div>
          ))}
          {c.extraction_errors.map((failure) => (
            <div key={failure.reason} className="row tiny">
              <Badge color="amber">{failure.count}</Badge>
              <span className="muted" style={{ marginLeft: 8 }}>{failure.reason}</span>
            </div>
          ))}
        </div>
      </div>
    </Card>
  );
}

export function Dashboard() {
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: () => api.get<DashboardStats>("/tenders/stats/overview"),
    refetchInterval: 15000,
  });
  const urgent = useQuery({
    queryKey: ["tenders", "urgent"],
    queryFn: () =>
      api.get<Page<TenderSummary>>("/tenders", {
        bands: ["highly_relevant", "relevant"],
        sort: "deadline",
        only_open: true,
        page_size: 8,
      }),
    refetchInterval: 20000,
  });
  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<Source[]>("/sources"),
    refetchInterval: 20000,
  });

  if (stats.isLoading) return <Loading />;
  if (stats.error) return <ErrorState error={stats.error} />;
  const s = stats.data!;

  const bandOrder: RelevanceBand[] = [
    "highly_relevant",
    "relevant",
    "low_relevance",
    "out_of_scope",
  ];
  const bandTotal = Object.values(s.by_band).reduce((a, b) => a + b, 0) || 1;
  const sourceMax = Math.max(1, ...Object.values(s.by_source));

  return (
    <>
      <TopBar
        title="Tableau de bord"
        sub="Vue d'ensemble de la veille et du pipeline d'ingestion"
      />
      <div className="content stack">
        <div className="grid cols-4">
          {/* The headline is what can still be bid on. The archive stays
              visible underneath — it feeds duplicate detection and the win/loss
              history — but it is not the number to act on. */}
          <StatTile
            label="Appels d'offres ouverts"
            value={s.open_tenders ?? s.total_tenders}
            tone="blue"
            foot={
              s.archived_tenders
                ? `+ ${s.archived_tenders} archivés (échéance passée)`
                : "échéance non dépassée"
            }
          />
          <StatTile
            label="Dernières 24 h"
            value={s.ingested_last_24h}
            tone="teal"
            foot="nouvellement ingérés"
          />
          <StatTile
            label="Échéance < 7 j"
            value={s.relevant_closing_within_7_days}
            tone="amber"
            foot="pertinents & urgents"
          />
          <StatTile
            label="Très pertinents"
            value={s.by_band.highly_relevant ?? 0}
            tone="violet"
            foot="score ≥ 0,75"
          />
        </div>

        <div className="grid cols-2">
          <Card title="Répartition par pertinence" hint={`${bandTotal} évalués`}>
            <div className="stack" style={{ gap: 12 }}>
              {bandOrder.map((band) => {
                const count = s.by_band[band] ?? 0;
                const meta = s.band_metadata[band];
                return (
                  <div key={band} className="bar-row" style={{ marginBottom: 0 }}>
                    <div className="bl">
                      <Badge color={BAND_COLOR[band]}>{meta?.label ?? BAND_LABEL[band]}</Badge>
                    </div>
                    <div className="bt">
                      <Meter value={count / bandTotal} color={meta?.color ?? "var(--muted-2)"} />
                    </div>
                    <div className="bv">{count}</div>
                  </div>
                );
              })}
            </div>
          </Card>

          <Card title="Volume par source">
            {Object.keys(s.by_source).length === 0 ? (
              <div className="muted tiny">Aucune donnée pour le moment.</div>
            ) : (
              <div className="stack" style={{ gap: 12 }}>
                {Object.entries(s.by_source)
                  .sort((a, b) => b[1] - a[1])
                  .map(([source, count], i) => (
                    <div key={source} className="bar-row" style={{ marginBottom: 0 }}>
                      <div className="bl mono">{source}</div>
                      <div className="bt">
                        <Meter value={count / sourceMax} color={SOURCE_TONE[i % SOURCE_TONE.length]} />
                      </div>
                      <div className="bv">{count}</div>
                    </div>
                  ))}
              </div>
            )}
          </Card>
        </div>

        <Card
          title="Échéances proches — à traiter en priorité"
          hint={<Link to="/tenders" className="badge blue">Tout voir →</Link>}
        >
          {urgent.isLoading ? (
            <Loading />
          ) : (urgent.data?.items.length ?? 0) === 0 ? (
            <div className="muted tiny">Aucun appel d'offres pertinent à échéance rapprochée.</div>
          ) : (
            <div className="table-wrap" style={{ border: "none" }}>
              <table className="data">
                <thead>
                  <tr>
                    <th>Objet</th>
                    <th>Acheteur</th>
                    <th>Score</th>
                    <th>Échéance</th>
                    <th>Reste</th>
                  </tr>
                </thead>
                <tbody>
                  {urgent.data!.items.map((t) => (
                    <tr key={t.id}>
                      <td style={{ maxWidth: 340 }}>
                        <Link to={`/tenders?open=${t.id}`}>{t.title}</Link>
                      </td>
                      <td className="muted">{t.buyer ?? "—"}</td>
                      <td>
                        <Badge color={BAND_COLOR[t.relevance_band]}>
                          {t.relevance_score != null ? t.relevance_score.toFixed(2) : "—"}
                        </Badge>
                      </td>
                      <td className="mono tiny">{fmtDate(t.deadline, true)}</td>
                      <td>
                        {t.days_until_deadline != null && (
                          <Badge color={t.is_urgent ? "rose" : "gray"}>
                            {Math.max(0, Math.round(t.days_until_deadline))} j
                          </Badge>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <CompletenessCard />

        <Card title="Santé des sources" hint={<Link to="/sources" className="badge blue">Détails →</Link>}>
          <div className="grid cols-3">
            {(sources.data ?? []).map((src) => (
              <div key={src.key} className="row" style={{ padding: "6px 0" }}>
                <Dot color={healthColor(src.health)} />
                <span className="mono">{src.key}</span>
                <Badge color={healthColor(src.health)}>{src.health}</Badge>
                <span className="spacer" />
                <span className="tiny muted">{src.total_items_ingested} ingérés</span>
              </div>
            ))}
            {(sources.data?.length ?? 0) === 0 && (
              <div className="muted tiny">Aucune source enregistrée.</div>
            )}
          </div>
        </Card>
      </div>
    </>
  );
}
