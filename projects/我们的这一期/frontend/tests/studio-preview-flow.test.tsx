import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import Studio from "@/components/studio";
import { AppError } from "@/lib/api";
import type { Episode, Status, Version } from "@/lib/types";
import { clearWorkspaceDrafts, readWorkspaceDraft, setDraftOwner } from "@/components/studio/draft-cache";

const mocks = vi.hoisted(() => ({ api: vi.fn(), upload: vi.fn(), checkExport: vi.fn() }));
vi.mock("@/lib/api", () => ({
  api: mocks.api, uploadAsset: mocks.upload, checkExport: mocks.checkExport,
  exportUrl: (id: string) => `/api/v1/versions/${id}/export`,
  AppError: class extends Error {
    constructor(public code: string, message: string, public status = 0, public uncertain = false) { super(message); this.name = "AppError"; }
  },
}));

const eid = "e".repeat(32), aid = "a".repeat(32), currentId = "b".repeat(32), newId = "d".repeat(32), confirmedId = "f".repeat(32);
const makeVersion = (overrides: Partial<Version> = {}): Version => ({
  id: currentId, title: "导演提出的新故事", state: "draft", origin: "ai", media_url: null, job_id: null,
  segments: [{ asset_id: aid, start: 4.02, end: 10.62, caption: "先喝口", purpose: "让这句话说完整", evidence_ids: ["speech-1"] }],
  error: null, base_version_id: null, created_at: "2026-11-14T08:00:00Z", narration_text: "", narration_approved: false,
  narration_ready: false, packaging: "variety_v2", evidence_revision: 1, ...overrides,
});
const makeEpisode = (): Episode => ({
  id: eid, title: "朋友聚会", background: "", created_at: "2026-11-14T08:00:00Z",
  assets: [{ id: aid, filename: "现场原片.mp4", duration: 20, width: 720, height: 1280, has_audio: true,
    size: 1024, media_url: `/api/v1/assets/${aid}/media`, source_kind: "original", contributor: null, interview_text: null, fixed_segment: null }],
  versions: [makeVersion({ id: confirmedId, title: "之前已确认的一版", state: "confirmed", media_url: `/api/v1/versions/${confirmedId}/media` }), makeVersion()],
  evidence: [{ id: "speech-1", asset_id: aid, start: 4.02, end: 10.62, kind: "speech", note: "先倒上一杯了，先喝口。" }],
  evidence_revision: 1, evidence_confirmed_revision: 1, analysis_state: "confirmed", analysis_notes: [], analysis_coverage: [],
  directions: [], directions_revision: 1, selected_direction: null, jobs: [],
  director: { revision: 2, focus: "conversation", notes: "原声说完整", messages: [], directions_current: true },
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 },
});
const status: Status = { cloud_enabled: false, workflow_enabled: false, cloud_reason: "模型生成尚未启用", media_ready: true, stage: "阶段3-2", model_mode: "cloud" };
type Options = { method?: string; body?: Record<string, unknown>; signal?: AbortSignal };
let saved: Episode;
let renderFailure: Error | null;
let routeAtRender: string[];
async function service(path: string, options: Options = {}) {
  if (path === "/status") return { ...status };
  if (path === `/episodes/${eid}`) return structuredClone(saved);
  if (path === `/episodes/${eid}/versions` && options.method === "POST") {
    const next = makeVersion({ ...options.body, id: newId, state: "draft", media_url: null } as Partial<Version>);
    saved.versions.push(next);
    return structuredClone(next);
  }
  if (/^\/versions\/[^/]+\/render$/.test(path) && options.method === "POST") {
    routeAtRender.push(window.location.search);
    if (renderFailure) throw renderFailure;
    const selected = saved.versions.find(item => path === `/versions/${item.id}/render`);
    if (!selected) throw new Error("The render referenced a version that was not saved");
    selected.state = "rendering";
    return { id: "job-preview", state: "queued" };
  }
  throw new Error(`Unexpected test route: ${path}`);
}
const writes = () => mocks.api.mock.calls.filter(([, options]) => options?.method === "POST");
const versionWrites = () => writes().filter(([path]) => path.endsWith("/versions"));
const renderWrites = () => writes().filter(([path]) => path.endsWith("/render"));
const voiceWrites = () => writes().filter(([path]) => path.endsWith("/narration"));
function openEditor() {
  window.history.replaceState(null, "", `/episodes/${eid}?step=edit&version=${currentId}`);
  return render(<Studio episodeId={eid} />);
}
async function chooseOriginalSound() {
  fireEvent.click(await screen.findByRole("button", { name: "保留现场原声" }));
  return screen.getByRole("button", { name: "生成预览" });
}
beforeEach(() => {
  clearWorkspaceDrafts(); setDraftOwner("preview-flow-user");
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  mocks.api.mockReset(); mocks.upload.mockReset(); mocks.checkExport.mockReset();
  saved = makeEpisode(); renderFailure = null; routeAtRender = []; mocks.api.mockImplementation(service);
});
afterEach(cleanup);

