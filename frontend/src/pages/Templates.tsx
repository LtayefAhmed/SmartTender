import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { DocumentTemplate, TemplateKind, TemplateVariables } from "../api/types";
import { Badge, Empty, ErrorState, Loading } from "../components/ui";
import { useToast } from "../components/toast";

/**
 * The template library.
 *
 * The parcours asks that the Bureau d'Études extend the format library itself,
 * and Inetum's own solution slide names the formats it wants: BEI, BAD, Bureau
 * d'Étude. Both say the same thing — adding a funder's format has to be
 * dropping a Word file, not shipping a release. This screen is what makes that
 * sentence true; without it, "templates are data" is a claim about the database
 * schema and nothing more.
 *
 * The variable catalogue is shown beside the upload rather than buried in
 * documentation, because it is the one thing a template author cannot guess and
 * the first thing to go stale when it lives anywhere but the code that enforces
 * it. Two variables are marked as not yet produced: they are accepted in a
 * template and render empty, so a template written today does not need
 * rewriting when the structured career timeline lands.
 */
const KINDS: { value: TemplateKind; label: string; hint?: string }[] = [
  { value: "cv", label: "CV adapté" },
  { value: "fiche_expert", label: "Fiche expert" },
  { value: "lettre", label: "Lettre d'accompagnement" },
  { value: "matrice_conformite", label: "Matrice de conformité" },
  { value: "formulaire_tech", label: "Formulaire technique" },
  {
    value: "formulaire_fin",
    label: "Formulaire financier",
    hint: "Aucune donnée tarifaire dans la plateforme — un gabarit déposé ici restera vide.",
  },
];

