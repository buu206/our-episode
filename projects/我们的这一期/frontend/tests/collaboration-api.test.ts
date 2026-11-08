// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, mediaUrl, setCsrf, uploadSubmission } from "../src/lib/api";
import { routeContract, UPLOAD_BODY_LIMIT, type ApiRole } from "../src/lib/routes";
import { ContextSpanSchema, InvitationSchema, RevisionSchema, ShareCreatedSchema, ViewerShareSchema, type Submission } from "../src/lib/collaboration-types";

const id = "a".repeat(32), otherId = "b".repeat(32), requestId = "00000000-0000-4000-8000-000000000001";
const timestamp = "2026-11-08T00:00:00Z";
const submission: Submission = { id, invite_id: id, participant_id: id, display_name: "朋友", kind: "video", state: "draft",
  text: "表达", question_id: null, filename: "test.mp4", duration: 2, size: 123, preview_state: "ready", preview_job_id: id,
  preview_error: null, media_url: `/api/v1/participant/submissions/${id}/media`, created_at: timestamp, revisions: [], adoptions: [],
  can_delete_draft: true, deleted: false, deletion_pending: false };
const invitation = { id, role: "interview", label: "朋友", expires_at: 100, closed: false, accepting_new: true,
  context: { asset_id: id, start: 0, end: 3 }, questions: [{ id: "question", text: "当时怎么想？", skipped: false }],
  context_job_id: id, context_state: "ready", context_error: null, context_preview_url: "/api/v1/participant/context/media", created_at: timestamp };
const share = { id, episode_id: id, version_id: id, label: "家人", title: "一期", state: "active", revision: 1,
  created_at: timestamp, updated_at: timestamp, expires_at: 100, closed_at: null, restricted_reason: null, confirmed_at: timestamp, watch_path: `/watch/${id}` };
const viewerShare = { id, label: "家人", title: "一期", version_id: id, revision: 1, expires_at: 100, confirmed_at: timestamp,
  media_url: `/api/v1/viewer/shares/${id}/media?revision=1` };
const roles: ApiRole[] = ["owner", "participant", "viewer"];
const event = (role: ApiRole) => role === "owner" ? "episode:auth-expired" : `episode:${role}-auth-expired`;
const authPath = (role: ApiRole) => role === "owner" ? "/auth" : `/${role}/auth`;
const error = (status = 401) => Response.json({ error: { code: "DENIED", message: "受限" } }, { status });
beforeEach(() => { for (const role of roles) setCsrf(null, role); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); for (const role of roles) setCsrf(null, role); });

