import { useState } from "react";

/** Chip-style multi-value input (keywords, sectors, connectors…). Enter or
 *  comma commits a value; each chip has a remove button. */
export function TagInput({
  value,
  onChange,
  placeholder,
}: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");

  function commit() {
    const v = draft.trim();
    if (v && !value.includes(v)) onChange([...value, v]);
    setDraft("");
  }

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
    </div>
  );
}
