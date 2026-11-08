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
  if (path === `/episodes/${eid}/versions` && options.method === "POST") {
    const result = version({ ...options.body, state: "draft", media_url: null } as Partial<Version>);
    saved.versions.push(result);
    return result;
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

  it("requires playback to reach the end, explicit human acknowledgement, and a version-specific confirmation", async () => {
    saved.versions = [version()]; route("preview", vid);
    render(<Studio episodeId={eid} />);
    await screen.findByRole("heading", { name: "第一版" });
    const button = screen.getByRole("button", { name: "确认当前版本" });
    expect(button).toBeDisabled();
    const acknowledgement = screen.getByRole("checkbox");
    expect(acknowledgement).toBeDisabled();
    fireEvent.ended(screen.getByLabelText("第一版"));
    expect(acknowledgement).toBeEnabled(); expect(button).toBeDisabled();
    fireEvent.click(acknowledgement); fireEvent.click(button);
    const dialog = screen.getByRole("dialog", { name: "确认“第一版”？" });
    expect(mocks.api.mock.calls.some(([path]) => String(path).endsWith("/confirm"))).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认此版本" }));
    await screen.findByText("已确认此版本");
    expect(mocks.api).toHaveBeenCalledWith(`/versions/${vid}/confirm`, { method: "POST", body: {} });
  });

  it("preserves dirty evidence across refresh and prevents confirming a stale revision", async () => {
    saved.analysis_state = "review";
    saved.evidence = [{ id: "evidence-1", asset_id: aid, start: 0, end: 3, note: "待核对的文字", kind: "speech" }];
    route("direction"); render(<Studio episodeId={eid} />);
    const field = await screen.findByLabelText("核对后的文字，不确定的原话请移除");
    fireEvent.change(field, { target: { value: "我核对后的内容" } });
    saved.evidence_revision = 2; saved.evidence[0].note = "服务器的新版本";
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await screen.findByText(/来源核对已在服务端更新/);
    expect(field).toHaveValue("我核对后的内容");
    expect(screen.getByRole("button", { name: "保存并确认证据" })).toBeDisabled();
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
    fireEvent.change(await screen.findByLabelText("核对后的文字，不确定的原话请移除"), { target: { value: "本人核对草案" } });
    first.unmount(); saved.evidence_revision = 2;
    render(<Studio episodeId={eid} />);
    expect(await screen.findByLabelText("核对后的文字，不确定的原话请移除")).toHaveValue("本人核对草案");
    expect(screen.getByRole("button", { name: "保存并确认证据" })).toBeDisabled();
    expect(readWorkspaceDraft(eid)?.evidence?.revision).toBe(1);
  });
});
