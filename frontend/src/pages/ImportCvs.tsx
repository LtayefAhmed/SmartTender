import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Cv, Page } from "../api/types";
import { TopBar } from "../components/Layout";
import { Badge, Card, Empty, ErrorState, Loading, Spinner } from "../components/ui";
import { useToast } from "../components/toast";
import { fmtBytes, fmtDate } from "../lib/format";

interface Result {
  ok: boolean;
  name: string;
  message: string;
  code?: string;
}

export function ImportCvs() {
  const toast = useToast();
  const qc = useQueryClient();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<Result[]>([]);
  const [link, setLink] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);

  const cvs = useQuery({
    queryKey: ["cvs"],
    queryFn: () => api.get<Page<Cv>>("/cvs", { page_size: 50 }),
  });

  async function submit(files: FileList | File[], source: "upload" | "link" = "upload", sourceUrl?: string) {
    const list = Array.from(files);
    if (!list.length) return;
    setBusy(true);
    for (const file of list) {
      const form = new FormData();
      form.append("file", file);
      form.append("source", source);
      if (sourceUrl) form.append("source_url", sourceUrl);
      try {
        await api.upload<Cv>("/cvs", form);
        setResults((r) => [{ ok: true, name: file.name, message: "Importé." }, ...r]);
        toast.ok("CV importé", file.name);
      } catch (e) {
        const err = e as ApiError;
        setResults((r) => [{ ok: false, name: file.name, message: err.message, code: err.code }, ...r]);
        toast.err("Refusé : " + file.name, err.message);
      }
    }
    setBusy(false);
    qc.invalidateQueries({ queryKey: ["cvs"] });
    if (fileInputRef.current) fileInputRef.current.value = "";
    if (folderInputRef.current) folderInputRef.current.value = "";
  }

  async function submitLink() {
    const url = link.trim();
    if (!url) return;
    setLinkBusy(true);
    try {
      // Fetched by the browser, never by the server: an operator-supplied URL
      // is untrusted input, and resolving it from the backend would be an
      // open door to internal addresses (SSRF). The browser already refuses
      // that for us, and hands over only the bytes.
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const name = url.split("/").pop()?.split("?")[0] || "cv";
      const file = new File([blob], name, { type: blob.type || "application/octet-stream" });
      await submit([file], "link", url);
      setLink("");
    } catch (e: any) {
      toast.err(
        "Import par lien impossible",
        "Le site source bloque probablement les requêtes cross-origin (CORS). Téléchargez le fichier puis importez-le via le dossier."
      );
    } finally {
      setLinkBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await api.del(`/cvs/${id}`);
      qc.invalidateQueries({ queryKey: ["cvs"] });
      toast.ok("CV supprimé", "");
    } catch (e: any) {
      toast.err("Suppression impossible", e.message);
    }
  }

  return (
    <>
      <TopBar
        title="Import CVs"
        sub="Matching · phase 1 — importer les CVs. Le rapprochement avec les appels d'offres arrive dans une prochaine phase."
      />
      <div className="content grid cols-2" style={{ alignItems: "start" }}>
        <div className="stack">
          <Card>
            <div
              className={`dropzone ${over ? "over" : ""}`}
              onClick={() => fileInputRef.current?.click()}
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
              <div className="big">{busy ? <Spinner /> : "⧫"}</div>
              <div style={{ fontWeight: 600 }}>Glissez-déposez un ou plusieurs CVs</div>
              <div className="tiny muted mt">ou cliquez pour parcourir · PDF, DOCX · max 25 Mo</div>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,.docx"
                multiple
                hidden
                onChange={(e) => e.target.files && submit(e.target.files)}
              />
            </div>
            <div className="row" style={{ marginTop: 12, justifyContent: "center" }}>
              <button className="btn sm" onClick={() => folderInputRef.current?.click()} disabled={busy}>
                📁 Importer un dossier
              </button>
              <input
                ref={folderInputRef}
                type="file"
                hidden
                multiple
                // @ts-ignore — non-standard but supported by every Chromium-based browser
                webkitdirectory=""
                onChange={(e) => e.target.files && submit(e.target.files)}
              />
            </div>
          </Card>

          <Card title="Importer depuis un lien">
            <div className="row" style={{ gap: 8 }}>
              <input
                className="input"
                placeholder="https://exemple.com/cv-candidat.pdf"
                value={link}
                onChange={(e) => setLink(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && submitLink()}
                disabled={linkBusy}
              />
              <button className="btn sm" onClick={submitLink} disabled={linkBusy || !link.trim()}>
                {linkBusy ? <Spinner /> : "Importer"}
              </button>
            </div>
            <div className="tiny muted mt">
              Le fichier est récupéré par votre navigateur, pas par le serveur.
            </div>
          </Card>

          {results.length > 0 && (
            <Card title="Résultats" hint={`${results.length}`}>
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
                  </div>
                ))}
              </div>
            </Card>
          )}
        </div>

        <Card title="CVs importés" hint={cvs.data ? `${cvs.data.total}` : undefined}>
          {cvs.isLoading ? (
            <Loading />
          ) : cvs.error ? (
            <ErrorState error={cvs.error} />
          ) : !cvs.data?.items.length ? (
            <Empty icon="⧫">Aucun CV importé pour l'instant.</Empty>
          ) : (
            <div className="stack" style={{ gap: 8 }}>
              {cvs.data.items.map((c) => (
                <div key={c.id} className="row spread">
                  <div style={{ minWidth: 0 }}>
                    <div className="tiny" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                      {c.original_filename}
                    </div>
                    <div className="tiny muted">
                      {fmtBytes(c.size_bytes)} · {fmtDate(c.created_at)}
                      {c.uploaded_by ? ` · ${c.uploaded_by}` : ""}
                    </div>
                  </div>
                  <div className="row">
                    <Badge color={c.source === "link" ? "blue" : "gray"}>{c.source}</Badge>
                    <button className="btn sm ghost" onClick={() => remove(c.id)}>
                      ✕
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
