import { NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Health, Page, Notification } from "../api/types";
import { Dot } from "./ui";

const NAV = [
  {
    section: "Pilotage",
    items: [
      { to: "/", icon: "◈", label: "Tableau de bord", end: true },
      { to: "/tenders", icon: "▤", label: "Appels d'offres" },
    ],
  },
  {
    section: "Détection",
    items: [
      { to: "/scrape", icon: "⧉", label: "Lancer un scraping" },
      { to: "/upload", icon: "⭱", label: "Import manuel" },
      { to: "/schedules", icon: "◷", label: "Planifications" },
      { to: "/sources", icon: "⊞", label: "Sources & santé" },
    ],
  },
  {
    section: "Matching",
    items: [
      { to: "/matching/cv-import", icon: "⧫", label: "Import CVs" },
      { to: "/job-match", icon: "◎", label: "Recherche par fiche de poste" },
      { to: "/proposals", icon: "◆", label: "Génération de proposition" },
    ],
  },
  {
    section: "Suivi",
    items: [
      { to: "/notifications", icon: "◉", label: "Notifications", badge: true },
      { to: "/admin", icon: "⚙", label: "Administration" },
    ],
  },
];

export function Layout() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api.get<Health>("/health"),
    refetchInterval: 15000,
  });

  const unread = useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: () =>
      api.get<Page<Notification>>("/notifications", { unread_only: true, page_size: 1 }),
    refetchInterval: 20000,
  });
  const unreadCount = unread.data?.total ?? 0;

  const h = health.data;
  const healthTone = !h
    ? "gray"
    : h.status === "healthy"
    ? "green"
    : h.degraded_components.length
    ? "amber"
    : "red";

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <i style={{ background: "var(--teal)" }} />
            <i style={{ background: "var(--blue)" }} />
            <i style={{ background: "var(--amber)" }} />
            <i style={{ background: "var(--rose)" }} />
          </div>
          <div>
            <div className="brand-name">SmartTender</div>
            <div className="brand-sub">AI · Module 1</div>
          </div>
        </div>

        {NAV.map((group) => (
          <div key={group.section}>
            <div className="nav-section">{group.section}</div>
            {group.items.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={(item as any).end}
                className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}
              >
                <span className="ico">{item.icon}</span>
                {item.label}
                {(item as any).badge && unreadCount > 0 && (
                  <span className="nav-badge">{unreadCount}</span>
                )}
              </NavLink>
            ))}
          </div>
        ))}

        <div className="spacer" />
        <div
          className="row tiny muted"
          style={{ padding: "10px", borderTop: "1px solid var(--line)" }}
          title={h ? JSON.stringify(h.checks, null, 2) : ""}
        >
          <Dot color={healthTone} />
          <span>{h ? `Système ${h.status}` : "…"}</span>
          {h && <span className="mono" style={{ marginLeft: "auto" }}>v{h.version}</span>}
        </div>
      </aside>

      <main className="main">
        <Outlet context={{ health: h }} />
      </main>
    </div>
  );
}

export function TopBar({ title, sub, actions }: { title: string; sub?: string; actions?: React.ReactNode }) {
  return (
    <div className="topbar">
      <div>
        <h1>{title}</h1>
        {sub && <div className="sub">{sub}</div>}
      </div>
      <div className="topbar-right">{actions}</div>
    </div>
  );
}
