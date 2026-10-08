import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { consumeOwnerSession } from "./api/session";
import "./styles.css";

const initialToken = consumeOwnerSession();
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App initialToken={initialToken} />
    </ErrorBoundary>
  </StrictMode>,
);
