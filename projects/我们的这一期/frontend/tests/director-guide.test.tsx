import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DirectorDirections, DirectorGuide, DirectorGuideDock, hasCurrentDirections, type DirectorGuideProps } from "@/components/studio/director-guide";
import type { Direction, DirectorState, Episode, Version } from "@/lib/types";

const state = (overrides: Partial<DirectorState> = {}): DirectorState => ({ revision: 0, focus: null, notes: "", messages: [], directions_current: false, ...overrides });
const episode = (overrides: Partial<Episode> = {}): Episode => ({
  id: "episode", title: "这一桌", background: "", created_at: "2026-11-13", assets: [], versions: [], jobs: [], directions: [],
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 },
  evidence: [], evidence_revision: 1, evidence_confirmed_revision: null, analysis_state: "empty", analysis_notes: [], analysis_coverage: [],
  directions_revision: null, selected_direction: null, director: state(), ...overrides,
});
const asset = { id: "asset", filename: "现场.mp4", duration: 15, width: 720, height: 1280, has_audio: true, size: 1024,
  media_url: "/api/v1/assets/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/media", source_kind: "original", contributor: null, interview_text: null, fixed_segment: null };
const direction: Direction = { id: "d1", title: "同桌聊天", summary: "保留这一桌的完整聊天，再接到大家的举杯。", evidence: [{ asset_id: "asset", start: 0, end: 8, note: "同桌聊天" }] };
const confirmed = (): Episode => episode({ assets: [asset], analysis_state: "confirmed", evidence: [{ id: "e1", asset_id: "asset", start: 0, end: 8, note: "同桌聊天", kind: "visual" }], evidence_confirmed_revision: 1, director: state({ focus: "conversation" }) });
const readyVersion: Version = { id: "v1", title: "候选一", state: "ready", origin: "ai", segments: [], media_url: "/api/v1/versions/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/media", job_id: null, error: null,
  base_version_id: null, created_at: "2026-11-13", narration_text: "", narration_approved: false, narration_ready: false, packaging: "original", evidence_revision: 1 };
const props = (overrides: Partial<DirectorGuideProps> = {}): DirectorGuideProps => ({ episode: episode(), step: "materials", busy: false, canGenerate: true, pending: false,
  onMessage: vi.fn().mockResolvedValue(true), onNavigate: vi.fn(), onGenerate: vi.fn(), onSelect: vi.fn(), onDirtyChange: vi.fn(), ...overrides });

