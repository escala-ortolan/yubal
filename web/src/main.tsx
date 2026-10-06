import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ErrorBoundary } from "./components/common/error-boundary";
import { JobsProvider } from "./features/jobs/jobs-context";
import { ThemeProvider } from "./hooks/use-theme";
import "./index.css";
import { router } from "./router";
import { IntakeDashboard } from "./features/intake/dashboard";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <ThemeProvider>
        {document
          .querySelector('meta[name="yubal-intake-only"]')
          ?.getAttribute("content") === "true" ||
        import.meta.env.VITE_INTAKE_ONLY === "true" ? (
          <IntakeDashboard />
        ) : (
          <JobsProvider>
            <main className="text-foreground">
              <RouterProvider router={router} />
            </main>
          </JobsProvider>
        )}
      </ThemeProvider>
    </ErrorBoundary>
  </StrictMode>,
);
