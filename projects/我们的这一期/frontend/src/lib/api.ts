import { z } from "zod";
import { AssetSchema, AuthSchema, EpisodeSchema, EpisodesSchema, ImpactSchema, JobSchema, OkSchema, StatusSchema, VersionSchema, type Asset } from "./types";
import {
  ParticipantAuthSchema, InvitationSchema, InvitationCreatedSchema, InvitationsSchema, SubmissionSchema,
  SubmissionsSchema, RevisionSchema, ObjectionSchema, ObjectionsSchema, ShareSchema, ShareCreatedSchema,
  SharesSchema, ViewerAuthSchema, ViewerShareSchema, type Submission, type UploadSubmissionFields,
} from "./collaboration-types";
import { exportUrl, mediaUrl, routeContract, type ApiMethod, type ApiRole, type RouteKind } from "./routes";
export { exportUrl, mediaUrl } from "./routes";
export type { ApiRole } from "./routes";
export type { UploadSubmissionFields } from "./collaboration-types";

const sessions: Record<ApiRole, { csrf: string | null; epoch: number }> = {
  owner: { csrf: null, epoch: 0 }, participant: { csrf: null, epoch: 0 }, viewer: { csrf: null, epoch: 0 },
};
const expiryEvents: Record<ApiRole, string> = {
  owner: "episode:auth-expired", participant: "episode:participant-auth-expired", viewer: "episode:viewer-auth-expired",
};
export function setCsrf(value: string | null, role: ApiRole = "owner"): void {
  sessions[role].csrf = value; sessions[role].epoch += 1;
}
export class AppError extends Error {
  constructor(public readonly code: string, message: string, public readonly status = 0, public readonly uncertain = false) {
    super(message); this.name = code === "ABORTED" ? "AbortError" : "AppError";
  }
}
export type ApiOptions = { method?: ApiMethod; body?: unknown; signal?: AbortSignal };
const schemas: Partial<Record<RouteKind, z.ZodType>> = {
  status: StatusSchema, auth: AuthSchema, ok: OkSchema, episodes: EpisodesSchema, episode: EpisodeSchema,
  asset: AssetSchema, impact: ImpactSchema, version: VersionSchema, job: JobSchema,
  participantAuth: ParticipantAuthSchema, invitation: InvitationSchema, invitationCreated: InvitationCreatedSchema,
  invitations: InvitationsSchema, submission: SubmissionSchema, submissions: SubmissionsSchema,
  revision: RevisionSchema, objection: ObjectionSchema, objections: ObjectionsSchema,
  share: ShareSchema, shareCreated: ShareCreatedSchema, shares: SharesSchema, viewerAuth: ViewerAuthSchema, viewerShare: ViewerShareSchema,
};
const errorSchema = z.object({ error: z.object({ code: z.string().min(1), message: z.string().min(1) }) });
function businessError(data: unknown, status: number, requestEpoch?: number, role: ApiRole = "owner"): AppError {
  if (status === 401 && requestEpoch !== undefined && requestEpoch === sessions[role].epoch) {
    setCsrf(null, role);
    if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(expiryEvents[role]));
  }
  const parsed = errorSchema.safeParse(data);
  return parsed.success
    ? new AppError(parsed.data.error.code, parsed.data.error.message, status, status >= 500)
    : new AppError("HTTP_ERROR", `请求未能完成（${status}），请刷新状态后检查。`, status, status >= 500);
}
function checkedResult<T>(kind: RouteKind, data: unknown, write: boolean, role: ApiRole = "owner"): T {
  const parsed = schemas[kind]?.safeParse(data);
  if (!parsed?.success) throw new AppError("INVALID_RESPONSE", "服务返回的资料不完整，请刷新状态；刚才的操作可能已经保存。", 502, write);
  // Submission response shapes are shared; media capabilities are not.
  const submissions = kind === "submission" ? [parsed.data as Submission]
    : kind === "submissions" ? (parsed.data as { items: Submission[] }).items : [];
  if (submissions.some((submission) => submission.media_url !== null && !mediaUrl(submission.media_url, role))) {
    throw new AppError("INVALID_RESPONSE", "服务返回的媒体不属于当前身份，请刷新后核对。", 502, write);
  }
  return parsed.data as T;
}
function abortError(write: boolean): AppError {
  return new AppError("ABORTED", "已停止等待；服务端操作可能仍在处理，请稍后查看状态。", 0, write);
}

