import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { DashboardStats, Page, TenderSummary, Source } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Meter, StatTile, Loading, ErrorState, Dot } from "../components/ui";
import { BAND_COLOR, BAND_LABEL, fmtDate, healthColor } from "../lib/format";
import type { RelevanceBand } from "../api/types";

const SOURCE_TONE = ["var(--teal)", "var(--blue)", "var(--amber)", "var(--violet)", "var(--rose)"];

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
          <StatTile label="Appels d'offres" value={s.total_tenders} tone="blue" foot="dans le référentiel" />
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
