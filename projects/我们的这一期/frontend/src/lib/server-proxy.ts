/** Server-only transport. Business permissions and durable state stay in FastAPI. */
import { routeContract } from "./routes";

type ProxyTimeouts = { jsonMs: number; uploadMs: number; mediaMs: number; idleMs: number };
export type ProxyOptions = {
  backendUrl?: string; appOrigin?: string; backendMode?: "normal" | "demo";
  fetchImpl?: typeof fetch; timeouts?: Partial<ProxyTimeouts>;
};
const defaults: ProxyTimeouts = { jsonMs: 30_000, uploadMs: 30 * 60_000, mediaMs: 15 * 60_000, idleMs: 90_000 };
const safeToken = /^[A-Za-z0-9_-]{16,256}$/;
const responseHeaders = ["content-type", "content-length", "content-disposition", "content-range", "accept-ranges", "etag", "last-modified", "expires", "pragma"];
const hopHeaders = new Set(["connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"]);

function failure(status: number, code: string, message: string): Response {
  return Response.json({ error: { code, message } }, { status, headers: {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
  } });
}
function origin(value: string | undefined): URL | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.pathname !== "/" || url.search || url.hash) return null;
    return url;
  } catch { return null; }
}
function loopback(host: string): boolean { return ["127.0.0.1", "localhost", "[::1]"].includes(host); }
function trustedFrontend(host: string): boolean {
  if (loopback(host)) return true;
  // Explicit APP_ORIGIN may name a private LAN IPv4 address for supervised phone acceptance.
  // Never a wildcard, DNS hostname or public address. This does not change the loopback backend.
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  return parts[0] === 10 || (parts[0] === 192 && parts[1] === 168) || (parts[0] === 172 && parts[1]! >= 16 && parts[1]! <= 31);
}
function forbiddenHeaders(headers: Headers): Set<string> {
  return new Set([...hopHeaders, ...(headers.get("connection") ?? "").split(",").map((value) => value.trim().toLowerCase()).filter(Boolean)]);
}
function applicationCookie(request: Request, name: string): string | null {
  const values = (request.headers.get("cookie") ?? "").split(";").map((entry) => entry.trim())
    .filter((entry) => entry.slice(0, entry.indexOf("=")) === name).map((entry) => entry.slice(entry.indexOf("=") + 1));
  return values.length === 1 && safeToken.test(values[0]!) ? `${name}=${values[0]}` : null;
}
function cookiesFrom(headers: Headers): string[] {
  if (typeof headers.getSetCookie === "function") return headers.getSetCookie();
  return (headers.get("set-cookie") ?? "").split(/,(?=\s*[^\s;,=]+=)/).filter(Boolean);
}

