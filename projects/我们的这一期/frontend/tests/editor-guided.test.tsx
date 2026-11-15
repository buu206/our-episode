import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EditorPanel } from "@/components/studio/panels";
import type { Draft } from "@/components/studio/model";
import type { Episode } from "@/lib/types";

const assetId = "a".repeat(32);
const makeEpisode = (): Episode => ({
  id: "e".repeat(32), title: "周末聚会", background: "测试素材", created_at: "2026-11-13T10:00:00+00:00",
  assets: [{ id: assetId, filename: "测试原片.mp4", duration: 20, width: 720, height: 1280, has_audio: true,
    size: 1024, media_url: `/api/v1/assets/${assetId}/media`, source_kind: "original", contributor: null,
    interview_text: null, fixed_segment: null }],
  versions: [], jobs: [], directions: [], evidence: [], evidence_revision: 1, evidence_confirmed_revision: 1,
  analysis_state: "confirmed", analysis_notes: [], analysis_coverage: [], directions_revision: 1, selected_direction: null,
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 },
});
const makeDraft = (): Draft => ({
  title: "聚会第一版", packaging: "variety_v2", narration_text: "镜头给到这一桌！在场，开拍！", base_version_id: "b".repeat(32),
  segments: [
    { asset_id: assetId, start: 0, end: 6, caption: "一起开场", purpose: "先交代聚会的场景", evidence_ids: ["proof-1"], transition: "cut", transition_duration: 0 },
    { asset_id: assetId, start: 8, end: 12.3, caption: "", evidence_ids: ["proof-2"], transition: "dissolve", transition_duration: .3 },
  ],
});
const callbacks = () => ({ onChange: vi.fn(), onSave: vi.fn(), onSource: vi.fn(), onMaterials: vi.fn(), onContinue: vi.fn() });

function Editable({ initial = makeDraft(), actions = callbacks(), episode = makeEpisode() }: { initial?: Draft; actions?: ReturnType<typeof callbacks>; episode?: Episode }) {
  const [draft, setDraft] = useState(initial);
  return <EditorPanel episode={episode} source={null} draft={draft} busy={false} demo={false} guided {...actions}
    onChange={next => { actions.onChange(next); setDraft(next); }} />;
}

