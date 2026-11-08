import { z } from "zod";
import { ErrorDetailSchema } from "./types";

const id = z.string().regex(/^[a-f0-9]{32}$/);
const nonnegative = z.number().finite().nonnegative();
const positiveInteger = z.number().int().positive().max(Number.MAX_SAFE_INTEGER);
const text = (min: number, max: number) => z.string().refine((value) => {
  const size = Array.from(value).length;
  return size >= min && size <= max;
}, "Text exceeds the API character limit");
// Unknown future states remain visible, but never imply permission to act.
const state = z.string().min(1).max(120);
const timestamp = z.string().min(1);
const credential = z.string().min(1).max(256);
const submissionMedia = z.string().regex(/^\/api\/v1\/(?:participant|collaboration)\/submissions\/[a-f0-9]{32}\/media$/);

export const ParticipantAuthSchema = z.object({ participant_id: id, invite_id: id, display_name: text(1, 40), csrf_token: credential });
export type ParticipantAuth = z.infer<typeof ParticipantAuthSchema>;
export const ContextSpanSchema = z.object({ asset_id: id, start: nonnegative, end: nonnegative })
  .refine((span) => span.end > span.start && span.end - span.start <= 30, "Invalid context interval");
export type ContextSpan = z.infer<typeof ContextSpanSchema>;
export const QuestionSchema = z.object({ id: z.string().min(1).max(80), text: text(1, 200), skipped: z.boolean() });
export type Question = z.infer<typeof QuestionSchema>;
export const InvitationSchema = z.object({
  id, role: z.enum(["contribute", "interview", "review"]), label: text(1, 40), expires_at: nonnegative,
  closed: z.boolean(), accepting_new: z.boolean(), context: ContextSpanSchema.nullable(),
  questions: z.array(QuestionSchema).max(8), context_job_id: id.nullable(), context_state: state,
  context_error: ErrorDetailSchema.nullable(),
  context_preview_url: z.literal("/api/v1/participant/context/media").nullable(), created_at: timestamp,
});
export type Invitation = z.infer<typeof InvitationSchema>;
export const InvitationCreatedSchema = InvitationSchema.extend({ invite_code: credential });
export type InvitationCreated = z.infer<typeof InvitationCreatedSchema>;
export const InvitationsSchema = z.object({ items: z.array(InvitationSchema) });
export type Invitations = z.infer<typeof InvitationsSchema>;
export type InvitationList = Invitations;
export const InvitationListSchema = InvitationsSchema;

export const RevisionSchema = z.object({
  id, submission_id: id, text: text(0, 100), start: nonnegative, end: nonnegative, state, created_at: timestamp,
}).refine((revision) => revision.end >= revision.start, "Invalid expression interval");
export type Revision = z.infer<typeof RevisionSchema>;
export type ExpressionRevision = Revision;
export const AdoptionSchema = z.object({ id, revision_id: id, state, asset_id: id.nullable(), job_id: id.nullable(), error: ErrorDetailSchema.nullable() });
export type Adoption = z.infer<typeof AdoptionSchema>;
export const SubmissionSchema = z.object({
  id, invite_id: id, participant_id: id, display_name: text(1, 40), kind: z.enum(["text", "audio", "video"]), state,
  text: text(0, 100), question_id: z.string().max(80).nullable(), filename: z.string().nullable(),
  duration: nonnegative, size: z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER),
  preview_state: state, preview_job_id: id.nullable(), preview_error: ErrorDetailSchema.nullable(),
  media_url: submissionMedia.nullable(), created_at: timestamp,
  revisions: z.array(RevisionSchema), adoptions: z.array(AdoptionSchema),
  can_delete_draft: z.boolean(), deleted: z.boolean(), deletion_pending: z.boolean(),
}).refine((submission) => submission.media_url === null || submission.media_url.endsWith(`/submissions/${submission.id}/media`), "Mismatched submission media");
export type Submission = z.infer<typeof SubmissionSchema>;
export const SubmissionsSchema = z.object({ items: z.array(SubmissionSchema) });
export type Submissions = z.infer<typeof SubmissionsSchema>;
export type SubmissionList = Submissions;
export const SubmissionListSchema = SubmissionsSchema;
export const ObjectionSchema = z.object({
  id, invite_id: id, participant_id: id, display_name: text(1, 40), submission_id: id.nullable(), asset_id: id.nullable(),
  start: nonnegative.nullable(), end: nonnegative.nullable(), reason: text(1, 1000), state,
  created_at: timestamp, resolved_at: timestamp.nullable(),
}).refine((objection) => objection.start === null ? objection.end === null
  : objection.end !== null && objection.end > objection.start, "Invalid objection interval");