describe("collaboration route and response contracts", () => {
  it("covers every existing collaboration/share operation with the correct role and method", () => {
    const entries: Array<[string, string, ApiRole]> = [
      [`/episodes/${id}/collaboration/invitations`, "GET", "owner"], [`/episodes/${id}/collaboration/invitations`, "POST", "owner"],
      [`/collaboration/invitations/${id}/close`, "POST", "owner"], [`/collaboration/invitations/${id}/retry`, "POST", "owner"],
      [`/episodes/${id}/collaboration/submissions`, "GET", "owner"], [`/collaboration/submissions/${id}/media`, "GET", "owner"],
      [`/collaboration/submissions/${id}/revisions`, "POST", "owner"], [`/collaboration/revisions/${id}/adopt`, "POST", "owner"],
      [`/episodes/${id}/collaboration/objections`, "GET", "owner"],
      ["/participant/auth/login", "POST", "participant"], ["/participant/auth/me", "GET", "participant"], ["/participant/auth/logout", "POST", "participant"],
      ["/participant/invitation", "GET", "participant"], ["/participant/invitation/questions", "PUT", "participant"], ["/participant/context/media", "GET", "participant"],
      ["/participant/submissions", "GET", "participant"], ["/participant/submissions", "POST", "participant"], ["/participant/submissions/upload", "POST", "participant"],
      [`/participant/submissions/${id}`, "PUT", "participant"], [`/participant/submissions/${id}?confirm=true`, "DELETE", "participant"],
      [`/participant/submissions/${id}/retry`, "POST", "participant"], [`/participant/submissions/${id}/media`, "GET", "participant"],
      [`/participant/submissions/${id}/submit`, "POST", "participant"], [`/participant/submissions/${id}/withdraw`, "POST", "participant"],
      [`/participant/revisions/${id}/decision`, "POST", "participant"], ["/participant/objections", "GET", "participant"], ["/participant/objections", "POST", "participant"],
      [`/participant/objections/${id}/resolve`, "POST", "participant"], [`/versions/${id}/shares`, "POST", "owner"], [`/episodes/${id}/shares`, "GET", "owner"],
      [`/shares/${id}/update`, "POST", "owner"], [`/shares/${id}/close`, "POST", "owner"],
      ["/viewer/auth/login", "POST", "viewer"], ["/viewer/auth/me", "GET", "viewer"], ["/viewer/auth/logout", "POST", "viewer"],
      [`/viewer/shares/${id}`, "GET", "viewer"], [`/viewer/shares/${id}/media?revision=1`, "GET", "viewer"], [`/viewer/shares/${id}/media?revision=1`, "HEAD", "viewer"],
    ];
    for (const [path, method, role] of entries) expect(routeContract(path, method)?.role, `${method} ${path}`).toBe(role);
    expect(routeContract("/participant/submissions/upload", "POST")).toMatchObject({ upload: true, bodyLimit: UPLOAD_BODY_LIMIT });
    expect(routeContract(`/participant/submissions/${id}/media`, "HEAD")).toBeNull();
    expect(routeContract(`/participant/jobs/${id}`, "GET")).toBeNull();
    expect(routeContract(`/collaboration/objections/${id}/resolve`, "POST")).toBeNull();
    expect(routeContract(`/viewer/shares/${id}/export?revision=1`, "GET")).toBeNull();
  });

  it("requires a canonical safe revision and rejects extra, encoded or cross-role media paths", () => {
    const path = `/api/v1/viewer/shares/${id}/media`;
    expect(mediaUrl(path + "?revision=2", "viewer")).toBe(path + "?revision=2");
    expect(mediaUrl(path + "?revision=2")).toBeNull();
    expect(mediaUrl(submission.media_url)).toBeNull();
    for (const suffix of ["", "?revision=0", "?revision=-1", "?revision=01", "?revision=1.5", "?revision=1&revision=2", "?revision=9007199254740992", "?revision=%31", "?revision=1#code=x"]) {
      expect(mediaUrl(path + suffix, "viewer"), suffix).toBeNull();
    }
    expect(mediaUrl(submission.media_url, "participant")).toBe(submission.media_url);
    expect(mediaUrl("/api/v1/participant/context/media", "participant")).toBe("/api/v1/participant/context/media");
    for (const role of ["owner", "viewer"] as const) expect(mediaUrl(submission.media_url, role)).toBeNull();
    expect(mediaUrl(`/api/v1/collaboration/submissions/${id}/media`)).toBe(`/api/v1/collaboration/submissions/${id}/media`);
    expect(mediaUrl(`/api/v1/assets/${id}/media`, "participant")).toBeNull();
  });

  it("checks intervals, publication identity and the one-time share code response", () => {
    expect(ContextSpanSchema.safeParse({ asset_id: id, start: 1, end: 31 }).success).toBe(true);
    expect(ContextSpanSchema.safeParse({ asset_id: id, start: 1, end: 31.1 }).success).toBe(false);
    expect(RevisionSchema.safeParse({ id, submission_id: id, text: "😀".repeat(100), start: 0, end: 0, state: "proposed", created_at: timestamp }).success).toBe(true);
    expect(InvitationSchema.safeParse({ ...invitation, context_preview_url: `/api/v1/assets/${id}/media` }).success).toBe(false);
    expect(ViewerShareSchema.safeParse(viewerShare).success).toBe(true);
    expect(ViewerShareSchema.safeParse({ ...viewerShare, revision: 2 }).success).toBe(false);
    expect(ViewerShareSchema.safeParse({ ...viewerShare, id: otherId }).success).toBe(false);
    expect(ShareCreatedSchema.safeParse({ ...share, share_code: null, watch_url: null }).success).toBe(true);
    expect(ShareCreatedSchema.safeParse({ ...share, share_code: "fixture-code", watch_url: `/watch/${id}#code=fixture-code` }).success).toBe(true);
    expect(ShareCreatedSchema.safeParse({ ...share, share_code: "fixture-code", watch_url: "https://external.invalid" }).success).toBe(false);
    expect(ShareCreatedSchema.safeParse({ ...share, share_code: null, watch_url: `/watch/${id}` }).success).toBe(false);
  });

  it("validates participant responses and refuses cross-role or mismatched submission media", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [submission] })));
    expect(await api("/participant/submissions")).toEqual({ items: [submission] });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [{ ...submission, media_url: `/api/v1/collaboration/submissions/${id}/media` }] })));
    await expect(api("/participant/submissions")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ items: [{ ...submission, media_url: `/api/v1/participant/submissions/${otherId}/media` }] })));
    await expect(api("/participant/submissions")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });
});

