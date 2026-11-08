import { proxyRequest } from "@/lib/server-proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
type Context = { params: Promise<{ path: string[] }> };
async function handle(request: Request, context: Context): Promise<Response> {
  return proxyRequest(request, (await context.params).path);
}
export { handle as GET, handle as HEAD, handle as POST, handle as PUT, handle as DELETE };
