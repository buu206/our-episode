import { z } from "zod";

const id = z.string().min(1).max(128);
const finite = z.number().finite();
const nonnegative = finite.min(0);
const nullableId = id.nullable();
// State strings intentionally remain open. Unknown states must not enable UI actions.
const state = z.string().min(1).max(120);
const boundedText = (min: number, max: number) => z.string().refine((value) => {
  const length = Array.from(value).length;
  return length >= min && length <= max;
}, "Text exceeds the API character limit");
const mediaPath = z.string().regex(/^\/api\/v1\/(?:assets|versions)\/[a-f0-9]{32}\/(?:media|export)$/);
export const ErrorDetailSchema = z.object({ code: z.string(), message: z.string() });
export type ErrorDetail = z.infer<typeof ErrorDetailSchema>;
export const AuthSchema = z.object({ user_id: id, csrf_token: z.string().min(1) });
export type Auth = z.infer<typeof AuthSchema>;
export const StatusSchema = z.object({
  cloud_enabled: z.boolean(), cloud_reason: z.string(), media_ready: z.boolean(),
  stage: z.string(), workflow_enabled: z.boolean(), model_mode: state,
});
export type Status = z.infer<typeof StatusSchema>;
export const VisualBeatSchema = z.object({
  kind: z.enum(["focus", "burst", "comic_space", "cutout", "headline"]), source_time: nonnegative,
  duration: finite.min(.4).max(2.5), text: boundedText(0, 8).optional(),
  position: z.enum(["top_left", "top_right", "bottom_left", "bottom_right"]).optional(),
  contour: z.array(z.object({ x: finite.min(0).max(1), y: finite.min(0).max(1) })).min(3).max(64).optional(),
  target: z.object({ x: finite.min(0).max(1), y: finite.min(0).max(1),
    width: finite.min(.08).max(1), height: finite.min(.08).max(1) })
    .refine(r => r.x + r.width <= 1.000001 && r.y + r.height <= 1.000001, "Invalid visual target"),
}).refine(value => (value.kind === "cutout") === (value.contour !== undefined), "Cutout requires a reviewed contour");
export const EditorialBeatSchema = z.object({
  role: z.enum(["setup", "action", "reaction", "closing"]),
  treatment: z.enum(["none", "chapter", "accent"]), reason: boundedText(1, 160),
  evidence_ids: z.array(id).min(1).max(10), source_time: nonnegative.nullable().optional(),
  duration: nonnegative.max(6).optional(), text: boundedText(0, 10).optional(), detail: boundedText(0, 40).optional(),
}).strict();
export const SubtitleEditSchema = z.object({ cue_id: z.string().regex(/^[a-f0-9]{32}$/), text: z.string().max(500), hidden: z.boolean(), reviewed: z.boolean() });
export const SubtitleTrackSchema = z.object({ version_id: id, rows: z.array(z.object({
  cue_id: z.string().regex(/^[a-f0-9]{32}$/), segment_index: z.number().int().nonnegative(), asset_id: id,
  start: nonnegative, end: finite.positive(), text: z.string(), original_text: z.string(), source_text: z.string(),
  hidden: z.boolean(), reviewed: z.boolean(), locked: z.boolean(), flags: z.array(z.string()),
  media_url: z.string().regex(/^\/api\/v1\/assets\/[a-f0-9]{32}\/media$/),
})) });
export type SubtitleTrack = z.infer<typeof SubtitleTrackSchema>;
export const SegmentSchema = z.object({
  asset_id: id, start: nonnegative, end: finite.positive(), caption: boundedText(0, 100),
  purpose: boundedText(0, 200).optional(), evidence_ids: z.array(id).max(40).optional(),
  transition: z.enum(["cut", "dissolve", "fade_black"]).optional(),
  transition_duration: nonnegative.max(.5).optional(),
  visual_beats: z.array(VisualBeatSchema).max(4).optional(),
  editorial: EditorialBeatSchema.optional(),
  subtitle_edits: z.array(SubtitleEditSchema).max(150).optional(),
}).refine((value) => value.end > value.start, "Invalid segment interval");
export type Segment = z.infer<typeof SegmentSchema>;
export const AssetSchema = z.object({
  id, filename: z.string(), duration: finite.positive(), width: z.number().int().positive(),
  height: z.number().int().positive(), has_audio: z.boolean(), size: z.number().int().nonnegative(),
  media_url: mediaPath, source_kind: state, contributor: z.string().nullable(),
  interview_text: z.string().nullable(),
  fixed_segment: z.object({ start: nonnegative, end: finite.positive() }).nullable(),
});
export type Asset = z.infer<typeof AssetSchema>;
export const VersionSchema = z.object({
  id, title: z.string(), state, origin: state, segments: z.array(SegmentSchema).max(30),
  media_url: mediaPath.nullable(), job_id: nullableId, error: ErrorDetailSchema.nullable(),
  base_version_id: nullableId, created_at: z.string(), narration_text: boundedText(0, 120),
  narration_approved: z.boolean(), narration_ready: z.boolean(), packaging: state,
  evidence_revision: z.number().int().nonnegative().nullable(),
});
export type Version = z.infer<typeof VersionSchema>;
export const JobSchema = z.object({
  id, kind: state, state, stage: z.string(), error: ErrorDetailSchema.nullable(),
  version_id: nullableId, result: z.record(z.string(), z.unknown()).nullable(),
});
export type Job = z.infer<typeof JobSchema>;
export const EvidenceSchema = z.object({
  id, asset_id: id, start: nonnegative, end: finite.positive(),
  note: boundedText(1, 2000), kind: z.enum(["visual", "speech"]),
}).refine((value) => value.end > value.start, "Invalid evidence interval");
export type Evidence = z.infer<typeof EvidenceSchema>;
export const EvidenceSpanSchema = z.object({
  asset_id: id, start: nonnegative, end: finite.positive(), note: boundedText(1, 2000),
}).refine((value) => value.end > value.start, "Invalid direction interval");
export type EvidenceSpan = z.infer<typeof EvidenceSpanSchema>;
export const DirectionSchema = z.object({
  id, title: boundedText(1, 120), summary: boundedText(1, 2000),
  evidence: z.array(EvidenceSpanSchema).min(1).max(40),
});
export type Direction = z.infer<typeof DirectionSchema>;
export const CoverageSchema = z.object({
  asset_id: id, duration: finite.positive(), sample_times: z.array(nonnegative),
  unsampled_intervals: z.array(z.object({ start: nonnegative, end: nonnegative })),
  max_gap_seconds: nonnegative, method: z.string(), audio_analyzed: z.boolean(),
  time_basis: z.string(),
});
export type Coverage = z.infer<typeof CoverageSchema>;
export const BudgetSchema = z.object({
  spent: nonnegative, reserved: nonnegative, limit: nonnegative,
  total_spent: nonnegative, total_reserved: nonnegative, total_limit: nonnegative,
});
export type Budget = z.infer<typeof BudgetSchema>;
export const QuotaSchema = z.object({
  original_count: z.number().int().nonnegative(), total_seconds: nonnegative,
  total_bytes: z.number().int().nonnegative(), max_count: z.number().int().positive(),
  max_seconds: finite.positive(), max_bytes: z.number().int().positive(),
});
export type Quota = z.infer<typeof QuotaSchema>;
export const PendingCleanupSchema = z.object({ asset_id: id, filename: z.string() });
export type PendingCleanup = z.infer<typeof PendingCleanupSchema>;
export const RetentionSchema = z.object({
  expires_at: finite.nullable(), deletion_requested: z.boolean(), cleanup_pending: z.boolean(),
  counts: z.object({ assets: nonnegative, versions: nonnegative, submissions: nonnegative, shares: nonnegative }),
});
export type Retention = z.infer<typeof RetentionSchema>;
export const DirectorStateSchema = z.object({
    revision: z.number().int().nonnegative(), focus: z.enum(["auto", "conversation", "highlights", "warm"]).nullable(),
    notes: boundedText(0, 1500), directions_current: z.boolean(),
    messages: z.array(z.object({ id: z.string().min(1), role: z.enum(["user", "director"]), text: boundedText(1, 5000), created_at: z.string() })).max(100),
});
export type DirectorState = z.infer<typeof DirectorStateSchema>;
export const EpisodeSchema = z.object({
  workflow: z.object({ enabled: z.boolean(), reason: z.string(), analysis_cached: z.boolean() }).nullable().optional(),
  director: DirectorStateSchema.nullable().optional(),
  retention: RetentionSchema.optional(),
  id, title: z.string(), background: z.string(), created_at: z.string(),
  assets: z.array(AssetSchema), versions: z.array(VersionSchema), jobs: z.array(JobSchema),
  directions: z.array(DirectionSchema).max(3), budget: BudgetSchema,
  evidence: z.array(EvidenceSchema).max(100), evidence_revision: z.number().int().nonnegative(),
  evidence_confirmed_revision: z.number().int().nonnegative().nullable(), analysis_state: state,
  analysis_notes: z.array(z.string()), analysis_coverage: z.array(CoverageSchema),
  directions_revision: z.number().int().nonnegative().nullable(), selected_direction: nullableId,
  // A pre-upgrade backend may omit these. Absence is not an empty cleanup queue or zero usage.
  quota: QuotaSchema.nullable().optional(),
  pending_cleanup: z.array(PendingCleanupSchema).nullable().optional(),
});
export type Episode = z.infer<typeof EpisodeSchema>;
export const EpisodesSchema = z.object({ items: z.array(EpisodeSchema) });
export type Episodes = z.infer<typeof EpisodesSchema>;
export const ImpactSchema = z.object({
  asset_id: id, versions: z.array(id), message: z.string(),
  shares: z.array(z.object({ id, label: z.string(), version_id: id, state })),
});
export type Impact = z.infer<typeof ImpactSchema>;
export const OkSchema = z.object({ ok: z.literal(true) });
export type Ok = z.infer<typeof OkSchema>;
export type Packaging = "variety_v2" | "variety" | "standard" | "light" | "original";
export type EpisodeInput = { title: string; background?: string };
export type VersionInput = {
  title: string; segments: Segment[]; base_version_id?: string | null;
  narration_text?: string; packaging?: Packaging;
};
export type EvidenceReviewInput = { revision: number; items: Evidence[]; confirmed: true; confirmation_mode?: "detailed" | "overview" };
export type DirectionSelectInput = { direction_id: string; revision: number; brief_revision?: number; packaging: Packaging };
