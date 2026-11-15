// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { api, setCsrf } from "../src/lib/api";
import { routeContract } from "../src/lib/routes";

const eid = "e".repeat(32), pid = "d".repeat(32);
afterEach(() => { setCsrf(null); vi.unstubAllGlobals(); });

it("limits director chat and explicit actions to owner routes and the intended methods", () => {
  expect(routeContract(`/episodes/${eid}/director/chat`, "GET")).toMatchObject({ kind: "directorChat", role: "owner", public: false });
  expect(routeContract(`/episodes/${eid}/director/chat`, "POST")).toMatchObject({ kind: "job", role: "owner", public: false });
  expect(routeContract(`/episodes/${eid}/director/actions/${pid}`, "POST")).toMatchObject({ kind: "directorChatAction", public: false });
  for (const method of ["GET", "DELETE", "PUT"]) expect(routeContract(`/episodes/${eid}/director/actions/${pid}`, method)).toBeNull();
  expect(routeContract(`/episodes/${eid}/director/chat?episode=other`, "GET")).toBeNull();
  expect(routeContract(`/participant/episodes/${eid}/director/chat`, "GET")).toBeNull();
});

it("reads real conversation and requires CSRF to accept a concrete proposal", async () => {
  const state = { messages: [{ id: pid, role: "assistant", text: "可以缩短旁白。", source: "model", created_at: "2026-11-13" }],
    proposals: [{ id: pid, message_id: pid, kind: "revise_narration", title: "缩短开场", explanation: "保留原声", narration_text: "在场，开拍！", status: "pending" }],
    can_chat: true, reason: "已就绪", mode: "cloud", pending_job_id: null, last_job_id: null, last_error: null };
  const fetcher = vi.fn(async (_url: string, init: RequestInit) => init.method === "GET" ? Response.json(state)
    : Response.json({ ok: true, next_step: "edit", version_id: pid }));
  vi.stubGlobal("fetch", fetcher);
  expect(await api(`/episodes/${eid}/director/chat`)).toEqual(state);
  await expect(api(`/episodes/${eid}/director/actions/${pid}`, { method: "POST", body: { confirm: true } })).rejects.toMatchObject({ code: "AUTH_REQUIRED" });
  setCsrf("test-csrf");
  expect(await api(`/episodes/${eid}/director/actions/${pid}`, { method: "POST", body: { confirm: true } })).toMatchObject({ version_id: pid });
  expect(new Headers(fetcher.mock.calls[1][1].headers).get("x-csrf-token")).toBe("test-csrf");
});

it("rejects malformed or unbounded model-facing states and navigation targets", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ messages: [{ role: "assistant", text: "假回复" }] })));
  await expect(api(`/episodes/${eid}/director/chat`)).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  setCsrf("test-csrf");
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ok: true, next_step: "https://other.example/" })));
  await expect(api(`/episodes/${eid}/director/actions/${pid}`, { method: "POST", body: { confirm: true } })).rejects.toMatchObject({ code: "INVALID_RESPONSE", uncertain: true });
});
