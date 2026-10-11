import "@testing-library/jest-dom/vitest";
import { Blob as PlatformBlob } from "node:buffer";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// jsdom exposes its own Blob, but the platform Response returns a Blob from
// another realm, so `response.blob() instanceof Blob` fails in tests. Real
// browsers have a single realm; align the test environment with the platform.
globalThis.Blob = PlatformBlob as unknown as typeof Blob;

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});
