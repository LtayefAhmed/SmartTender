import { useEffect, useState } from "react";

type Choice = "light" | "dark" | "system";

const KEY = "smarttender.theme";

/**
 * Light / dark / system.
 *
 * Three states rather than a switch, because "system" is a real answer and the
 * only one that keeps following the OS when the user changes it at dusk. A
 * two-state switch silently converts "follow my machine" into a fixed choice
 * the first time it is touched.
 *
 * The attribute is written on `<html>` so the whole document — including the
 * scrim and the scrollbars, which sit outside any React root — flips with it.
 */
export function ThemeToggle() {
  const [choice, setChoice] = useState<Choice>(
    () => (localStorage.getItem(KEY) as Choice) || "system",
  );

  useEffect(() => {
    const root = document.documentElement;
    if (choice === "system") {
      // Removed rather than resolved: leaving the attribute off is what lets
      // the CSS media query keep answering, so a user who switches their OS
      // to dark at 18:00 sees this follow without reloading.
      root.removeAttribute("data-theme");
      localStorage.removeItem(KEY);
    } else {
      root.setAttribute("data-theme", choice);
      localStorage.setItem(KEY, choice);
    }
  }, [choice]);

  return (
    <div className="theme-toggle" role="group" aria-label="Thème de l'interface">
      {(
        [
          ["light", "☀", "Clair"],
          ["dark", "☾", "Sombre"],
          ["system", "◐", "Système"],
        ] as [Choice, string, string][]
      ).map(([value, glyph, label]) => (
        <button
          key={value}
          onClick={() => setChoice(value)}
          aria-pressed={choice === value}
          title={label}
          aria-label={label}
        >
          {glyph}
        </button>
      ))}
    </div>
  );
}
