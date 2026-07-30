import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { AcceptedResponse } from "../api/types";
import { TopBar } from "../components/Layout";
import { Card, Spinner } from "../components/ui";
import { useToast } from "../components/toast";

interface Result {
  ok: boolean;
  name: string;
  message: string;
  code?: string;
  tenderId?: string;
}

export function Upload() {
  const toast = useToast();
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [meta, setMeta] = useState({ title: "", buyer: "", country: "" });

  async function submit(files: FileList | File[]) {
    const list = Array.from(files);
    if (!list.length) return;
    setBusy(true);
    for (const file of list) {
      const form = new FormData();
      form.append("file", file);
      if (meta.title) form.append("title", meta.title);
      if (meta.buyer) form.append("buyer", meta.buyer);
      if (meta.country) form.append("country", meta.country);
      try {
        const res = await api.upload<AcceptedResponse>("/upload", form);
        setResults((r) => [
          { ok: true, name: file.name, message: res.message, tenderId: res.tender_id ?? undefined },
          ...r,
        ]);
        toast.ok("Document accepté", file.name);
      } catch (e) {
        const err = e as ApiError;
        setResults((r) => [
          { ok: false, name: file.name, message: err.message, code: err.code },
          ...r,
        ]);
        toast.err("Refusé : " + file.name, err.message);
      }
    }
    setBusy(false);
    qc.invalidateQueries({ queryKey: ["tenders"] });
    if (inputRef.current) inputRef.current.value = "";
  }

  return (
    <>
      <TopBar
        title="Import manuel"
        sub="Entrée B · validation synchrone — un fichier refusé n'entre jamais dans le pipeline"
      />
      <div className="content grid cols-2" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card>
            <div
              className={`dropzone ${over ? "over" : ""}`}
              onClick={() => inputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setOver(true);
              }}
              onDragLeave={() => setOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setOver(false);
                submit(e.dataTransfer.files);
              }}
            >
              <div className="big">{busy ? <Spinner /> : "⭱"}</div>
              <div style={{ fontWeight: 600 }}>Glissez-déposez un document</div>
              <div className="tiny muted mt">ou cliquez pour parcourir · PDF, DOCX, HTML · max 25 Mo</div>
              <input
                ref={inputRef}
                type="file"
                accept=".pdf,.docx,.html,.htm"
                multiple
                hidden
                onChange={(e) => e.target.files && submit(e.target.files)}
              />
            </div>
          </Card>

          <Card title="Métadonnées (optionnel)">
            <div className="field">
              <label>Titre (sinon extrait du document)</label>
              <input
                className="input"
                value={meta.title}
                onChange={(e) => setMeta({ ...meta, title: e.target.value })}
              />
            </div>
            <div className="grid cols-2" style={{ gap: 14 }}>
              <div className="field">
                <label>Acheteur</label>
                <input
                  className="input"
                  value={meta.buyer}
                  onChange={(e) => setMeta({ ...meta, buyer: e.target.value })}
                />
              </div>
              <div className="field">
                <label>Pays</label>
                <input
                  className="input"
                  value={meta.country}
                  onChange={(e) => setMeta({ ...meta, country: e.target.value })}
                />
              </div>
            </div>
          </Card>
        </div>

        <Card title="Résultats de validation" hint={`${results.length}`}>
          {results.length === 0 ? (
            <div className="muted tiny">
              Les documents validés sont dédupliqués, stockés, puis analysés (extraction + OCR) et
              scorés automatiquement.
            </div>
          ) : (
            <div className="stack" style={{ gap: 10 }}>
              {results.map((r, i) => (
                <div
                  key={i}
                  className="card"
                  style={{
                    padding: 12,
                    background: "var(--panel-2)",
                    borderLeft: `3px solid ${r.ok ? "var(--teal)" : "var(--red)"}`,
                  }}
                >
                  <div className="row spread">
                    <span style={{ fontWeight: 600, fontSize: 13 }}>
                      {r.ok ? "✅" : "✕"} {r.name}
                    </span>
                    {r.code && <span className="badge red tiny mono">{r.code}</span>}
                  </div>
                  <div className="tiny muted mt">{r.message}</div>
                  {r.tenderId && (
                    <a className="badge blue tiny mt" href={`/tenders?open=${r.tenderId}`}>
                      Voir l'appel d'offres →
                    </a>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