export async function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const contract = routeContract(path, method);
  if (!contract || contract.kind === "media" || contract.upload) throw new AppError("INVALID_API_PATH", "此操作不属于当前网页接口。", 400);
  const { role } = contract;
  const { csrf, epoch: requestEpoch } = sessions[role];
  const write = method !== "GET" && method !== "HEAD";
  if (options.signal?.aborted) throw abortError(write);
  if (write && !contract.public && !csrf) throw new AppError(role === "owner" ? "AUTH_REQUIRED" : `${role.toUpperCase()}_AUTH_REQUIRED`, "登录状态已失效，请重新登录。", 401);
  const headers = new Headers({ Accept: "application/json" });
  let body: string | undefined;
  if (options.body !== undefined) {
    try { body = JSON.stringify(options.body); } catch { throw new AppError("INVALID_REQUEST", "输入无法保存，请检查后重试。", 400); }
    if (body === undefined) throw new AppError("INVALID_REQUEST", "输入无法保存，请检查后重试。", 400);
    if (new TextEncoder().encode(body).byteLength > contract.bodyLimit) {
      throw new AppError("REQUEST_TOO_LARGE", "本次内容超过可提交大小，输入仍保留，请缩短后再保存。", 413);
    }
    headers.set("Content-Type", "application/json");
  }
  if (write && csrf) headers.set("X-CSRF-Token", csrf);
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { method, body, headers, signal: options.signal,
      credentials: "same-origin", cache: "no-store", redirect: "error" });
  } catch (error) {
    if (options.signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw abortError(write);
    throw new AppError("NETWORK_ERROR", write
      ? "连接中断，提交结果尚不确定。请先刷新状态，不要直接重复提交。"
      : "暂时无法连接本机服务，请检查服务后刷新。", 0, write);
  }
  let data: unknown;
  try { data = await response.json(); } catch {
    if (options.signal?.aborted) throw abortError(write);
    if (!response.ok) throw businessError(undefined, response.status, contract.public ? undefined : requestEpoch, role);
    throw new AppError("INVALID_RESPONSE", "无法读取服务结果，请先刷新状态后检查。", response.status, write);
  }
  if (!response.ok) throw businessError(data, response.status, contract.public ? undefined : requestEpoch, role);
  return checkedResult<T>(contract.kind, data, write, role);
}

/** Authorize HEAD before handing the same URL to the browser download UI.
 * The final download is independently authorized by the backend. No whole-video Blob is created.
 */
export async function prepareExport(versionId: string, signal?: AbortSignal): Promise<string> {
  const requestEpoch = sessions.owner.epoch;
  const url = exportUrl(versionId);
  if (signal?.aborted) throw abortError(false);
  let response: Response;
  try {
    response = await fetch(url, { method: "HEAD", credentials: "same-origin",
      cache: "no-store", redirect: "error", signal });
  } catch (error) {
    if (signal?.aborted || (error instanceof Error && error.name === "AbortError")) throw abortError(false);
    throw new AppError("NETWORK_ERROR", "无法核对导出权限，请检查服务后重试。", 0);
  }
  if (!response.ok) {
    let data: unknown;
    // HTTP HEAD deliberately has no response body, including for controlled errors.
    try { data = await response.json(); } catch { data = undefined; }
    throw businessError(data, response.status, requestEpoch);
  }
  const valid = [200, 206].includes(response.status) && response.headers.get("content-type")?.split(";", 1)[0]?.trim() === "video/mp4";
  await response.body?.cancel();
  if (!valid) throw new AppError("INVALID_RESPONSE", "服务未返回可导出的成片，请刷新当前版本。", 502);
  return url;
}

export async function checkExport(versionId: string, signal?: AbortSignal): Promise<void> {
  await prepareExport(versionId, signal);
}

/** XHR provides actual bytes transmitted. Upload completion still precedes server validation. */
export function uploadAsset(episodeId: string, file: File, onProgress?: (sent: number, total: number) => void,
  signal?: AbortSignal): Promise<Asset> {
  return uploadMultipart<Asset>(`/episodes/${episodeId}/assets`, file, {}, onProgress, signal);
}

