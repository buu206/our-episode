import type { Asset, Episode, Evidence, Job, Packaging, Segment, Version } from "@/lib/types";

export const steps = ["materials", "direction", "edit", "preview"] as const;
export type Step = typeof steps[number];
export const stepTitles: Record<Step, string> = { materials: "带来素材", direction: "核对与方向", edit: "安排这一期", preview: "预览与确认" };
export type Draft = { title: string; segments: Segment[]; packaging: Packaging; narration_text: string; base_version_id?: string };
export type Source = { id: string; start: number; end?: number; sequence: number };
export type UploadRow = { name: string; sent: number; total: number; state: "waiting" | "uploading" | "processing" | "done" | "failed" | "unknown" | "skipped"; error?: string };
export const hasNarratedIntro = (packaging: Packaging) => ["standard", "variety", "variety_v2"].includes(packaging);
export const blankDraft = (): Draft => ({ title: "", segments: [], packaging: "original", narration_text: "" });
export const isActiveJob = (job: Job) => ["queued", "running"].includes(job.state)
  || job.kind === "director_run" && ["active", "pausing", "cancelling"].includes(job.state);
export const modelJob = (job: Job) => ["analysis", "directions", "narration"].includes(job.kind);
export const costUncertain = (code?: string) => ["COST_UNKNOWN", "BILLING_MISMATCH"].includes(code || "");
export const versionState = (state: string) => ({ draft: "待合成", voicing: "音频处理中", rendering: "正在合成", ready: "待你预览确认", confirmed: "已确认", failed: "处理失败", restricted: "内容受限" }[state] || "状态待核对");
export const originName = (origin: string) => ({ manual: "人工剪接 · 非AI结果", ai: "AI辅助版本 · 仍需核对", demo: "固定演示结果 · 非真实AI" }[origin] || "来源待核对");
export const clockTime = (seconds: number) => Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}` : "—";
export const bytes = (value: number) => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB`;
// Display whole-second bounds without changing the source timing on an unrelated edit.
export const wholeSecond = (seconds: number, edge: "start" | "end") => Number.isFinite(seconds) ? (edge === "start" ? Math.floor(seconds) : Math.ceil(seconds)) : "";
export function editedSecond(value: string, edge: "start" | "end", duration?: number): number {
  if (value === "") return NaN;
  const seconds = Math.round(Number(value));
  // The displayed final second may extend beyond a fractional media duration.
  return edge === "end" && duration !== undefined && seconds === Math.ceil(duration) ? duration : seconds;
}
export function draftFromVersion(version: Version, withoutNarration = false): Draft {
  return { title: `${version.title} · 修改`.slice(0, 120), segments: version.segments.map(item => ({ ...item })),
    packaging: withoutNarration ? ["variety", "variety_v2"].includes(version.packaging) ? version.packaging as Packaging : "light" : ["original", "light", "standard", "variety", "variety_v2"].includes(version.packaging) ? version.packaging as Packaging : "original",
    narration_text: withoutNarration ? "" : version.narration_text, base_version_id: version.id };
}
export function segmentFromAsset(asset: Asset): Segment {
  return { asset_id: asset.id, start: asset.fixed_segment?.start ?? 0,
    end: asset.fixed_segment?.end ?? Math.min(asset.duration, 10), caption: "" };
}
export function editedDuration(segments: Segment[]): number {
  const transitions = segments.some(item => item.transition && item.transition !== "cut");
  return segments.reduce((sum, item) => {
    const length = item.end - item.start;
    return sum + (transitions ? Math.ceil(length * 30 - 1e-8) / 30 : length)
      - Math.round((item.transition_duration || 0) * 30) / 30;
  }, 0);
}
export function draftProblem(draft: Draft, episode: Episode): string | null {
  if (!draft.title.trim()) return "先给这一版起个名字。";
  if (!draft.segments.length || draft.segments.length > 30) return "请选择1–30个片段。";
  let duration = 0;
  for (const [index, segment] of draft.segments.entries()) {
    const asset = episode.assets.find(item => item.id === segment.asset_id);
    if (!asset) return `第${index + 1}段来源已不可用，请移除或重新选择。`;
    if (!Number.isFinite(segment.start) || !Number.isFinite(segment.end) || segment.start < 0 || segment.end <= segment.start || segment.end > asset.duration + .001)
      return `第${index + 1}段的起止时间需在原片范围内。`;
    if (asset.fixed_segment && (Math.abs(segment.start - asset.fixed_segment.start) > .001 || Math.abs(segment.end - asset.fixed_segment.end) > .001 || segment.caption !== ""))
      return `第${index + 1}段是本人确认的固定表达，不能直接截短或改写花字。`;
    const transition = segment.transition || "cut", overlap = segment.transition_duration || 0;
    if (segment.editorial) {
      if (draft.packaging !== "variety_v2") return "这一版有导演安排的版式，请保留综艺节奏包装。";
      const beat = segment.editorial;
      if (segment.caption.trim() || segment.visual_beats?.length) return "已有导演版式，请通过导演调整文案，避免叠加两套花字。";
      if (beat.treatment !== "none" && (beat.source_time == null || beat.source_time < segment.start || beat.source_time + (beat.duration || 0) > segment.end + .000001))
        return `第${index + 1}段调整后截断了导演安排，请恢复时间或让导演重新安排。`;
    }
    if (segment.visual_beats?.length) {
      if (draft.packaging !== "variety_v2") return "这一版有镜头特效，请保留综艺节奏包装。";
      if (segment.visual_beats.some(b => b.source_time < segment.start || b.source_time + b.duration > segment.end + .000001))
        return `第${index + 1}段调整后截断了镜头特效，请恢复起止时间，或让导演重新安排。`;
    }
    if (transition === "cut" && overlap !== 0) return `第${index + 1}段直接切换不需要转场时长。`;
    if (transition !== "cut") {
      const previous = draft.segments[index - 1];
      if (!previous || overlap < .1 || overlap > .5 || Math.max(overlap, Math.round(overlap * 30) / 30) > Math.min(segment.end - segment.start, previous.end - previous.start) / 2)
        return `第${index + 1}段转场与当前顺序或长度不匹配，请改为直接切换。`;
      if (asset.fixed_segment || episode.assets.find(item => item.id === previous.asset_id)?.fixed_segment)
        return `第${index + 1}段转场会覆盖已确认表达，请改为直接切换。`;
    }
    duration += segment.end - segment.start;
  }
  const limit = hasNarratedIntro(draft.packaging) && draft.narration_text.trim() ? 60 : 90;
  if (duration > limit + .001) return `选段合计超过${limit}秒，请缩短后保存${limit === 60 ? "，为独立片头预留空间" : ""}。`;
  return null;
}
export function evidenceProblem(items: Evidence[], episode: Episode): string | null {
  for (const [index, item] of items.entries()) {
    const asset = episode.assets.find(asset => asset.id === item.asset_id);
    if (!asset || !item.note.trim() || !Number.isFinite(item.start) || !Number.isFinite(item.end) || item.start < 0 || item.end <= item.start || item.end > asset.duration + .001)
      return `第${index + 1}条需有效来源、原片内的起止时间和核对后的文字。`;
  }
  return null;
}