describe("guided editor", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  });
  it("puts story, sound choice, packaging and the next action in one visible card", () => {
    const draft = makeDraft(), actions = callbacks();
    render(<EditorPanel episode={makeEpisode()} source={null} draft={draft} busy={false} demo={false} guided {...actions} />);
    const plan = screen.getByRole("region", { name: "导演的镜头安排" });
    expect(within(plan).getByText("2 个片段 · 正文约 10 秒")).toBeVisible();
    expect(within(plan).getByText("先交代聚会的场景")).toBeVisible();
    expect(within(plan).getByText("已保存选段")).toBeVisible();
    expect(within(plan).getByLabelText("开场旁白文案")).toHaveValue(draft.narration_text);
    expect(within(plan).getByRole("button", { name: "保留现场原声" })).toBeVisible();
    expect(within(plan).getByRole("combobox", { name: "节目包装" })).toBeVisible();
    expect(within(plan).getByRole("button", { name: "下一步：确认旁白" })).toBeVisible();
    expect(screen.queryByLabelText("新版本名称")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存为新版本" })).not.toBeInTheDocument();
    expect(actions.onChange).not.toHaveBeenCalled();
    expect(actions.onSave).not.toHaveBeenCalled();
  });

  it("uses original sound without changing timing, flowers, packaging or source metadata, and can restore the text", () => {
    const original = makeDraft(), actions = callbacks();
    render(<Editable initial={original} actions={actions} />);
    fireEvent.click(screen.getByRole("button", { name: "保留现场原声" }));
    expect(actions.onChange).toHaveBeenLastCalledWith({ ...original, narration_text: "" });
    expect(screen.getByRole("button", { name: "保留现场原声" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByLabelText("开场旁白文案")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成预览" })).toBeEnabled();
    expect(actions.onSave).not.toHaveBeenCalled();
    expect(actions.onContinue).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "加开场旁白" }));
    expect(screen.getByLabelText("开场旁白文案")).toHaveValue(original.narration_text);
    expect(actions.onChange).toHaveBeenLastCalledWith(original);
    fireEvent.click(screen.getByRole("button", { name: "下一步：确认旁白" }));
    expect(actions.onContinue).toHaveBeenCalledTimes(1);
  });

  it("allows writing narration after starting with original sound and keeps an empty entry open while typing", () => {
    const original = { ...makeDraft(), narration_text: "" }, actions = callbacks();
    render(<Editable initial={original} actions={actions} />);
    fireEvent.click(screen.getByRole("button", { name: "加开场旁白" }));
    const field = screen.getByLabelText("开场旁白文案");
    expect(field).toHaveValue("");
    fireEvent.change(field, { target: { value: "镜头给到这一桌！" } });
    expect(actions.onChange).toHaveBeenLastCalledWith({ ...original, narration_text: "镜头给到这一桌！" });
    expect(screen.getByRole("button", { name: "下一步：确认旁白" })).toBeEnabled();
    fireEvent.change(field, { target: { value: "" } });
    expect(field).toBeVisible();
    expect(screen.getByRole("button", { name: "生成预览" })).toBeEnabled();
  });

  it("does not carry a previous version's backed-up narration into another version", () => {
    const actions = callbacks(), original = makeDraft();
    const view = render(<EditorPanel episode={makeEpisode()} source={null} draft={original} busy={false} demo={false} guided {...actions} />);
    fireEvent.click(screen.getByRole("button", { name: "保留现场原声" }));
    const different = { ...original, base_version_id: "c".repeat(32), narration_text: "" };
    view.rerender(<EditorPanel episode={makeEpisode()} source={null} draft={different} busy={false} demo={false} guided {...actions} />);
    fireEvent.click(screen.getByRole("button", { name: "加开场旁白" }));
    expect(actions.onChange).toHaveBeenLastCalledWith(different);
    expect(screen.getByLabelText("开场旁白文案")).toHaveValue("");
  });

  it("opens detailed adjustments directly in a dialog and preserves edits after closing and reopening", () => {
    const original = makeDraft(), actions = callbacks();
    render(<Editable initial={original} actions={actions} />);
    fireEvent.click(screen.getByRole("button", { name: "微调片段" }));
    let dialog = screen.getByRole("dialog", { name: "微调片段与包装" });
    expect(dialog).toBeVisible();
    expect(within(dialog).getAllByRole("button", { name: "回看选段" })).toHaveLength(2);
    fireEvent.change(within(dialog).getByLabelText("新版本名称"), { target: { value: "保留原声的一版" } });
    fireEvent.change(within(dialog).getAllByLabelText("花字（最多100字）")[0], { target: { value: "今晚开场" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "完成微调" }));
    expect(screen.queryByRole("dialog", { name: "微调片段与包装" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("新版本名称")).not.toBeInTheDocument();
    expect(actions.onChange).toHaveBeenLastCalledWith({ ...original, title: "保留原声的一版", segments: [{ ...original.segments[0], caption: "今晚开场" }, original.segments[1]] });
    expect(actions.onSave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "微调片段" }));
    dialog = screen.getByRole("dialog", { name: "微调片段与包装" });
    expect(within(dialog).getByLabelText("新版本名称")).toHaveValue("保留原声的一版");
    expect(within(dialog).getAllByLabelText("花字（最多100字）")[0]).toHaveValue("今晚开场");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存为新版本" }));
    expect(actions.onSave).toHaveBeenCalledTimes(1);
  });

  it("opens original footage beside the chosen story beat with precise source boundaries", () => {
    const actions = callbacks(), draft = makeDraft();
    render(<EditorPanel episode={makeEpisode()} source={null} draft={draft} busy={false} demo={false} guided {...actions} />);
    fireEvent.click(screen.getByRole("button", { name: "回看第2段" }));
    expect(actions.onSource).toHaveBeenCalledWith(assetId, 8, 12.3);
    expect(screen.getByRole("dialog", { name: "原片预览" })).toBeVisible();
    expect(screen.getByLabelText("测试原片.mp4")).toHaveAttribute("src", `/api/v1/assets/${assetId}/media`);
    expect(actions.onChange).not.toHaveBeenCalled();
  });

  it("keeps the route to materials obvious for an empty plan", () => {
    const actions = callbacks();
    render(<EditorPanel episode={makeEpisode()} source={null} draft={{ ...makeDraft(), segments: [], narration_text: "" }} busy={false} demo={false} guided {...actions} />);
    expect(screen.getByRole("button", { name: "生成预览" })).toBeDisabled();
    expect(screen.queryByLabelText("新版本名称")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "去素材中加入选段" }));
    expect(actions.onMaterials).toHaveBeenCalledTimes(1);
    expect(actions.onSave).not.toHaveBeenCalled();
  });

  it("disables all primary choices while busy", () => {
    const actions = callbacks();
    render(<EditorPanel episode={makeEpisode()} source={null} draft={makeDraft()} busy demo={false} guided {...actions} />);
    for (const label of ["下一步：确认旁白", "微调片段", "保留现场原声", "加开场旁白", "回看第1段"]) expect(screen.getByRole("button", { name: label })).toBeDisabled();
    expect(screen.getByLabelText("开场旁白文案")).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "节目包装" })).toBeDisabled();
    expect(actions.onContinue).not.toHaveBeenCalled();
    expect(actions.onChange).not.toHaveBeenCalled();
  });

  it("shows validation errors before continuing and keeps source restrictions in the dialog", () => {
    const episode = makeEpisode(); episode.assets[0].fixed_segment = { start: 0, end: 6 };
    const actions = callbacks();
    render(<EditorPanel episode={episode} source={null} draft={makeDraft()} busy={false} demo={false} guided {...actions} />);
    const plan = screen.getByRole("region", { name: "导演的镜头安排" });
    expect(within(plan).getByRole("alert")).toHaveTextContent("不能直接截短或改写花字");
    expect(screen.getByRole("button", { name: "下一步：确认旁白" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "微调片段" }));
    const dialog = screen.getByRole("dialog", { name: "微调片段与包装" });
    expect(within(dialog).getAllByLabelText("开始（秒）")[0]).toBeDisabled();
    expect(within(dialog).getAllByLabelText("花字（最多100字）")[0]).toBeDisabled();
  });

  it("shows invalid seconds inside the open adjustment dialog and blocks saving until corrected", () => {
    const actions = callbacks();
    render(<Editable actions={actions} />);
    fireEvent.click(screen.getByRole("button", { name: "微调片段" }));
    const dialog = screen.getByRole("dialog", { name: "微调片段与包装" });
    const start = within(dialog).getAllByLabelText("开始（秒）")[0];
    fireEvent.change(start, { target: { value: "20" } });
    expect(within(dialog).getByRole("alert")).toBeVisible();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("第1段的起止时间需在原片范围内");
    const save = within(dialog).getByRole("button", { name: "保存为新版本" });
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(actions.onSave).not.toHaveBeenCalled();
    expect(start).toHaveValue(20);
    fireEvent.change(start, { target: { value: "0" } });
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(actions.onSave).toHaveBeenCalledTimes(1);
  });

  it("keeps a server save failure visible in the dialog and permits retrying a valid draft", () => {
    const actions = callbacks(), draft = makeDraft();
    function SaveFailure() {
      const [feedbackError, setFeedbackError] = useState("");
      return <EditorPanel episode={makeEpisode()} source={null} draft={draft} busy={false} demo={false} guided {...actions}
        feedbackError={feedbackError} onSave={() => { actions.onSave(); setFeedbackError("保存失败，请稍后重试。"); }} />;
    }
    render(<SaveFailure />);
    fireEvent.click(screen.getByRole("button", { name: "微调片段" }));
    const dialog = screen.getByRole("dialog", { name: "微调片段与包装" });
    const save = within(dialog).getByRole("button", { name: "保存为新版本" });
    fireEvent.click(save);
    expect(within(dialog).getByRole("alert")).toBeVisible();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("保存失败，请稍后重试。");
    expect(within(dialog).getByLabelText("新版本名称")).toHaveValue(draft.title);
    expect(save).toBeEnabled();
    fireEvent.click(save);
    expect(actions.onSave).toHaveBeenCalledTimes(2);
    expect(actions.onContinue).not.toHaveBeenCalled();
    expect(actions.onChange).not.toHaveBeenCalled();
  });

  it("changes only the chosen packaging and clears narration for formats that do not support it", () => {
    const actions = callbacks(), original = makeDraft();
    render(<Editable initial={original} actions={actions} />);
    fireEvent.change(screen.getByRole("combobox", { name: "节目包装" }), { target: { value: "light" } });
    expect(actions.onChange).toHaveBeenLastCalledWith({ ...original, packaging: "light", narration_text: "" });
    expect(screen.getByRole("button", { name: "加开场旁白" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "生成预览" })).toBeEnabled();
    fireEvent.change(screen.getByRole("combobox", { name: "节目包装" }), { target: { value: "variety_v2" } });
    fireEvent.click(screen.getByRole("button", { name: "加开场旁白" }));
    expect(screen.getByLabelText("开场旁白文案")).toHaveValue(original.narration_text);
  });

  it("keeps fixed-demo speech clearly labelled", () => {
    render(<EditorPanel episode={makeEpisode()} source={null} draft={makeDraft()} busy={false} demo guided {...callbacks()} />);
    expect(screen.getByLabelText("演示片头文字（不会朗读）")).toHaveValue(makeDraft().narration_text);
    expect(screen.getByText("演示只生成提示音，不朗读文字。")).toBeVisible();
    expect(screen.queryByRole("button", { name: "加开场旁白" })).not.toBeInTheDocument();
  });

  it("preserves the original editor when guided mode is not requested", () => {
    render(<EditorPanel episode={makeEpisode()} source={null} draft={makeDraft()} busy={false} demo={false} {...callbacks()} />);
    expect(screen.queryByText("导演的镜头安排")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "微调片段" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("新版本名称")).toBeVisible();
    expect(screen.getByRole("button", { name: "保存为新版本" })).toBeEnabled();
  });
});
