import type { ReactNode } from "react";
import { useEffect } from "react";

/** Right-side slide-over used for tender detail. Closes on Escape / scrim. */
export function Drawer({
  open,
  onClose,
  head,
  children,
}: {
  open: boolean;
  onClose: () => void;
  head: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <>
      <div className="drawer-scrim" onClick={onClose} />
      <div className="drawer" role="dialog" aria-modal="true">
        <div className="drawer-head">{head}</div>
        <div className="drawer-body">{children}</div>
      </div>
    </>
  );
}
