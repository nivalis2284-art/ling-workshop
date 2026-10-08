const SESSION_KEY = "lingnexus.owner-session";

// Called once before React mounts, including in StrictMode. Never use localStorage.
export function consumeOwnerSession(): string | null {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  if (fragment.has("token")) {
    const token = fragment.get("token")?.trim() || null;
    fragment.delete("token");
    const rest = fragment.toString();
    window.history.replaceState(
      null,
      "",
      window.location.pathname +
        window.location.search +
        (rest ? "#" + rest : ""),
    );
    try {
      if (token) window.sessionStorage.setItem(SESSION_KEY, token);
      else window.sessionStorage.removeItem(SESSION_KEY);
    } catch {
      /* A blocked storage area still permits this in-memory session. */
    }
    return token;
  }
  try {
    return window.sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export function forgetOwnerSession() {
  try {
    window.sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* No stored session to remove. */
  }
}
