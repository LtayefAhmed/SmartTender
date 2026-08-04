import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { BusinessDomain } from "../api/types";

/**
 * Picks search terms from Inetum's service catalogue.
 *
 * The catalogue names what the company sells — SAGE X3, S/4HANA, Copilot. A
 * public buyer never writes those words: a Tunisian notice says "progiciel de
 * gestion intégré". So choosing a domain does **not** send its product names to
 * the portals; it sends the buyer-side vocabulary the backend pairs with it.
 *
 * The product names are still shown, because they are what makes a domain
 * recognisable to whoever is running the search — they are simply not what gets
 * searched.
 *
 * Free typing stays available alongside: the catalogue is a shortcut, never a
 * fence around what can be searched.
 */
export function DomainPicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (terms: string[]) => void;
}) {
  const [open, setOpen] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["business-domains"],
    queryFn: () =>
      api.get<{ profile_version: string; domains: BusinessDomain[] }>(
        "/admin/business-domains"
      ),
    staleTime: 5 * 60 * 1000,
  });

  if (isLoading) return <div className="tiny muted">Chargement des domaines…</div>;
  const domains = data?.domains ?? [];
  if (!domains.length) return null;

  // A domain counts as chosen when every one of its search terms is present,
  // so toggling it off removes exactly what toggling it on added.
  const isChosen = (d: BusinessDomain) =>
    d.search_terms.length > 0 && d.search_terms.every((t) => selected.includes(t));

  function toggleDomain(d: BusinessDomain) {
    if (isChosen(d)) {
      onChange(selected.filter((t) => !d.search_terms.includes(t)));
    } else {
      onChange([...selected, ...d.search_terms.filter((t) => !selected.includes(t))]);
    }
  }

  function toggleTerm(term: string) {
    onChange(
      selected.includes(term) ? selected.filter((t) => t !== term) : [...selected, term]
    );
  }

  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="chips">
        {domains.map((d) => {
          const chosen = isChosen(d);
          const expanded = open === d.name;
          return (
            <button
              key={d.name}
              className="chip"
              title={`Expertises : ${d.expertise.slice(0, 6).join(", ")}`}
              style={{
                cursor: "pointer",
                borderColor: chosen ? "var(--teal)" : "var(--line)",
                background: chosen ? "rgba(27,211,188,.14)" : "var(--panel-2)",
              }}
              onClick={() => setOpen(expanded ? null : d.name)}
            >
              {chosen ? "✓ " : ""}
              {d.name}
              <span className="tiny muted" style={{ marginLeft: 4 }}>
                {expanded ? "▴" : "▾"}
              </span>
            </button>
          );
        })}
      </div>

      {open &&
        domains
          .filter((d) => d.name === open)
          .map((d) => (
            <div
              key={d.name}
              className="card"
              style={{ padding: 12, background: "var(--panel-2)" }}
            >
              <div className="row spread mb">
                <b className="tiny">{d.name}</b>
                <button className="btn sm" onClick={() => toggleDomain(d)}>
                  {isChosen(d) ? "Tout retirer" : "Tout ajouter"}
                </button>
              </div>

              <div className="tiny muted mb">
                Termes envoyés aux portails — cochez ce que vous cherchez :
              </div>
              <div className="chips mb">
                {d.search_terms.map((t) => (
                  <button
                    key={t}
                    className="chip"
                    style={{
                      cursor: "pointer",
                      borderColor: selected.includes(t) ? "var(--teal)" : "var(--line)",
                      background: selected.includes(t)
                        ? "rgba(27,211,188,.14)"
                        : "transparent",
                    }}
                    onClick={() => toggleTerm(t)}
                  >
                    {selected.includes(t) ? "✓ " : ""}
                    {t}
                  </button>
                ))}
              </div>

              {/* Shown, never searched: these are Inetum's words, not a
                  buyer's. They are what the scorer looks for once a tender has
                  been retrieved. */}
              <div className="tiny muted">
                <b>Expertises couvertes</b> (utilisées pour le score, pas pour la
                recherche) : {d.expertise.join(" · ")}
              </div>
            </div>
          ))}
    </div>
  );
}
