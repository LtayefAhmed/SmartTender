/**
 * API client.
 *
 * One place that knows how to talk to the backend: base URL, auth header, and
 * the platform's uniform error envelope. Every hook goes through `api()`.
 *
 * The base URL and key come from `window` (injected by nginx in the container,
 * empty in dev where Vite proxies `/api`). That is what lets a single build run
 * unchanged across environments.
 */

declare global {
  interface Window {
    __SMARTTENDER_API__?: string;
    __SMARTTENDER_API_KEY__?: string;
  }
}

const BASE = (window.__SMARTTENDER_API__ ?? "").replace(/\/$/, "");
const API_KEY = window.__SMARTTENDER_API_KEY__ ?? "dev-local-key";

/** Mirrors the backend's ErrorResponse. `code` is stable; branch on it. */
export class ApiError extends Error {
  code: string;
  status: number;
  detail?: Record<string, unknown>;
  retryable: boolean;
  constructor(status: number, body: any) {
    super(body?.message ?? `HTTP ${status}`);
    this.code = body?.code ?? `http_${status}`;
    this.status = status;
    this.detail = body?.detail;
    this.retryable = body?.retryable ?? false;
  }
}

export interface UserContext {
  userId: string;
}
// The demo UI acts as one operator; a real deployment gets this from auth.
export const USER_ID = "operator";

async function request<T>(
  method: string,
  path: string,
  opts: { body?: unknown; form?: FormData; query?: Record<string, unknown> } = {}
): Promise<T> {
  let url = BASE + path;
  if (opts.query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(opts.query)) {
      if (v === undefined || v === null || v === "") continue;
      if (Array.isArray(v)) v.forEach((item) => qs.append(k, String(item)));
      else qs.append(k, String(v));
    }
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }

  const headers: Record<string, string> = {
    "X-API-Key": API_KEY,
    "X-User-Id": USER_ID,
  };
  let payload: BodyInit | undefined;
  if (opts.form) {
    payload = opts.form; // browser sets multipart boundary
  } else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(opts.body);
  }

  const res = await fetch(url, { method, headers, body: payload });
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

function safeJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return { message: text };
  }
}

export const api = {
  get: <T>(path: string, query?: Record<string, unknown>) =>
    request<T>("GET", path, { query }),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, { body }),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, { body }),
  del: <T>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, form: FormData) => request<T>("POST", path, { form }),
};
