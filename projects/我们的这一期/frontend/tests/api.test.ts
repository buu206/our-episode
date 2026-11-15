// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, AppError, exportUrl, mediaUrl, prepareExport, setCsrf, uploadAsset, uploadDirectorVoice } from "../src/lib/api";
import { EpisodeSchema, SegmentSchema, type Episode, type Status } from "../src/lib/types";

const id = "a".repeat(32);
const status: Status = { cloud_enabled: false, cloud_reason: "固定演示非真实AI", media_ready: true, stage: "阶段2-3", workflow_enabled: true, model_mode: "demo" };
const asset = { id, filename: "fixture.mp4", duration: 1, width: 1280, height: 720, has_audio: false, size: 100,
  media_url: `/api/v1/assets/${id}/media`, source_kind: "original", contributor: null, interview_text: null, fixed_segment: null };
const episode = { id, title: "一期", background: "", created_at: "2026-11-08T01:00:00Z", assets: [asset], versions: [], jobs: [],
  directions: [], evidence: [], evidence_revision: 1, evidence_confirmed_revision: null, analysis_state: "empty", analysis_notes: [], analysis_coverage: [],
  directions_revision: null, selected_direction: null, budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 } };
beforeEach(() => setCsrf(null));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); setCsrf(null); });

describe("core API contract", () => {
  it("saves bounded director requirements through the owner route and validates its state", async () => {
    setCsrf("csrf-fixture");
    const state = { revision: 1, focus: "conversation", notes: "", directions_current: false,
      messages: [{ id: "request-01", role: "user", text: "保留聊天", created_at: "2026-11-13T12:00:00Z" }] };
    const fetcher = vi.fn(async (_path, init: RequestInit) => {
      expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-fixture");
      return Response.json(state);
    }); vi.stubGlobal("fetch", fetcher);
    expect(await api(`/episodes/${id}/director/messages`, { method: "POST", body: { revision: 0, client_id: "request-01", choice: "conversation" } })).toEqual(state);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("preserves disabled-cloud / enabled-demo distinction and makes same-origin no-store reads", async () => {
    const fetcher = vi.fn(async (_path, init: RequestInit) => {
      expect(init.credentials).toBe("same-origin"); expect(init.cache).toBe("no-store");
      expect(new Headers(init.headers).has("x-csrf-token")).toBe(false);
      return Response.json(status);
    }); vi.stubGlobal("fetch", fetcher);
    expect(await api<Status>("/status")).toEqual(status);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("validates server responses and retains legitimate unknown states without granting invented success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...episode, analysis_state: "future_review" })));
    expect((await api<Episode>(`/episodes/${id}`)).analysis_state).toBe("future_review");
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...episode, assets: [{ ...asset, media_url: "https://external.invalid/media" }] })));
    await expect(api(`/episodes/${id}`)).rejects.toMatchObject({ code: "INVALID_RESPONSE", uncertain: false });
  });

  it("does not invent zero quota or a clean deletion queue for an older response", () => {
    const parsed = EpisodeSchema.parse(episode);
    expect(parsed.quota).toBeUndefined(); expect(parsed.pending_cleanup).toBeUndefined();
    const upgraded = EpisodeSchema.parse({ ...episode, quota: { original_count: 4, total_seconds: 8, total_bytes: 1000, max_count: 8, max_seconds: 600, max_bytes: 1024 ** 3 }, pending_cleanup: [{ asset_id: id, filename: "待清理.mp4" }] });
    expect(upgraded.quota?.original_count).toBe(4); expect(upgraded.pending_cleanup).toHaveLength(1);
  });

  it("matches backend Unicode character limits, not UTF-16 code units", () => {
    expect(SegmentSchema.safeParse({ asset_id: id, start: 0, end: 1, caption: "😀".repeat(100) }).success).toBe(true);
    expect(SegmentSchema.safeParse({ asset_id: id, start: 0, end: 1, caption: "😀".repeat(101) }).success).toBe(false);
  });

  it("preserves director evidence and transitions across response parsing", async () => {
    const segment = { asset_id: id, start: 0, end: 4, caption: "开场", purpose: "交代本期场景",
      evidence_ids: ["source-one"], transition: "cut", transition_duration: 0 };
    const version = { id, title: "导演版", state: "draft", origin: "ai", segments: [segment],
      media_url: null, job_id: null, error: null, base_version_id: null, created_at: "2026-11-13T00:00:00Z",
      narration_text: "", narration_approved: false, narration_ready: false, packaging: "original", evidence_revision: 1 };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ ...episode, versions: [version] })));
    const result = await api<Episode>(`/episodes/${id}`);
    expect(result.versions[0].segments[0]).toEqual(segment);
  });

  it("keeps CSRF only in memory, sends it for writes, and clears it at logout boundaries", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const fetcher = vi.fn(async (_path, init: RequestInit) => {
      expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-fixture"); return Response.json(episode);
    }); vi.stubGlobal("fetch", fetcher); setCsrf("csrf-fixture");
    await api("/episodes", { method: "POST", body: { title: "一期" } });
    setCsrf(null);
    await expect(api("/episodes", { method: "POST", body: {} })).rejects.toMatchObject({ code: "AUTH_REQUIRED" });
    expect(fetcher).toHaveBeenCalledTimes(1); expect(storage).not.toHaveBeenCalled();
  });

  it("announces expired sessions once, but not invalid login or a stale response from an older session", async () => {
    const expired = vi.fn(); window.addEventListener("episode:auth-expired", expired);
    const rejected = () => Response.json({ error: { code: "AUTH_REQUIRED", message: "需要登录" } }, { status: 401 });
    try {
      vi.stubGlobal("fetch", vi.fn(async () => rejected()));
      await expect(api("/auth/login", { method: "POST", body: { invite_code: "wrong" } })).rejects.toMatchObject({ status: 401 });
      expect(expired).not.toHaveBeenCalled();
      setCsrf("old-session");
      let resolve!: (value: Response) => void;
      vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((done) => { resolve = done; })));
      const pending = api("/auth/me"); setCsrf("new-session"); resolve(rejected());
      await expect(pending).rejects.toMatchObject({ status: 401 }); expect(expired).not.toHaveBeenCalled();
      vi.stubGlobal("fetch", vi.fn(async () => rejected()));
      await expect(api("/auth/me")).rejects.toMatchObject({ status: 401 }); expect(expired).toHaveBeenCalledTimes(1);
      await expect(api("/episodes", { method: "POST", body: {} })).rejects.toMatchObject({ code: "AUTH_REQUIRED" });
    } finally { window.removeEventListener("episode:auth-expired", expired); }
  });

  it.each(["https://external.invalid/status", "//external.invalid/status", "/status?backend=evil", "/%2e%2e/auth/me"])("blocks arbitrary client paths: %s", async (path) => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    await expect(api(path)).rejects.toMatchObject({ code: "INVALID_API_PATH" }); expect(fetcher).not.toHaveBeenCalled();
  });

  it("returns structured errors and flags unknown write outcomes without retrying", async () => {
    setCsrf("csrf-fixture");
    const fetcher = vi.fn(async () => { throw new Error("private transport details"); }); vi.stubGlobal("fetch", fetcher);
    await expect(api("/episodes", { method: "POST", body: {} })).rejects.toMatchObject({ code: "NETWORK_ERROR", uncertain: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "COST_UNKNOWN", message: "保留占用，不能重试" } }, { status: 409 })));
    await expect(api(`/jobs/${id}/retry`, { method: "POST", body: {} })).rejects.toMatchObject({ code: "COST_UNKNOWN", status: 409 });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "DELETION_PENDING", message: "仍需清理" } }, { status: 503 })));
    await expect(api(`/assets/${id}?confirm=true`, { method: "DELETE" })).rejects.toMatchObject({ code: "DELETION_PENDING", uncertain: true });
  });

  it("preserves cancellation signals and treats an aborted write as potentially committed", async () => {
    setCsrf("csrf-fixture"); const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn(async (_path, init: RequestInit) => {
      expect(init.signal).toBe(controller.signal); controller.abort(); throw new DOMException("abort", "AbortError");
    }));
    await expect(api("/episodes", { method: "POST", body: {}, signal: controller.signal })).rejects.toMatchObject({ name: "AbortError", code: "ABORTED", uncertain: true });
  });

  it("checks actual UTF-8 body size, allows bounded evidence above the old JSON limit, and never loses input", async () => {
    setCsrf("csrf-fixture"); const fetcher = vi.fn(async () => Response.json(episode)); vi.stubGlobal("fetch", fetcher);
    const body = { revision: 1, items: [{ note: "中".repeat(30_000) }], confirmed: true };
    await api(`/episodes/${id}/evidence`, { method: "PUT", body });
    expect(body.items[0]?.note).toHaveLength(30_000);
    await expect(api("/episodes", { method: "POST", body: { title: "中".repeat(30_000) } })).rejects.toMatchObject({ code: "REQUEST_TOO_LARGE" });
    await expect(api(`/episodes/${id}/evidence`, { method: "PUT", body: { note: "中".repeat(700_000) } })).rejects.toMatchObject({ code: "REQUEST_TOO_LARGE" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("returns only controlled same-origin media/export paths", () => {
    expect(exportUrl(id)).toBe(`/api/v1/versions/${id}/export`);
    expect(mediaUrl(asset.media_url)).toBe(asset.media_url);
    expect(mediaUrl("https://external.invalid/file")).toBeNull();
    expect(mediaUrl("/api/v1/auth/me")).toBeNull();
    expect(mediaUrl("/api/v1/versions/../../private/media")).toBeNull();
    expect(() => exportUrl("../private")).toThrow();
  });

  it("checks export authorization with HEAD without creating a full Blob", async () => {
    let cancelled = false;
    vi.stubGlobal("fetch", vi.fn(async (_path, init: RequestInit) => {
      expect(init.method).toBe("HEAD");
      return new Response(new ReadableStream({ cancel() { cancelled = true; } }), { status: 200, headers: { "Content-Type": "video/mp4" } });
    }));
    expect(await prepareExport(id)).toBe(exportUrl(id)); expect(cancelled).toBe(true);
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: { code: "SOURCE_REMOVED", message: "来源已删除" } }, { status: 409 })));
    await expect(prepareExport(id)).rejects.toMatchObject({ code: "SOURCE_REMOVED", status: 409 });
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 409 })));
    await expect(prepareExport(id)).rejects.toMatchObject({ code: "HTTP_ERROR", status: 409 });
  });
});

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload: { onprogress?: (event: ProgressEvent) => void } = {};
  onload?: () => void; onerror?: () => void; ontimeout?: () => void; onabort?: () => void;
  status = 201; responseText = JSON.stringify(asset); withCredentials = false; timeout = 0;
  headers = new Map<string, string>(); body?: Document | XMLHttpRequestBodyInit | null;
  constructor() { FakeXHR.instances.push(this); }
  open = vi.fn();
  setRequestHeader(name: string, value: string) { this.headers.set(name.toLowerCase(), value); }
  send(body?: Document | XMLHttpRequestBodyInit | null) { this.body = body; }
  abort() { this.onabort?.(); }
}

