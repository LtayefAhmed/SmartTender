import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Page, TenderSummary, TenderDetail, RelevanceBand } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Meter, Loading, ErrorState, Empty, Spinner } from "../components/ui";
import { Drawer } from "../components/Drawer";
import { useToast } from "../components/toast";
import {
  BAND_COLOR,
  BAND_LABEL,
  fmtBytes,
  fmtDate,
  fmtMoney,
  statusColor,
} from "../lib/format";

const BANDS: RelevanceBand[] = ["highly_relevant", "relevant", "low_relevance"];
const PAGE_SIZE = 20;

export function Tenders() {
  const [params, setParams] = useSearchParams();
  const [q, setQ] = useState("");
  const [bands, setBands] = useState<RelevanceBand[]>([]);
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [sort, setSort] = useState("-relevance_score");
  const [page, setPage] = useState(1);
  const openId = params.get("open");

  useEffect(() => {
    setPage(1);
  }, [q, bands, onlyOpen, sort]);

  const list = useQuery({
    queryKey: ["tenders", { q, bands, onlyOpen, sort, page }],
    queryFn: () =>
      api.get<Page<TenderSummary>>("/tenders", {
        q,
        bands,
        only_open: onlyOpen,
        sort,
        page,
        page_size: PAGE_SIZE,
      }),
    refetchInterval: 15000,
  });

  function toggleBand(b: RelevanceBand) {
    setBands((prev) => (prev.includes(b) ? prev.filter((x) => x !== b) : [...prev, b]));
  }

  function setSortField(field: string) {
    setSort((prev) => (prev === `-${field}` ? field : `-${field}`));
  }

  const total = list.data?.total ?? 0;
  const pages = Math.ceil(total / PAGE_SIZE);

  return (
    <>
      <TopBar
        title="Appels d'offres"
        sub={`${total} résultat${total > 1 ? "s" : ""} · cliquer une ligne pour le détail et le scoring`}
      />
      <div className="content stack">
        <Card>
          <div className="row wrap" style={{ gap: 12 }}>
            <input
              className="input"
              style={{ maxWidth: 320 }}
              placeholder="Rechercher objet, acheteur, référence…"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <div className="row" style={{ gap: 6 }}>
              {BANDS.map((b) => (
                <button
                  key={b}
                  className={`badge ${bands.includes(b) ? BAND_COLOR[b] : "gray"}`}
                  style={{ cursor: "pointer", opacity: bands.length && !bands.includes(b) ? 0.5 : 1 }}
                  onClick={() => toggleBand(b)}
                >
                  {BAND_LABEL[b]}
                </button>
              ))}
            </div>
            <label className="row tiny muted" style={{ cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={onlyOpen}
                onChange={(e) => setOnlyOpen(e.target.checked)}
              />
              Uniquement ouverts
            </label>
            <span className="spacer" />
            {list.isFetching && <Spinner />}
          </div>
        </Card>

        {list.isLoading ? (
          <Loading />
        ) : list.error ? (
          <ErrorState error={list.error} />
        ) : total === 0 ? (
          <Empty icon="▤">
            <div>Aucun appel d'offres.</div>
            <div className="tiny mt">Lancez un scraping ou importez un document pour commencer.</div>
          </Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th className="th-sort" onClick={() => setSortField("title")}>
                      Objet
                    </th>
                    <th>Acheteur</th>
                    <th>Source</th>
                    <th className="th-sort" onClick={() => setSortField("relevance_score")}>
                      Score {sort.includes("relevance_score") ? (sort[0] === "-" ? "↓" : "↑") : ""}
                    </th>
                    <th className="th-sort" onClick={() => setSortField("deadline")}>
                      Échéance {sort.includes("deadline") ? (sort[0] === "-" ? "↓" : "↑") : ""}
                    </th>
                    <th>État</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data!.items.map((t) => (
                    <tr key={t.id} onClick={() => setParams({ open: t.id })}>
                      <td style={{ maxWidth: 360 }}>
                        <div style={{ fontWeight: 500 }}>{t.title}</div>
                        {t.reference && <div className="tiny muted mono">{t.reference}</div>}
                      </td>
                      <td className="muted" style={{ maxWidth: 180 }}>
                        {t.buyer ?? "—"}
                      </td>
                      <td>
                        <span className="badge gray mono">{t.source_key}</span>
                        {t.duplicate_hits > 0 && (
                          <span className="badge violet tiny" title="Vu sur plusieurs portails">
                            ×{t.duplicate_hits + 1}
                          </span>
                        )}
                      </td>
                      <td>
                        <div className="row" style={{ gap: 8, minWidth: 120 }}>
                          <Badge color={BAND_COLOR[t.relevance_band]}>
                            {t.relevance_score != null ? t.relevance_score.toFixed(2) : "—"}
                          </Badge>
                        </div>
                      </td>
                      <td className="mono tiny">
                        {fmtDate(t.deadline)}
                        {t.is_urgent && <div className="badge rose tiny">urgent</div>}
                      </td>
                      <td>
                        <Badge color={statusColor(t.pipeline_state)}>{t.pipeline_state}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {pages > 1 && (
              <div className="row spread">
                <span className="tiny muted">
                  Page {page} / {pages}
                </span>
                <div className="row">
                  <button className="btn sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                    ← Précédent
                  </button>
                  <button
                    className="btn sm"
                    disabled={page >= pages}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    Suivant →
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {openId && <TenderDrawer id={openId} onClose={() => setParams({})} />}
    </>
  );
}

function TenderDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const toast = useToast();
  const detail = useQuery({
    queryKey: ["tender", id],
    queryFn: () => api.get<TenderDetail>(`/tenders/${id}`),
  });

  async function download() {
    try {
      const res = await api.get<{ url: string }>(`/tenders/${id}/download`);
      window.open(res.url, "_blank");
    } catch (e: any) {
      toast.err("Téléchargement indisponible", e.message);
    }
  }

  const t = detail.data;
  return (
    <Drawer
      open
      onClose={onClose}
      head={
        <div className="row spread">
          <div style={{ minWidth: 0 }}>
            <div className="tiny muted mono">{t?.source_key ?? ""} · {t?.reference ?? ""}</div>
            <h2 style={{ fontSize: 16, marginTop: 4 }}>{t?.title ?? "Chargement…"}</h2>
          </div>
          <button className="btn sm ghost" onClick={onClose}>
            ✕
          </button>
        </div>
      }
    >
      {detail.isLoading ? (
        <Loading />
      ) : detail.error ? (
        <ErrorState error={detail.error} />
      ) : t ? (
        <div className="stack">
          <div className="row wrap">
            <Badge color={BAND_COLOR[t.relevance_band]}>
              {BAND_LABEL[t.relevance_band]}
              {t.relevance_score != null ? ` · ${t.relevance_score.toFixed(3)}` : ""}
            </Badge>
            <Badge color={statusColor(t.status)}>{t.status}</Badge>
            <Badge color={statusColor(t.pipeline_state)}>{t.pipeline_state}</Badge>
            {t.storage_key && (
              <button className="btn sm" onClick={download}>
                ⭳ Document original
              </button>
            )}
            {t.source_url && (
              <a className="btn sm" href={t.source_url} target="_blank" rel="noreferrer">
                ↗ Source
              </a>
            )}
          </div>

          <dl className="kv">
            <dt>Acheteur</dt>
            <dd>{t.buyer ?? "—"}</dd>
            <dt>Financeur</dt>
            <dd>{t.funding_organization ?? "—"}</dd>
            <dt>Pays / lieu</dt>
            <dd>{[t.country, t.location].filter(Boolean).join(" · ") || "—"}</dd>
            <dt>Secteur</dt>
            <dd>{t.sector ?? "—"}</dd>
            <dt>Type</dt>
            <dd>{t.procurement_type}</dd>
            <dt>Publication</dt>
            <dd>{fmtDate(t.publication_date, true)}</dd>
            <dt>Échéance</dt>
            <dd>
              {fmtDate(t.deadline, true)}
              {t.days_until_deadline != null &&
                ` (${Math.round(t.days_until_deadline)} j)`}
            </dd>
            <dt>Budget estimé</dt>
            <dd>{fmtMoney(t.estimated_budget, t.currency)}</dd>
            {t.cpv_codes.length > 0 && (
              <>
                <dt>CPV</dt>
                <dd className="mono tiny">{t.cpv_codes.join(", ")}</dd>
              </>
            )}
            {t.seen_on_sources.length > 1 && (
              <>
                <dt>Vu sur</dt>
                <dd>{t.seen_on_sources.join(", ")} ({t.duplicate_hits} doublon(s))</dd>
              </>
            )}
          </dl>

          {t.latest_score && (
            <Card title="Explication du score" hint={`profil ${t.latest_score.profile_version}`}>
              {Object.entries(t.latest_score.breakdown)
                .sort((a, b) => b[1].weighted - a[1].weighted)
                .map(([name, c]) => (
                  <div className="crit" key={name}>
                    <div className="crit-head">
                      <span className="crit-name">{name.replace(/_/g, " ")}</span>
                      <span className="crit-w">×{c.weight}</span>
                      <span className="crit-v" style={{ color: valueColor(c.value) }}>
                        {c.value != null ? c.value.toFixed(2) : "n/a"} → {c.weighted.toFixed(3)}
                      </span>
                    </div>
                    {c.value != null && (
                      <div className="mb">
                        <Meter value={c.value} color={valueColor(c.value)} />
                      </div>
                    )}
                    <div className="crit-expl">{c.explanation}</div>
                  </div>
                ))}
              {t.latest_score.band === "out_of_scope" && (
                <div className="badge rose mt">Vetoé — hors périmètre (mot-clé bloquant)</div>
              )}
            </Card>
          )}

          {t.description && (
            <Card title="Texte extrait" hint={t.extra?.["extraction_method"] as string}>
              <pre className="text-preview">{t.description}</pre>
            </Card>
          )}

          {t.documents.length > 0 && (
            <Card title={`Pièces jointes (${t.documents.length})`}>
              <div className="stack" style={{ gap: 8 }}>
                {t.documents.map((d) => (
                  <div key={d.id} className="row spread">
                    <span className="tiny">{d.name ?? "document"}</span>
                    <div className="row">
                      <span className="tiny muted">{fmtBytes(d.size_bytes)}</span>
                      <Badge color={statusColor(d.status)}>{d.status}</Badge>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <div className="tiny muted mono">id: {t.id}</div>
        </div>
      ) : null}
    </Drawer>
  );
}

function valueColor(v: number | null): string {
  if (v == null) return "var(--muted-2)";
  if (v >= 0.75) return "var(--teal)";
  if (v >= 0.5) return "var(--amber)";
  if (v > 0) return "var(--blue)";
  return "var(--muted-2)";
}