export async function proxyRequest(request: Request, path: readonly string[], options: ProxyOptions = {}): Promise<Response> {
  const backend = origin(options.backendUrl ?? process.env.BACKEND_URL);
  const frontend = origin(options.appOrigin ?? process.env.APP_ORIGIN);
  const configuredMode = options.backendMode ?? process.env.BACKEND_MODE;
  if (!backend || backend.protocol !== "http:" || !loopback(backend.hostname) || !backend.port
      || (configuredMode !== undefined && configuredMode !== "normal" && configuredMode !== "demo")
      || !frontend || !(frontend.protocol === "https:" || (trustedFrontend(frontend.hostname) && frontend.port))) {
    return failure(503, "PROXY_NOT_CONFIGURED", "网页服务尚未正确配置，请检查启动设置。");
  }
  let incoming: URL;
  try { incoming = new URL(request.url); } catch { return failure(400, "INVALID_REQUEST", "请求地址无效。"); }
  // Next constructs Request.url from its internal fetchHostname/initURL. It can
  // be localhost even when the browser used 127.0.0.1. Validate the actual Host
  // header and browser Origin against configuration, never forwarded headers.
  if (request.headers.get("host") !== frontend.host) {
    return failure(400, "INVALID_HOST", "请求地址与网页入口不匹配。");
  }
  const write = !["GET", "HEAD"].includes(request.method);
  const fetchSite = request.headers.get("sec-fetch-site");
  const receivedOrigin = request.headers.get("origin");
  if ((receivedOrigin && receivedOrigin !== frontend.origin) || (write && receivedOrigin !== frontend.origin)
      || (fetchSite && fetchSite !== "same-origin" && !(fetchSite === "none" && !write))) {
    return failure(403, "INVALID_ORIGIN", "请求来源不匹配，请从配置的网页入口重新操作。");
  }
  if (path.length === 0 || path.some((part) => !/^[a-z0-9_-]+$/.test(part))) {
    return failure(400, "INVALID_API_PATH", "接口地址无效。");
  }
  const relative = "/" + path.join("/");
  if (incoming.pathname !== "/api/v1" + relative) return failure(400, "INVALID_API_PATH", "接口地址无效。");
  const contract = routeContract(relative + incoming.search, request.method);
  if (!contract) return failure(404, "NOT_FOUND", "此接口不属于当前制作网页。");
  const baseCookie = (configuredMode ?? (backend.port === "8790" ? "demo" : "normal")) === "demo" ? "episode_demo_session" : "episode_session";
  const cookieName = baseCookie + (contract.role === "owner" ? "" : `_${contract.role}`);
  const cookie = applicationCookie(request, cookieName);
  const localEntry = process.env.LOCAL_DEMO_ENTRY === "1" && configuredMode === "normal"
    && frontend.protocol === "http:" && loopback(frontend.hostname) && relative === "/auth/me" && request.method === "GET";
  if (!contract.public && !cookie && !localEntry) return failure(401, contract.role === "owner" ? "AUTH_REQUIRED" : `${contract.role.toUpperCase()}_AUTH_REQUIRED`, "请使用当前身份的邀请重新登录。");
  const csrf = request.headers.get("x-csrf-token");
  if (write && !contract.public && (!csrf || !safeToken.test(csrf))) return failure(403, "CSRF_FAILED", "会话校验失败，请刷新后重试。");
  const length = request.headers.get("content-length");
  if (length && (!/^\d+$/.test(length) || !Number.isSafeInteger(Number(length)) || Number(length) > contract.bodyLimit)) {
    return failure(413, "REQUEST_TOO_LARGE", "上传或请求内容超过允许大小。");
  }
  const contentType = request.headers.get("content-type");
  if (write && request.body && (contract.upload
    ? !contentType?.toLowerCase().startsWith("multipart/form-data; boundary=")
    : contentType !== null && contentType.split(";", 1)[0]?.trim().toLowerCase() !== "application/json")) {
    return failure(415, "INVALID_CONTENT_TYPE", "请求内容格式不符合接口要求。");
  }
  const blockedRequestHeaders = forbiddenHeaders(request.headers);
  const headers = new Headers({ "accept-encoding": "identity" });
  for (const name of ["accept", "content-type", "content-length", "range", "if-range", "if-none-match", "if-modified-since"]) {
    const value = request.headers.get(name);
    if (value !== null && !blockedRequestHeaders.has(name)) headers.set(name, value);
  }
  if (cookie) headers.set("cookie", cookie);
  if (localEntry) headers.set("x-local-demo-entry", "1");
  if (write && csrf) headers.set("x-csrf-token", csrf);
  if (write) headers.set("origin", backend.origin);
  const abort = new AbortController();
  const timeout = { ...defaults, ...options.timeouts };
  let reason: "client" | "timeout" | "limit" | null = null;
  let idle: ReturnType<typeof setTimeout> | undefined;
  let requestReader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let responseReader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  const stop = (why: typeof reason) => {
    reason ??= why; abort.abort(); clean();
    void requestReader?.cancel().catch(() => undefined);
    void responseReader?.cancel().catch(() => undefined);
  };
  const onDisconnect = () => stop("client");
  const clean = () => { clearTimeout(deadline); clearTimeout(idle); request.signal.removeEventListener("abort", onDisconnect); };
  const touch = () => { clearTimeout(idle); idle = setTimeout(() => stop("timeout"), timeout.idleMs); };
  if (request.signal.aborted) return failure(408, "REQUEST_ABORTED", "已停止等待，请重新检查任务状态。");
  request.signal.addEventListener("abort", onDisconnect, { once: true });
  const deadline = setTimeout(() => stop("timeout"), contract.upload ? timeout.uploadMs : contract.kind === "media" ? timeout.mediaMs : timeout.jsonMs);
  touch();
  let body: ReadableStream<Uint8Array> | undefined;
  if (write && request.body) {
    const source = request.body.getReader();
    requestReader = source;
    let bytes = 0;
    body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const next = await source.read();
          if (next.done) { controller.close(); return; }
          bytes += next.value.byteLength;
          if (bytes > contract.bodyLimit) { stop("limit"); await source.cancel(); controller.error(new Error("REQUEST_TOO_LARGE")); return; }
          touch(); controller.enqueue(next.value);
        } catch { controller.error(new Error("REQUEST_STREAM_FAILED")); }
      },
      async cancel() { await source.cancel().catch(() => undefined); },
    }, { highWaterMark: 0 });
  }
  const upstreamUrl = backend.origin + "/api/v1" + relative + incoming.search;
  try {
    if (body && !contentType) {
      // Next adapts even an empty DELETE into a non-null ReadableStream.
      // Peek under the same deadline/byte guard before connecting upstream:
      // an empty stream is valid; an untyped nonempty body must never mutate data.
      const peek = body.getReader();
      let next = await peek.read();
      while (!next.done && next.value.byteLength === 0) next = await peek.read();
      if (abort.signal.aborted) throw new Error("REQUEST_ABORTED");
      await peek.cancel();
      if (!next.done) {
        clean();
        return failure(415, "INVALID_CONTENT_TYPE", "请求内容格式不符合接口要求。");
      }
      body = undefined;
    }
    const init: RequestInit & { duplex?: "half" } = { method: request.method, headers, body,
      signal: abort.signal, redirect: "manual", cache: "no-store", ...(body ? { duplex: "half" as const } : {}) };
    const upstream = await (options.fetchImpl ?? fetch)(upstreamUrl, init);
    if ((upstream.status >= 300 && upstream.status < 400 && upstream.status !== 304) || upstream.redirected) {
      await upstream.body?.cancel(); clean();
      return failure(502, "UPSTREAM_REDIRECT", "服务返回了意外跳转，操作未自动重试，请刷新状态核对。");
    }
    if (upstream.headers.get("content-encoding") && upstream.headers.get("content-encoding") !== "identity") {
      await upstream.body?.cancel(); clean();
      return failure(502, "UPSTREAM_ENCODING", "服务返回的传输格式异常，请刷新状态核对。");
    }
    const outputHeaders = new Headers({ "cache-control": "no-store", "x-content-type-options": "nosniff", "referrer-policy": "no-referrer" });
    const blockedResponseHeaders = forbiddenHeaders(upstream.headers);
    for (const name of responseHeaders) {
      const value = upstream.headers.get(name);
      if (value !== null && !blockedResponseHeaders.has(name)) outputHeaders.set(name, value);
    }
    if (!blockedResponseHeaders.has("set-cookie")) for (const setCookie of cookiesFrom(upstream.headers)) {
      if (setCookie.slice(0, setCookie.indexOf("=")) === cookieName) {
        const secureCookie = frontend.protocol === "https:" && !/;\s*secure\s*(;|$)/i.test(setCookie)
          ? `${setCookie}; Secure` : setCookie;
        outputHeaders.append("set-cookie", secureCookie);
      }
    }
    if (request.method === "HEAD" || upstream.status === 204 || upstream.status === 304 || !upstream.body) {
      await upstream.body?.cancel(); clean();
      return new Response(null, { status: upstream.status, headers: outputHeaders });
    }
    const reader = upstream.body.getReader();
    responseReader = reader;
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const next = await reader.read();
          if (abort.signal.aborted) { clean(); controller.error(new Error("UPSTREAM_STREAM_ABORTED")); return; }
          if (next.done) { clean(); controller.close(); return; }
          touch(); controller.enqueue(next.value);
        } catch { clean(); controller.error(new Error("UPSTREAM_STREAM_FAILED")); }
      },
      async cancel() { stop("client"); await reader.cancel().catch(() => undefined); },
    }, { highWaterMark: 0 });
    return new Response(stream, { status: upstream.status, headers: outputHeaders });
  } catch {
    clean();
    if (reason === "limit") return failure(413, "REQUEST_TOO_LARGE", "上传或请求内容超过允许大小。");
    if (reason === "timeout") return failure(504, "UPSTREAM_TIMEOUT", "等待本机服务超时；提交结果可能已保存，请先刷新状态。");
    if (reason === "client") return failure(408, "REQUEST_ABORTED", "已停止等待；请重新检查任务状态。");
    return failure(503, "UPSTREAM_UNAVAILABLE", "暂时无法连接本机服务；提交结果不确定，请先刷新状态。");
  }
}
