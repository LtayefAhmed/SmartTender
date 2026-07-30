import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { ToastProvider } from "./components/toast";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Tenders } from "./pages/Tenders";
import { Scrape } from "./pages/Scrape";
import { Upload } from "./pages/Upload";
import { Schedules } from "./pages/Schedules";
import { Sources } from "./pages/Sources";
import { Notifications } from "./pages/Notifications";
import { Admin } from "./pages/Admin";
import "./styles.css";

const qc = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 5000 },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "tenders", element: <Tenders /> },
      { path: "scrape", element: <Scrape /> },
      { path: "upload", element: <Upload /> },
      { path: "schedules", element: <Schedules /> },
      { path: "sources", element: <Sources /> },
      { path: "notifications", element: <Notifications /> },
      { path: "admin", element: <Admin /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={qc}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </React.StrictMode>
);
