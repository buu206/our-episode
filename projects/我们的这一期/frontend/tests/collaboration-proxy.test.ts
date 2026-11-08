// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";
import { proxyRequest, type ProxyOptions } from "../src/lib/server-proxy";
import type { ApiRole } from "../src/lib/routes";

const id = "a".repeat(32), origin = "http://127.0.0.1:3000";
const roles: ApiRole[] = ["owner", "participant", "viewer"];
const fixture = "fixture-session-1234", csrf = "test_csrf_credential_1234567890";
const cookieName = (role: ApiRole, demo = false) => `episode_${demo ? "demo_" : ""}session${role === "owner" ? "" : `_${role}`}`;
const authPath = (role: ApiRole) => role === "owner" ? "/auth" : `/${role}/auth`;
const allCookies = roles.flatMap(role => [false, true].map(demo => `${cookieName(role, demo)}=${fixture}-${role}`)).join("; ");
function request(path: string, init: RequestInit & { duplex?: "half" } = {}) {
  const headers = new Headers({ Host: "127.0.0.1:3000", Cookie: allCookies, "X-CSRF-Token": csrf });
  if (init.method && !["GET", "HEAD"].includes(init.method)) headers.set("Origin", origin);
  if (typeof init.body === "string") headers.set("Content-Type", "application/json");
  new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  return new Request(`${origin}/api/v1${path}`, { ...init, headers });
}
const transport = (callback: (url: string, init: RequestInit) => Response | Promise<Response>) =>
  vi.fn((url: RequestInfo | URL, init?: RequestInit) => callback(String(url), init ?? {}));
function run(path: string, init: RequestInit & { duplex?: "half" }, fetchImpl: ReturnType<typeof transport>, options: ProxyOptions = {}) {
  return proxyRequest(request(path, init), path.split("?", 1)[0]!.slice(1).split("/"),
    { backendUrl: "http://127.0.0.1:8788", appOrigin: origin, ...options, fetchImpl: fetchImpl as typeof fetch });
}
afterEach(() => vi.useRealTimers());

