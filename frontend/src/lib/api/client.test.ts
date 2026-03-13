import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest, getRuntimeApiKey, setRuntimeApiKey } from "@/lib/api/client";

describe("api client", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    setRuntimeApiKey("");
  });

  it("injects X-API-Key header when set", async () => {
    setRuntimeApiKey("abc123");

    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "content-type": "application/json" }
      })
    );

    await apiRequest<{ ok: boolean }>("/health");

    const requestInit = fetchSpy.mock.calls[0]?.[1];
    const headers = new Headers((requestInit as RequestInit).headers);
    expect(headers.get("X-API-Key")).toBe("abc123");
    expect(getRuntimeApiKey()).toBe("abc123");
  });

  it("throws ApiError for non-2xx response", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" }
      })
    );

    await expect(apiRequest("/tasks")).rejects.toBeInstanceOf(ApiError);
  });
});
