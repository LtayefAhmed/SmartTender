import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ConnectorInfo, Source } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Dot, Loading } from "../components/ui";
import { useToast } from "../components/toast";
import { fmtDuration, fmtRelative, healthColor } from "../lib/format";

export function Sources() {
  const toast = useToast();
  const qc = useQueryClient();

  const sources = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.get<Source[]>("/sources"),
    refetchInterval: 15000,
  });
  const registry = useQuery({
    queryKey: ["sources", "registry"],
    queryFn: () =>
      api.get<{ connectors: ConnectorInfo[]; errors: Record<string, string> }>("/sources/registry"),
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["sources"] });
  };
  const toggle = useMutation({
    mutationFn: (key: string) => api.post<Source>(`/sources/${key}/toggle`),
    onSuccess: invalidate,
  });
  const resetCircuit = useMutation({
    mutationFn: (key: string) => api.post<Source>(`/sources/${key}/reset-circuit`),
    onSuccess: () => {
      toast.ok("Circuit réinitialisé");
      invalidate();
    },
  });
  const sync = useMutation({
    mutationFn: () => api.post<{ created: number; updated: number }>("/sources/sync"),
    onSuccess: (r) => {
      toast.ok("Configuration rechargée", `${r.created} créées, ${r.updated} mises à jour`);
      invalidate();
      qc.invalidateQueries({ queryKey: ["sources", "registry"] });
    },
  });

  const infoByKey = new Map((registry.data?.connectors ?? []).map((c) => [c.key, c]));

  return (
    <>
      <TopBar
        title="Sources & santé"
        sub="État des connecteurs · pourquoi une source ne tourne pas · disjoncteurs"
        actions={
          <button className="btn" onClick={() => sync.mutate()} disabled={sync.isPending}>
            ⟳ Recharger la config
          </button>
        }
      />
      <div className="content stack">
        {sources.isLoading ? (
          <Loading />
        ) : (
          <div className="grid cols-2">
            {(sources.data ?? []).map((s) => {
              const info = infoByKey.get(s.key);
              return (
                <Card key={s.key}>
                  <div className="row spread mb">
                    <div className="row">
                      <Dot color={healthColor(s.health)} />
                      <span style={{ fontWeight: 600 }}>{s.name}</span>
                    </div>
                    <Badge color={healthColor(s.health)}>{s.health}</Badge>
                  </div>

                  <div className="row wrap tiny muted mb" style={{ gap: 10 }}>
                    <span className="mono">{s.key}</span>
                    {s.strategy && <span className="badge gray tiny">{s.strategy}</span>}
                    {s.country && <span>{s.country}</span>}
                    {info && !info.available && (
                      <span className="badge violet tiny" title={info.missing_credentials?.join(", ")}>
                        {info.unavailable_reason}
                      </span>
                    )}
                  </div>

                  {s.health_reason && (
                    <div
                      className="tiny mb"
                      style={{
                        color: "var(--amber)",
                        background: "rgba(255,180,84,.07)",
                        padding: "8px 10px",
                        borderRadius: 8,
                      }}
                    >
                      {s.health_reason}
                    </div>
                  )}

                  {info && info.missing_credentials?.length > 0 && (
                    <div className="tiny muted mb">
                      À configurer :{" "}
                      {info.missing_credentials.map((v) => (
                        <div key={v} className="mono" style={{ fontSize: 10.5 }}>
                          {v}
                        </div>
                      ))}
                    </div>
                  )}

                  <div className="grid cols-3 tiny" style={{ gap: 8, margin: "10px 0" }}>
                    <Metric label="Exécutions" value={s.total_runs} />
                    <Metric
                      label="Taux succès"
                      value={s.success_rate != null ? `${Math.round(s.success_rate * 100)}%` : "—"}
                    />
                    <Metric label="Ingérés" value={s.total_items_ingested} />
                    <Metric label="Trouvés" value={s.total_items_found} />
                    <Metric
                      label="Doublons"
                      value={s.duplicate_ratio != null ? `${Math.round(s.duplicate_ratio * 100)}%` : "—"}
                    />
                    <Metric label="Dernier" value={fmtRelative(s.last_run_at)} />
                  </div>

                  {s.last_error_type && (
                    <div className="tiny" style={{ color: "var(--red)" }}>
                      Dernière erreur : {s.last_error_type}
                    </div>
                  )}

                  <div className="row mt" style={{ gap: 8 }}>
                    <Badge color={s.circuit_state === "closed" ? "green" : "red"}>
                      circuit {s.circuit_state}
                    </Badge>
                    {s.last_duration_seconds != null && (
                      <span className="tiny muted">{fmtDuration(s.last_duration_seconds)}</span>
                    )}
                    <span className="spacer" />
                    {s.circuit_state !== "closed" && (
                      <button className="btn sm" onClick={() => resetCircuit.mutate(s.key)}>
                        Réarmer
                      </button>
                    )}
                    <button className="btn sm" onClick={() => toggle.mutate(s.key)}>
                      {s.enabled ? "Désactiver" : "Activer"}
                    </button>
                  </div>
                </Card>
              );
            })}
          </div>
        )}

        {registry.data && Object.keys(registry.data.errors).length > 0 && (
          <Card title="Erreurs de configuration">
            {Object.entries(registry.data.errors).map(([k, v]) => (
              <div key={k} className="tiny" style={{ color: "var(--red)" }}>
                <span className="mono">{k}</span>: {v}
              </div>
            ))}
          </Card>
        )}
      </div>
    </>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 10.5 }}>
        {label}
      </div>
      <div style={{ fontWeight: 600 }}>{value}</div>
    </div>
  );
}