describe("three-role proxy capabilities", () => {
  it.each(roles)("forwards only the %s cookie and refuses other roles' response cookies", async (role) => {
    const upstream = transport((_url, init) => {
      const headers = new Headers(init.headers);
      expect(headers.get("cookie")).toBe(`${cookieName(role)}=${fixture}-${role}`);
      expect(headers.get("x-csrf-token")).toBe(csrf);
      expect(headers.get("origin")).toBe("http://127.0.0.1:8788");
      expect(headers.has("authorization")).toBe(false);
      const output = new Headers();
      for (const item of roles) for (const demo of [false, true]) output.append("Set-Cookie", `${cookieName(item, demo)}=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; Path=/`);
      return Response.json({ ok: true }, { headers: output });
    });
    const result = await run(`${authPath(role)}/logout`, { method: "POST", body: "{}", headers: { Authorization: "unrelated" } }, upstream);
    expect(result.status).toBe(200); expect(await result.json()).toEqual({ ok: true });
    expect(result.headers.getSetCookie()).toHaveLength(1); expect(result.headers.getSetCookie()[0]).toMatch(new RegExp(`^${cookieName(role)}=`));
    expect(result.headers.get("cache-control")).toBe("no-store"); expect(result.headers.get("referrer-policy")).toBe("no-referrer");
  });

  it.each(roles)("keeps %s cookie selection separate for a demo instance", async (role) => {
    const upstream = transport((_url, init) => {
      expect(new Headers(init.headers).get("cookie")).toBe(`${cookieName(role, true)}=${fixture}-${role}`);
      return Response.json({ ok: true }, { headers: { "Set-Cookie": `${cookieName(role)}=${fixture}; Path=/` } });
    });
    const result = await run(`${authPath(role)}/me`, {}, upstream, { backendUrl: "http://127.0.0.1:39201", backendMode: "demo" });
    expect(result.status).toBe(200); await result.text(); expect(result.headers.getSetCookie()).toHaveLength(0);
  });

  it.each(roles)("requires %s's own cookie and rejects duplicates before connecting", async (role) => {
    const upstream = transport(() => Response.json({ ok: true }));
    const others = roles.filter(item => item !== role).map(item => `${cookieName(item)}=${fixture}`).join("; ");
    for (const cookie of [others, `${cookieName(role, true)}=${fixture}`, `${cookieName(role)}=${fixture}; ${cookieName(role)}=${fixture}`]) {
      const response = await run(`${authPath(role)}/me`, { headers: { Cookie: cookie } }, upstream);
      expect(response.status).toBe(401); expect((await response.json()).error.code).toBe(role === "owner" ? "AUTH_REQUIRED" : `${role.toUpperCase()}_AUTH_REQUIRED`);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each(["participant", "viewer"] as const)("allows public %s login while enforcing source checks and scoped Set-Cookie", async (role) => {
    const upstream = transport((_url, init) => {
      expect(new Headers(init.headers).has("cookie")).toBe(false);
      return Response.json({ ok: true }, { headers: { "Set-Cookie": `${cookieName(role)}=${fixture}; HttpOnly; SameSite=strict; Path=/` } });
    });
    const path = `${authPath(role)}/login`;
    const success = await run(path, { method: "POST", body: "{}", headers: { Cookie: `${cookieName("owner")}=${fixture}`, "X-CSRF-Token": "" } }, upstream);
    expect(success.status).toBe(200); await success.text(); expect(success.headers.getSetCookie()).toHaveLength(1);
    const denied = await run(path, { method: "POST", body: "{}", headers: { Origin: "http://external.invalid" } }, upstream);
    expect(denied.status).toBe(403); expect(upstream).toHaveBeenCalledTimes(1);
  });

  it.each(["participant", "viewer"] as const)("requires CSRF and exact Origin for %s writes", async (role) => {
    const upstream = transport(() => Response.json({ ok: true }));
    const cases: Record<string, string>[] = [{ "X-CSRF-Token": "" }, { Origin: "" }, { "Sec-Fetch-Site": "cross-site" }];
    for (const headers of cases) {
      const response = await run(`${authPath(role)}/logout`, { method: "POST", body: "{}", headers }, upstream);
      expect(response.status).toBe(403);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("preserves viewer Range/HEAD authorization including stale publication errors without retry", async () => {
    const path = `/viewer/shares/${id}/media?revision=3`;
    const bytes = new Uint8Array([6, 7, 8]);
    const allowed = transport((url, init) => {
      expect(url).toBe(`http://127.0.0.1:8788/api/v1${path}`);
      const headers = new Headers(init.headers);
      expect(headers.get("cookie")).toBe(`episode_session_viewer=${fixture}-viewer`);
      expect(headers.get("range")).toBe("bytes=6-8"); expect(headers.get("if-none-match")).toBe('"previous"');
      return new Response(init.method === "HEAD" ? null : bytes, { status: 206,
        headers: { "Content-Type": "video/mp4", "Content-Length": "3", "Content-Range": "bytes 6-8/100", "Accept-Ranges": "bytes" } });
    });
    for (const method of ["GET", "HEAD"]) {
      const response = await run(path, { method, headers: { Range: "bytes=6-8", "If-None-Match": '"previous"' } }, allowed);
      expect(response.status).toBe(206); expect(response.headers.get("content-range")).toBe("bytes 6-8/100");
      expect([...new Uint8Array(await response.arrayBuffer())]).toEqual(method === "GET" ? [...bytes] : []);
      expect(response.headers.get("cache-control")).toBe("no-store");
    }
    const denied = transport(() => Response.json({ error: { code: "SHARE_REVISION_CONFLICT", message: "发布已更新" } }, { status: 409 }));
    const response = await run(path, { headers: { Range: "bytes=0-1", "If-None-Match": '"previous"' } }, denied);
    expect(response.status).toBe(409); expect((await response.json()).error.code).toBe("SHARE_REVISION_CONFLICT"); expect(denied).toHaveBeenCalledTimes(1);
  });

  it("blocks unsafe viewer query capabilities without forwarding", async () => {
    const upstream = transport(() => Response.json({ ok: true }));
    for (const suffix of ["", "?revision=0", "?revision=1&download=true", "?revision=1&revision=2", "?revision=%31", "?revision=9007199254740992"]) {
      const response = await run(`/viewer/shares/${id}/media${suffix}`, {}, upstream);
      expect(response.status).toBe(404);
    }
    expect(upstream).not.toHaveBeenCalled();
  });

  it("streams participant multipart above 10MiB with boundaries intact and bounded buffering", async () => {
    let produced = 0, consumed = 0; const chunkSize = 64 * 1024, count = 176;
    const source = new ReadableStream<Uint8Array>({ pull(controller) {
      if (produced === count) { controller.close(); return; }
      controller.enqueue(new Uint8Array(chunkSize).fill(produced % 251)); produced += 1;
    } }, { highWaterMark: 0 });
    const upstream = transport(async (_url, init) => {
      expect(produced).toBe(0);
      expect(new Headers(init.headers).get("cookie")).toBe(`episode_session_participant=${fixture}-participant`);
      expect(new Headers(init.headers).get("content-type")).toBe("multipart/form-data; boundary=fixture");
      const reader = (init.body as ReadableStream<Uint8Array>).getReader();
      while (true) {
        const chunk = await reader.read(); if (chunk.done) break;
        expect(chunk.value.byteLength).toBe(chunkSize); expect(chunk.value[0]).toBe(consumed % 251); consumed += 1;
        expect(produced - consumed).toBeLessThanOrEqual(1);
      }
      return Response.json({ ok: true });
    });
    const response = await run("/participant/submissions/upload", { method: "POST", body: source, duplex: "half",
      headers: { "Content-Type": "multipart/form-data; boundary=fixture" } }, upstream);
    expect(response.status).toBe(200); await response.text(); expect(consumed * chunkSize).toBe(11 * 1024 * 1024);
  });

  it("keeps ordinary collaboration JSON at 64KiB and participant upload within the total byte limit", async () => {
    const upstream = transport(async (_url, init) => { await new Response(init.body).arrayBuffer(); return Response.json({ ok: true }); });
    const largeJson = await run("/participant/submissions", { method: "POST", body: "x".repeat(65537) }, upstream);
    expect(largeJson.status).toBe(413);
    const never = transport(() => Response.json({ ok: true }));
    const tooLarge = await run("/participant/submissions/upload", { method: "POST", body: "x", headers: {
      "Content-Type": "multipart/form-data; boundary=fixture", "Content-Length": String(1024 ** 3 + 1024 ** 2 + 1),
    } }, never);
    expect(tooLarge.status).toBe(413); expect(never).not.toHaveBeenCalled();
  });

  it("propagates participant upload cancellation without a second request", async () => {
    const controller = new AbortController(); let receivedAbort = false;
    const upstream = transport((_url, init) => new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => { receivedAbort = true; reject(new Error("cancelled")); }, { once: true });
      controller.abort();
    }));
    const response = await run("/participant/submissions/upload", { method: "POST", body: "data", signal: controller.signal,
      headers: { "Content-Type": "multipart/form-data; boundary=fixture" } }, upstream);
    expect(response.status).toBe(408); expect(receivedAbort).toBe(true); expect(upstream).toHaveBeenCalledTimes(1);
  });
});
