import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Studio from "@/components/studio";
import { AppError } from "@/lib/api";
import type { Asset, Episode, Job, Status, Version } from "@/lib/types";
import { clearWorkspaceDrafts, readWorkspaceDraft, setDraftOwner } from "@/components/studio/draft-cache";

const mocks = vi.hoisted(() => ({ api: vi.fn(), upload: vi.fn(), checkExport: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: mocks.api, uploadAsset: mocks.upload, checkExport: mocks.checkExport,
  exportUrl: (id: string) => `/api/v1/versions/${id}/export`,
  AppError: class extends Error {
    constructor(public code: string, message: string, public status = 0, public uncertain = false) { super(message); this.name = code === "ABORTED" ? "AbortError" : "AppError"; }
  },
}));

const eid = "e".repeat(32), aid = "a".repeat(32), vid = "b".repeat(32), jid = "c".repeat(32);
const asset = (overrides: Partial<Asset> = {}): Asset => ({ id: aid, filename: "现场原片.mp4", duration: 20, width: 1280, height: 720,
  has_audio: true, size: 1024, media_url: `/api/v1/assets/${aid}/media`, source_kind: "original", contributor: null, interview_text: null, fixed_segment: null, ...overrides });
const version = (overrides: Partial<Version> = {}): Version => ({ id: vid, title: "第一版", state: "ready", origin: "manual",
  segments: [{ asset_id: aid, start: 0, end: 8, caption: "" }], media_url: `/api/v1/versions/${vid}/media`, job_id: null,
  error: null, base_version_id: null, created_at: "2026-11-08T10:00:00+00:00", narration_text: "", narration_approved: false, narration_ready: false, packaging: "original", evidence_revision: null, ...overrides });
const episode = (overrides: Partial<Episode> = {}): Episode => ({ id: eid, title: "一起做饭", background: "授权测试内容", created_at: "2026-11-08T10:00:00+00:00",
  assets: [asset()], versions: [], jobs: [], directions: [], budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 },
  evidence: [], evidence_revision: 1, evidence_confirmed_revision: null, analysis_state: "empty", analysis_notes: [], analysis_coverage: [], directions_revision: null, selected_direction: null,
  quota: { original_count: 1, total_seconds: 20, total_bytes: 1024, max_count: 8, max_seconds: 600, max_bytes: 1024 ** 3 }, pending_cleanup: [], ...overrides });