export function Templates() {
  const [kind, setKind] = useState<TemplateKind>("cv");
  const [showAll, setShowAll] = useState(false);

  const library = useQuery({
    queryKey: ["templates", showAll],
    queryFn: () =>
      api.get<{ total: number; items: DocumentTemplate[] }>("/templates", {
        active_only: !showAll,
      }),
  });

  const catalogue = useQuery({
    queryKey: ["template-variables", kind],
    queryFn: () => api.get<TemplateVariables>("/templates/variables", { kind }),
  });

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div>
        <h1>Gabarits de documents</h1>
        <p className="muted" style={{ maxWidth: 720 }}>
          Un gabarit est un fichier Word ordinaire, avec des trous nommés. Ajouter le
          format d'un bailleur — BEI, BAD, bureau d'études — c'est déposer un fichier :
          aucune modification du programme n'est nécessaire.
        </p>
      </div>

      <UploadForm kind={kind} onKindChange={setKind} />

      <Catalogue query={catalogue} kind={kind} />

      <div className="card">
        <div className="card-title">
          Bibliothèque
          <label className="tiny muted row" style={{ gap: 6, marginLeft: "auto" }}>
            <input
              type="checkbox"
              checked={showAll}
              onChange={(event) => setShowAll(event.target.checked)}
            />
            afficher les versions retirées
          </label>
        </div>

        {library.isLoading && <Loading />}
        {library.error && <ErrorState error={library.error} />}
        {library.data && library.data.items.length === 0 && (
          <Empty>
            Aucun gabarit. Déposez-en un ci-dessus, ou construisez celui par défaut avec{" "}
            <code>python scripts/build_default_template.py</code>.
          </Empty>
        )}
        {library.data && library.data.items.length > 0 && (
          <div className="stack" style={{ gap: 6 }}>
            {library.data.items.map((item) => (
              <TemplateRow key={item.id} item={item} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function UploadForm({
  kind,
  onKindChange,
}: {
  kind: TemplateKind;
  onKindChange: (value: TemplateKind) => void;
}) {
  const client = useQueryClient();
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [key, setKey] = useState("");
  const [label, setLabel] = useState("");
  const [funder, setFunder] = useState("");

  const upload = useMutation({
    mutationFn: (form: FormData) =>
      api.upload<DocumentTemplate & { unavailable_variables?: string[] }>(
        "/templates",
        form
      ),
    onSuccess: (data) => {
      client.invalidateQueries({ queryKey: ["templates"] });
      const waiting = data.unavailable_variables ?? [];
      toast.ok(
        `Gabarit accepté — ${data.key} v${data.version}`,
        waiting.length
          ? `${data.variables.length} variables reconnues. ${waiting.join(", ")} ne sont pas encore alimentées et resteront vides.`
          : `${data.variables.length} variables reconnues.`
      );
      if (fileRef.current) fileRef.current.value = "";
      setKey("");
      setLabel("");
      setFunder("");
    },
    // The message from the API names the offending variables. Showing it
    // verbatim is the point: "invalid template" would send the author back to
    // guessing, which is exactly what the check exists to prevent.
    onError: (error: unknown) =>
      toast.err(
        "Gabarit refusé",
        error instanceof Error ? error.message : "Le dépôt a échoué."
      ),
  });

  const selected = KINDS.find((item) => item.value === kind);

  return (
    <div className="card">
      <div className="card-title">Déposer un gabarit</div>

      <div className="grid cols-2" style={{ gap: 12 }}>
        <label className="stack" style={{ gap: 4 }}>
          <span className="tiny muted">Fichier .docx</span>
          <input ref={fileRef} className="input" type="file" accept=".docx" />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="tiny muted">Type de document</span>
          <select
            className="select"
            value={kind}
            onChange={(event) => onKindChange(event.target.value as TemplateKind)}
          >
            {KINDS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="tiny muted">
            Clé — stable entre versions, redéposer la même crée une v2
          </span>
          <input
            className="input"
            placeholder="cv_bei"
            value={key}
            onChange={(event) => setKey(event.target.value)}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="tiny muted">Libellé</span>
          <input
            className="input"
            placeholder="CV au format BEI"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="tiny muted">
            Bailleur — laisser vide pour un gabarit maison
          </span>
          <input
            className="input"
            placeholder="BEI"
            value={funder}
            onChange={(event) => setFunder(event.target.value)}
          />
        </label>
      </div>

      {selected?.hint && (
        <div className="tiny muted" style={{ marginTop: 10 }}>
          ⚠ {selected.hint}
        </div>
      )}

      <div className="row" style={{ marginTop: 12, gap: 10 }}>
        <button
          className="btn primary"
          disabled={upload.isPending}
          onClick={() => {
            const file = fileRef.current?.files?.[0];
            if (!file) return toast.err("Aucun fichier", "Choisissez un .docx.");
            if (!key.trim() || !label.trim())
              return toast.err("Champs manquants", "La clé et le libellé sont requis.");
            const form = new FormData();
            form.append("file", file);
            form.append("key", key.trim());
            form.append("label", label.trim());
            form.append("kind", kind);
            if (funder.trim()) form.append("funder", funder.trim());
            upload.mutate(form);
          }}
        >
          {upload.isPending ? "Vérification…" : "Déposer"}
        </button>
        <span className="tiny muted">
          Le fichier est analysé au dépôt : un gabarit réclamant une variable inconnue
          est refusé ici, pas au moment de produire un dossier.
        </span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function Catalogue({
  query,
  kind,
}: {
  query: { isLoading: boolean; error: unknown; data: TemplateVariables | undefined };
  kind: TemplateKind;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="card">
      <div className="card-title" style={{ cursor: "pointer" }} onClick={() => setOpen(!open)}>
        Variables disponibles
        <span className="hint">
          {KINDS.find((k) => k.value === kind)?.label} · {open ? "▴" : "▾"}
        </span>
      </div>

      {!open && (
        <div className="tiny muted">
          Les noms à écrire entre <code>{"{{ }}"}</code> dans le fichier Word. Cliquez
          pour les afficher.
        </div>
      )}

      {open && (
        <>
          {query.isLoading && <Loading />}
          {query.error && <ErrorState error={query.error} />}
          {query.data && (
            <div className="stack" style={{ gap: 8 }}>
              {query.data.variables.map((variable) => (
                <div key={variable.name} className="row" style={{ gap: 10 }}>
                  <code
                    className="mono tiny"
                    style={{ minWidth: 150, color: "var(--chart-1)" }}
                  >
                    {variable.kind === "records"
                      ? `{% for x in ${variable.name} %}`
                      : `{{ ${variable.name} }}`}
                  </code>
                  <span className="tiny">{variable.label}</span>
                  {!variable.available && (
                    <Badge color="amber">pas encore alimentée</Badge>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
function TemplateRow({ item }: { item: DocumentTemplate }) {
  const client = useQueryClient();
  const toast = useToast();

  const retire = useMutation({
    mutationFn: () => api.del<DocumentTemplate>(`/templates/${item.id}`),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["templates"] });
      toast.ok("Gabarit retiré", "Le fichier reste lisible : un document produit avec lui doit rester explicable.");
    },
  });

  return (
    <div className="row spread" style={{ gap: 10 }}>
      <div className="row" style={{ gap: 8, minWidth: 0 }}>
        <Badge color={item.is_active ? "teal" : "gray"}>
          {item.is_active ? "actif" : "retiré"}
        </Badge>
        <span style={{ fontWeight: 600, fontSize: 13 }}>{item.label}</span>
        <span className="tiny muted mono">
          {item.key} · v{item.version}
        </span>
        {item.funder && <Badge color="violet">{item.funder}</Badge>}
        <span className="tiny muted">{item.variables.length} variables</span>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn sm ghost" onClick={() => download(item.id)}>
          ↓ .docx
        </button>
        {item.is_active && (
          <button
            className="btn sm ghost"
            disabled={retire.isPending}
            onClick={() => retire.mutate()}
          >
            Retirer
          </button>
        )}
      </div>
    </div>
  );
}

async function download(templateId: string) {
  try {
    const res = await api.get<{ url: string }>(`/templates/${templateId}/download`);
    window.open(res.url, "_blank", "noopener");
  } catch {
    window.alert("Ce gabarit n'a pas pu être ouvert.");
  }
}