describe("persistent director guide", () => {
  it("starts the selected original-sound draft only after its explicit action", () => {
    const p = props({ step: "direction", episode: { ...confirmed(), directions: [direction], directions_revision: 1, director: state({ directions_current: true }) } });
    const auto = vi.fn();
    render(<DirectorDirections {...p} onAutoSelect={auto} />);
    expect(auto).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: /导演直接出原声初稿/ }));
    expect(auto).toHaveBeenCalledWith(direction);
    expect(p.onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '先微调，再开拍' }));
    expect(p.onSelect).toHaveBeenCalledWith(direction);
  });
  it("omits the guide for an older cached episode instead of fabricating director state", () => {
    const p = props({ episode: episode({ director: undefined }) });
    const view = render(<DirectorGuide {...p} />);
    expect(view.container).toBeEmptyDOMElement();
    expect(p.onMessage).not.toHaveBeenCalled();
    expect(p.onGenerate).not.toHaveBeenCalled();
  });

  it("offers three explicit starting choices and saves only the clicked choice", async () => {
    const p = props(); render(<DirectorGuide {...p} />);
    expect(screen.getByRole("heading", { name: "这一期，最想留下什么？" })).toBeVisible();
    expect(within(screen.getByLabelText("这一期想留下什么")).getAllByRole("button")).toHaveLength(3);
    expect(p.onGenerate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /保留聊天/ }));
    await waitFor(() => expect(p.onMessage).toHaveBeenCalledWith({ choice: "conversation" }));
    expect(p.onGenerate).not.toHaveBeenCalled();
    expect(screen.queryByText("已为你选择最合适的方向")).not.toBeInTheDocument();
  });

  it("keeps text on failed save, marks it dirty, and clears only after a successful retry", async () => {
    const save = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const p = props({ onMessage: save }); render(<DirectorGuide {...p} />);
    const input = screen.getByLabelText("还有想保留或避开的内容？");
    fireEvent.change(input, { target: { value: "保留聊天，别放尴尬镜头。" } });
    expect(p.onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await screen.findByRole("alert");
    expect(input).toHaveValue("保留聊天，别放尴尬镜头。");
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(save).toHaveBeenLastCalledWith({ text: "保留聊天，别放尴尬镜头。" });
    // Dirty state is delivered by useEffect after the input DOM has committed.
    await waitFor(() => expect(p.onDirtyChange).toHaveBeenLastCalledWith(false));
    expect(p.onGenerate).not.toHaveBeenCalled();
  });

  it("preserves text when the callback rejects and prevents duplicate requests while saving", async () => {
    let reject: (error: Error) => void = () => {};
    const save = vi.fn().mockImplementation(() => new Promise<boolean>((_, fail) => { reject = fail; }));
    const p = props({ onMessage: save }); render(<DirectorGuide {...p} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "完整保留这句话" } });
    const submit = screen.getByRole("button", { name: "保存导演要求" });
    fireEvent.click(submit); fireEvent.click(submit);
    expect(submit).toBeDisabled(); expect(save).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error("network")));
    expect(screen.getByRole("textbox")).toHaveValue("完整保留这句话");
    expect(screen.getByRole("alert")).toHaveTextContent("输入已保留");
  });

  it("retains an unsent message across step changes and blocks generation until it is saved", () => {
    const p = props({ episode: confirmed(), step: "direction" });
    const view = render(<DirectorGuide {...p} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "别截断原声" } });
    expect(screen.getByRole("button", { name: /请导演提出拍法/ })).toBeDisabled();
    view.rerender(<DirectorGuide {...p} step="edit" />);
    expect(screen.getByRole("textbox")).toHaveValue("别截断原声");
    expect(p.onGenerate).not.toHaveBeenCalled();
  });

  it.each([false, true])("never calls a model automatically; generation is gated when canGenerate=%s", canGenerate => {
    const p = props({ episode: episode({ assets: [asset], director: state({ focus: "auto" }) }), canGenerate });
    render(<DirectorGuide {...p} />);
    const button = screen.getByRole("button", { name: /让导演先看素材/ });
    expect(p.onGenerate).not.toHaveBeenCalled();
    if (!canGenerate) { expect(button).toBeDisabled(); fireEvent.click(button); expect(p.onGenerate).not.toHaveBeenCalled(); }
    else { expect(button).toBeEnabled(); fireEvent.click(button); expect(p.onGenerate).toHaveBeenCalledWith("analysis"); }
  });

  it("navigates to unconfirmed evidence without approving it or generating directions", () => {
    const p = props({ episode: episode({ assets: [asset], analysis_state: "review", director: state({ focus: "auto" }) }) });
    render(<DirectorGuide {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /看看素材理解是否准确/ }));
    expect(p.onNavigate).toHaveBeenCalledWith("direction");
    expect(p.onGenerate).not.toHaveBeenCalled(); expect(p.onMessage).not.toHaveBeenCalled();
  });

  it("uses the actual direction and original summary without marking the first one recommended", () => {
    const second: Direction = { ...direction, id: "d2", title: "举杯时刻", summary: "先交代这一桌，再保留举杯现场。" };
    const p = props({ step: "direction", episode: { ...confirmed(), directions: [direction, second], directions_revision: 1, director: state({ focus: "conversation", directions_current: true }) } });
    render(<DirectorDirections {...p} />);
    expect(screen.getByText(direction.summary)).toBeVisible(); expect(screen.getByText(second.summary)).toBeVisible();
    const cards = screen.getAllByRole("article");
    fireEvent.click(within(cards[1]).getByRole("button", { name: /按这个拍/ }));
    expect(p.onSelect).toHaveBeenCalledWith(second); expect(p.onGenerate).not.toHaveBeenCalled();
    expect(screen.queryByText(/推荐/)).not.toBeInTheDocument();
  });

  it("keeps a new proposal and its action visible even when an older confirmed version exists", () => {
    const p = props({ step: "direction", version: { ...readyVersion, state: "confirmed" }, episode: {
      ...confirmed(), versions: [{ ...readyVersion, state: "confirmed" }], directions: [direction], directions_revision: 1,
      director: state({ focus: "conversation", directions_current: true }),
    } });
    render(<><DirectorDirections {...p} /><DirectorGuideDock {...p} /></>);
    const action = screen.getByRole("button", { name: /按这个拍/ });
    expect(action).toBeVisible();
    expect(screen.getByText(direction.summary)).toBeVisible();
    expect(screen.getByLabelText("还有想保留或避开的内容？")).not.toBeVisible();
    expect(screen.queryByText(/再看看这次/)).not.toBeInTheDocument();
    fireEvent.click(action);
    expect(p.onSelect).toHaveBeenCalledExactlyOnceWith(direction);
  });

  it.each(["state", "evidence", "directions", "brief", "empty"])("hides stale directions when the %s confirmation is out of date", mismatch => {
    const e = { ...confirmed(), directions: [direction], directions_revision: 1, director: state({ focus: "auto", directions_current: true }) };
    if (mismatch === "state") e.analysis_state = "review";
    if (mismatch === "evidence") e.evidence_confirmed_revision = 0;
    if (mismatch === "directions") e.directions_revision = 0;
    if (mismatch === "brief") e.director.directions_current = false;
    if (mismatch === "empty") e.evidence = [];
    render(<DirectorDirections {...props({ episode: e })} />);
    expect(screen.queryByRole("button", { name: /按这个拍/ })).not.toBeInTheDocument();
    expect(hasCurrentDirections(e)).toBe(false);
  });

  it("disables actions while work is pending and waits for server messages", () => {
    const p = props({ pending: true }); render(<DirectorGuide {...p} />);
    expect(screen.getByRole("status")).toHaveTextContent("正在处理这一期");
    expect(screen.getByRole("button", { name: /你先看看/ })).toBeDisabled();
    expect(screen.getByRole("textbox")).toBeDisabled();
    expect(p.onGenerate).not.toHaveBeenCalled(); expect(p.onMessage).not.toHaveBeenCalled();
  });

  it("folds long generated summaries and earlier history while preserving their complete text", () => {
    const summary = "镜头按真实对话展开。".repeat(18) + "这是真实说明的最后一句。";
    const messages: DirectorState["messages"] = Array.from({ length: 5 }, (_, index) => ({ id: `m${index}`, role: index % 2 ? "director" : "user", text: `服务端记录${index}`, created_at: "2026-11-13" }));
    const p = props({ episode: { ...confirmed(), directions: [{ ...direction, summary }], directions_revision: 1, director: state({ focus: "conversation", messages, directions_current: true }) } });
    render(<><DirectorGuide {...p} /><DirectorDirections {...p} /></>);
    expect(screen.getByText("服务端记录0")).not.toBeVisible();
    expect(screen.getByText("服务端记录3")).toBeVisible(); expect(screen.getByText("服务端记录4")).toBeVisible();
    expect(screen.queryByText(summary)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开方案1完整说明" }));
    expect(screen.getByText(summary)).toBeVisible();
    fireEvent.click(screen.getByText("查看之前的 3 条沟通"));
    expect(screen.getByText("服务端记录0")).toBeVisible();
  });

  it("keeps the current preview usable without forcing an initial focus choice", () => {
    const p = props({ step: "preview", episode: episode({ versions: [readyVersion] }), version: readyVersion });
    render(<DirectorGuide {...p} />);
    expect(screen.getByText("当前候选就在下面")).toBeVisible();
    expect(screen.queryByRole("button", { name: /让导演先看素材|请导演提出拍法/ })).not.toBeInTheDocument();
    expect(p.onGenerate).not.toHaveBeenCalled();
    expect(screen.queryByText(/当前要求或素材有更新/)).not.toBeInTheDocument();
  });

  it.each(["draft", "failed"])("keeps a %s preview on its current page instead of navigating back to editing", versionState => {
    const version = { ...readyVersion, state: versionState, media_url: null };
    const p = props({ step: "preview", episode: episode({ versions: [version] }), version });
    render(<DirectorGuide {...p} />);
    expect(screen.getByText("在下方继续制作")).toBeVisible();
    expect(screen.queryByRole("button", { name: /继续安排这一期/ })).not.toBeInTheDocument();
    expect(p.onNavigate).not.toHaveBeenCalled();
  });

  it("marks fixed demonstration directions and respects the workspace action lock", () => {
    const p = props({ demo: true, episode: { ...confirmed(), directions: [direction], directions_revision: 1, director: state({ focus: "auto", directions_current: true }) } });
    render(<DirectorDirections {...p} busy />);
    expect(screen.getByText("固定演示 · 非真实 AI")).toBeVisible();
    const select = screen.getByRole("button", { name: /按这个拍/ });
    expect(select).toBeDisabled(); fireEvent.click(select);
    expect(p.onSelect).not.toHaveBeenCalled();
  });

  it("does not accept a submitted message beyond the 1000-character limit", () => {
    const p = props(); render(<DirectorGuide {...p} />);
    const input = screen.getByRole("textbox");
    expect(input).toHaveAttribute("maxLength", "1000");
    fireEvent.change(input, { target: { value: "字".repeat(1001) } });
    fireEvent.submit(input.closest("form")!);
    expect(p.onMessage).not.toHaveBeenCalled();
  });
});

