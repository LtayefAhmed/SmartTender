import { useMemo, useRef, useState } from "react";

/**
 * Type-ahead multi-select over a known vocabulary.
 *
 * Replaces free text for criteria the portal defines: a country typed by hand
 * was silently unrecognised, and the search then crawled the portal's whole
 * feed to return nothing. Proposing only what the source understands removes
 * that class of mistake at the source.
 *
 * Typing still works — the field is a filter, not a cage. A value the list
 * does not contain can be committed deliberately with Enter, because a
 * vocabulary can lag behind reality and refusing the entry outright would be
 * worse than warning about it afterwards.
 *
 * Matching folds case and accents: someone typing "tunisie", "TUNISIE" or
 * "Tunisie" means the same country, and the fold is the whole point of
 * offering a list.
 */
export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  allowFreeText = true,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  options: { name: string; hint?: string }[];
  placeholder?: string;
  allowFreeText?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const fold = (s: string) =>
    s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();

  const matches = useMemo(() => {
    const needle = fold(draft.trim());
    const chosen = new Set(value.map(fold));
    const pool = options.filter((o) => !chosen.has(fold(o.name)));
    if (!needle) return pool.slice(0, 8);
    // Entries starting with what was typed come first: someone typing "ma"
    // wants Maroc and Mali before Roumanie, which merely contains the letters.
    const starts = pool.filter((o) => fold(o.name).startsWith(needle));
    const contains = pool.filter(
      (o) => !fold(o.name).startsWith(needle) && fold(o.name).includes(needle)
    );
    return [...starts, ...contains].slice(0, 8);
  }, [draft, options, value]);

  function add(name: string) {
    if (!value.some((v) => fold(v) === fold(name))) onChange([...value, name]);
    setDraft("");
    setOpen(false);
    inputRef.current?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") {
      e.preventDefault();
      if (matches.length) add(matches[0].name);
      else if (allowFreeText && draft.trim()) add(draft.trim());
    } else if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "Backspace" && !draft && value.length) {
      onChange(value.slice(0, -1));
    }
  }

  const unknown = (name: string) =>
    options.length > 0 && !options.some((o) => fold(o.name) === fold(name));

  return (
    <div style={{ position: "relative" }}>
      {value.length > 0 && (
        <div className="chips mb">
          {value.map((tag) => (
            <span
              key={tag}
              className="chip"
              title={unknown(tag) ? "Absent de la liste de la source" : undefined}
              style={unknown(tag) ? { borderColor: "var(--amber, #FFB454)" } : undefined}
            >
              {unknown(tag) && "⚠ "}
              {tag}
              <button onClick={() => onChange(value.filter((t) => t !== tag))}>×</button>
            </span>
          ))}
        </div>
      )}

      <input
        ref={inputRef}
        className="input"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => {
          setDraft(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        // A click on a suggestion fires after blur, so closing is deferred.
        onBlur={() => setTimeout(() => setOpen(false), 150)}
        onKeyDown={onKeyDown}
      />

      {open && matches.length > 0 && (
        <div
          className="card"
          style={{
            position: "absolute",
            zIndex: 20,
            left: 0,
            right: 0,
            marginTop: 4,
            padding: 4,
            maxHeight: 260,
            overflowY: "auto",
            background: "var(--panel)",
            // Uses the shared token so the popover matches every other
            // floating surface, light or dark.
            boxShadow: "var(--shadow-lg)",
          }}
        >
          {matches.map((o) => (
            <button
              key={o.name}
              className="row spread"
              style={{
                width: "100%",
                textAlign: "left",
                padding: "6px 8px",
                background: "transparent",
                border: 0,
                cursor: "pointer",
                borderRadius: 6,
              }}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => add(o.name)}
            >
              <span>{o.name}</span>
              {o.hint && <span className="tiny muted">{o.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
