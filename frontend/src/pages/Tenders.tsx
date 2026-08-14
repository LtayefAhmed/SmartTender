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

  function print() {
    if (!detail.data) return;
    printTender(detail.data);
  }

  async function downloadDocument(documentId: string) {
    try {
      const res = await api.get<{ url: string }>(
        `/tenders/${id}/documents/${documentId}/download`
      );
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
                    {/* A stored attachment is openable. Collecting a cahier des
                        charges nobody can reach is close to not collecting it. */}
                    {d.status === "stored" ? (
                      <button
                        className="tiny"
                        style={{
                          background: "transparent",
                          border: 0,
                          padding: 0,
                          color: "var(--teal)",
                          cursor: "pointer",
                          textAlign: "left",
                          textDecoration: "underline",
                        }}
                        onClick={() => openDocument(t.id, d.id, toast)}
                      >
                        ↓ {d.name ?? "document"}
                      </button>
                    ) : (
                      <span className="tiny muted" title={d.source_url ?? undefined}>
                        {d.name ?? "document"}
                      </span>
                    )}
                    <div className="row">
                      <span className="tiny muted">{fmtBytes(d.size_bytes)}</span>
                      <Badge color={statusColor(d.status)}>{d.status}</Badge>
                      {d.status === "stored" ? (
                        <button className="btn sm" onClick={() => downloadDocument(d.id)}>
                          ⭳
                        </button>
                      ) : (
                        d.source_url && (
                          <a
                            className="btn sm ghost"
                            href={d.source_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            ↗
                          </a>
                        )
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card title="Impression">
            <div className="row spread">
              <span className="tiny muted">
                Fiche complète de l'AO — informations, score, pièces jointes et liens, prête à
                imprimer ou enregistrer en PDF.
              </span>
              <button className="btn sm" onClick={print}>
                🖨️ Imprimer la fiche
              </button>
            </div>
          </Card>

          {publicationLinks(t.extra).length > 0 && (
            <Card title="Liens de publication">
              <div className="stack" style={{ gap: 6 }}>
                {publicationLinks(t.extra).map((url) => (
                  <a
                    key={url}
                    className="tiny"
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    style={{ wordBreak: "break-all" }}
                  >
                    ↗ {url}
                  </a>
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

/** Open one attachment through a short-lived presigned link.
 *
 *  The API never streams file bytes: a 25 MB PDF passing through a request
 *  handler would hold a worker for the whole transfer. It hands back a URL and
 *  the browser fetches the object store directly.
 */
async function openDocument(
  tenderId: string,
  documentId: string,
  toast: ReturnType<typeof useToast>
) {
  try {
    const res = await api.get<{ url: string }>(
      `/tenders/${tenderId}/documents/${documentId}/download`
    );
    window.open(res.url, "_blank", "noopener");
  } catch (e) {
    toast.err("Téléchargement impossible", (e as Error).message);
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Opens a separate window with a self-contained, print-ready sheet for the
 * whole tender — everything a bid manager would want on paper: the facts,
 * the score explanation, the attachments and the source links. A dedicated
 * window rather than `@media print` on the drawer itself: the drawer is a
 * fixed-height overlay clipped to the viewport, and the app's own CSS was
 * never written with a printed page in mind — this keeps the two concerns
 * (screen UI, printed sheet) from fighting each other.
 */
function printTender(t: TenderDetail) {
  const win = window.open("", "_blank", "width=900,height=1000");
  if (!win) return;

  const rows: [string, string][] = [
    ["Référence", t.reference ?? "—"],
    ["Source", t.source_key],
    ["Acheteur", t.buyer ?? "—"],
    ["Financeur", t.funding_organization ?? "—"],
    ["Pays / lieu", [t.country, t.location].filter(Boolean).join(" · ") || "—"],
    ["Secteur", t.sector ?? "—"],
    ["Type de procédure", t.procurement_type],
    ["Publication", fmtDate(t.publication_date, true)],
    ["Échéance", fmtDate(t.deadline, true)],
    ["Budget estimé", fmtMoney(t.estimated_budget, t.currency)],
    ["Statut", t.status],
    ["Pertinence", `${BAND_LABEL[t.relevance_band]}${t.relevance_score != null ? ` · ${t.relevance_score.toFixed(3)}` : ""}`],
  ];
  if (t.cpv_codes.length) rows.push(["CPV", t.cpv_codes.join(", ")]);
  if (t.seen_on_sources.length > 1) {
    rows.push(["Vu sur", `${t.seen_on_sources.join(", ")} (${t.duplicate_hits} doublon(s))`]);
  }

  const scoreRows = t.latest_score
    ? Object.entries(t.latest_score.breakdown)
        .sort((a, b) => b[1].weighted - a[1].weighted)
        .map(
          ([name, c]) => `
        <tr>
          <td>${escapeHtml(name.replace(/_/g, " "))}</td>
          <td>×${c.weight}</td>
          <td>${c.value != null ? c.value.toFixed(2) : "n/a"}</td>
          <td>${c.weighted.toFixed(3)}</td>
          <td>${escapeHtml(c.explanation)}</td>
        </tr>`
        )
        .join("")
    : "";

  const documentRows = t.documents
    .map(
      (d) => `<li>${escapeHtml(d.name ?? "document")} — ${d.status}${
        d.size_bytes ? ` — ${fmtBytes(d.size_bytes)}` : ""
      }</li>`
    )
    .join("");

  const linkRows = publicationLinks(t.extra)
    .map((url) => `<li><a href="${escapeHtml(url)}">${escapeHtml(url)}</a></li>`)
    .join("");

  win.document.write(`<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Fiche AO — ${escapeHtml(t.title)}</title>
<style>
  body { font-family: system-ui, sans-serif; color: #111; max-width: 860px; margin: 32px auto; padding: 0 20px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .meta { color: #555; font-size: 12px; margin-bottom: 24px; }
  h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .04em; color: #333; margin: 28px 0 10px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
  table.kv { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.kv td { padding: 5px 8px; vertical-align: top; }
  table.kv td:first-child { color: #666; width: 200px; }
  table.score { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.score th, table.score td { border-bottom: 1px solid #e2e2e2; padding: 6px 8px; text-align: left; }
  p.desc { font-size: 13px; line-height: 1.6; white-space: pre-wrap; }
  ul { font-size: 13px; padding-left: 20px; }
  a { color: #1a4fd6; word-break: break-all; }
  footer { margin-top: 36px; font-size: 11px; color: #999; border-top: 1px solid #ccc; padding-top: 10px; }
  @media print { body { margin: 0; } }
</style>
</head>
<body>
  <h1>${escapeHtml(t.title)}</h1>
  <div class="meta">Fiche imprimée le ${new Date().toLocaleDateString("fr-FR")} · id ${t.id}</div>

  <h2>Informations</h2>
  <table class="kv">${rows.map(([k, v]) => `<tr><td>${escapeHtml(k)}</td><td>${escapeHtml(v)}</td></tr>`).join("")}</table>

  ${t.description ? `<h2>Description</h2><p class="desc">${escapeHtml(t.description)}</p>` : ""}

  ${
    scoreRows
      ? `<h2>Explication du score (profil ${escapeHtml(t.latest_score!.profile_version)})</h2>
  <table class="score">
    <thead><tr><th>Critère</th><th>Poids</th><th>Valeur</th><th>Contribution</th><th>Explication</th></tr></thead>
    <tbody>${scoreRows}</tbody>
  </table>`
      : ""
  }

  ${documentRows ? `<h2>Pièces jointes</h2><ul>${documentRows}</ul>` : ""}
  ${linkRows ? `<h2>Liens de publication</h2><ul>${linkRows}</ul>` : ""}

  <footer>SmartTender AI — document généré automatiquement, à vérifier auprès de la source avant toute décision.</footer>
</body>
</html>`);
  win.document.close();
  win.focus();
  win.print();
}

/**
 * J360 hosts no files of its own — a tender's cahier des charges only shows
 * up once enrichment opens the notice's own page on the originating portal.
 * Those links are recorded in `extra.source_links` (see
 * `app.workers.tasks.pipeline._apply_detail`), not as typed columns, so they
 * are read defensively here rather than added to `TenderDetail`.
 *
 * `j360-ext.info` links are excluded: they are J360's own signed proxy of
 * the publication page (already read server-side to build this very drawer,
 * and expiring besides), not a source a user would want to open — the
 * originating portal's own link is what's useful here.
 */
function publicationLinks(extra: Record<string, unknown> | undefined): string[] {
  const links = extra?.["source_links"];
  if (!Array.isArray(links)) return [];
  return links.filter(
    (url): url is string =>
      typeof url === "string" && url.length > 0 && !url.includes("j360-ext.info")
  );
}

function valueColor(v: number | null): string {
  if (v == null) return "var(--muted-2)";
  if (v >= 0.75) return "var(--teal)";
  if (v >= 0.5) return "var(--amber)";
  if (v > 0) return "var(--blue)";
  return "var(--muted-2)";
}