/** The invitation determines the finer video duration/size limit on the server.
 * This boundary never retries, including when a request id can later be reconciled.
 */
export function uploadSubmission(fields: UploadSubmissionFields, onProgress?: (sent: number, total: number) => void,
  signal?: AbortSignal): Promise<Submission> {
  const valid = z.object({ kind: z.enum(["audio", "video"]), text: z.string()
    .refine((value) => Array.from(value).length <= 100 && !/[\p{Cc}\p{Cf}]/u.test(value)),
    question_id: z.string().max(80).nullable().optional(), client_request_id: z.uuid(),
  }).safeParse(fields);
  if (!valid.success) return Promise.reject(new AppError("INVALID_REQUEST", "投稿信息无效，请检查表达和本次请求编号。", 422));
  const allowed = fields.kind === "audio" ? /\.(?:wav|mp3|m4a)$/i : /\.(?:mp4|mov)$/i;
  if (!allowed.test(fields.file.name)) return Promise.reject(new AppError("INVALID_FORMAT", "请选择支持的音频或视频格式。", 422));
  if (fields.kind === "audio" && fields.file.size > 100_000_000) return Promise.reject(new AppError("UPLOAD_TOO_LARGE", "采访音频不能超过100MB。", 413));
  return uploadMultipart<Submission>("/participant/submissions/upload", fields.file,
    { kind: fields.kind, text: fields.text, client_request_id: fields.client_request_id,
      ...(fields.question_id == null ? {} : { question_id: fields.question_id }) }, onProgress, signal);
}

function uploadMultipart<T>(path: string, file: File, fields: Record<string, string>,
  onProgress?: (sent: number, total: number) => void, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const contract = routeContract(path, "POST");
    if (!contract?.upload) { reject(new AppError("INVALID_API_PATH", "上传地址无效。", 400)); return; }
    const { role } = contract;
    const { csrf, epoch: requestEpoch } = sessions[role];
    if (!csrf) { reject(new AppError(role === "owner" ? "AUTH_REQUIRED" : `${role.toUpperCase()}_AUTH_REQUIRED`, "请重新登录后上传。", 401)); return; }
    if (signal?.aborted) { reject(abortError(true)); return; }
    if (file.size > 1024 ** 3) { reject(new AppError("UPLOAD_TOO_LARGE", "单次素材不能超过1GB。", 413)); return; }
    const xhr = new XMLHttpRequest();
    let finished = false;
    const abort = () => xhr.abort();
    const finish = (callback: () => void) => {
      if (finished) return;
      finished = true;
      signal?.removeEventListener("abort", abort);
      callback();
    };
    xhr.open("POST", `/api/v1${path}`);
    xhr.withCredentials = true;
    xhr.timeout = 30 * 60 * 1000;
    xhr.setRequestHeader("X-CSRF-Token", csrf);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.upload.onprogress = (event) => { if (event.lengthComputable && !finished) onProgress?.(event.loaded, event.total); };
    xhr.onload = () => finish(() => {
      let data: unknown;
      try { data = JSON.parse(xhr.responseText); } catch {
        reject(xhr.status >= 400 ? businessError(undefined, xhr.status, requestEpoch, role)
          : new AppError("INVALID_RESPONSE", "上传响应无法读取，请先刷新素材列表核对。", xhr.status, true)); return;
      }
      if (xhr.status < 200 || xhr.status >= 300) { reject(businessError(data, xhr.status, requestEpoch, role)); return; }
      try { resolve(checkedResult<T>(contract.kind, data, true, role)); } catch (error) { reject(error); }
    });
    xhr.onerror = () => finish(() => reject(new AppError("NETWORK_ERROR", "上传连接中断，结果未知。请先刷新素材列表再决定是否重传。", 0, true)));
    xhr.ontimeout = () => finish(() => reject(new AppError("UPLOAD_TIMEOUT", "等待上传结果超时。请先检查素材列表；不会自动重传。", 0, true)));
    xhr.onabort = () => finish(() => reject(abortError(true)));
    signal?.addEventListener("abort", abort, { once: true });
    const form = new FormData(); form.append("file", file);
    for (const [name, value] of Object.entries(fields)) form.append(name, value);
    try { xhr.send(form); } catch { finish(() => reject(new AppError("NETWORK_ERROR", "上传未能完成，请先检查素材列表。", 0, true))); }
  });
}
