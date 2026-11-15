// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { proxyRequest, type ProxyOptions } from "../src/lib/server-proxy";
import { VOICE_BODY_LIMIT } from "../src/lib/routes";

const episode = "a".repeat(32);
const token = "fixture-token-1234";
const csrf = "test_csrf_credential_1234567890";
const origin = "http://127.0.0.1:3000";
const base: ProxyOptions = { backendUrl: "http://127.0.0.1:8788", appOrigin: origin };
function request(path: string, init: RequestInit & { duplex?: "half" } = {}): Request {
  const headers = new Headers({ Host: "127.0.0.1:3000", Cookie: `episode_session=${token}`, "X-CSRF-Token": csrf });
  if (init.method && !["GET", "HEAD"].includes(init.method)) headers.set("Origin", origin);
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");
  new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  return new Request(`${origin}/api/v1${path}`, { ...init, headers });
}
function parts(path: string): string[] { return path.split("?", 1)[0]!.slice(1).split("/"); }
function transport(callback: (url: string, init: RequestInit) => Promise<Response> | Response) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => callback(String(input), init ?? {}));
}
async function run(path: string, init: RequestInit & { duplex?: "half" }, fetchImpl: ReturnType<typeof transport>, options: ProxyOptions = {}) {
  return proxyRequest(request(path, init), parts(path), { ...base, ...options, fetchImpl: fetchImpl as typeof fetch });
}
afterEach(() => vi.useRealTimers());