describe("role-scoped sessions", () => {
  it.each(roles)("a %s 401 clears only that role, even when another role's epoch changes", async (role) => {
    const listeners = { owner: vi.fn<() => void>(), participant: vi.fn<() => void>(), viewer: vi.fn<() => void>() };
    for (const item of roles) { setCsrf(`csrf-${item}`, item); window.addEventListener(event(item), listeners[item]); }
    let complete!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { complete = resolve; })));
    const pending = api(`${authPath(role)}/me`);
    const unrelated = roles.find(item => item !== role)!; setCsrf(`csrf-${unrelated}`, unrelated);
    complete(error()); await expect(pending).rejects.toMatchObject({ status: 401 });
    try {
      for (const item of roles) expect(listeners[item]).toHaveBeenCalledTimes(item === role ? 1 : 0);
      const fetcher = vi.fn(async (_url, init: RequestInit) => {
        expect(new Headers(init.headers).get("x-csrf-token")).toBe(`csrf-${unrelated}`);
        return Response.json({ ok: true });
      }); vi.stubGlobal("fetch", fetcher);
      await api(`${authPath(unrelated)}/logout`, { method: "POST" });
      await expect(api(`${authPath(role)}/logout`, { method: "POST" })).rejects.toMatchObject({ status: 401 });
      expect(fetcher).toHaveBeenCalledTimes(1);
    } finally { for (const item of roles) window.removeEventListener(event(item), listeners[item]); }
  });

  it.each(["participant", "viewer"] as const)("ignores stale %s 401 and invalid login without clearing the newer session", async (role) => {
    const listener = vi.fn(); window.addEventListener(event(role), listener); setCsrf("csrf-old", role);
    let complete!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(resolve => { complete = resolve; })));
    const pending = api(`${authPath(role)}/me`); setCsrf("csrf-new", role); complete(error());
    await expect(pending).rejects.toMatchObject({ status: 401 });
    vi.stubGlobal("fetch", vi.fn(async () => error()));
    await expect(api(`${authPath(role)}/login`, { method: "POST", body: {} })).rejects.toMatchObject({ status: 401 });
    try {
      expect(listener).not.toHaveBeenCalled();
      vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => {
        expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-new"); return Response.json({ ok: true });
      }));
      await api(`${authPath(role)}/logout`, { method: "POST" });
    } finally { window.removeEventListener(event(role), listener); }
  });

  it.each([409, 410])("preserves viewer logout authorization after a share becomes unavailable (%s)", async (status) => {
    setCsrf("csrf-viewer", "viewer"); const listener = vi.fn(); window.addEventListener(event("viewer"), listener);
    vi.stubGlobal("fetch", vi.fn(async () => error(status)));
    await expect(api("/viewer/auth/me")).rejects.toMatchObject({ status });
    try {
      expect(listener).not.toHaveBeenCalled();
      vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => {
        expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-viewer"); return Response.json({ ok: true });
      })); await api("/viewer/auth/logout", { method: "POST" });
    } finally { window.removeEventListener(event("viewer"), listener); }
  });

  it("does not send owner CSRF for a participant write and never stores guest credentials", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem"), fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    setCsrf("csrf-owner");
    await expect(api("/participant/submissions", { method: "POST", body: {} })).rejects.toMatchObject({ code: "PARTICIPANT_AUTH_REQUIRED" });
    expect(fetcher).not.toHaveBeenCalled(); setCsrf("csrf-participant", "participant"); setCsrf("csrf-viewer", "viewer");
    expect(storage).not.toHaveBeenCalled();
  });
});

