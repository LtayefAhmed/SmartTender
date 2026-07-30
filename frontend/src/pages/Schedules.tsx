import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AcceptedResponse, ConnectorInfo, Page, Schedule } from "../api/types";
import { TopBar } from "../components/Layout";
import { Card, Loading, Empty } from "../components/ui";
import { TagInput } from "../components/TagInput";
import { useToast } from "../components/toast";
import { fmtRelative } from "../lib/format";

const PRESETS = [
  { key: "hourly", label: "Toutes les heures" },
  { key: "every_2_hours", label: "Toutes les 2 h" },
  { key: "every_6_hours", label: "Toutes les 6 h" },
  { key: "every_12_hours", label: "Toutes les 12 h" },
  { key: "daily", label: "Quotidien" },
  { key: "weekly", label: "Hebdomadaire" },
];

export function Schedules() {
  const toast = useToast();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);

  const schedules = useQuery({
    queryKey: ["schedules"],
    queryFn: () => api.get<Page<Schedule>>("/schedules", { page_size: 50 }),
    refetchInterval: 15000,
  });
  const registry = useQuery({
    queryKey: ["sources", "registry"],
    queryFn: () => api.get<{ connectors: ConnectorInfo[] }>("/sources/registry"),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["schedules"] });

  const toggle = useMutation({
    mutationFn: (id: string) => api.post<Schedule>(`/schedules/${id}/toggle`),
    onSuccess: invalidate,
  });
  const runNow = useMutation({
    mutationFn: (id: string) => api.post<AcceptedResponse>(`/schedules/${id}/run`),
    onSuccess: (r) => toast.ok("Planification lancée", r.message),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/schedules/${id}`),
    onSuccess: () => {
      toast.ok("Planification supprimée");
      invalidate();
    },
  });

  return (
    <>
      <TopBar
        title="Planifications"
        sub="Entrée C · scraping récurrent — modifiable à chaud, sans redémarrage"
        actions={
          <button className="btn primary" onClick={() => setCreating((c) => !c)}>
            {creating ? "Annuler" : "+ Nouvelle planification"}
          </button>
        }
      />
      <div className="content stack">
        {creating && (
          <CreateForm
            connectors={registry.data?.connectors.filter((c) => c.available) ?? []}
            onDone={() => {
              setCreating(false);
              invalidate();
            }}
          />
        )}

        {schedules.isLoading ? (
          <Loading />
        ) : (schedules.data?.items.length ?? 0) === 0 ? (
          <Empty icon="◷">Aucune planification. Créez-en une pour automatiser la veille.</Empty>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Nom</th>
                  <th>Cadence</th>
                  <th>Sources</th>
                  <th>Dernière</th>
                  <th>Prochaine</th>
                  <th>Exéc.</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {schedules.data!.items.map((s) => (
                  <tr key={s.id} style={{ cursor: "default", opacity: s.enabled ? 1 : 0.5 }}>
                    <td>
                      <div style={{ fontWeight: 500 }}>{s.name}</div>
                      {s.description && <div className="tiny muted">{s.description}</div>}
                    </td>
                    <td className="mono tiny">{s.cadence}</td>
                    <td className="tiny">
                      {s.connectors.length ? s.connectors.join(", ") : <span className="muted">toutes</span>}
                    </td>
                    <td className="tiny muted">{fmtRelative(s.last_run_at)}</td>
                    <td className="tiny muted">{s.next_run_at ? fmtRelative(s.next_run_at) : "—"}</td>
                    <td className="mono tiny">{s.total_run_count}</td>
                    <td>
                      <div className="row" style={{ gap: 6, justifyContent: "flex-end" }}>
                        <button className="btn sm" onClick={() => runNow.mutate(s.id)} title="Lancer maintenant">
                          ▷
                        </button>
                        <button className="btn sm" onClick={() => toggle.mutate(s.id)}>
                          {s.enabled ? "⏸" : "▶"}
                        </button>
                        <button
                          className="btn sm danger"
                          onClick={() => {
                            if (confirm(`Supprimer « ${s.name} » ?`)) remove.mutate(s.id);
                          }}
                        >
                          🗑
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function CreateForm({
  connectors,
  onDone,
}: {
  connectors: ConnectorInfo[];
  onDone: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [preset, setPreset] = useState("every_6_hours");
  const [selected, setSelected] = useState<string[]>([]);
  const [keywords, setKeywords] = useState<string[]>([]);
  const [publishedWithin, setPublishedWithin] = useState(7);
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!name.trim()) {
      toast.err("Nom requis");
      return;
    }
    setSaving(true);
    try {
      await api.post("/schedules", {
        name: name.trim(),
        preset,
        connectors: selected,
        filters: { keywords, published_within_days: publishedWithin },
      });
      toast.ok("Planification créée", name);
      onDone();
    } catch (e) {
      toast.err("Échec", (e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card title="Nouvelle planification" className="pad-lg">
      <div className="grid cols-2" style={{ gap: 16 }}>
        <div className="field">
          <label>Nom</label>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="tuneps-6h" />
        </div>
        <div className="field">
          <label>Cadence</label>
          <select className="select" value={preset} onChange={(e) => setPreset(e.target.value)}>
            {PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      </div>
      <div className="field">
        <label>Sources (vide = toutes les disponibles)</label>
        <div className="chips">
          {connectors.map((c) => (
            <button
              key={c.key}
              className="chip"
              style={{
                cursor: "pointer",
                borderColor: selected.includes(c.key) ? "var(--blue)" : "var(--line)",
              }}
              onClick={() =>
                setSelected((p) => (p.includes(c.key) ? p.filter((k) => k !== c.key) : [...p, c.key]))
              }
            >
              {c.key}
            </button>
          ))}
        </div>
      </div>
      <div className="grid cols-2" style={{ gap: 16 }}>
        <div className="field">
          <label>Mots-clés</label>
          <TagInput value={keywords} onChange={setKeywords} />
        </div>
        <div className="field">
          <label>Publié depuis (jours)</label>
          <input
            className="input"
            type="number"
            value={publishedWithin}
            onChange={(e) => setPublishedWithin(Number(e.target.value))}
          />
        </div>
      </div>
      <div className="row">
        <button className="btn primary" onClick={save} disabled={saving}>
          Créer
        </button>
        <button className="btn ghost" onClick={onDone}>
          Annuler
        </button>
      </div>
    </Card>
  );
}
