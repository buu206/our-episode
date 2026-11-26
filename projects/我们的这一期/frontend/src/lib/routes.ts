/** The core UI may reach only these existing API routes, never an arbitrary upstream URL. */
export type ApiMethod = "GET" | "HEAD" | "POST" | "PUT" | "DELETE";
export type ApiRole = "owner" | "participant" | "viewer";
export type RouteKind = "finishing" | "subtitles" | "directorVoices" | "directorVoice" | "directorVoiceStatus" | "directorChat" | "directorChatAction" | "directorState" | "retention" | "status" | "auth" | "ok" | "episodes" | "episode" | "asset" | "impact" | "version" | "job" | "media"
  | "participantAuth" | "invitation" | "invitationCreated" | "invitations" | "submission" | "submissions"
  | "revision" | "objection" | "objections" | "share" | "shareCreated" | "shares" | "viewerAuth" | "viewerShare";
export type RouteContract = { kind: RouteKind; role: ApiRole; public: boolean; upload: boolean; bodyLimit: number };
const objectId = "[a-f0-9]{32}";
const ordinaryLimit = 65_536;
export const EVIDENCE_BODY_LIMIT = 2 * 1024 * 1024;
export const UPLOAD_BODY_LIMIT = 1024 ** 3 + 1024 ** 2;
export const VOICE_BODY_LIMIT = 12 * 1024 ** 2 + 65_536;
const routes: Array<[RegExp, Partial<Record<ApiMethod, RouteKind>>]> = [
  [new RegExp(`^/versions/${objectId}/finishing$`), { GET: "finishing", PUT: "finishing" }],
  [new RegExp(`^/versions/${objectId}/finishing/commit$`), { POST: "version" }],
  [/^\/status$/, { GET: "status" }],
  [/^\/auth\/login$/, { POST: "auth" }],
  [/^\/auth\/me$/, { GET: "auth" }],
  [/^\/auth\/logout$/, { POST: "ok" }],
  [/^\/episodes$/, { GET: "episodes", POST: "episode" }],
  [new RegExp(`^/episodes/${objectId}$`), { GET: "episode", DELETE: "ok" }],
  [new RegExp(`^/episodes/${objectId}/retention$`), { GET: "retention" }],
  [new RegExp(`^/episodes/${objectId}/director/messages$`), { POST: "directorState" }],
  [new RegExp(`^/episodes/${objectId}/director/chat$`), { GET: "directorChat", POST: "job" }],
  [new RegExp(`^/episodes/${objectId}/director/actions/${objectId}$`), { POST: "directorChatAction" }],
  [new RegExp(`^/episodes/${objectId}/director/voice/status$`), { GET: "directorVoiceStatus" }],
  [new RegExp(`^/episodes/${objectId}/director/voice$`), { GET: "directorVoices", POST: "directorVoice" }],
  [new RegExp(`^/episodes/${objectId}/director/voice/${objectId}$`), { GET: "directorVoice" }],
  [new RegExp(`^/episodes/${objectId}/director/voice/by-client/[A-Za-z0-9_-]{8,80}$`), { GET: "directorVoice" }],
  [new RegExp(`^/episodes/${objectId}/assets$`), { POST: "asset" }],
  [new RegExp(`^/episodes/${objectId}/versions$`), { POST: "version" }],
  [new RegExp(`^/episodes/${objectId}/(?:analysis|directions)$`), { POST: "job" }],
  [new RegExp(`^/episodes/${objectId}/evidence$`), { PUT: "episode" }],
  [new RegExp(`^/episodes/${objectId}/direction$`), { POST: "version" }],
  [new RegExp(`^/assets/${objectId}$`), { DELETE: "ok" }],
  [new RegExp(`^/assets/${objectId}/impact$`), { GET: "impact" }],
  [new RegExp(`^/assets/${objectId}/media$`), { GET: "media", HEAD: "media" }],
  [new RegExp(`^/versions/${objectId}/(?:render|narration|review|visual-review|audio-review|sequence-review)$`), { POST: "job" }],
  [new RegExp(`^/versions/${objectId}/director-run/(?:pause|resume|cancel)$`), { POST: "job" }],
  [new RegExp(`^/versions/${objectId}/subtitles$`), { GET: "subtitles" }],
  [new RegExp(`^/versions/${objectId}/confirm$`), { POST: "version" }],
  [new RegExp(`^/versions/${objectId}/(?:media|export)$`), { GET: "media", HEAD: "media" }],
  [new RegExp(`^/jobs/${objectId}$`), { GET: "job" }],
  [new RegExp(`^/jobs/${objectId}/retry$`), { POST: "job" }],
  [new RegExp(`^/episodes/${objectId}/collaboration/invitations$`), { GET: "invitations", POST: "invitationCreated" }],
  [new RegExp(`^/collaboration/invitations/${objectId}/close$`), { POST: "invitation" }],
  [new RegExp(`^/collaboration/invitations/${objectId}/retry$`), { POST: "job" }],
  [new RegExp(`^/episodes/${objectId}/collaboration/submissions$`), { GET: "submissions" }],
  [new RegExp(`^/collaboration/submissions/${objectId}/media$`), { GET: "media" }],
  [new RegExp(`^/collaboration/submissions/${objectId}/revisions$`), { POST: "revision" }],
  [new RegExp(`^/collaboration/revisions/${objectId}/adopt$`), { POST: "job" }],
  [new RegExp(`^/episodes/${objectId}/collaboration/objections$`), { GET: "objections" }],
  [/^\/participant\/auth\/login$/, { POST: "participantAuth" }],
  [/^\/participant\/auth\/me$/, { GET: "participantAuth" }],
  [/^\/participant\/auth\/logout$/, { POST: "ok" }],
  [/^\/participant\/invitation$/, { GET: "invitation" }],
  [/^\/participant\/invitation\/questions$/, { PUT: "invitation" }],
  [/^\/participant\/context\/media$/, { GET: "media" }],
  [/^\/participant\/submissions$/, { GET: "submissions", POST: "submission" }],
  [/^\/participant\/submissions\/upload$/, { POST: "submission" }],
  [new RegExp(`^/participant/submissions/${objectId}$`), { PUT: "submission", DELETE: "ok" }],
  [new RegExp(`^/participant/submissions/${objectId}/retry$`), { POST: "job" }],
  [new RegExp(`^/participant/submissions/${objectId}/media$`), { GET: "media" }],
  [new RegExp(`^/participant/submissions/${objectId}/(?:submit|withdraw)$`), { POST: "submission" }],
  [new RegExp(`^/participant/revisions/${objectId}/decision$`), { POST: "revision" }],
  [/^\/participant\/objections$/, { GET: "objections", POST: "objection" }],
  [new RegExp(`^/participant/objections/${objectId}/resolve$`), { POST: "objection" }],
  [new RegExp(`^/versions/${objectId}/shares$`), { POST: "shareCreated" }],
  [new RegExp(`^/episodes/${objectId}/shares$`), { GET: "shares" }],
  [new RegExp(`^/shares/${objectId}/(?:update|close)$`), { POST: "share" }],
  [/^\/viewer\/auth\/login$/, { POST: "viewerAuth" }],
  [/^\/viewer\/auth\/me$/, { GET: "viewerAuth" }],
  [/^\/viewer\/auth\/logout$/, { POST: "ok" }],
  [new RegExp(`^/viewer/shares/${objectId}$`), { GET: "viewerShare" }],
  [new RegExp(`^/viewer/shares/${objectId}/media$`), { GET: "media", HEAD: "media" }],
];

