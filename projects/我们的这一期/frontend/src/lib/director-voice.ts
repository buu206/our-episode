import { z } from "zod";

export const DIRECTOR_VOICE_MAX_BYTES = 12 * 1024 * 1024;
export const DIRECTOR_VOICE_MAX_SECONDS = 60;
const id = z.string().regex(/^[a-f0-9]{32}$/);
export const DirectorVoiceStatusSchema = z.object({
  can_record: z.boolean(), reason: z.string(), provider: z.literal("MiniMax"), model: z.literal("asr-1.0"),
  mode: z.enum(["cloud", "demo"]), max_seconds: z.literal(60), max_bytes: z.literal(DIRECTOR_VOICE_MAX_BYTES),
});
export const DirectorVoiceSchema = z.object({
  id, job_id: id, state: z.enum(["queued", "running", "succeeded", "failed"]),
  text: z.string().max(2000).nullable(),
  error: z.object({ code: z.string().min(1), message: z.string().min(1) }).nullable(),
  mode: z.enum(["cloud", "demo"]),
});
export type DirectorVoice = z.infer<typeof DirectorVoiceSchema>;
export const DirectorVoicesSchema = z.object({ items: z.array(DirectorVoiceSchema).max(20) });
export type DirectorVoices = z.infer<typeof DirectorVoicesSchema>;
export type DirectorVoiceStatus = z.infer<typeof DirectorVoiceStatusSchema>;
export type DirectorVoiceConsent = { client_id: string; confirm_send: true; accept_unknown_retention: true };
