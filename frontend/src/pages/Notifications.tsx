import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Notification, Page, Preference } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Loading, Empty } from "../components/ui";
import { TagInput } from "../components/TagInput";
import { useToast } from "../components/toast";
import { fmtRelative } from "../lib/format";

const BLANK: Preference = {
  email: "",
  display_name: "",
  team: "",
  company: "",
  active: true,
  sectors: [],
  industries: [],
  countries: [],
  keywords: [],
  excluded_keywords: [],
  connectors: [],
  buyers: [],
  cpv_codes: [],
  channels: ["in_app", "email"],
  min_relevance_band: "relevant",
  min_score: null,
  min_budget: null,
  digest_frequency: "immediate",
  max_notifications_per_day: 50,
};

export function Notifications() {
  const [tab, setTab] = useState<"feed" | "prefs">("feed");
  return (
    <>
      <TopBar
        title="Notifications"
        sub="Ce qui vous concerne — ciblage par secteur, pays, mots-clés et pertinence"
        actions={
          <div className="row" style={{ gap: 6 }}>
            <button
              className={`btn sm ${tab === "feed" ? "primary" : "ghost"}`}
              onClick={() => setTab("feed")}
            >
              Flux
            </button>
            <button
              className={`btn sm ${tab === "prefs" ? "primary" : "ghost"}`}
              onClick={() => setTab("prefs")}
            >
              Préférences
            </button>
          </div>
        }
      />
      <div className="content">{tab === "feed" ? <Feed /> : <Prefs />}</div>
    </>
  );
}

function Feed() {
  const qc = useQueryClient();
  const feed = useQuery({
    queryKey: ["notifications", "feed"],
    queryFn: () => api.get<Page<Notification>>("/notifications", { page_size: 40 }),
    refetchInterval: 12000,
  });
  const markRead = useMutation({
    mutationFn: (id: string) => api.post(`/notifications/${id}/read`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });

  if (feed.isLoading) return <Loading />;
  if ((feed.data?.items.length ?? 0) === 0)
    return <Empty icon="◉">Aucune notification. Elles apparaissent dès qu'un appel d'offres correspond à vos critères.</Empty>;

  return (
    <div className="stack">
      {feed.data!.items.map((n) => {
        const unread = !n.read_at;
        const isDigest = (n.payload?.kind as string) === "digest";
        return (
          <Card
            key={n.id}
            className={unread ? "" : ""}
          >
            <div className="row spread">
              <div className="row">
                <Badge color={n.channel === "email" ? "blue" : "teal"}>{n.channel}</Badge>
                {isDigest && <Badge color="violet">récapitulatif</Badge>}
                {unread && <span className="dot" style={{ background: "var(--rose)" }} />}
              </div>
              <span className="tiny muted">{fmtRelative(n.created_at)}</span>
            </div>
            <div style={{ fontWeight: 600, marginTop: 8 }}>{n.subject}</div>
            {n.tender_id && !isDigest && (
              <a className="tiny" href={`/tenders?open=${n.tender_id}`} style={{ color: "var(--blue)" }}>
                Voir l'appel d'offres →
              </a>
            )}
            {Object.keys(n.match_reason ?? {}).length > 0 && (
              <div className="tiny muted mt">
                Correspondance : {Object.entries(n.match_reason).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(" · ")}
              </div>
            )}
            {unread && (
              <button className="btn sm ghost mt" onClick={() => markRead.mutate(n.id)}>
                Marquer comme lu
              </button>
            )}
          </Card>
        );
      })}
    </div>
  );
}

function Prefs() {
  const toast = useToast();
  const [pref, setPref] = useState<Preference | null>(null);

  const load = useQuery({
    queryKey: ["preferences"],
    queryFn: async () => {
      try {
        return await api.get<Preference>("/preferences");
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return { ...BLANK };
        throw e;
      }
    },
  });
  const current = pref ?? load.data ?? null;

  const save = useMutation({
    mutationFn: (p: Preference) => api.put<Preference>("/preferences", p),
    onSuccess: () => toast.ok("Préférences enregistrées"),
    onError: (e) => toast.err("Échec", (e as Error).message),
  });

  if (load.isLoading || !current) return <Loading />;
  const set = (patch: Partial<Preference>) => setPref({ ...current, ...patch });

  return (
    <div className="grid cols-2" style={{ alignItems: "start" }}>
      <Card title="Ciblage" className="pad-lg" >
        <p className="tiny muted mb">
          Chaque liste vide signifie « aucune restriction sur ce critère » — pas « ne rien recevoir ».
        </p>
        <div className="field">
          <label>Secteurs</label>
          <TagInput value={current.sectors} onChange={(v) => set({ sectors: v })} />
        </div>
        <div className="field">
          <label>Pays</label>
          <TagInput value={current.countries} onChange={(v) => set({ countries: v })} />
        </div>
        <div className="field">
          <label>Mots-clés</label>
          <TagInput value={current.keywords} onChange={(v) => set({ keywords: v })} />
        </div>
        <div className="field">
          <label>Mots-clés exclus (veto)</label>
          <TagInput value={current.excluded_keywords} onChange={(v) => set({ excluded_keywords: v })} />
        </div>
        <div className="field">
          <label>Acheteurs</label>
          <TagInput value={current.buyers} onChange={(v) => set({ buyers: v })} />
        </div>
      </Card>

      <Card title="Livraison" className="pad-lg">
        <div className="field">
          <label>Email</label>
          <input
            className="input"
            value={current.email ?? ""}
            onChange={(e) => set({ email: e.target.value })}
            placeholder="vous@inetum.com"
          />
        </div>
        <div className="grid cols-2" style={{ gap: 14 }}>
          <div className="field">
            <label>Pertinence minimale</label>
            <select
              className="select"
              value={current.min_relevance_band}
              onChange={(e) => set({ min_relevance_band: e.target.value })}
            >
              <option value="low_relevance">Peu pertinent</option>
              <option value="relevant">Pertinent</option>
              <option value="highly_relevant">Très pertinent</option>
            </select>
          </div>
          <div className="field">
            <label>Fréquence</label>
            <select
              className="select"
              value={current.digest_frequency}
              onChange={(e) => set({ digest_frequency: e.target.value })}
            >
              <option value="immediate">Immédiat</option>
              <option value="daily">Récapitulatif quotidien</option>
              <option value="weekly">Récapitulatif hebdomadaire</option>
            </select>
          </div>
        </div>
        <div className="field">
          <label>Canaux</label>
          <div className="row">
            {["in_app", "email"].map((ch) => (
              <label key={ch} className="row tiny" style={{ cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={current.channels.includes(ch)}
                  onChange={(e) =>
                    set({
                      channels: e.target.checked
                        ? [...current.channels, ch]
                        : current.channels.filter((c) => c !== ch),
                    })
                  }
                />
                {ch === "in_app" ? "Dans l'app" : "Email"}
              </label>
            ))}
          </div>
        </div>
        <div className="field">
          <label>Plafond quotidien</label>
          <input
            className="input"
            type="number"
            value={current.max_notifications_per_day}
            onChange={(e) => set({ max_notifications_per_day: Number(e.target.value) })}
          />
        </div>
        <button className="btn primary" onClick={() => save.mutate(current)} disabled={save.isPending}>
          Enregistrer
        </button>
      </Card>
    </div>
  );
}
