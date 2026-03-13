export class ApiError extends Error {
  status: number;
  path: string;
  body: unknown;

  constructor(message: string, status: number, path: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
    this.body = body;
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  headers?: HeadersInit;
  timeoutMs?: number;
};

let runtimeApiKey = "";

const envBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim();
const defaultTimeout = Number(import.meta.env.VITE_REQUEST_TIMEOUT_MS || 15000);

export function setRuntimeApiKey(apiKey: string): void {
  runtimeApiKey = apiKey.trim();
}

export function getRuntimeApiKey(): string {
  return runtimeApiKey;
}

function resolvePath(path: string): string {
  if (!envBaseUrl) return path;
  return `${envBaseUrl.replace(/\/$/, "")}${path}`;
}

async function readResponseBody(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  return response.text();
}

async function requestRaw(path: string, options: RequestOptions = {}): Promise<Response> {
  const timeoutMs = options.timeoutMs ?? defaultTimeout;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const headers = new Headers(options.headers || {});
  if (runtimeApiKey) headers.set("X-API-Key", runtimeApiKey);
  if (options.body !== undefined && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  try {
    return await fetch(resolvePath(path), {
      method: options.method || "GET",
      headers,
      body:
        options.body === undefined
          ? undefined
          : options.body instanceof FormData
            ? options.body
            : JSON.stringify(options.body),
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(`Request timed out after ${timeoutMs}ms`, 408, path, null);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await requestRaw(path, options);
  const body = await readResponseBody(response);

  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : response.statusText;
    throw new ApiError(detail || `Request failed (${response.status})`, response.status, path, body);
  }

  return body as T;
}

export async function apiRequestText(path: string, options: RequestOptions = {}): Promise<string> {
  const response = await requestRaw(path, options);
  const body = await response.text();
  if (!response.ok) {
    throw new ApiError(body || response.statusText, response.status, path, body);
  }
  return body;
}
