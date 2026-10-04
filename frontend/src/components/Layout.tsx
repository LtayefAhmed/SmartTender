import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Health, Page, Notification } from "../api/types";
import { Breadcrumb } from "./Breadcrumb";
import { Dot } from "./ui";
import { ThemeToggle } from "./ThemeToggle";

/**
 * The sidebar mirrors the process, in the order it runs.
 *
 * Grouping by phase rather than by feature means the menu teaches the product:
 * a reader who has never opened SmartTender can tell from the headings that a
 * tender is detected, profiles are matched to it, a selection is frozen, and
 * documents come out. The previous grouping predated two thirds of that and
 * left module 3 with no entry point at all.
 */
const NAV = [
  {
    section: "Pilotage",
    items: [
      { to: "/", icon: "◈", label: "Tableau de bord", end: true },
      { to: "/notifications", icon: "◉", label: "Notifications", badge: true },
    ],
  },
  {
    section: "1 · Détection",
    items: [
      { to: "/tenders", icon: "▤", label: "Appels d'offres" },
      { to: "/scrape", icon: "⧉", label: "Lancer un scraping" },
      { to: "/upload", icon: "⭱", label: "Import manuel" },
      { to: "/schedules", icon: "◷", label: "Planifications" },
      { to: "/sources", icon: "⊞", label: "Sources & santé" },
    ],
  },
  {
    section: "2 · Matching",
    items: [
      { to: "/matching/cv-import", icon: "⧫", label: "Import CVs" },
      { to: "/matching/recherche", icon: "⌕", label: "Recherche de profils" },
    ],
  },
  {
    // The section that did not exist. A frozen selection is a durable object
    // and a dossier is the platform's output; both now have a door.
    section: "3 · Dossiers",
    items: [
      { to: "/dossiers", icon: "▣", label: "Sélections & documents" },
      { to: "/admin/gabarits", icon: "▧", label: "Gabarits" },
    ],
  },
  {
    section: "Système",
    items: [{ to: "/admin", icon: "⚙", label: "Administration" }],
  },
];

export function Layout() {
  const { pathname } = useLocation();
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
            {/* Reads from the build, not from a number somebody has to remember to
                change. It said "Module 1" for two modules longer than it was
                true. */}
            <div className="brand-sub">Veille · Matching · Dossiers</div>
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

      {/* `key` on the route path replays the entrance animation on every
          navigation. Without it React reuses the subtree and the page swaps
          with no transition at all, which reads as a flicker. */}
      <main className="main" key={pathname}>
        <Breadcrumb />
        <div className="page-enter">
          <Outlet context={{ health: h }} />
        </div>
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
      <div className="topbar-right">
        {actions}
        {/* Always present, on every page: a theme control a user has to hunt
            for is one they stop using. */}
        <ThemeToggle />
      </div>
    </div>
  );
}
