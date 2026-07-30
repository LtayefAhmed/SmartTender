/** Minimal toast system — success/error feedback for mutations. */
import { createContext, useCallback, useContext, useState } from "react";
import type { ReactNode } from "react";

type Kind = "ok" | "err" | "info";
interface Toast {
  id: number;
  kind: Kind;
  title: string;
  message?: string;
}
interface Ctx {
  push: (t: Omit<Toast, "id">) => void;
  ok: (title: string, message?: string) => void;
  err: (title: string, message?: string) => void;
}

const ToastCtx = createContext<Ctx | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { ...t, id }]);
    setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== id)), 5200);
  }, []);

  const ctx: Ctx = {
    push,
    ok: (title, message) => push({ kind: "ok", title, message }),
    err: (title, message) => push({ kind: "err", title, message }),
  };

  return (
    <ToastCtx.Provider value={ctx}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            <div className="tt">{t.title}</div>
            {t.message && <div className="tm">{t.message}</div>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast(): Ctx {
  const ctx = useContext(ToastCtx);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return ctx;
}
