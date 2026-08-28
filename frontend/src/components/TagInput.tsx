import { useState } from "react";

/** Chip-style multi-value input (keywords, sectors, connectors…). Enter or
 *  comma commits a value; each chip has a remove button.
 *
 *  `suggestions`, when given, renders the not-yet-picked values as small
 *  outlined chips below the input — a click adds them directly, since a
 *  recruiter picking "PMP" from a list types nothing at all. */
export function TagInput({
  value,
  onChange,
  placeholder,
  suggestions,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  suggestions?: string[];
}) {
  const [draft, setDraft] = useState("");

  function commit() {
    const v = draft.trim();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  }

  const remaining = suggestions?.filter((s) => !value.includes(s)) ?? [];

  return (
    <div>
      <div className="chips mb" style={{ display: value.length ? "flex" : "none" }}>
        {value.map((tag) => (
          <span key={tag} className="chip">
            {tag}
            <button onClick={() => onChange(value.filter((t) => t !== tag))}>×</button>
          </span>
        ))}
      </div>
      <input
        className="input"
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit();
          } else if (e.key === "Backspace" && !draft && value.length) {
            onChange(value.slice(0, -1));
          }
        }}
        onBlur={commit}
      />
      {remaining.length > 0 && (
        <div className="chips mt" style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {remaining.map((s) => (
            <button
              key={s}
              type="button"
              className="chip suggestion"
              onClick={() => onChange([...value, s])}
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