describe("restricted same-origin proxy", () => {
  it.each(['pause','resume','cancel'])("permits only bounded director control %s", async action => {
    const upstream=transport(()=>Response.json({id:episode,kind:'director_run',state:'paused'}));
    expect((await run(`/versions/${episode}/director-run/${action}`,{method:'POST',body:'{}'},upstream)).status).toBe(200);
    expect((await run(`/versions/${episode}/director-run/approve`,{method:'POST',body:'{}'},upstream)).status).toBe(404);
    expect((await run(`/versions/${episode}/director-run/${action}`,{method:'GET'},upstream)).status).toBe(404);
  });
  it.each(['review', 'visual-review'])("permits an owner %s without opening arbitrary version actions", async (action) => {
    const upstream = transport(() => Response.json({ id: episode, kind: 'technical_review', state: 'queued' }));
    const response = await run(`/versions/${episode}/${action}`, { method: 'POST', body: '{}' }, upstream);
    expect(response.status).toBe(200);
    expect(upstream).toHaveBeenCalledOnce();
    expect((await run(`/versions/${episode}/${action}`, { method: 'DELETE' }, upstream)).status).toBe(404);
    expect((await run(`/versions/${episode}/anything`, { method: 'POST', body: '{}' }, upstream)).status).toBe(404);
  });
  it("bounds voice uploads separately and permits owner recovery reads", async () => {
    const upstream = transport(() => Response.json({ id: episode }));
    const path = `/episodes/${episode}/director/voice`;
    const oversized = await run(path, { method: "POST", body: "audio", headers: {
      "Content-Type": "multipart/form-data; boundary=fixture", "Content-Length": String(VOICE_BODY_LIMIT + 1),
    } }, upstream);
    expect(oversized.status).toBe(413); expect(upstream).not.toHaveBeenCalled();
    const drain = transport(async (_url, init) => { await new Response(init.body).arrayBuffer(); return Response.json({ id: episode }); });
    const streamed = await run(path, { method: "POST", body: "x".repeat(VOICE_BODY_LIMIT + 1),
      headers: { "Content-Type": "multipart/form-data; boundary=fixture" } }, drain);
    expect(streamed.status).toBe(413);
    const status = await run(path + "/status", {}, upstream); expect(status.status).toBe(200); await status.text();
    const recovery = await run(path + "/by-client/voice-request-123", {}, upstream); expect(recovery.status).toBe(200); await recovery.text();
    const invalid = await run(path + "/by-client/short", {}, upstream); expect(invalid.status).toBe(404);
  });
  it("forwards director requirements only as an owner POST", async () => {
    const upstream = transport((_url, init) => {
      expect(new Headers(init.headers).get("cookie")).toBe(`episode_session=${token}`);
      return Response.json({ revision: 1 });
    });
    const result = await run(`/episodes/${episode}/director/messages`, { method: "POST", body: JSON.stringify({ revision: 0, client_id: "request-one", choice: "auto" }) }, upstream);
    expect(result.status).toBe(200); await result.text();
    const invalid = await run(`/episodes/${episode}/director/messages`, { method: "GET" }, upstream);
    expect(invalid.status).toBe(404); expect(upstream).toHaveBeenCalledTimes(1);
  });
  it("accepts Next's internal localhost URL only with the configured external Host and Origin", async () => {
    const upstream = transport(() => Response.json({ ok: true }));
    const adapted = new Request("http://localhost:3000/api/v1/episodes", { method: "POST", body: "{}", headers: {
      Host: "127.0.0.1:3000", Origin: origin, Cookie: `episode_session=${token}`, "X-CSRF-Token": csrf, "Content-Type": "application/json",
    } });
    const result = await proxyRequest(adapted, ["episodes"], { ...base, fetchImpl: upstream as typeof fetch });
    expect(result.status).toBe(200); await result.text();
  });
  it("rewrites only a validated origin and forwards the matching owner credentials", async () => {
    const upstream = transport((_url, init) => {
      const h = new Headers(init.headers);
      expect(h.get("origin")).toBe("http://127.0.0.1:8788");
      expect(h.get("cookie")).toBe(`episode_session=${token}`);
      expect(h.get("x-csrf-token")).toBe(csrf);
      expect(h.has("x-forwarded-host")).toBe(false);
      expect(h.has("authorization")).toBe(false);
      expect(h.has("connection")).toBe(false);
      expect(init.redirect).toBe("manual");
      expect(init.cache).toBe("no-store");
      return Response.json({ id: episode });
    });
    const result = await run("/episodes", { method: "POST", body: "{}", headers: {
      cookie: `other=unrelated; episode_demo_session=${token}; episode_session=${token}`,
      "X-Forwarded-Host": "external.invalid", Authorization: "unrelated-value", Connection: "keep-alive",
    } }, upstream);
    expect(result.status).toBe(200); expect(await result.json()).toEqual({ id: episode });
    expect(result.headers.get("cache-control")).toBe("no-store");
  });

  it.each([
    [{ Host: "external.invalid" }, 400],
    [{ Origin: "http://external.invalid" }, 403],
    [{ Origin: "" }, 403],
    [{ "Sec-Fetch-Site": "same-site" }, 403],
    [{ "Sec-Fetch-Site": "cross-site" }, 403],
    [{ Cookie: `episode_demo_session=${token}` }, 401],
    [{ "X-CSRF-Token": "" }, 403],
    [{ Cookie: `episode_session=${token}; episode_session=${token}` }, 401],
  ])("rejects a hostile or wrong-environment request before connecting (%j)", async (headers, status) => {
    const upstream = transport(() => Response.json({ ok: true }));
    const response = await run("/episodes", { method: "POST", body: "{}", headers }, upstream);
    expect(response.status).toBe(status); expect(upstream).not.toHaveBeenCalled();
    expect((await response.json()).error).toHaveProperty("code");
  });

  it("also rejects cross-site media GET and does not grant participant/viewer owner rights", async () => {
    const upstream = transport(() => Response.json({ ok: true }));
    const cases: Record<string, string>[] = [{ "Sec-Fetch-Site": "cross-site" }, { Cookie: `episode_session_viewer=${token}` }, { Cookie: `episode_session_participant=${token}` }];
    for (const headers of cases) {
      const result = await run(`/versions/${episode}/media`, { headers }, upstream);
      expect([401, 403]).toContain(result.status);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each(["https://external.invalid", "http://external.invalid:9000", "http://127.0.0.1:8788/private", "http://someone:pass@127.0.0.1:8788"])("refuses an unsupported backend configuration: %s", async (backendUrl) => {
    const upstream = transport(() => Response.json({ ok: true }));
    const response = await run("/status", {}, upstream, { backendUrl });
    expect(response.status).toBe(503); expect(upstream).not.toHaveBeenCalled();
    expect(await response.text()).not.toContain(backendUrl);
  });

  it("supports fixed server-configured loopback test ports with an explicit demo cookie scope", async () => {
    const upstream = transport((url, init) => {
      expect(url).toBe("http://127.0.0.1:38190/api/v1/auth/me");
      expect(new Headers(init.headers).get("cookie")).toBe(`episode_demo_session=${token}`);
      return Response.json({ user_id: "owner", csrf_token: csrf });
    });
    const result = await run("/auth/me", { headers: { Cookie: `episode_demo_session=${token}; episode_session=${token}` } }, upstream,
      { backendUrl: "http://127.0.0.1:38190", backendMode: "demo" });
    expect(result.status).toBe(200); await result.text();
  });

  it.each(["/status?url=http://external.invalid", "/status?backend=8790", "/auth/login/extra", "/viewer/auth/me/extra", "/episodes//", "/%2e%2e/status", "/status#secret"])("blocks unknown paths/query targets: %s", async (path) => {
    const upstream = transport(() => Response.json({ ok: true }));
    const response = await run(path, {}, upstream);
    expect([400, 404]).toContain(response.status); expect(upstream).not.toHaveBeenCalled();
  });

  it("permits login without an existing cookie but still checks its source", async () => {
    const upstream = transport(() => Response.json({ user_id: "owner", csrf_token: csrf }));
    const result = await run("/auth/login", { method: "POST", body: "{}", headers: { Cookie: "", "X-CSRF-Token": "" } }, upstream);
    expect(result.status).toBe(200); await result.text();
  });

  it("keeps multiple same-role Set-Cookie values while filtering other identities and hop-by-hop headers", async () => {
    const upstream = transport(() => {
      const headers = new Headers({ Connection: "x-private-hop", "x-private-hop": "discard", "Cache-Control": "public, max-age=9999", "Content-Type": "application/json" });
      headers.append("Set-Cookie", `episode_session=${token}; HttpOnly; SameSite=strict; Path=/`);
      headers.append("Set-Cookie", "episode_session_viewer=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Path=/");
      headers.append("Set-Cookie", "episode_session=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Path=/");
      headers.append("Set-Cookie", `episode_demo_session=${token}; Path=/`);
      return new Response("{}", { headers });
    });
    const result = await run("/status", {}, upstream);
    expect(result.headers.getSetCookie()).toHaveLength(2);
    expect(result.headers.getSetCookie()[1]).toContain("Thu, 01 Jan");
    expect(result.headers.getSetCookie().every(value => value.startsWith("episode_session="))).toBe(true);
    expect(result.headers.has("connection")).toBe(false);
    expect(result.headers.has("x-private-hop")).toBe(false);
    expect(result.headers.get("cache-control")).toBe("no-store"); await result.text();
  });

  it("passes Range and conditional headers to the authoritative backend and preserves a 409", async () => {
    const upstream = transport((_url, init) => {
      const headers = new Headers(init.headers);
      expect(headers.get("range")).toBe("bytes=0-127");
      expect(headers.get("if-none-match")).toBe('"old"');
      return Response.json({ error: { code: "SOURCE_REMOVED", message: "来源已删除" } }, { status: 409 });
    });
    const result = await run(`/versions/${episode}/media`, { headers: { Range: "bytes=0-127", "If-None-Match": '"old"' } }, upstream);
    expect(result.status).toBe(409); expect((await result.json()).error.code).toBe("SOURCE_REMOVED");
  });

  it("preserves partial media bytes and HEAD metadata without reading its body", async () => {
    const get = transport(() => new Response(new Uint8Array([0, 1, 2]), { status: 206,
      headers: { "Content-Type": "video/mp4", "Content-Range": "bytes 0-2/99", "Content-Length": "3", "Accept-Ranges": "bytes", "Content-Disposition": "inline" } }));
    const result = await run(`/versions/${episode}/media`, { headers: { Range: "bytes=0-2" } }, get);
    expect(result.status).toBe(206); expect(result.headers.get("content-range")).toBe("bytes 0-2/99");
    expect([...new Uint8Array(await result.arrayBuffer())]).toEqual([0, 1, 2]);
    const head = transport(() => new Response(null, { headers: { "Content-Length": "999", "Content-Type": "video/mp4" } }));
    const headResult = await run(`/versions/${episode}/media`, { method: "HEAD" }, head);
    expect(headResult.headers.get("content-length")).toBe("999"); expect(await headResult.text()).toBe("");
  });

  it("streams an upload above 10MiB without truncation or multipart decoding", async () => {
    const count = 176, size = 64 * 1024;
    let sent = 0, consumed = 0;
    const source = new ReadableStream<Uint8Array>({ pull(controller) {
      if (sent === count) { controller.close(); return; }
      controller.enqueue(new Uint8Array(size).fill(sent % 251)); sent += 1;
    } }, { highWaterMark: 0 });
    const upstream = transport(async (_url, init) => {
      expect(sent).toBe(0);
      expect(new Headers(init.headers).get("content-type")).toBe("multipart/form-data; boundary=fixture");
      const reader = (init.body as ReadableStream<Uint8Array>).getReader();
      while (true) {
        const row = await reader.read(); if (row.done) break;
        expect(row.value.byteLength).toBe(size); expect(row.value[0]).toBe(consumed % 251); consumed += 1;
        expect(sent - consumed).toBeLessThanOrEqual(1);
      }
      return Response.json({ ok: true });
    });
    const response = await run(`/episodes/${episode}/assets`, { method: "POST", body: source, duplex: "half",
      headers: { "Content-Type": "multipart/form-data; boundary=fixture" } }, upstream);
    expect(response.status).toBe(200); await response.text();
    expect(consumed * size).toBe(11 * 1024 * 1024);
  });

  it("checks declared limits before sending and enforces streamed bytes even without Content-Length", async () => {
    const unreachable = transport(() => Response.json({ ok: true }));
    const tooBig = await run(`/episodes/${episode}/assets`, { method: "POST", body: "x", headers: {
      "Content-Type": "multipart/form-data; boundary=fixture", "Content-Length": String(1024 ** 3 + 1024 ** 2 + 1),
    } }, unreachable);
    expect(tooBig.status).toBe(413); expect(unreachable).not.toHaveBeenCalled();
    const drain = transport(async (_url, init) => {
      await new Response(init.body).arrayBuffer(); return Response.json({ ok: true });
    });
    const result = await run("/episodes", { method: "POST", body: "x".repeat(65_537) }, drain);
    expect(result.status).toBe(413); expect((await result.json()).error.code).toBe("REQUEST_TOO_LARGE");
    const evidence = await run(`/episodes/${episode}/evidence`, { method: "PUT", body: "x".repeat(80_000) }, drain);
    expect(evidence.status).toBe(200); await evidence.text();
  });

  it("forwards Next's non-null but empty DELETE stream without requiring a content type", async () => {
    const upstream = transport((_url, init) => {
      expect(init.method).toBe("DELETE"); expect(init.body).toBeUndefined();
      expect(new Headers(init.headers).has("content-type")).toBe(false);
      return Response.json({ ok: true });
    });
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.close(); } });
    const response = await run(`/assets/${episode}?confirm=true`, { method: "DELETE", body, duplex: "half" }, upstream);
    expect(response.status).toBe(200); expect(await response.json()).toEqual({ ok: true });
    expect(upstream).toHaveBeenCalledTimes(1);
  });

  it("rejects a genuinely nonempty untyped DELETE before contacting the backend", async () => {
    const upstream = transport(() => Response.json({ ok: true }));
    const body = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array()); controller.enqueue(new TextEncoder().encode("{}")); controller.close();
    } });
    const response = await run(`/assets/${episode}?confirm=true`, { method: "DELETE", body, duplex: "half" }, upstream);
    expect(response.status).toBe(415); expect((await response.json()).error.code).toBe("INVALID_CONTENT_TYPE");
    expect(upstream).not.toHaveBeenCalled();
  });

  it("bounds a stalled untyped request without sending a deletion upstream", async () => {
    vi.useFakeTimers(); const cancelled = vi.fn();
    const upstream = transport(() => Response.json({ ok: true }));
    const body = new ReadableStream<Uint8Array>({ cancel: cancelled }, { highWaterMark: 0 });
    const pending = run(`/assets/${episode}?confirm=true`, { method: "DELETE", body, duplex: "half" }, upstream,
      { timeouts: { jsonMs: 20, idleMs: 100 } });
    await vi.advanceTimersByTimeAsync(21); const response = await pending;
    expect(response.status).toBe(504); expect(cancelled).toHaveBeenCalled(); expect(upstream).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([301, 302, 307, 308])("does not follow upstream redirect %s", async (status) => {
    const upstream = transport((_url, init) => { expect(init.redirect).toBe("manual"); return new Response(null, { status, headers: { Location: "http://external.invalid/private" } }); });
    const result = await run("/status", {}, upstream);
    expect(result.status).toBe(502); expect(await result.text()).not.toContain("external.invalid");
    expect(upstream).toHaveBeenCalledTimes(1);
  });

  it("cancels an upstream request on the client abort without an automatic retry", async () => {
    const controller = new AbortController(); let upstreamAborted = false;
    const upstream = transport((_url, init) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => { upstreamAborted = true; reject(new Error("private transport detail")); });
    }));
    const pending = run("/episodes", { method: "POST", body: "{}", signal: controller.signal }, upstream);
    controller.abort(); const result = await pending;
    expect(result.status).toBe(408); expect(upstreamAborted).toBe(true); expect(upstream).toHaveBeenCalledTimes(1);
    expect(await result.text()).not.toContain("private transport detail");
  });

  it("uses a longer media timeout, and downstream cancellation releases the upstream reader", async () => {
    vi.useFakeTimers(); let cancelled = false;
    const upstream = transport(() => new Response(new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array([1])); }, cancel() { cancelled = true; },
    }, { highWaterMark: 0 })));
    const result = await run(`/versions/${episode}/media`, {}, upstream, { timeouts: { jsonMs: 10, mediaMs: 1000, idleMs: 1000 } });
    await vi.advanceTimersByTimeAsync(20); expect(cancelled).toBe(false);
    await result.body!.cancel(); expect(cancelled).toBe(true); expect(vi.getTimerCount()).toBe(0);
  });

  it("bounds an unresponsive upstream and cleans timers", async () => {
    vi.useFakeTimers();
    const upstream = transport((_url, init) => new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(new Error("timeout")))));
    const pending = run("/status", {}, upstream, { timeouts: { jsonMs: 15, idleMs: 100 } });
    await vi.advanceTimersByTimeAsync(16); const result = await pending;
    expect(result.status).toBe(504); expect(vi.getTimerCount()).toBe(0);
    expect((await result.json()).error.code).toBe("UPSTREAM_TIMEOUT");
  });
});