describe("guided preview action", () => {
  it("saves an explicit original-sound change, renders the returned new id, and preserves the confirmed film", async () => {
    saved.versions[1].narration_text = "这桌的故事，开始了。";
    const confirmed = structuredClone(saved.versions[0]);
    openEditor();
    const trigger = await chooseOriginalSound();
    expect(writes()).toHaveLength(0);
    fireEvent.click(trigger);
    await waitFor(() => expect(renderWrites()).toHaveLength(1));
    expect(versionWrites()).toHaveLength(1);
    expect(versionWrites()[0][1].body).toMatchObject({ narration_text: "", base_version_id: currentId,
      segments: [{ asset_id: aid, start: 4.02, end: 10.62, evidence_ids: ["speech-1"] }] });
    expect(renderWrites()[0][0]).toBe(`/versions/${newId}/render`);
    expect(routeAtRender).toEqual([`?step=preview&version=${newId}`]);
    expect(saved.versions[0]).toEqual(confirmed);
    expect(saved.versions[1].narration_text).toBe("这桌的故事，开始了。");
    expect(voiceWrites()).toHaveLength(0);
    // The render request is observable before React commits the saved draft
    // and its cache-clearing effect. Wait for the persisted UI outcome too.
    await waitFor(() => expect(readWorkspaceDraft(eid)?.draft).toBeUndefined());
  });

  it("renders an unchanged saved draft directly without manufacturing another version", async () => {
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(renderWrites()).toHaveLength(1));
    expect(versionWrites()).toHaveLength(0);
    expect(renderWrites()[0][0]).toBe(`/versions/${currentId}/render`);
    expect(saved.versions).toHaveLength(2);
  });

  it("renders the draft actually shown after previewing a different version and returning to arrange", async () => {
    openEditor();
    await screen.findByRole("button", { name: "生成预览" });
    fireEvent.click(screen.getByRole("button", { name: /预览与确认/ }));
    fireEvent.click(screen.getByRole("button", { name: "切换版本（2）" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "本期版本" })).getByRole("button", { name: /之前已确认的一版/ }));
    expect(window.location.search).toContain(`version=${confirmedId}`);
    fireEvent.click(screen.getByRole("button", { name: /安排这一期/ }));
    expect(screen.getByText("让这句话说完整")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(renderWrites()).toHaveLength(1));
    expect(renderWrites()[0][0]).toBe(`/versions/${currentId}/render`);
    expect(versionWrites()).toHaveLength(0);
    expect(window.location.search).toContain(`step=preview&version=${currentId}`);
    expect(saved.versions[0].state).toBe("confirmed");
  });

  it("does not substitute the previewed film when the editor's base disappears", async () => {
    openEditor();
    await screen.findByRole("button", { name: "生成预览" });
    fireEvent.click(screen.getByRole("button", { name: /预览与确认/ }));
    fireEvent.click(screen.getByRole("button", { name: "切换版本（2）" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "本期版本" })).getByRole("button", { name: /之前已确认的一版/ }));
    saved.versions = saved.versions.filter(item => item.id !== currentId);
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}/versions` && options.method === "POST") {
        throw new AppError("VERSION_NOT_FOUND", "草案来源已不可用，请核对后重建", 409);
      }
      return service(path, options);
    });
    fireEvent.click(screen.getByRole("button", { name: "刷新状态" }));
    await screen.findByRole("button", { name: "切换版本（1）" });
    fireEvent.click(screen.getByRole("button", { name: /安排这一期/ }));
    fireEvent.click(screen.getByRole("button", { name: "生成预览" }));
    await screen.findByText("草案来源已不可用，请核对后重建");
    expect(versionWrites()).toHaveLength(1);
    expect(versionWrites()[0][1].body.base_version_id).toBe(currentId);
    expect(renderWrites()).toHaveLength(0);
    expect(saved.versions[0].state).toBe("confirmed");
    expect(screen.getByText("让这句话说完整")).toBeVisible();
  });

  it.each(["ready", "confirmed"] as const)("opens an unchanged %s version without creating or re-rendering it", async state => {
    saved.versions[1] = makeVersion({ state, media_url: `/api/v1/versions/${currentId}/media` });
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "生成预览" }));
    await waitFor(() => expect(window.location.search).toContain("step=preview"));
    expect(writes()).toHaveLength(0);
    expect(saved.versions[1].state).toBe(state);
  });

  it("takes narration to its confirmation step and never starts a voice or render call on continue", async () => {
    saved.versions[1].narration_text = "这桌的故事，开始了。";
    openEditor();
    fireEvent.change(await screen.findByRole("textbox", { name: /^开场旁白文案/ }), { target: { value: "镜头给到这一桌！在场，开拍！" } });
    fireEvent.click(screen.getByRole("button", { name: "下一步：确认旁白" }));
    await waitFor(() => expect(window.location.search).toContain(`step=preview&version=${newId}`));
    expect(versionWrites()).toHaveLength(1);
    expect(versionWrites()[0][1].body.narration_text).toBe("镜头给到这一桌！在场，开拍！");
    expect(renderWrites()).toHaveLength(0);
    expect(voiceWrites()).toHaveLength(0);
  });

  it("keeps a newly saved draft after a definite render failure and retries only its render", async () => {
    saved.versions[1].narration_text = "等待清除的旁白";
    renderFailure = new AppError("MEDIA_UNAVAILABLE", "合成暂时不可用", 503);
    openEditor();
    fireEvent.click(await chooseOriginalSound());
    await screen.findByText("合成暂时不可用");
    expect(window.location.search).toContain(`step=preview&version=${newId}`);
    expect(saved.versions.at(-1)?.id).toBe(newId);
    expect(versionWrites()).toHaveLength(1);
    renderFailure = null;
    const retry = await screen.findByRole("button", { name: "开始本机合成" });
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);
    await waitFor(() => expect(renderWrites()).toHaveLength(2));
    expect(renderWrites().map(([path]) => path)).toEqual([`/versions/${newId}/render`, `/versions/${newId}/render`]);
    expect(versionWrites()).toHaveLength(1);
    expect(voiceWrites()).toHaveLength(0);
  });

  it("locks retry after an unknown render result instead of resaving or resending", async () => {
    saved.versions[1].narration_text = "等待清除的旁白";
    renderFailure = new AppError("NETWORK_ERROR", "提交结果还未确定", 0, true);
    openEditor();
    fireEvent.click(await chooseOriginalSound());
    await screen.findByText(/提交结果还未确定/);
    expect(window.location.search).toContain(newId);
    const retry = screen.getByRole("button", { name: "开始本机合成" });
    expect(retry).toBeDisabled();
    fireEvent.click(retry);
    expect(versionWrites()).toHaveLength(1);
    expect(renderWrites()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "我已核对当前记录" })).toBeEnabled();
  });

  it("coalesces rapid clicks while the new version is being saved", async () => {
    saved.versions[1].narration_text = "等待清除的旁白";
    let release!: () => void;
    const deferred = new Promise<void>(resolve => { release = resolve; });
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (path === `/episodes/${eid}/versions` && options.method === "POST") await deferred;
      return service(path, options);
    });
    openEditor();
    const trigger = await chooseOriginalSound();
    fireEvent.click(trigger); fireEvent.click(trigger);
    expect(versionWrites()).toHaveLength(1);
    release();
    await waitFor(() => expect(renderWrites()).toHaveLength(1));
    expect(versionWrites()).toHaveLength(1);
  });
});
