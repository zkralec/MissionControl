import { Navigate, createBrowserRouter } from "react-router-dom";
import { AppShell } from "@/app/layouts/app-shell";
import { AlertsPage } from "@/pages/alerts-page";
import { HomePage } from "@/pages/home-page";
import { ObservabilityPage } from "@/pages/observability-page";
import { RunsPage } from "@/pages/runs-page";
import { SettingsPage } from "@/pages/settings-page";
import { WorkflowsPage } from "@/pages/workflows-page";

export const legacyRedirects = {
  "/dashboard": "/",
  "/tasks": "/runs",
  "/automations": "/workflows",
  "/system": "/observability"
} as const;

export const appRoutes = [
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "workflows", element: <WorkflowsPage /> },
      { path: "runs", element: <RunsPage /> },
      { path: "alerts", element: <AlertsPage /> },
      { path: "settings", element: <SettingsPage /> },
      { path: "observability", element: <ObservabilityPage /> },
      { path: "dashboard", element: <Navigate to="/" replace /> },
      { path: "tasks", element: <Navigate to="/runs" replace /> },
      { path: "automations", element: <Navigate to="/workflows" replace /> },
      { path: "system", element: <Navigate to="/observability" replace /> },
      { path: "*", element: <Navigate to="/" replace /> }
    ]
  }
];

export const router = createBrowserRouter(appRoutes, {
  basename: import.meta.env.BASE_URL
});