const status: Status = { cloud_enabled: false, workflow_enabled: false, cloud_reason: "AI尚未接入", media_ready: true, stage: "阶段3-1", model_mode: "cloud" };
let saved: Episode;
let runningStatus: Status;
type Options = { method?: string; body?: Record<string, unknown>; signal?: AbortSignal };
async function service(path: string, options: Options = {}) {
  if (path === "/status") return { ...runningStatus };
  if (path === `/episodes/${eid}`) return structuredClone(saved);
  if (path === `/episodes/${eid}/director/messages` && saved.director) {
    const body = options.body!;
    saved.director = { ...saved.director, revision: saved.director.revision + 1, directions_current: false,
      focus: (body.choice as NonNullable<Episode["director"]>["focus"]) || saved.director.focus,
      notes: body.text ? String(body.text) : saved.director.notes,
      messages: [...saved.director.messages, { id: String(body.client_id), role: "user", text: String(body.text || body.choice), created_at: "2026-11-13T12:00:00Z" }] };
    return structuredClone(saved.director);
  }
  if (path === `/episodes/${eid}/versions` && options.method === "POST") {
    const result = version({ ...options.body, state: "draft", media_url: null } as Partial<Version>);
    saved.versions.push(result);
    return result;
  }
  if (path === `/episodes/${eid}/evidence` && options.method === "PUT") {
    saved.evidence = options.body?.items as Episode["evidence"];
    saved.evidence_revision++;
    saved.evidence_confirmed_revision = saved.evidence_revision;
    saved.analysis_state = "confirmed";
    return structuredClone(saved);
  }
  if (path === `/versions/${vid}/confirm`) {
    saved.versions[0] = { ...saved.versions[0], state: "confirmed" };
    return saved.versions[0];
  }
  throw new Error(`Unexpected test route: ${path}`);
}
function route(step = "materials", current?: string) { window.history.replaceState(null, "", `/episodes/${eid}?step=${step}${current ? `&version=${current}` : ""}`); }
beforeEach(() => {
  clearWorkspaceDrafts(); setDraftOwner("test-user");
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  mocks.api.mockReset(); mocks.upload.mockReset(); mocks.checkExport.mockReset();
  saved = episode(); runningStatus = { ...status }; mocks.api.mockImplementation(service); route();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("Studio guarded workflow", () => {
  it("enables only episode-granted operations and reuses cached analysis without destructive modal", async () => {
    saved.workflow = { enabled: true, reason: "本期素材已授权", analysis_cached: true };
    saved.analysis_state = "confirmed"; saved.evidence_confirmed_revision = 1;
    saved.evidence = [{ id: "ev", asset_id: aid, start: 0, end: 3, note: "已核对", kind: "visual" }];
    mocks.api.mockImplementation((path: string, options: Options = {}) => path.endsWith("/analysis") ? Promise.resolve({ id: jid, state: "succeeded" }) : service(path, options));
    route("direction"); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: /查看与修改详细内容/ }));
    const reuse = await screen.findByRole("button", { name: "载入已有分析" });
    expect(reuse).toBeEnabled();
    fireEvent.click(reuse);
    await waitFor(() => expect(mocks.api.mock.calls.some(([path]) => path.endsWith("/analysis"))).toBe(true));
    expect(screen.queryByText(/重新分析会使旧依据/)).not.toBeInTheDocument();
    saved.workflow.enabled = false;
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "载入已有分析" })).toBeDisabled());
  });

  it("keeps completed job history folded while the current preview and its action stay visible", async () => {
    saved.versions = [version({ state: "confirmed" })];
    saved.jobs = Array.from({ length: 20 }, (_, index): Job => ({ id: `old-job-${index}`, kind: "render", state: "succeeded",
      stage: `历史合成步骤${index + 1}`, error: null, version_id: vid, result: null }));
    route("preview", vid); render(<Studio episodeId={eid} />);
    expect(await screen.findByRole("button", { name: "导出这一版MP4" })).toBeVisible();
    expect(screen.getByLabelText("第一版")).toBeVisible();
    const record = screen.getByText("历史合成步骤20");
    expect(record).not.toBeVisible();
    const history = record.closest("details")!;
    expect(history).not.toHaveAttribute("open");
    fireEvent.click(history.querySelector("summary")!);
    expect(record).toBeVisible();
    expect(screen.getAllByText("历史合成步骤20")).toHaveLength(1);
    fireEvent.click(history.querySelector("summary")!);
    expect(record).not.toBeVisible();
    expect(mocks.api.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
  });

  it("opens a director-created draft while preserving the confirmed source", async () => {
    const nextId = "d".repeat(32);
    saved.versions = [version({ state: "confirmed", packaging: "standard", narration_text: "旧旁白" })];
    route("preview", vid); render(<Studio episodeId={eid} />);
    await screen.findByRole("button", { name: "以这版为基础继续修改" });
    saved.versions.push(version({ id: nextId, title: "导演新草案", state: "draft", packaging: "standard", media_url: null, narration_text: "在场，开拍！", base_version_id: vid }));
    const event = new CustomEvent("director:navigate", { cancelable: true, detail: { episodeId: eid, step: "edit", versionId: nextId } });
    act(() => { window.dispatchEvent(event); });
    expect(event.defaultPrevented).toBe(true);
    await waitFor(() => expect(screen.getByLabelText("旁白文案（可选，最多120字）")).toHaveValue("在场，开拍！"));
    expect(window.location.search).toContain(nextId);
    expect(saved.versions[0]).toMatchObject({ state: "confirmed", narration_text: "旧旁白" });
    expect(mocks.api.mock.calls.every(([, options]) => !options?.method || options.method === "GET")).toBe(true);
  });

  it("keeps unsaved local edits when director draft navigation is cancelled", async () => {
    const nextId = "d".repeat(32);
    saved.versions = [version({ packaging: "standard", narration_text: "旧旁白" })];
    route("preview", vid); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "以这版为基础继续修改" }));
    fireEvent.change(await screen.findByLabelText("旁白文案（可选，最多120字）"), { target: { value: "未保存的旁白" } });
    saved.versions.push(version({ id: nextId, state: "draft", narration_text: "在场，开拍！" }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    act(() => { window.dispatchEvent(new CustomEvent("director:navigate", { cancelable: true, detail: { episodeId: eid, step: "edit", versionId: nextId } })); });
    expect(confirm).toHaveBeenCalledOnce();
    expect(screen.getByLabelText("旁白文案（可选，最多120字）")).toHaveValue("未保存的旁白");
    expect(window.location.search).not.toContain(nextId);
    confirm.mockRestore();
  });

  it.each(["variety", "variety_v2"])("retains approved narration for %s and offers local rendering without a new voice call", async packaging => {
    const nextId = "d".repeat(32);
    const narration = "镜头给到这一桌！在场，开拍！";
    saved.versions = [version({ packaging: "standard", narration_text: narration, narration_approved: true, narration_ready: true })];
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}/versions` && options.method === "POST") {
        const result = version({ ...options.body, id: nextId, state: "draft", media_url: null, narration_approved: true, narration_ready: true } as Partial<Version>);
        saved.versions.push(result);
        return result;
      }
      if (path === `/versions/${nextId}/render`) return { id: jid };
      return service(path, options);
    });
    route("preview", vid); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "以这版为基础继续修改" }));
    fireEvent.change(await screen.findByLabelText("节目包装"), { target: { value: packaging } });
    expect(screen.getByLabelText("旁白文案（可选，最多120字）")).toHaveValue(narration);
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "开始本机合成" })).toBeEnabled());
    expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/versions`, expect.objectContaining({ body: expect.objectContaining({ packaging, narration_text: narration, base_version_id: vid }) }));
    expect(screen.queryByRole("button", { name: "确认文案并生成旁白" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("本版旁白文案 · 音频已就绪"));
    expect(screen.getByText(narration)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "开始本机合成" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/versions/${nextId}/render`, expect.objectContaining({ method: "POST" })));
    expect(mocks.api.mock.calls.some(([path]) => path.endsWith("/narration"))).toBe(false);
    expect(saved.versions[0].packaging).toBe("standard");
  });

  it("shows one overview per asset, hides twenty detail rows and confirms in one explicit action without generating", async () => {
    const assets = [asset(), asset({ id: "d".repeat(32), filename: "第二段.mp4" }), asset({ id: "f".repeat(32), filename: "第三段.mp4" })];
    saved = episode({ assets, analysis_state: "review", evidence: Array.from({ length: 20 }, (_, i) => ({ id: `proof-${i}`, asset_id: assets[i % 3].id, start: .15, end: 1.37, kind: i % 2 ? "speech" : "visual", note: `测试摘录${i}` })) });
    runningStatus.workflow_enabled = true;
    const original = structuredClone(saved.evidence);
    route("direction"); const view = render(<Studio episodeId={eid} />);
    expect(await screen.findAllByRole("button", { name: "看看原片" })).toHaveLength(3);
    expect(screen.queryByRole("button", { name: "回看此处" })).not.toBeInTheDocument();
    expect(view.container.querySelector("video")).toBeNull();
    expect(screen.getByRole("button", { name: "根据已确认证据提出方向" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "确认内容概览" }));
    await screen.findByText("已确认，可以继续选择方向。");
    const writes = mocks.api.mock.calls.filter(([, options]) => options?.method === "PUT");
    expect(writes).toHaveLength(1);
    expect(writes[0][1].body).toEqual({ revision: 1, items: original, confirmed: true, confirmation_mode: "overview" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "根据已确认证据提出方向" })).toBeEnabled();
    expect(mocks.api.mock.calls.some(([path]) => path.endsWith("/directions"))).toBe(false);
    view.unmount(); render(<Studio episodeId={eid} />);
    await screen.findByText("已确认，可以继续选择方向。");
    expect(screen.queryByRole("button", { name: "确认内容概览" })).not.toBeInTheDocument();
  });

  it("opens a detail at precise source bounds in a dialog, stops at its end, and restores focus on close", async () => {
    saved.analysis_state = "review";
    saved.evidence = [{ id: "proof", asset_id: aid, start: 1.23, end: 4.56, note: "测试原声", kind: "speech" }];
    route("direction"); const view = render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: /查看与修改详细内容/ }));
    expect(screen.getByLabelText("开始（秒）")).toHaveValue(1);
    expect(screen.getByLabelText("结束（秒）")).toHaveValue(5);
    expect(screen.getByLabelText("开始（秒）")).toHaveAttribute("step", "1");
    const trigger = screen.getByRole("button", { name: "回看此处" }); trigger.focus(); fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "原片预览" });
    const video = within(dialog).getByLabelText("现场原片.mp4") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 20 });
    fireEvent.loadedMetadata(video);
    expect(video.currentTime).toBe(1.23);
    const pause = vi.mocked(video.pause); pause.mockClear();
    video.currentTime = 4.56; fireEvent.timeUpdate(video); expect(pause).toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭预览" }));
    expect(view.container.querySelector("video")).toBeNull();
    expect(video).not.toHaveAttribute("src");
    await waitFor(() => expect(trigger).toHaveFocus());
    fireEvent.change(screen.getByLabelText("核对后的文字，不确定的原话请移除"), { target: { value: "修改文字不改变时间" } });
    fireEvent.click(screen.getByRole("button", { name: "确认内容概览" }));
    await screen.findByText("已确认，可以继续选择方向。");
    expect(saved.evidence[0]).toMatchObject({ start: 1.23, end: 4.56, note: "修改文字不改变时间" });
  });

  it("keeps a fractional final frame within the media duration when editing the displayed final second", async () => {
    saved.assets[0].duration = 15.1;
    saved.analysis_state = "review";
    saved.evidence = [{ id: "proof", asset_id: aid, start: 14.85, end: 15.1, note: "片尾", kind: "visual" }];
    route("direction"); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: /查看与修改详细内容/ }));
    expect(screen.getByLabelText("开始（秒）")).toHaveValue(14);
    expect(screen.getByLabelText("结束（秒）")).toHaveValue(16);
    fireEvent.change(screen.getByLabelText("结束（秒）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "确认内容概览" }));
    await screen.findByText(/第1条需有效来源/);
    expect(mocks.api.mock.calls.some(([, options]) => options?.method === "PUT")).toBe(false);
    fireEvent.change(screen.getByLabelText("结束（秒）"), { target: { value: "16" } });
    fireEvent.click(screen.getByRole("button", { name: "确认内容概览" }));
    await screen.findByText("已确认，可以继续选择方向。");
    expect(saved.evidence[0]).toMatchObject({ start: 14.85, end: 15.1 });
  });

  it("previews an editor segment in a dismissible dialog without rounding the saved cut", async () => {
    saved.versions = [version({ segments: [{ asset_id: aid, start: 1.25, end: 3.75, caption: "" }] })];
    route("edit", vid); const view = render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "回看选段" }));
    const dialog = screen.getByRole("dialog", { name: "原片预览" });
    const video = within(dialog).getByLabelText("现场原片.mp4") as HTMLVideoElement;
    Object.defineProperty(video, "duration", { configurable: true, value: 20 });
    fireEvent.loadedMetadata(video); expect(video.currentTime).toBe(1.25);
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(view.container.querySelector("video")).toBeNull();
    expect(screen.getByLabelText("开始（秒）")).toHaveValue(1);
    expect(screen.getByLabelText("结束（秒）")).toHaveValue(4);
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/versions`, expect.objectContaining({ body: expect.objectContaining({ segments: [{ asset_id: aid, start: 1.25, end: 3.75, caption: "" }] }) })));
  });

  it("shows shot purpose and preserves the director contract when saving a new version", async () => {
    const segments = [{ asset_id: aid, start: 0, end: 4, caption: "", purpose: "交代这一桌的场景", evidence_ids: ["e1"] },
      { asset_id: aid, start: 5, end: 9, caption: "", purpose: "承接完整原声", evidence_ids: ["e2"], transition: "dissolve" as const, transition_duration: .3 }];
    saved.versions = [version({ segments })]; route("edit", vid); render(<Studio episodeId={eid} />);
    expect(await screen.findByText("交代这一桌的场景")).toBeInTheDocument();
    expect(screen.getByText(/从上一镜头叠化进入/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/versions`, expect.objectContaining({ body: expect.objectContaining({ segments }) })));
  });

  it("persists a director choice and supplementary requirement through the workspace", async () => {
    saved.director = { revision: 0, focus: null, notes: "", messages: [], directions_current: false };
    render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByText("偏好与要求"));
    expect(screen.getByRole("button", { name: /保留聊天/ })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /保留聊天/ }));
    await waitFor(() => expect(saved.director?.focus).toBe("conversation"));
    const input = screen.getByLabelText("还有想保留或避开的内容？");
    await waitFor(() => expect(input).not.toBeDisabled());
    fireEvent.change(input, { target: { value: "原声保留完整" } });
    expect(screen.getByText(/有未保存的导演补充要求/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(saved.director?.notes).toBe("原声保留完整");
    const posts = mocks.api.mock.calls.filter(([path]) => path.endsWith("/director/messages"));
    expect(posts).toHaveLength(2);
    expect(posts[0][1].body).toMatchObject({ revision: 0, choice: "conversation" });
    expect(posts[1][1].body).toMatchObject({ revision: 1, text: "原声保留完整" });
  });

  it("keeps the unsent requirement after a revision conflict and retries against the refreshed revision", async () => {
    saved.director = { revision: 0, focus: "auto", notes: "", messages: [], directions_current: false };
    let failed = false;
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path.endsWith("/director/messages") && !failed) {
        failed = true; saved.director!.revision = 3;
        throw new AppError("DIRECTOR_REVISION_CONFLICT", "需求已更新", 409);
      }
      return service(path, options);
    });
    render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByText("偏好与要求"));
    const input = screen.getByLabelText("还有想保留或避开的内容？");
    expect(input).toBeVisible();
    fireEvent.change(input, { target: { value: "少一点花字" } });
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await screen.findByText(/还没有保存成功/);
    expect(input).toHaveValue("少一点花字");
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await waitFor(() => expect(input).toHaveValue(""));
    const posts = mocks.api.mock.calls.filter(([path]) => path.endsWith("/director/messages"));
    expect(posts[1][1].body.revision).toBe(3);
    expect(posts[1][1].body.client_id).not.toBe(posts[0][1].body.client_id);
  });

  it.each(["先微调，再开拍", "导演直接出原声初稿"])("selects only the displayed brief revision via %s", async (label) => {
    saved = episode({ analysis_state: "confirmed", evidence_confirmed_revision: 1, directions_revision: 1,
      evidence: [{ id: "evidence-1", asset_id: aid, start: 0, end: 8, note: "合成测试区间", kind: "visual" }],
      directions: [{ id: "direction-1", title: "保留完整对话", summary: "从场景到完整原声。", evidence: [{ asset_id: aid, start: 0, end: 8, note: "合成测试区间" }] }],
      director: { revision: 7, focus: "conversation", notes: "原声说完整", messages: [], directions_current: true } });
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}/direction`) {
        // A different tab has already generated a new brief with the same direction ID.
        saved.director!.revision = 8;
        throw new AppError("DIRECTOR_BRIEF_CHANGED", "要求已更新，请重新查看方向", 409);
      }
      return service(path, options);
    });
    route("direction"); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: label }));
    await screen.findByText("要求已更新，请重新查看方向");
    expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/direction`, expect.objectContaining({ body: expect.objectContaining({ brief_revision: 7, revision: 1 }) }));
    expect(saved.versions).toHaveLength(0);
    expect(window.location.search).toContain("step=direction");
  });

  it("accumulates separate file selections, avoids duplicate pending files and lets a pending file be removed", async () => {
    render(<Studio episodeId={eid} />);
    const input = await screen.findByLabelText(/选择视频文件/);
    const first = new File(["one"], "第一段.mp4", { lastModified: 1 });
    const second = new File(["two"], "第二段.mp4", { lastModified: 2 });
    fireEvent.change(input, { target: { files: [first] } });
    fireEvent.change(input, { target: { files: [second] } });
    fireEvent.change(input, { target: { files: [first] } });
    const pending = screen.getByRole("list", { name: "待上传素材" });
    expect(within(pending).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "上传这2份素材" })).toBeEnabled();
    fireEvent.click(within(pending).getByRole("button", { name: "移除待上传的第一段.mp4" }));
    expect(within(pending).queryByText("第一段.mp4")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上传这1份素材" })).toBeEnabled();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("shows each saved asset before the batch completes and retains earlier uploads across another batch and remount", async () => {
    let finishSecond!: () => void;
    const saveFile = (file: File) => { const result = asset({ id: file.name, filename: file.name }); saved.assets.push(result); return result; };
    mocks.upload.mockImplementationOnce(async (_id, file) => saveFile(file));
    mocks.upload.mockImplementationOnce((_id, file) => new Promise<Asset>(resolve => { finishSecond = () => resolve(saveFile(file)); }));
    mocks.upload.mockImplementationOnce(async (_id, file) => saveFile(file));
    const view = render(<Studio episodeId={eid} />);
    const input = await screen.findByLabelText(/选择视频文件/);
    fireEvent.change(input, { target: { files: [new File(["1"], "第一段.mp4"), new File(["2"], "第二段.mp4")] } });
    fireEvent.click(screen.getByRole("button", { name: "上传这2份素材" }));
    await screen.findByRole("heading", { name: "第一段.mp4" });
    await waitFor(() => expect(mocks.upload).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("heading", { name: "第二段.mp4" })).not.toBeInTheDocument();
    expect(screen.getByText("已保存到本期")).toBeInTheDocument();
    await act(async () => finishSecond());
    await screen.findByText(/已接收2／2份/);
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { files: [new File(["3"], "第三段.mp4")] } });
    fireEvent.click(screen.getByRole("button", { name: "上传这1份素材" }));
    await screen.findByRole("heading", { name: "第三段.mp4" });
    await waitFor(() => expect(screen.getAllByText("已保存到本期")).toHaveLength(3));
    view.unmount(); render(<Studio episodeId={eid} />);
    for (const name of ["第一段.mp4", "第二段.mp4", "第三段.mp4"]) await screen.findByRole("heading", { name });
    expect(mocks.upload).toHaveBeenCalledTimes(3);
  });

  it("stops the queue on an unknown receipt without retrying or claiming remaining files were uploaded", async () => {
    mocks.upload.mockRejectedValueOnce(new AppError("NETWORK_ERROR", "传输结果未确定", 0, true));
    render(<Studio episodeId={eid} />);
    const input = await screen.findByLabelText(/选择视频文件/);
    fireEvent.change(input, { target: { files: [new File(["1"], "第一段.mp4"), new File(["2"], "第二段.mp4")] } });
    fireEvent.click(screen.getByRole("button", { name: "上传这2份素材" }));
    await screen.findByText("接收结果待核对");
    expect(screen.getByText("未上传")).toBeInTheDocument();
    expect(mocks.upload).toHaveBeenCalledTimes(1);
    expect(input).toBeDisabled();
  });

  it("keeps acknowledged upload success when the next list read fails and pauses the remaining queue", async () => {
    let failNextRead = false;
    mocks.upload.mockImplementationOnce(async () => { failNextRead = true; return asset(); });
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}` && failNextRead) { failNextRead = false; throw new AppError("NETWORK_ERROR", "读取列表失败"); }
      return service(path, options);
    });
    render(<Studio episodeId={eid} />);
    const input = await screen.findByLabelText(/选择视频文件/);
    fireEvent.change(input, { target: { files: [new File(["1"], "第一段.mp4"), new File(["2"], "第二段.mp4")] } });
    fireEvent.click(screen.getByRole("button", { name: "上传这2份素材" }));
    await screen.findByText(/已保存素材的最新列表暂未读回/);
    expect(screen.getByText("已保存到本期")).toBeInTheDocument();
    expect(screen.getByText("未上传")).toBeInTheDocument();
    expect(mocks.upload).toHaveBeenCalledTimes(1);
  });

  it("opens original media in a dialog and unloads it on close, escape or source withdrawal", async () => {
    const view = render(<Studio episodeId={eid} />);
    const trigger = await screen.findByRole("button", { name: "预览原片" });
    expect(view.container.querySelector("video")).toBeNull();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "原片预览" });
    const video = within(dialog).getByLabelText("现场原片.mp4");
    expect(video).toHaveAttribute("src", asset().media_url);
    fireEvent.click(within(dialog).getByRole("button", { name: "关闭预览" }));
    expect(screen.queryByRole("dialog", { name: "原片预览" })).not.toBeInTheDocument();
    expect(video).not.toHaveAttribute("src");
    fireEvent.click(trigger);
    fireEvent(screen.getByRole("dialog", { name: "原片预览" }), new Event("cancel", { cancelable: true }));
    expect(view.container.querySelector("video")).toBeNull();
    fireEvent.click(trigger);
    saved.assets = [];
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "原片预览" })).not.toBeInTheDocument());
    expect(view.container.querySelector("video")).toBeNull();
  });

  it("keeps local manual editing available while the model workflow is blocked and saves a separate version", async () => {
    render(<Studio episodeId={eid} />);
    await screen.findByRole("heading", { name: "一起做饭" });
    fireEvent.click(screen.getByRole("button", { name: "加入选段" }));
    fireEvent.click(screen.getByRole("button", { name: /03安排这一期/ }));
    fireEvent.change(screen.getByLabelText("新版本名称"), { target: { value: "人工剪接新版本" } });
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    await screen.findByRole("heading", { name: "人工剪接新版本" });
    // The preview can render before the save/refresh operation and passive cache
    // effect finish. Wait for the completed save, then observe cache cleanup.
    await screen.findByText("新版本已保存；现在可合成并预览，旧版本保留。");
    expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/versions`, expect.objectContaining({ method: "POST", body: expect.objectContaining({ title: "人工剪接新版本", packaging: "original", narration_text: "", segments: [{ asset_id: aid, start: 0, end: 10, caption: "" }] }) }));
    expect(window.location.search).toContain(`version=${vid}`);
    expect(screen.getByRole("button", { name: "开始本机合成" })).toBeEnabled();
    expect(mocks.api.mock.calls.some(([path]) => String(path).endsWith("/analysis"))).toBe(false);
    await waitFor(() => expect(readWorkspaceDraft(eid)).toBeNull());
  });

  it("accepts explicit human acknowledgement without an ended event and still requires version-specific confirmation", async () => {
    saved.versions = [version()]; route("preview", vid);
    render(<Studio episodeId={eid} />);
    await screen.findByRole("heading", { name: "第一版" });
    const button = screen.getByRole("button", { name: "确认当前版本" });
    expect(button).toBeDisabled();
    const acknowledgement = screen.getByRole("checkbox");
    expect(acknowledgement).toBeEnabled(); expect(button).toBeDisabled();
    fireEvent.click(acknowledgement); fireEvent.click(button);
    const dialog = screen.getByRole("dialog", { name: "确认“第一版”？" });
    expect(mocks.api.mock.calls.some(([path]) => String(path).endsWith("/confirm"))).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认此版本" }));
    await screen.findByText("已确认此版本");
    expect(mocks.api).toHaveBeenCalledWith(`/versions/${vid}/confirm`, { method: "POST", body: {} });
  });

  it("never checks consent on playback completion or clears the user's check when a late ended event arrives", async () => {
    saved.versions = [version()]; route("preview", vid);
    render(<Studio episodeId={eid} />);
    const check = await screen.findByRole("checkbox");
    fireEvent.ended(screen.getByLabelText("第一版"));
    expect(check).not.toBeChecked();
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeDisabled();
    fireEvent.click(check);
    fireEvent.play(screen.getByLabelText("第一版"));
    fireEvent.ended(screen.getByLabelText("第一版"));
    expect(check).toBeChecked();
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeEnabled();
    expect(mocks.api.mock.calls.some(([path]) => String(path).endsWith("/confirm"))).toBe(false);
  });

  it("allows acknowledgement after foreground revalidation without replay, but locks while the read is pending or failed", async () => {
    saved.versions = [version()]; route("preview", vid);
    render(<Studio episodeId={eid} />);
    const check = await screen.findByRole("checkbox");
    fireEvent.ended(screen.getByLabelText("第一版"));
    fireEvent.click(check);
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(check).toBeDisabled(); expect(check).not.toBeChecked();
    let rejectRead!: (error: Error) => void;
    mocks.api.mockImplementation((path: string, options: Options = {}) => path === `/episodes/${eid}`
      ? new Promise((_, reject) => { rejectRead = reject; }) : service(path, options));
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(check).toBeDisabled();
    await act(async () => { rejectRead(new AppError("NETWORK_ERROR", "连接中断")); });
    expect(check).toBeDisabled();
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeDisabled();
    mocks.api.mockImplementation(service);
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(check).toBeEnabled());
    expect(screen.getByText("已经播完，可以确认这一版了。")).toBeInTheDocument();
    expect(check).not.toBeChecked();
    fireEvent.click(check);
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeEnabled();
  });

  it("does not transfer acknowledgement to another version or a changed media URL", async () => {
    const secondId = "d".repeat(32);
    saved.versions = [version(), version({ id: secondId, title: "第二版", media_url: `/api/v1/versions/${secondId}/media` })];
    route("preview", vid); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("checkbox"));
    expect(screen.queryByRole("dialog", { name: "本期版本" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换版本（2）" }));
    const versions = screen.getByRole("dialog", { name: "本期版本" });
    fireEvent.click(within(versions).getByRole("button", { name: /第二版.*待你预览确认/ }));
    expect(screen.queryByRole("dialog", { name: "本期版本" })).not.toBeInTheDocument();
    expect(window.location.search).toContain(`version=${secondId}`);
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    saved.versions[1].media_url += "?revision=2";
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.getByRole("checkbox")).not.toBeChecked());
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    saved.evidence_revision++;
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.getByRole("checkbox")).not.toBeChecked());
    expect(screen.getByRole("button", { name: "确认当前版本" })).toBeDisabled();
    saved.versions[1].state = "restricted";
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await waitFor(() => expect(screen.queryByRole("checkbox")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "确认当前版本" })).not.toBeInTheDocument();
    expect(mocks.api.mock.calls.some(([path]) => String(path).endsWith("/confirm"))).toBe(false);
  });

  it("preserves dirty evidence across refresh and prevents confirming a stale revision", async () => {
    saved.analysis_state = "review";
    saved.evidence = [{ id: "evidence-1", asset_id: aid, start: 0, end: 3, note: "待核对的文字", kind: "speech" }];
    route("direction"); render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: /查看与修改详细内容/ }));
    const field = await screen.findByLabelText("核对后的文字，不确定的原话请移除");
    fireEvent.change(field, { target: { value: "我核对后的内容" } });
    saved.evidence_revision = 2; saved.evidence[0].note = "服务器的新版本";
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await screen.findByText(/来源核对已在服务端更新/);
    expect(field).toHaveValue("我核对后的内容");
    expect(screen.getByRole("button", { name: "确认内容概览" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "根据已确认证据提出方向" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新分析当前素材" })).toBeDisabled();
  });

  it("reports actual upload progress and keeps successful files after a later upload fails", async () => {
    let resolveFirst!: (value: Asset) => void;
    mocks.upload.mockImplementationOnce((_id, _file, progress) => { progress(5, 20); return new Promise<Asset>(resolve => { resolveFirst = resolve; }); });
    mocks.upload.mockRejectedValueOnce(new AppError("INVALID_MEDIA", "第二份文件无法解码", 422));
    render(<Studio episodeId={eid} />);
    const input = await screen.findByLabelText(/选择视频文件/);
    fireEvent.change(input, { target: { files: [new File(["a"], "一.mp4"), new File(["b"], "二.mov")] } });
    fireEvent.click(screen.getByRole("button", { name: "上传这2份素材" }));
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "5");
    expect(screen.getByRole("progressbar")).toHaveAttribute("max", "20");
    await act(async () => { resolveFirst(asset()); });
    await screen.findByText(/已接收1／2份/);
    expect(screen.getByText("第二份文件无法解码")).toBeInTheDocument();
    expect(screen.getAllByText("现场原片.mp4").length).toBeGreaterThan(0);
    expect(mocks.upload).toHaveBeenCalledTimes(2);
  });

  it("makes deletion wait for explicit impact confirmation and exposes cleanup retry after a controlled failure", async () => {
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/assets/${aid}/impact`) return { asset_id: aid, versions: [vid], shares: [{ id: "share-1", label: "朋友", version_id: vid, state: "active" }], message: "关联版本会被限制" };
      if (path === `/assets/${aid}?confirm=true`) { saved.assets = []; saved.pending_cleanup = [{ asset_id: aid, filename: "现场原片.mp4" }]; throw new AppError("DELETION_PENDING", "物理清理尚未完成", 503, true); }
      return service(path, options);
    });
    render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "删除现场原片.mp4" }));
    const dialog = await screen.findByRole("dialog", { name: "删除“现场原片.mp4”？" });
    expect(within(dialog).getByText(/影响 1 个版本、1 条/)).toBeInTheDocument();
    expect(mocks.api.mock.calls.some(([, options]) => options?.method === "DELETE")).toBe(false);
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "确认删除应用副本" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "确认删除应用副本" }));
    await screen.findByRole("heading", { name: "删除尚未清理完成" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "取消" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("button", { name: "重试清理" })).toBeEnabled();
    expect(screen.queryByText(/上次请求结果未确定/)).not.toBeInTheDocument();
  });

  it("restores a saved editor from step/version URL and locks confirmed interview expression fields", async () => {
    saved.assets = [asset({ source_kind: "interview", fixed_segment: { start: 0, end: 8 }, contributor: "本人" })];
    saved.versions = [version({ state: "draft", packaging: "light" })];
    route("edit", vid); render(<Studio episodeId={eid} />);
    const title = await screen.findByLabelText("新版本名称");
    expect(title).toHaveValue("第一版");
    expect(screen.getByLabelText("开始（秒）")).toBeDisabled();
    expect(screen.getByLabelText("结束（秒）")).toBeDisabled();
    expect(screen.getByLabelText("花字（最多100字）")).toBeDisabled();
    expect(screen.getByRole("button", { name: "移除本段" })).toBeEnabled();
  });

  it("stops polling after jobs become terminal and refreshes once when the page becomes visible", async () => {
    vi.useFakeTimers();
    const job: Job = { id: jid, kind: "render", state: "queued", stage: "排队", error: null, version_id: vid, result: null };
    saved.jobs = [job]; let reads = 0;
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}`) { reads++; if (reads > 1) saved.jobs[0].state = "succeeded"; }
      return service(path, options);
    });
    await act(async () => { render(<Studio episodeId={eid} />); });
    expect(reads).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(reads).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(reads).toBe(2);
    await act(async () => { fireEvent(document, new Event("visibilitychange")); });
    expect(reads).toBe(3);
  });

  it("backs off repeated polling errors to thirty seconds and resets after success or returning to the foreground", async () => {
    vi.useFakeTimers();
    saved.jobs = [{ id: jid, kind: "render", state: "running", stage: "合成", error: null, version_id: vid, result: null }];
    let reads = 0;
    let failing = false;
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}`) {
        reads++;
        if (failing) throw new AppError("NETWORK_ERROR", "连接暂时中断");
      }
      return service(path, options);
    });
    const advance = async (milliseconds: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); }); };
    await act(async () => { render(<Studio episodeId={eid} />); });
    failing = true;
    await advance(2000);
    expect(reads).toBe(2);
    for (const delay of [2000, 4000, 8000, 16000, 30000, 30000]) {
      const before = reads;
      await advance(delay - 1);
      expect(reads).toBe(before);
      await advance(1);
      expect(reads).toBe(before + 1);
    }
    failing = false;
    await advance(30000);
    const afterSuccess = reads;
    failing = true;
    await advance(1999);
    expect(reads).toBe(afterSuccess);
    await advance(1);
    expect(reads).toBe(afterSuccess + 1);
    await advance(2000);
    expect(reads).toBe(afterSuccess + 2);

    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    const beforeHidden = reads;
    await advance(60000);
    expect(reads).toBe(beforeHidden);
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    await act(async () => { document.dispatchEvent(new Event("visibilitychange")); });
    expect(reads).toBe(beforeHidden + 1);
    await advance(1999);
    expect(reads).toBe(beforeHidden + 1);
    await advance(1);
    expect(reads).toBe(beforeHidden + 2);
  });

  it("does not apply a late saved-version response after navigating to another episode", async () => {
    let finish!: (value: Version) => void;
    const second = "d".repeat(32);
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}/versions`) return new Promise<Version>(resolve => { finish = resolve; });
      if (path === `/episodes/${second}`) return episode({ id: second, title: "另一期" });
      return service(path, options);
    });
    const view = render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "加入选段" }));
    fireEvent.click(screen.getByRole("button", { name: /03安排这一期/ }));
    fireEvent.click(screen.getByRole("button", { name: "保存为新版本" }));
    window.history.pushState(null, "", `/episodes/${second}?step=materials`);
    view.rerender(<Studio episodeId={second} />);
    await screen.findByRole("heading", { name: "另一期" });
    await act(async () => { finish(version()); });
    expect(window.location.pathname).toBe(`/episodes/${second}`);
    expect(window.location.search).toBe("?step=materials");
    expect(screen.queryByRole("heading", { name: "第一版" })).not.toBeInTheDocument();
  });

  it("does not offer model retries when billing is unknown or actions for an unknown version state", async () => {
    saved.jobs = [{ id: jid, kind: "analysis", state: "failed", stage: "待核对", error: { code: "COST_UNKNOWN", message: "调用费用未核实" }, version_id: null, result: null }];
    saved.versions = [version({ state: "future_state" })]; route("preview", vid);
    render(<Studio episodeId={eid} />);
    await screen.findByText(/调用费用未核实/);
    expect(screen.queryByRole("button", { name: "重试这一失败步骤" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "以这版为基础继续修改" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "导出这一版MP4" })).not.toBeInTheDocument();
  });

  it("authorizes the exact confirmed version before handing its download to the browser", async () => {
    saved.versions = [version({ state: "confirmed" })]; route("preview", vid);
    let authorize!: () => void;
    mocks.checkExport.mockImplementation(() => new Promise<void>(resolve => { authorize = resolve; }));
    const clicks: string[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicks.push(this.getAttribute("href") || ""); });
    render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "导出这一版MP4" }));
    expect(mocks.checkExport).toHaveBeenCalledWith(vid);
    expect(clicks).toEqual([]);
    expect(screen.queryByText(/已将这一版交给浏览器/)).not.toBeInTheDocument();
    await act(async () => { authorize(); });
    expect(clicks).toEqual([`/api/v1/versions/${vid}/export`]);
    await screen.findByText(/是否下载完成以浏览器为准/);
    click.mockRestore();
  });

  it("blocks a download when authorization now reports a restricted source", async () => {
    saved.versions = [version({ state: "confirmed" })]; route("preview", vid);
    mocks.checkExport.mockImplementation(async () => { saved.versions[0].state = "restricted"; throw new AppError("SOURCE_RESTRICTED", "来源授权已变化", 409); });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: "导出这一版MP4" }));
    await screen.findByText("来源授权已变化");
    expect(click).not.toHaveBeenCalled();
    expect(screen.queryByText(/已将这一版交给浏览器/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("button", { name: "导出这一版MP4" })).not.toBeInTheDocument());
    click.mockRestore();
  });

  it("keeps the dirty editor while media stays removed until a foreground source check completes", async () => {
    saved.versions = [version({ state: "draft" })]; route("edit", vid);
    const view = render(<Studio episodeId={eid} />);
    const title = await screen.findByLabelText("新版本名称");
    fireEvent.change(title, { target: { value: "尚未保存的修改" } });
    fireEvent.click(screen.getByRole("button", { name: "回看选段" }));
    expect(view.container.querySelector("video")).not.toBeNull();
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(view.container.querySelector("video")).toBeNull();
    let finish!: (value: Episode) => void;
    mocks.api.mockImplementation((path: string, options: Options = {}) => path === `/episodes/${eid}` ? new Promise<Episode>(resolve => { finish = resolve; }) : service(path, options));
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(view.container.querySelector("video")).toBeNull();
    expect(title).toHaveValue("尚未保存的修改");
    expect(screen.getByRole("button", { name: "保存为新版本" })).toBeDisabled();
    saved.assets = []; saved.versions[0].state = "restricted";
    await act(async () => { finish(structuredClone(saved)); });
    expect(title).toHaveValue("尚未保存的修改");
    expect(view.container.querySelector("video")).toBeNull();
    expect(screen.getByText("此来源已不可用。请移除本段，再核对新版本。")).toBeInTheDocument();
  });

  it("restores unsaved editor memory after an unguardable route unmount and isolates another user", async () => {
    saved.versions = [version({ state: "draft" })]; route("edit", vid);
    const first = render(<Studio episodeId={eid} />);
    fireEvent.change(await screen.findByLabelText("新版本名称"), { target: { value: "内存中的未保存编辑" } });
    first.unmount();
    const second = render(<Studio episodeId={eid} />);
    expect(await screen.findByLabelText("新版本名称")).toHaveValue("内存中的未保存编辑");
    expect(readWorkspaceDraft(eid)?.draft?.base_version_id).toBe(vid);
    second.unmount(); setDraftOwner("another-user");
    render(<Studio episodeId={eid} />);
    expect(await screen.findByLabelText("新版本名称")).toHaveValue("第一版");
  });

  it("preserves the original evidence revision when restoring an older memory draft", async () => {
    saved.analysis_state = "review";
    saved.evidence = [{ id: "proof", asset_id: aid, start: 0, end: 3, note: "旧证据", kind: "speech" }];
    route("direction"); const first = render(<Studio episodeId={eid} />);
    fireEvent.click(await screen.findByRole("button", { name: /查看与修改详细内容/ }));
    fireEvent.change(await screen.findByLabelText("核对后的文字，不确定的原话请移除"), { target: { value: "本人核对草案" } });
    first.unmount(); saved.evidence_revision = 2;
    render(<Studio episodeId={eid} />);
    expect(await screen.findByLabelText("核对后的文字，不确定的原话请移除")).toHaveValue("本人核对草案");
    expect(screen.getByRole("button", { name: "确认内容概览" })).toBeDisabled();
    expect(readWorkspaceDraft(eid)?.evidence?.revision).toBe(1);
  });
});

it("shows the saved subtitle version immediately while render is pending and keeps it on render failure", async () => {
  const nextId="d".repeat(32), cue="f".repeat(32);
  saved.versions=[version({packaging:"variety_v2"})];route("preview",vid);
  let rejectRender!: (error: Error) => void;
  mocks.api.mockImplementation(async (path: string, options: Options={}) => {
    if(path===`/versions/${vid}/subtitles`) return {version_id:vid,rows:[{cue_id:cue,segment_index:0,asset_id:aid,start:0,end:1,text:"测试",original_text:"测试",source_text:"测试",hidden:false,reviewed:false,locked:false,flags:[],media_url:`/api/v1/assets/${aid}/media`}]};
    if(path===`/episodes/${eid}/versions` && options.method==="POST") {
      const result=version({...options.body,id:nextId,state:"draft",media_url:null} as Partial<Version>);
      saved.versions.push(result);return result;
    }
    if(path===`/versions/${nextId}/subtitles`) return {version_id:nextId,rows:[]};
    if(path===`/versions/${nextId}/render`) return new Promise((_,reject)=>{rejectRender=reject;});
    return service(path,options);
  });
  render(<Studio episodeId={eid}/>);
  fireEvent.click(await screen.findByRole("button",{name:"校对字幕"}));
  fireEvent.change(await screen.findByLabelText("屏幕上显示的字幕"),{target:{value:"修改后的测试字幕"}});
  fireEvent.click(screen.getByRole("button",{name:"保存并生成新版"}));
  await waitFor(()=>expect(rejectRender).toBeTypeOf("function"));
  expect(screen.getByRole("heading",{name:"第一版 · 字幕校对"})).toBeInTheDocument();
  expect(screen.queryByText(/地址指定的版本当前不可用/)).not.toBeInTheDocument();
  expect(new URLSearchParams(window.location.search).get("version")).toBe(nextId);
  await act(async()=>rejectRender(new AppError("MEDIA_UNAVAILABLE","合成服务暂未就绪",409)));
  await screen.findByText(/合成服务暂未就绪/);
  expect(screen.getByRole("heading",{name:"第一版 · 字幕校对"})).toBeInTheDocument();
  expect(mocks.api.mock.calls.filter(([path,options])=>path===`/episodes/${eid}/versions` && options?.method==="POST")).toHaveLength(1);
});