class FakeXHR {
  static instances: FakeXHR[] = [];
  upload: { onprogress?: (event: ProgressEvent) => void } = {};
  onload?: () => void; onerror?: () => void; ontimeout?: () => void; onabort?: () => void;
  status = 201; responseText = JSON.stringify(submission); withCredentials = false; timeout = 0;
  headers = new Map<string, string>(); body?: XMLHttpRequestBodyInit | Document | null;
  constructor() { FakeXHR.instances.push(this); }
  open = vi.fn();
  setRequestHeader(name: string, value: string) { this.headers.set(name.toLowerCase(), value); }
  send(body?: XMLHttpRequestBodyInit | Document | null) { this.body = body; }
  abort() { this.onabort?.(); }
}
const fields = () => ({ file: new File(["test"], "test.MOV"), kind: "video" as const, text: "表达", question_id: "question", client_request_id: requestId });
describe("participant multipart upload", () => {
  beforeEach(() => { FakeXHR.instances = []; vi.stubGlobal("XMLHttpRequest", FakeXHR); setCsrf("csrf-participant", "participant"); });
  it("sends every documented field with participant CSRF and actual byte progress", async () => {
    setCsrf("csrf-owner"); const progress = vi.fn(), input = fields(); const pending = uploadSubmission(input, progress);
    const xhr = FakeXHR.instances[0]!;
    expect(xhr.open).toHaveBeenCalledWith("POST", "/api/v1/participant/submissions/upload");
    expect(xhr.headers.get("x-csrf-token")).toBe("csrf-participant"); expect(xhr.headers.has("content-type")).toBe(false);
    expect(xhr.withCredentials).toBe(true);
    const form = xhr.body as FormData;
    expect(form.get("file")).toBe(input.file);
    for (const name of ["kind", "text", "question_id", "client_request_id"] as const) expect(form.get(name)).toBe(input[name]);
    xhr.upload.onprogress?.({ lengthComputable: false, loaded: 1, total: 0 } as ProgressEvent); expect(progress).not.toHaveBeenCalled();
    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 9, total: 11 } as ProgressEvent); expect(progress).toHaveBeenCalledWith(9, 11);
    xhr.onload?.(); expect(await pending).toEqual(submission); expect(FakeXHR.instances).toHaveLength(1);
  });

  it("retains one request id across an uncertain result without resending", async () => {
    const input = fields(), pending = uploadSubmission(input); FakeXHR.instances[0]!.ontimeout?.();
    await expect(pending).rejects.toMatchObject({ code: "UPLOAD_TIMEOUT", uncertain: true });
    expect(input.client_request_id).toBe(requestId); expect(FakeXHR.instances).toHaveLength(1);
    const controller = new AbortController(); const aborting = uploadSubmission(input, undefined, controller.signal);
    controller.abort(); await expect(aborting).rejects.toMatchObject({ code: "ABORTED", uncertain: true });
    expect(FakeXHR.instances).toHaveLength(2);
  });

  it("validates UUID, literal text, extensions and upload limits without allocating a large file", async () => {
    const big = new File(["x"], "test.mp4"); Object.defineProperty(big, "size", { value: 1024 ** 3 + 1 });
    await expect(uploadSubmission({ ...fields(), file: big })).rejects.toMatchObject({ code: "UPLOAD_TOO_LARGE" });
    const audio = new File(["x"], "test.wav"); Object.defineProperty(audio, "size", { value: 100_000_001 });
    await expect(uploadSubmission({ ...fields(), kind: "audio", file: audio })).rejects.toMatchObject({ code: "UPLOAD_TOO_LARGE" });
    await expect(uploadSubmission({ ...fields(), client_request_id: "not-uuid" })).rejects.toMatchObject({ code: "INVALID_REQUEST" });
    await expect(uploadSubmission({ ...fields(), text: "x\n隐藏" })).rejects.toMatchObject({ code: "INVALID_REQUEST" });
    await expect(uploadSubmission({ ...fields(), text: "😀".repeat(101) })).rejects.toMatchObject({ code: "INVALID_REQUEST" });
    await expect(uploadSubmission({ ...fields(), file: new File(["x"], "test.exe") })).rejects.toMatchObject({ code: "INVALID_FORMAT" });
    expect(FakeXHR.instances).toHaveLength(0);
  });

  it("allows a contribution above the interview limit and leaves role-specific quota to the server", async () => {
    const file = new File(["x"], "test.mp4"); Object.defineProperty(file, "size", { value: 100_000_001 });
    const pending = uploadSubmission({ ...fields(), file, question_id: null });
    const xhr = FakeXHR.instances[0]!; expect((xhr.body as FormData).has("question_id")).toBe(false);
    xhr.status = 409; xhr.responseText = JSON.stringify({ error: { code: "TOO_MANY_ASSETS", message: "配额不足" } }); xhr.onload?.();
    await expect(pending).rejects.toMatchObject({ code: "TOO_MANY_ASSETS", uncertain: false }); expect(FakeXHR.instances).toHaveLength(1);
  });

  it("isolates upload 401 from the owner and ignores a stale participant upload 401", async () => {
    setCsrf("csrf-owner"); const ownerExpired = vi.fn(), participantExpired = vi.fn();
    window.addEventListener(event("owner"), ownerExpired); window.addEventListener(event("participant"), participantExpired);
    try {
      const stale = uploadSubmission(fields()); setCsrf("csrf-new", "participant");
      const first = FakeXHR.instances[0]!; first.status = 401; first.responseText = "{}"; first.onload?.();
      await expect(stale).rejects.toMatchObject({ status: 401 }); expect(participantExpired).not.toHaveBeenCalled();
      const pending = uploadSubmission(fields()); const second = FakeXHR.instances[1]!; second.status = 401; second.responseText = "not-json"; second.onload?.();
      await expect(pending).rejects.toMatchObject({ status: 401 }); expect(participantExpired).toHaveBeenCalledTimes(1); expect(ownerExpired).not.toHaveBeenCalled();
      vi.stubGlobal("fetch", vi.fn(async (_url, init: RequestInit) => {
        expect(new Headers(init.headers).get("x-csrf-token")).toBe("csrf-owner"); return Response.json({ ok: true });
      })); await api("/auth/logout", { method: "POST" });
    } finally { window.removeEventListener(event("owner"), ownerExpired); window.removeEventListener(event("participant"), participantExpired); }
  });
});