export function routeContract(path: string, method: string): RouteContract | null {
  if (!path.startsWith("/") || path.startsWith("//") || /[%\\#\s]/.test(path)) return null;
  const [pathname, search, ...extra] = path.split("?");
  if (extra.length || !pathname || pathname.includes("..")) return null;
  const viewerMedia = new RegExp(`^/viewer/shares/${objectId}/media$`).test(pathname);
  if (viewerMedia) {
    if (!search || !/^revision=[1-9]\d*$/.test(search) || !Number.isSafeInteger(Number(search.slice(9)))) return null;
  } else if (search !== undefined) {
    if (method !== "DELETE" || !new RegExp(`^/(?:episodes|assets|participant/submissions)/${objectId}$`).test(pathname)
      || !/^confirm=(?:true|false)$/.test(search)) return null;
  }
  for (const [pattern, methods] of routes) {
    const kind = methods[method as ApiMethod];
    if (kind && pattern.test(pathname)) {
      const role: ApiRole = pathname.startsWith("/participant/") ? "participant" : pathname.startsWith("/viewer/") ? "viewer" : "owner";
      const upload = ((kind === "asset" || kind === "directorVoice") && method === "POST") || pathname === "/participant/submissions/upload";
      return { kind, role, upload, public: pathname === "/status" || /^\/(?:participant\/|viewer\/)?auth\/login$/.test(pathname),
        bodyLimit: upload ? (kind === "directorVoice" ? VOICE_BODY_LIMIT : UPLOAD_BODY_LIMIT) : pathname.endsWith("/evidence") ? EVIDENCE_BODY_LIMIT : ordinaryLimit };
    }
  }
  return null;
}

export function exportUrl(versionId: string): string {
  if (!/^[a-f0-9]{32}$/.test(versionId)) throw new Error("无效版本编号。");
  return `/api/v1/versions/${versionId}/export`;
}

export function mediaUrl(value: string | null | undefined, role: ApiRole = "owner"): string | null {
  if (!value?.startsWith("/api/v1/")) return null;
  const contract = routeContract(value.slice(7), "GET");
  return contract?.kind === "media" && contract.role === role ? value : null;
}