describe("compact director entry", () => {
  it.each(["materials", "direction", "edit", "preview"] as const)("defaults to a compact entry on %s while leaving its input mounted", step => {
    const p = props({ step }); const view = render(<DirectorGuideDock {...p} />);
    const dock = view.container.querySelector(".director-guide-dock") as HTMLDetailsElement;
    expect(dock.open).toBe(false);
    expect(screen.getByText("偏好与要求")).toBeVisible();
    expect(screen.getByLabelText("还有想保留或避开的内容？")).not.toBeVisible();
    fireEvent.click(screen.getByText("偏好与要求"));
    expect(screen.getByRole("textbox")).toBeVisible();
    expect(p.onGenerate).not.toHaveBeenCalled(); expect(p.onMessage).not.toHaveBeenCalled();
  });

  it("opens when explicitly requested and preserves unsent input while moving to editing", async () => {
    const p = props(); const view = render(<DirectorGuideDock {...p} defaultOpen />);
    const input = screen.getByRole("textbox");
    expect(input).toBeVisible();
    fireEvent.change(input, { target: { value: "请保留这句完整原话" } });
    view.rerender(<DirectorGuideDock {...p} step="direction" />);
    expect(screen.getByRole("textbox")).toBe(input);
    view.rerender(<DirectorGuideDock {...p} step="edit" />);
    expect(screen.getByRole("textbox")).toBe(input);
    expect(input).toHaveValue("请保留这句完整原话");
    expect(input).toBeVisible();
    fireEvent.click(screen.getByText("偏好与要求"));
    expect(input).toBeVisible();
    expect(screen.getByText("有未发送的要求，先保存")).toBeVisible();
    expect(p.onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: "保存导演要求" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(p.onDirtyChange).toHaveBeenLastCalledWith(false);
  });

  it("collapses after the primary guidance is no longer requested without remounting the input", async () => {
    const p = props(); const view = render(<DirectorGuideDock {...p} defaultOpen />);
    const input = screen.getByRole("textbox");
    view.rerender(<DirectorGuideDock {...p} step="edit" />);
    expect(input).not.toBeVisible();
    view.rerender(<DirectorGuideDock {...p} step="materials" defaultOpen />);
    await waitFor(() => expect(input).toBeVisible());
    expect(screen.getByRole("textbox")).toBe(input);
  });

  it("collapses a formerly primary guide when directions become ready on the same step", () => {
    const p = props({ step: "direction" });
    const view = render(<DirectorGuideDock {...p} defaultOpen />);
    const input = screen.getByRole("textbox");
    view.rerender(<DirectorGuideDock {...p} defaultOpen={false} />);
    expect(input).not.toBeVisible();
  });

  it("does not render an empty director entry for old cached episodes", () => {
    const view = render(<DirectorGuideDock {...props({ episode: episode({ director: undefined }), step: "edit" })} />);
    expect(view.container).toBeEmptyDOMElement();
  });
});