export type Objection = z.infer<typeof ObjectionSchema>;
export const ObjectionsSchema = z.object({ items: z.array(ObjectionSchema) });
export type Objections = z.infer<typeof ObjectionsSchema>;
export type ObjectionList = Objections;
export const ObjectionListSchema = ObjectionsSchema;

const shareFields = {
  id, episode_id: id, version_id: id, label: text(1, 40), title: z.string(), state, revision: positiveInteger,
  created_at: timestamp, updated_at: timestamp, expires_at: nonnegative, closed_at: nonnegative.nullable(),
  restricted_reason: ErrorDetailSchema.nullable(), confirmed_at: timestamp,
  watch_path: z.string().regex(/^\/watch\/[a-f0-9]{32}$/),
};
export const ShareSchema = z.object(shareFields).refine((share) => share.watch_path === `/watch/${share.id}`, "Mismatched share address");
export type Share = z.infer<typeof ShareSchema>;
export const ShareCreatedSchema = z.object({ ...shareFields, share_code: credential.nullable(), watch_url: z.string().nullable() })
  .refine((share) => share.watch_path === `/watch/${share.id}` && (share.share_code === null
    ? share.watch_url === null
    : /^[A-Za-z0-9_-]+$/.test(share.share_code) && share.watch_url === `${share.watch_path}#code=${share.share_code}`), "Mismatched share invitation");
export type ShareCreated = z.infer<typeof ShareCreatedSchema>;
export const SharesSchema = z.object({ items: z.array(ShareSchema) });
export type Shares = z.infer<typeof SharesSchema>;
export type SharesList = Shares;
export const SharesListSchema = SharesSchema;
export const ViewerAuthSchema = z.object({ share_id: id, label: text(1, 40), csrf_token: credential });
export type ViewerAuth = z.infer<typeof ViewerAuthSchema>;
export const ViewerShareSchema = z.object({
  id, label: text(1, 40), title: z.string(), version_id: id, revision: positiveInteger,
  expires_at: nonnegative, confirmed_at: timestamp, media_url: z.string(),
}).refine((share) => share.media_url === `/api/v1/viewer/shares/${share.id}/media?revision=${share.revision}`, "Mismatched published media revision");
export type ViewerShare = z.infer<typeof ViewerShareSchema>;

export type InvitationInput = { role: Invitation["role"]; label: string; context?: ContextSpan | null; questions?: string[] };
export type ParticipantLoginInput = { invite_code: string; display_name: string };
export type QuestionsInput = { questions: Question[] };
export type SubmissionDraftInput = { text: string; question_id?: string | null };
export type TextSubmissionInput = SubmissionDraftInput & { client_request_id: string };
export type RevisionInput = { text: string; start: number; end: number };
export type RevisionDecisionInput = { decision: "confirmed" | "rejected" };
export type ConfirmInput = { confirmed: true };
export type SubmissionConfirmInput = ConfirmInput & { expected_draft: { text: string; question_id: string | null } };
export type ObjectionInput = { submission_id?: string | null; start?: number | null; end?: number | null; reason: string };
export type ShareCreateInput = { label: string; expires_days: 1 | 3 | 7; confirmed: true; client_request_id: string };
export type ShareUpdateInput = { version_id: string; expected_revision: number; confirmed: true; client_request_id: string };
export type ShareCloseInput = { expected_revision: number; confirmed: true };
export type ViewerLoginInput = { share_id: string; share_code: string };
export type UploadSubmissionFields = { file: File; kind: "audio" | "video"; text: string; question_id?: string | null; client_request_id: string };
