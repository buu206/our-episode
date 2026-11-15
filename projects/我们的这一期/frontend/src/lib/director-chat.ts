import { z } from "zod";

const id = z.string().min(1).max(128);
const step = z.enum(["materials", "direction", "edit", "preview"]);
export const DirectorChatMessageSchema = z.object({
  id, role: z.enum(["user", "assistant"]), text: z.string().min(1).max(4000),
  created_at: z.string(), source: z.enum(["model", "demo", "user"]),
  client_id: z.string().min(1).max(80).nullable().optional(),
});
export const DirectorChatProposalSchema = z.object({
  id, kind: z.enum(["revise_directions", "revise_narration", "navigate"]),
  title: z.string().min(1).max(60), explanation: z.string().max(300),
  status: z.enum(["pending", "applied", "stale"]), message_id: id,
  requirement: z.string().max(1000).nullable().optional(),
  narration_text: z.string().max(120).nullable().optional(), target_step: step.nullable().optional(),
});
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
export type DirectorChatContext = { step: DirectorChatStep | "home" | "collaboration" | "sharing"; version_id?: string };
