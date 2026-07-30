import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AcceptedResponse, Page, ScoringProfile } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Meter, Loading, Empty } from "../components/ui";
import { useToast } from "../components/toast";
import { fmtDate } from "../lib/format";

interface DupRecord {
  id: string;
  canonical_tender_id: string | null;
  strategy: string;
  similarity: number | null;
  source_key: string | null;
  title: string | null;
  detected_at: string;
}
interface LogRow {
  id: number;
  ts: string;
  level: string;
  event: string;
  stage: string | null;
  connector: string | null;
  message: string | null;
  error_type: string | null;
}

export function Admin() {
  const [tab, setTab] = useState<"scoring" | "duplicates" | "logs">("scoring");
  return (
    <>
      <TopBar
        title="Administration"
        sub="Pondérations de scoring · doublons rejetés · journal d'audit"
        actions={
          <div className="row" style={{ gap: 6 }}>
            {(["scoring", "duplicates", "logs"] as const).map((t) => (
              <button
                key={t}
                className={`btn sm ${tab === t ? "primary" : "ghost"}`}
                onClick={() => setTab(t)}
              >
                {t === "scoring" ? "Scoring" : t === "duplicates" ? "Doublons" : "Journal"}
              </button>
            ))}
          </div>
        }
      />
      <div className="content">
        {tab === "scoring" && <Scoring />}
        {tab === "duplicates" && <Duplicates />}
        {tab === "logs" && <Logs />}
      </div>
    </>
  );
}

function Scoring() {
  const toast = useToast();
  const profile = useQuery({
    queryKey: ["scoring-profile"],
    queryFn: () => api.get<ScoringProfile>("/admin/scoring/profile"),
  });
  const reload = useMutation({
    mutationFn: () => api.post<{ version: string }>("/admin/scoring/reload"),
    onSuccess: (r) => {
      toast.ok("Profil rechargé", `version ${r.version}`);
      profile.refetch();
    },
  });
  const rescore = useMutation({
    mutationFn: () => api.post<AcceptedResponse>("/admin/scoring/rescore?limit=5000"),
    onSuccess: (r) => toast.ok("Re-scoring lancé", r.message),
  });

  if (profile.isLoading) return <Loading />;
  const p = profile.data!;
  const weights = Object.entries(p.weights).sort((a, b) => b[1] - a[1]);
  const maxW = Math.max(...weights.map(([, w]) => w), 0.01);

  return (
    <div className="stack">
      <Card
        title="Profil de scoring actif"
        hint={`${p.name} · v${p.version}`}
      >
        <p className="tiny muted mb">
          Les poids proviennent de <span className="mono">config/scoring.yaml</span>. Modifiez le
          fichier puis rechargez ; le re-scoring recalcule les appels d'offres existants.
        </p>
        <div className="stack" style={{ gap: 10 }}>
          {weights.map(([name, w]) => (
            <div key={name} className="bar-row" style={{ marginBottom: 0 }}>
              <div className="bl" style={{ textTransform: "capitalize" }}>
                {name.replace(/_/g, " ")}
              </div>
              <div className="bt">
                <Meter value={w / maxW} color="var(--blue)" />
              </div>
              <div className="bv">{(w * 100).toFixed(0)}%</div>
            </div>
          ))}
        </div>
        <div className="row mt">
          <button className="btn" onClick={() => reload.mutate()} disabled={reload.isPending}>
            ⟳ Recharger depuis le disque
          </button>
          <button className="btn teal" onClick={() => rescore.mutate()} disabled={rescore.isPending}>
            Re-scorer les appels d'offres
          </button>
        </div>
      </Card>

      <Card title="Bandes de pertinence">
        <div className="row wrap" style={{ gap: 10 }}>
          {Object.entries(p.bands).map(([key, b]) => (
            <div key={key} className="row" style={{ gap: 8 }}>
              <span className="dot" style={{ background: b.color }} />
              <span className="tiny">
                {b.label} <span className="muted mono">≥ {b.min_score}</span>
              </span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

function Duplicates() {
  const dups = useQuery({
    queryKey: ["duplicates"],
    queryFn: () => api.get<Page<DupRecord>>("/admin/duplicates", { page_size: 40 }),
  });
  if (dups.isLoading) return <Loading />;
  if ((dups.data?.items.length ?? 0) === 0)
    return (
      <Empty icon="◈">
        Aucun doublon rejeté. Cette vue répond à « pourquoi cet appel d'offres n'apparaît pas ? ».
      </Empty>
    );
  return (
    <Card title={`Doublons rejetés (${dups.data!.total})`} hint="jamais affichés dans le tableau de bord">
      <div className="table-wrap" style={{ border: "none" }}>
        <table className="data">
          <thead>
            <tr>
              <th>Objet</th>
              <th>Source</th>
              <th>Stratégie</th>
              <th>Similarité</th>
              <th>Détecté</th>
            </tr>
          </thead>
          <tbody>
            {dups.data!.items.map((d) => (
              <tr key={d.id} style={{ cursor: "default" }}>
                <td style={{ maxWidth: 360 }}>{d.title ?? "—"}</td>
                <td className="mono tiny">{d.source_key}</td>
                <td>
                  <Badge color="violet">{d.strategy}</Badge>
                </td>
                <td className="mono tiny">{d.similarity != null ? d.similarity.toFixed(3) : "—"}</td>
                <td className="tiny muted">{fmtDate(d.detected_at, true)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function Logs() {
  const [event, setEvent] = useState("");
  const [level, setLevel] = useState("");
  const logs = useQuery({
    queryKey: ["logs", { event, level }],
    queryFn: () => api.get<Page<LogRow>>("/admin/logs", { event, level, page_size: 60 }),
    refetchInterval: 8000,
  });

  return (
    <Card
      title="Journal d'audit"
      hint="requêtable par appel d'offres, tâche, connecteur, événement"
    >
      <div className="row mb" style={{ gap: 10 }}>
        <input
          className="input"
          style={{ maxWidth: 240 }}
          placeholder="événement (ex. dedup.rejected)"
          value={event}
          onChange={(e) => setEvent(e.target.value)}
        />
        <select className="select" style={{ maxWidth: 140 }} value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">Tous niveaux</option>
          <option value="INFO">INFO</option>
          <option value="WARNING">WARNING</option>
          <option value="ERROR">ERROR</option>
        </select>
      </div>
      {logs.isLoading ? (
        <Loading />
      ) : (
        <div className="table-wrap" style={{ border: "none" }}>
          <table className="data">
            <thead>
              <tr>
                <th>Horodatage</th>
                <th>Niveau</th>
                <th>Événement</th>
                <th>Connecteur</th>
                <th>Message</th>
              </tr>
            </thead>
            <tbody>
              {logs.data!.items.map((l) => (
                <tr key={l.id} style={{ cursor: "default" }}>
                  <td className="tiny muted mono">{fmtDate(l.ts, true)}</td>
                  <td>
                    <Badge color={l.level === "ERROR" ? "red" : l.level === "WARNING" ? "amber" : "gray"}>
                      {l.level}
                    </Badge>
                  </td>
                  <td className="mono tiny">{l.event}</td>
                  <td className="tiny muted">{l.connector ?? "—"}</td>
                  <td className="tiny" style={{ maxWidth: 380 }}>
                    {l.message ?? l.error_type ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
