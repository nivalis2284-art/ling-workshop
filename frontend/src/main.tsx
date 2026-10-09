import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Base styles must come before the feature stylesheets so that pages can
// override them; the import order used to put them last.
import "./styles.css";
import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { consumeOwnerSession } from "./api/session";

const initialToken = consumeOwnerSession();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App initialToken={initialToken} />
    </ErrorBoundary>
  </StrictMode>,
);
