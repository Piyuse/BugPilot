export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

function errorText(data: unknown): string {
  if (typeof data === "string") return data;
  if (Array.isArray(data)) return data.map(errorText).join(" ");
  if (!data || typeof data !== "object") return "The request could not be completed.";
  return Object.entries(data).map(([key, value]) =>
    (key === "detail" || key === "non_field_errors" ? "" : key.replaceAll("_", " ") + ": ") + errorText(value)
  ).join("\n");
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = document.cookie.split("; ").find(value => value.startsWith("csrftoken="))?.slice("csrftoken=".length);
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("X-CSRFToken", decodeURIComponent(token));
  const response = await fetch("/api/" + path, { ...options, credentials: "same-origin", headers });
  let data: unknown;
  try { data = await response.json(); }
  catch { throw new ApiError("The server returned an unexpected response. Check that the Django backend is running.", response.status); }
  if (!response.ok) {
    if ((response.status === 401 || response.status === 403) && !path.startsWith("auth/")) {
      // A 403 may also mean a permissions error, so only reset the UI when
      // the session endpoint confirms that authentication has expired.
      void fetch("/api/auth/session/", { credentials: "same-origin" })
        .then(reply => reply.json())
        .then((session: { authenticated: boolean }) => {
          if (!session.authenticated) window.dispatchEvent(new Event("session-expired"));
        }).catch(() => {});
    }
    throw new ApiError(errorText(data), response.status);
  }
  return data as T;
}
