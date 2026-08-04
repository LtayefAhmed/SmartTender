import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type {
  AcceptedResponse,
  ConnectorInfo,
  ConnectorRun,
  Page,
  ScrapeJob,
} from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Meter, Loading, Spinner, Empty } from "../components/ui";
import { TagInput } from "../components/TagInput";
import { DomainPicker } from "../components/DomainPicker";
import { useToast } from "../components/toast";
import {
  fmtDuration,
  fmtRelative,
  isInactiveByDesign,
  statusColor,
  unavailableLabel,
} from "../lib/format";

export function Scrape() {
  const toast = useToast();
  const qc = useQueryClient();

  const registry = useQuery({
    queryKey: ["sources", "registry"],
    queryFn: () =>
      api.get<{ connectors: ConnectorInfo[]; available: string[]; errors: Record<string, string> }>(
        "/sources/registry"
      ),
  });

  const [selected, setSelected] = useState<string[]>([]);
  const [keywords, setKeywords] = useState<string[]>([]);
  const [countries, setCountries] = useState<string[]>([]);
  const [sectors, setSectors] = useState<string[]>([]);
  const [maxResults, setMaxResults] = useState(20);
  const [publishedWithin, setPublishedWithin] = useState<number | "">("");
  const [launching, setLaunching] = useState(false);

  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.get<Page<ScrapeJob>>("/scrape/jobs", { page_size: 8 }),
    refetchInterval: 3000, // live progress
  });

  async function launch() {
    setLaunching(true);
    try {
      const res = await api.post<AcceptedResponse>("/scrape", {
        connectors: selected,
        filters: {
          keywords,
          countries,
          sectors,
          max_results_per_source: maxResults,
          ...(publishedWithin ? { published_within_days: publishedWithin } : {}),
        },
      });
      toast.ok("Scraping lancé", res.message);
      qc.invalidateQueries({ queryKey: ["jobs"] });
    } catch (e) {
      if (e instanceof ApiError && e.detail) {
        toast.err("Impossible de lancer", `${e.message} — disponibles: ${(e.detail.available as string[])?.join(", ")}`);
      } else {
        toast.err("Impossible de lancer", (e as Error).message);
      }
    } finally {
      setLaunching(false);
    }
  }

  const available = registry.data?.connectors.filter((c) => c.available) ?? [];
  // Only sources whose unavailability is actionable are worth a line here.
  // A test connector that production images deliberately exclude is noise on
  // a screen whose job is "launch a search" — it reads as a broken source.
  const unavailable = (registry.data?.connectors ?? []).filter(
    (c) => !c.available && !isInactiveByDesign(c.unavailable_reason)
  );

  return (
    <>
      <TopBar
        title="Lancer un scraping"
        sub="Entrée A · recherche filtrée, une tâche isolée par source"
      />
      <div className="content grid cols-2" style={{ alignItems: "start" }}>
        <Card title="Nouvelle recherche" className="pad-lg">
          {registry.isLoading ? (
            <Loading />
          ) : (
            <>
              <div className="field">
                <label>Sources ({selected.length ? selected.length : "toutes les disponibles"})</label>
                <div className="chips">
                  {available.map((c) => (
                    <button
                      key={c.key}
                      className={`chip`}
                      style={{
                        cursor: "pointer",
                        borderColor: selected.includes(c.key) ? "var(--blue)" : "var(--line)",
                        background: selected.includes(c.key) ? "rgba(91,140,255,.14)" : "var(--panel-2)",
                      }}
                      onClick={() =>
                        setSelected((prev) =>
                          prev.includes(c.key) ? prev.filter((k) => k !== c.key) : [...prev, c.key]
                        )
                      }
                    >
                      {c.key} <span className="tiny muted">({c.strategy})</span>
                    </button>
                  ))}
                </div>
                {unavailable.length > 0 && (
                  <div className="tiny muted mt">
                    {unavailable.map((c) => (
                      <div key={c.key}>
                        <b>{c.key}</b> indisponible — {unavailableLabel(c.unavailable_reason)}
                        {c.missing_credentials?.length ? ` (${c.missing_credentials.join(", ")})` : ""}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="field">
                <label>Domaines Inetum</label>
                <DomainPicker selected={keywords} onChange={setKeywords} />
              </div>

              <div className="field">
                <label>Mots-clés envoyés aux portails</label>
                <TagInput value={keywords} onChange={setKeywords} placeholder="développement, maintenance…" />
                <div className="tiny muted" style={{ marginTop: 4 }}>
                  Alimentés par les domaines ci-dessus, ou saisis librement.
                </div>
              </div>
              <div className="grid cols-2" style={{ gap: 14 }}>
                <div className="field">
                  <label>Pays</label>
                  <TagInput value={countries} onChange={setCountries} placeholder="Tunisie…" />
                </div>
                <div className="field">
                  <label>Secteurs</label>
                  <TagInput value={sectors} onChange={setSectors} placeholder="Technologies…" />
                </div>
              </div>
              <div className="grid cols-2" style={{ gap: 14 }}>
                <div className="field">
                  <label>Résultats souhaités / source</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={500}
                    value={maxResults}
                    onChange={(e) => setMaxResults(Number(e.target.value))}
                  />
                  <div className="tiny muted" style={{ marginTop: 4 }}>
                    Nombre d'offres <b>correspondant à vos filtres</b>. Le scraper
                    parcourt autant de pages que nécessaire pour les atteindre.
                  </div>
                </div>
                <div className="field">
                  <label>Publié depuis (jours)</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    placeholder="illimité"
                    value={publishedWithin}
                    onChange={(e) => setPublishedWithin(e.target.value ? Number(e.target.value) : "")}
                  />
                </div>
              </div>

              <button className="btn primary" style={{ width: "100%" }} onClick={launch} disabled={launching}>
                {launching ? <Spinner /> : "⧉"} Lancer le scraping
              </button>
              <div className="tiny muted mt">
                Renvoie immédiatement · le pipeline ne bloque jamais. Suivez la progression à droite.
              </div>
            </>
          )}
        </Card>

        <Card title="Tâches récentes" hint={jobs.isFetching ? <Spinner /> : "actualisation auto"}>
          {jobs.isLoading ? (
            <Loading />
          ) : (jobs.data?.items.length ?? 0) === 0 ? (
            <Empty icon="⧉">Aucune tâche pour le moment.</Empty>
          ) : (
            <div className="stack" style={{ gap: 12 }}>
              {jobs.data!.items.map((job) => (
                <JobCard key={job.id} job={job} />
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}

function JobCard({ job }: { job: ScrapeJob }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="card" style={{ padding: 14, background: "var(--panel-2)" }}>
      <div className="row spread" style={{ cursor: "pointer" }} onClick={() => setOpen((o) => !o)}>
        <div className="row">
          <Badge color={statusColor(job.status)}>{job.status}</Badge>
          <span className="tiny muted mono">{job.trigger}</span>
        </div>
        <span className="tiny muted">{fmtRelative(job.created_at)}</span>
      </div>

      <div className="mt mb">
        <Meter
          value={job.progress}
          color={
            job.status === "failed"
              ? "var(--red)"
              : job.status === "partial"
              ? "var(--amber)"
              : "var(--teal)"
          }
        />
      </div>

      <div className="row wrap tiny muted" style={{ gap: 12 }}>
        <span>✅ {job.items_ingested} ingérés</span>
        <span>⧉ {job.items_found} trouvés</span>
        <span>◈ {job.items_duplicate} doublons</span>
        <span>✕ {job.items_rejected} rejetés</span>
        {job.duration_seconds != null && <span>{fmtDuration(job.duration_seconds)}</span>}
      </div>

      {open && (
        <div className="mt stack" style={{ gap: 8 }}>
          <div className="divider" style={{ margin: "4px 0" }} />
          {job.runs.map((r) => (
            <div key={r.id} className="stack" style={{ gap: 2 }}>
              <div className="row spread tiny">
                <div className="row">
                  <Badge color={statusColor(r.status)}>{r.connector_key}</Badge>
                  {r.error_type && (
                    <span className="badge red tiny" title={r.error_message ?? ""}>
                      {r.error_type}
                    </span>
                  )}
                </div>
                <span className="muted">
                  {r.items_ingested}/{r.items_found} · {r.pages_fetched}p · {r.http_requests} req
                </span>
              </div>
              <RunDiagnosis run={r} />
            </div>
          ))}
          {job.errors.length > 0 && (
            <div className="tiny" style={{ color: "var(--red)" }}>
              {job.errors.map((e, i) => (
                <div key={i}>
                  {e.connector}: {e.message}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Explains a run that found nothing.
 *
 * `items_found: 0` is the same number whether the portal had nothing matching
 * your filters or the selectors broke and the crawl went silently blind — and
 * those need opposite responses. `records_parsed` is what tells them apart, so
 * it is shown exactly when the answer is not obvious from the headline count.
 */
function RunDiagnosis({ run }: { run: ConnectorRun }) {
  const parsed = run.extra?.records_parsed;
  const filtered = run.extra?.items_filtered_out ?? 0;
  const dupes = run.extra?.items_duplicate_in_run ?? 0;

  if (run.extra?.skip_reason) {
    return <span className="tiny muted">ignoré — {run.extra.skip_reason}</span>;
  }
  if (parsed == null) return null;

  if (parsed === 0 && run.pages_fetched > 0) {
    return (
      <span className="tiny" style={{ color: "var(--red)" }}>
        ⚠ {run.pages_fetched} page(s) lues, 0 ligne extraite — sélecteurs probablement obsolètes
      </span>
    );
  }

  return (
    <div className="tiny muted">
      {run.items_found === 0 && (
        <div>
          {parsed} ligne(s) extraite(s), {filtered} écartée(s) par vos filtres
          {dupes > 0 && `, ${dupes} doublon(s)`} — les sélecteurs fonctionnent
        </div>
      )}
      <StopReason reason={run.extra?.stop_reason} parsed={parsed} />
    </div>
  );
}

/**
 * Says whether the result set is complete.
 *
 * "The portal had nothing more" and "we stopped at our own safety cap" look
 * identical from a result count, and lead to opposite conclusions — one means
 * you have seen everything, the other means you have not. Saying which is the
 * difference between trusting the number and being misled by it.
 */
function StopReason({ reason, parsed }: { reason?: string | null; parsed: number }) {
  if (!reason) return null;
  if (reason === "source_exhausted") {
    return (
      <div style={{ color: "var(--teal)" }}>
        ✓ Source parcourue entièrement — {parsed} offre(s) au total, il n'y en a pas d'autres
      </div>
    );
  }
  if (reason === "results_satisfied") {
    return <div>✓ Nombre de résultats demandé atteint — d'autres offres existent au-delà</div>;
  }
  if (reason === "page_cap") {
    return (
      <div style={{ color: "var(--amber, #FFB454)" }}>
        ⚠ Plafond de pages atteint — résultat partiel, la source en contient probablement plus
      </div>
    );
  }
  if (reason === "deadline") {
    return (
      <div style={{ color: "var(--amber, #FFB454)" }}>
        ⚠ Temps imparti écoulé — résultat partiel
      </div>
    );
  }
  return null;
}