describe("XHR upload recovery", () => {
  beforeEach(() => { FakeXHR.instances = []; vi.stubGlobal("XMLHttpRequest", FakeXHR); setCsrf("csrf-fixture"); });
  it("sends voice only with explicit consent and a bounded multipart owner request", async () => {
    const consent = { client_id: "voice-request-123", confirm_send: true, accept_unknown_retention: true } as const;
    const result = { id, job_id: id, state: "queued", text: null, error: null, mode: "cloud" };
    const pending = uploadDirectorVoice(id, new File(["audio"], "recording.webm", { type: "audio/webm" }), consent);
    const xhr = FakeXHR.instances[0]!;
    expect(xhr.open).toHaveBeenCalledWith("POST", `/api/v1/episodes/${id}/director/voice`);
    expect(xhr.headers.get("x-csrf-token")).toBe("csrf-fixture");
    expect(xhr.headers.has("content-type")).toBe(false);
    expect((xhr.body as FormData).get("accept_unknown_retention")).toBe("true");
    expect((xhr.body as FormData).get("client_id")).toBe(consent.client_id);
    expect(xhr.timeout).toBe(120_000);
    xhr.responseText = JSON.stringify(result); xhr.onload?.(); expect(await pending).toEqual(result);
    await expect(uploadDirectorVoice(id, new File([""], "empty.webm"), consent)).rejects.toMatchObject({ code: "UPLOAD_TOO_LARGE" });
    await expect(uploadDirectorVoice(id, new File(["x"], "fake.exe"), consent)).rejects.toMatchObject({ code: "INVALID_FORMAT" });
    await expect(uploadDirectorVoice(id, new File(["x"], "recording.wav"), { ...consent, accept_unknown_retention: false } as unknown as typeof consent)).rejects.toMatchObject({ code: "INVALID_REQUEST" });
    expect(FakeXHR.instances).toHaveLength(1);
  });
  it("keeps uncertain voice requests for by-client reconciliation without automatic resend", async () => {
    const pending = uploadDirectorVoice(id, new File(["audio"], "recording.m4a"),
      { client_id: "voice-request-unknown", confirm_send: true, accept_unknown_retention: true });
    FakeXHR.instances[0]!.ontimeout?.();
    await expect(pending).rejects.toMatchObject({ uncertain: true });
    expect(FakeXHR.instances).toHaveLength(1);
  });
  it("reports real transport progress while awaiting server validation, without overriding multipart boundary", async () => {
    const progress = vi.fn(); const pending = uploadAsset(id, new File(["test"], "fixture.mp4"), progress);
    const xhr = FakeXHR.instances[0]!;
    expect(xhr.withCredentials).toBe(true); expect(xhr.headers.has("content-type")).toBe(false);
    expect(xhr.body).toBeInstanceOf(FormData); expect(xhr.headers.get("x-csrf-token")).toBe("csrf-fixture");
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
    expect(progress).toHaveBeenCalledWith(50, 100);
    xhr.onload?.(); expect(await pending).toEqual(asset);
  });
  it("marks a timeout as uncertain and does not initiate another upload", async () => {
    const pending = uploadAsset(id, new File(["test"], "fixture.mp4"));
    FakeXHR.instances[0]!.ontimeout?.();
    await expect(pending).rejects.toMatchObject({ code: "UPLOAD_TIMEOUT", uncertain: true });
    expect(FakeXHR.instances).toHaveLength(1);
  });
  it("supports abort and preserves controlled quota errors without pretending partial success", async () => {
    const controller = new AbortController();
    const pending = uploadAsset(id, new File(["test"], "fixture.mp4"), undefined, controller.signal);
    controller.abort(); await expect(pending).rejects.toMatchObject({ code: "ABORTED", uncertain: true });
    const second = uploadAsset(id, new File(["test"], "fixture.mp4"));
    const xhr = FakeXHR.instances[1]!; xhr.status = 409;
    xhr.responseText = JSON.stringify({ error: { code: "TOO_MANY_ASSETS", message: "额度已用完" } }); xhr.onload?.();
    await expect(second).rejects.toBeInstanceOf(AppError);
  });

  it("expires the owner session when an upload returns 401", async () => {
    const expired = vi.fn(); window.addEventListener("episode:auth-expired", expired);
    try {
      const pending = uploadAsset(id, new File(["test"], "fixture.mp4"));
      const xhr = FakeXHR.instances[0]!; xhr.status = 401;
      xhr.responseText = JSON.stringify({ error: { code: "AUTH_REQUIRED", message: "需要登录" } }); xhr.onload?.();
      await expect(pending).rejects.toMatchObject({ status: 401 }); expect(expired).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener("episode:auth-expired", expired); }
  });
});
