import { z } from "zod";

const id = z.string().min(1).max(128);
const step = z.enum(["materials", "direction", "edit", "preview"]);
export const DirectorChatMessageSchema = z.object({
  id, role: z.enum(["user", "assistant"]), text: z.string().min(1).max(4000),
  created_at: z.string(), source: z.enum(["model", "demo", "user"]),
  client_id: z.string().min(1).max(80).nullable().optional(),
});
export const DirectorChatProposalSchema = z.object({
  id, kind: z.enum(["revise_directions", "revise_narration", "revise_subtitles", "revise_cut", "review_sequence", "navigate"]),
  title: z.string().min(1).max(60), explanation: z.string().max(300),
  status: z.enum(["pending", "applied", "stale"]), message_id: id,
  base_version_id: z.string().regex(/^[a-f0-9]{32}$/).nullable().optional(),
  subtitle_changes: z.array(z.object({before:z.string().max(160),after:z.string().min(1).max(160),start:z.number().finite().nonnegative(),end:z.number().finite().positive()}).refine(v=>v.end>v.start)).min(1).max(3).nullable().optional(),
  cut_changes: z.array(z.object({shot:z.number().int().min(1).max(8),start:z.number().finite().nonnegative(),end:z.number().finite().positive(),reason:z.string().min(1).max(100),removed:z.boolean()}).refine(v=>v.end>v.start)).min(1).max(24).nullable().optional(),
  start: z.number().finite().nonnegative().nullable().optional(),
  end: z.number().finite().positive().max(90).nullable().optional(),
  review_state: z.enum(["queued", "running", "succeeded", "failed", "stale"]).nullable().optional(),
  review_summary: z.string().max(1000).nullable().optional(),
  before_seconds: z.number().finite().positive().nullable().optional(),
  after_seconds: z.number().finite().positive().nullable().optional(),
  requirement: z.string().max(1000).nullable().optional(),
  narration_text: z.string().max(120).nullable().optional(), target_step: step.nullable().optional(),
}).refine(v=>v.kind!=="review_sequence" || (v.start!=null && v.end!=null && v.end>v.start && v.end-v.start<=24)).refine(v=>v.kind!=="revise_subtitles" || (!!v.subtitle_changes?.length && !!v.base_version_id)).refine(v=>v.kind!=="revise_cut" || (!!v.cut_changes?.length && !!v.base_version_id && !!v.before_seconds && !!v.after_seconds && v.after_seconds<v.before_seconds));
export const DirectorChatStateSchema = z.object({
  messages: z.array(DirectorChatMessageSchema).max(200),
  proposals: z.array(DirectorChatProposalSchema).max(200),
  pending_job_id: id.nullable(), last_job_id: id.nullable(),
  last_error: z.object({ code: z.string(), message: z.string() }).nullable(),
  can_chat: z.boolean(), reason: z.string(), mode: z.enum(["cloud", "demo"]),
});
export const DirectorChatActionResultSchema = z.object({
  ok: z.literal(true), next_step: step.nullable().optional(),
  version_id: id.nullable().optional(), job_id: id.nullable().optional(),
});
export type DirectorChatState = z.infer<typeof DirectorChatStateSchema>;
export type DirectorChatProposal = z.infer<typeof DirectorChatProposalSchema>;
export type DirectorChatActionResult = z.infer<typeof DirectorChatActionResultSchema>;
export type DirectorChatStep = z.infer<typeof step>;
export type DirectorChatContext = { step: DirectorChatStep | "home" | "collaboration" | "sharing"; version_id?: string; audio_focus_cue_id?: string };
