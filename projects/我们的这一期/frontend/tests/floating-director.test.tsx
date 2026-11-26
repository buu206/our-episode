import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FloatingDirector, clearDirectorChatMemory } from "@/components/floating-director";
import { AppError } from "@/lib/api";
import type { Episode, Status } from "@/lib/types";
import type { DirectorChatState } from "@/lib/director-chat";

const mocks = vi.hoisted(() => ({ api: vi.fn(), push: vi.fn(), pathname: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => mocks.pathname, useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/lib/api", () => ({ api: mocks.api, uploadDirectorVoice: vi.fn(), AppError: class extends Error { constructor(public code: string, message: string, public status = 0, public uncertain = false) { super(message); } } }));
const eid = "a".repeat(32), otherId = "b".repeat(32), vid = "c".repeat(32), jid = "d".repeat(32), pid = "f".repeat(32);
const episode = (id = eid): Episode => ({ id, title: id === eid ? "朋友这一天" : "下一期", background: "", created_at: "2026-11-13", assets: [], versions: [], jobs: [], directions: [],
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 }, evidence: [], evidence_revision: 0, evidence_confirmed_revision: null, analysis_state: "empty",
  analysis_notes: [], analysis_coverage: [], directions_revision: null, selected_direction: null });
const status: Status = { cloud_enabled: true, workflow_enabled: true, cloud_reason: "", media_ready: true, stage: "阶段3-2", model_mode: "cloud" };
const blank = (): DirectorChatState => ({ messages: [], proposals: [], pending_job_id: null, last_job_id: null, last_error: null, can_chat: true, reason: "", mode: "cloud" });
const job = { id: jid, kind: "director_chat", state: "queued", stage: "导演思考中", version_id: null, result: null, error: null };
let chat: DirectorChatState;
let list: Episode[];
type Options = { method?: string; body?: Record<string, unknown> };
async function service(path: string, options: Options = {}) {
  if (path === "/episodes" && options.method === "POST") { const created = { ...episode(), title: String(options.body?.title) }; list = [...list, created]; return created; }
  if (path === "/episodes") return { items: list };
  if (path.endsWith("/director/voice/status")) return { can_record: false, reason: "当前测试不启用麦克风", provider: "MiniMax", model: "asr-1.0", mode: "cloud", max_seconds: 60, max_bytes: 12582912 };
  if (path.endsWith("/director/voice")) return { items: [] };
  if (path.endsWith("/director/chat") && options.method === "POST") {
    chat = { ...chat, last_job_id: jid, messages: [...chat.messages,
      { id: "u1", role: "user", text: String(options.body?.text), source: "user", client_id: String(options.body?.client_id), created_at: "2026-11-13" },
      { id: "m1", role: "assistant", text: "这版缺少铺垫。先交代这一桌，再保留一句完整原声，会更连贯。", source: "model", created_at: "2026-11-13" }] };
    return job;
  }
  if (path.endsWith("/director/chat")) return structuredClone(chat);
  if (path === `/jobs/${jid}/retry`) { chat = { ...chat, pending_job_id: jid, last_error: null }; return job; }
  if (path.endsWith(`/director/actions/${pid}`)) { chat = { ...chat, proposals: chat.proposals.map(p => ({ ...p, status: "applied" })) }; return { ok: true, next_step: "edit", version_id: vid }; }
  throw new Error(`Unexpected route ${path}`);
}
function route(path = `/episodes/${eid}`, search = "?step=preview&version=" + vid) { mocks.pathname = path; window.history.replaceState(null, "", path + search); }
function renderWidget(overrides: Partial<Parameters<typeof FloatingDirector>[0]> = {}) { return render(<FloatingDirector ownerId="owner-a" status={status} allowed {...overrides} />); }
async function open() { fireEvent.click(screen.getByRole("button", { name: "打开AI导演" })); await waitFor(() => expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeInTheDocument()); }
async function openDirectorMenu(action: string) {
  fireEvent.click(screen.getByRole("button", { name: "打开导演功能" }));
  fireEvent.click(await screen.findByRole("button", { name: action }));
}
async function chooseEpisode(value: string) {
  fireEvent.click(screen.getByRole("button", { name: "切换导演对话的节目" }));
  const select = await screen.findByLabelText("选择导演对话的节目");
  await waitFor(() => expect(select).toBeEnabled());
  fireEvent.change(select, { target: { value } });
}
beforeEach(() => { clearDirectorChatMemory(); mocks.api.mockReset(); mocks.push.mockReset(); chat = blank(); list = [episode(), episode(otherId)]; mocks.api.mockImplementation(service); route(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("floating AI director", () => {
  it("keeps a distinct launcher, fetches only when opened, and shows genuine stored model replies", async () => {
    chat.messages = [{ id: "m0", role: "assistant", source: "model", text: "目前只有三个短镜头，故事缺少开端。", created_at: "2026-11-13" }];
    renderWidget(); expect(mocks.api).not.toHaveBeenCalled();
    await open(); expect(await screen.findByText("目前只有三个短镜头，故事缺少开端。")).toBeVisible();
    expect(screen.getByRole("dialog", { name: "在场导演" })).toHaveAttribute("aria-modal", "false");
    expect(screen.queryByRole("button", { name: "对话记录" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "语音输入" })).toBeVisible();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(screen.queryByText("已记录这条补充需求。")).not.toBeInTheDocument();
  });

  it("sends actual text with the current version and step, without turning the question into a saved brief", async () => {
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "为什么这版不连贯？" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    expect(await screen.findByText("这版缺少铺垫。先交代这一桌，再保留一句完整原声，会更连贯。")).toBeVisible();
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("");
    expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/chat`, expect.objectContaining({ method: "POST", body: expect.objectContaining({ text: "为什么这版不连贯？", version_id: vid, step: "preview", confirm_send: true }) }));
    expect(mocks.api.mock.calls.some(([path]) => path.endsWith("/director/messages"))).toBe(false);
  });

  it("appends recovered voice to the same current draft without posting or duplicating it on reopen", async () => {
    const voice = { id: "9".repeat(32), job_id: jid, state: "succeeded", text: "旁白短一点", error: null, mode: "cloud" };
    mocks.api.mockImplementation((path: string, options: Options = {}) => path.endsWith("/director/voice") ? { items: [voice] } : service(path, options));
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "保留现场声音" } });
    await openDirectorMenu("最近语音");
    fireEvent.click(within(screen.getByRole("region", { name: "最近语音" })).getByRole("button", { name: /旁白短一点/ }));
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("保留现场声音\n旁白短一点");
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "关闭导演对话" })); await open();
    await screen.findByText("文字已放到输入框，可修改后发送");
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("保留现场声音\n旁白短一点");
  });

  it("preserves an uncertain message and its client ID when voice attempts to fill the input", async () => {
    const voice = { id: "9".repeat(32), job_id: jid, state: "succeeded", text: "这句先别发送", error: null, mode: "cloud" };
    let first = true;
    mocks.api.mockImplementation((path: string, options: Options = {}) => {
      if (path.endsWith("/director/voice")) return { items: [voice] };
      if (options.method === "POST" && first) { first = false; throw new AppError("NETWORK_ERROR", "连接中断，结果不确定", 0, true); }
      return service(path, options);
    });
    renderWidget(); await open();
    const input = screen.getByLabelText("和AI导演对话");
    fireEvent.change(input, { target: { value: "原来的问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await screen.findByText("连接中断，结果不确定"); expect(input).toBeDisabled();
    fireEvent.change(input, { target: { value: "不能抹掉未知请求" } });
    await openDirectorMenu("最近语音");
    fireEvent.click(within(screen.getByRole("region", { name: "最近语音" })).getByRole("button", { name: /这句先别发送/ }));
    expect(input).toHaveValue("原来的问题");
    expect(await screen.findByRole("button", { name: "放入输入框" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await waitFor(() => expect(input).toHaveValue(""));
    const posts = mocks.api.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts).toHaveLength(2); expect(posts[1][1].body).toEqual(posts[0][1].body);
    fireEvent.click(screen.getByRole("button", { name: "放入输入框" }));
    expect(input).toHaveValue("这句先别发送");
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(2);
  });

  it("prevents repeat sends while the POST is waiting", async () => {
    let resolve: (value: unknown) => void = () => {};
    mocks.api.mockImplementation((path: string, options: Options = {}) => options.method === "POST" ? new Promise(done => { resolve = done; }) : service(path, options));
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "聊聊旁白" } });
    const button = screen.getByRole("button", { name: "发送给AI导演" }); fireEvent.click(button); fireEvent.click(button);
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    await act(async () => resolve(job));
  });

  it("reuses the exact request after an uncertain network result", async () => {
    let first = true;
    mocks.api.mockImplementation((path: string, options: Options = {}) => {
      if (options.method === "POST" && first) { first = false; throw new AppError("NETWORK_ERROR", "连接中断，结果不确定", 0, true); }
      return service(path, options);
    });
    renderWidget(); await open();
    const input = screen.getByLabelText("和AI导演对话"); fireEvent.change(input, { target: { value: "旁白太慢了" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await screen.findByText("连接中断，结果不确定"); expect(input).toHaveValue("旁白太慢了");
    await waitFor(() => expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await waitFor(() => expect(input).toHaveValue(""));
    const posts = mocks.api.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts[1][1].body).toEqual(posts[0][1].body);
  });

  it("reconciles an accepted message from its client ID after the POST response is lost", async () => {
    mocks.api.mockImplementation(async (path: string, options: Options = {}) => {
      if (options.method === "POST") { await service(path, options); throw new AppError("NETWORK_ERROR", "结果尚不确定", 0, true); }
      return service(path, options);
    });
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "不要抢原声" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await waitFor(() => expect(screen.getByLabelText("和AI导演对话")).toHaveValue(""));
    expect(screen.getByText("不要抢原声")).toBeVisible();
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  });

  it("preserves unsent text when closed, and on a same-owner remount", async () => {
    const view = renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "这句话还没发" } });
    fireEvent.click(screen.getByRole("button", { name: "关闭导演对话" }));
    expect(screen.getByRole("button", { name: "打开AI导演" })).toHaveFocus();
    await open(); expect(screen.getByLabelText("和AI导演对话")).toHaveValue("这句话还没发");
    view.unmount(); renderWidget();
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("这句话还没发");
  });

  it("does not carry one owner's unsent text into another owner's session", async () => {
    const view = renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "私人要求" } });
    view.rerender(<FloatingDirector ownerId="owner-b" status={status} allowed />);
    await open(); expect(screen.getByLabelText("和AI导演对话")).toHaveValue("");
    expect(screen.queryByText("私人要求")).not.toBeInTheDocument();
  });

  it("restores focus on Escape and clears visible history on auth expiry", async () => {
    chat.messages = [{ id: "u", role: "user", source: "user", text: "私密想法", created_at: "2026-11-13" }];
    renderWidget(); await open(); await screen.findByText("私密想法");
    fireEvent.keyDown(screen.getByRole("button", { name: "关闭导演对话" }), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开AI导演" })).toHaveFocus();
    await open(); act(() => window.dispatchEvent(new Event("episode:auth-expired")));
    expect(screen.queryByText("私密想法")).not.toBeInTheDocument();
  });

  it("hides the entire conversation while permissions are being revalidated", async () => {
    const view = renderWidget(); await open();
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/chat`, expect.anything()));
    const count = mocks.api.mock.calls.length;
    view.rerender(<FloatingDirector ownerId="owner-a" status={status} allowed={false} />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开AI导演" })).toBeDisabled();
    await act(async () => {}); expect(mocks.api).toHaveBeenCalledTimes(count);
  });

  it("shows the real readiness reason and never invents an answer when chat is unavailable", async () => {
    chat.can_chat = false; chat.reason = "编导模型尚未配置";
    renderWidget(); await open(); await screen.findByText("编导模型尚未配置");
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "你看懂了吗？" } });
    expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeDisabled();
    expect(screen.queryByText("AI 导演", { exact: true })).not.toBeInTheDocument();
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  });

  it("keeps demo replies explicitly marked", async () => {
    chat.mode = "demo"; chat.messages = [{ id: "m", role: "assistant", source: "demo", text: "固定测试回复", created_at: "2026-11-13" }];
    renderWidget({ status: { ...status, model_mode: "demo" } }); await open();
    expect(await screen.findByText("演示回复")).toBeVisible(); expect(screen.getByText(/固定回复，非真实 AI/)).toBeVisible();
  });

  it("renders the exact proposed narration and applies only after explicit confirmation", async () => {
    chat.messages = [{ id: "m", role: "assistant", source: "model", text: "开场可以更短。", created_at: "2026-11-13" }];
    chat.proposals = [{ id: pid, kind: "revise_narration", title: "缩短开场", explanation: "用一句短句把注意力交回现场。", narration_text: "在场，开拍！", status: "pending", message_id: "m" }];
    const navigate = vi.fn((event: Event) => event.preventDefault()); window.addEventListener("director:navigate", navigate);
    renderWidget(); await open();
    expect(await screen.findByLabelText("建议的新旁白")).toHaveTextContent("在场，开拍！");
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "确认文案，另存旁白草案" }));
    await waitFor(() => expect(navigate).toHaveBeenCalled());
    expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/actions/${pid}`, { method: "POST", body: { confirm: true } });
    expect(mocks.push).not.toHaveBeenCalled();
    window.removeEventListener("director:navigate", navigate);
  });

  it("names the cost-bearing action and disables stale suggestions", async () => {
    chat.messages = [{ id: "m", role: "assistant", source: "model", text: "建议先交代现场。", created_at: "2026-11-13" }];
    chat.proposals = [{ id: pid, kind: "revise_directions", title: "重新安排故事", explanation: "先场景后对话。", requirement: "先交代场景，再完整保留对话", status: "pending", message_id: "m" }];
    renderWidget(); await open();
    const card = await screen.findByRole("region", { name: "导演建议：重新安排故事" });
    expect(within(card).getByText("先交代场景，再完整保留对话")).toBeVisible();
    expect(within(card).getByText(/本期上限3元，累计30元/)).toBeVisible();
    expect(within(card).getByRole("button", { name: "确认要求并生成新方案" })).toBeVisible();
    chat.proposals[0].status = "stale"; act(() => window.dispatchEvent(new CustomEvent("director:updated", { detail: { episodeId: eid } })));
    await screen.findByText("节目已更新，这条建议不再适用");
    expect(screen.queryByRole("button", { name: "确认要求并生成新方案" })).not.toBeInTheDocument();
  });

  it("retries failed model work only after the user clicks", async () => {
    chat.last_job_id = jid; chat.last_error = { code: "TIMEOUT", message: "这次回答超时了" };
    renderWidget(); await open(); await screen.findByText("这次回答超时了");
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "重新让导演回答" }));
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/jobs/${jid}/retry`, { method: "POST", body: {} }));
    expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeDisabled();
  });

  it("lets the home page select a saved episode without making a new one", async () => {
    route("/", ""); renderWidget(); fireEvent.click(screen.getByRole("button", { name: "打开AI导演" }));
    await chooseEpisode(eid);
    await screen.findByLabelText("和AI导演对话");
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "这期未发送" } });
    await chooseEpisode(otherId);
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("");
    await chooseEpisode(eid);
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("这期未发送");
  });

  it("creates one episode from an explicit home theme and does not automatically call the model", async () => {
    list = []; route("/", ""); renderWidget(); fireEvent.click(screen.getByRole("button", { name: "打开AI导演" }));
    fireEvent.change(await screen.findByLabelText("新一期的主题"), { target: { value: "说好不点外卖" } });
    fireEvent.click(screen.getByRole("button", { name: "新建这期，开始聊" }));
    await screen.findByLabelText("和AI导演对话");
    const posts = mocks.api.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(posts).toHaveLength(1); expect(posts[0]).toEqual(["/episodes", { method: "POST", body: { title: "说好不点外卖", background: "说好不点外卖" } }]);
  });

  it("keeps the failed read visible and disallows sending until a fresh read succeeds", async () => {
    let fails = true;
    mocks.api.mockImplementation((path: string, options: Options = {}) => { if (path.endsWith("/director/chat") && fails) throw new Error("读取失败"); return service(path, options); });
    renderWidget(); await open(); await screen.findByText("读取失败");
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "你好导演" } });
    expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeDisabled();
    fails = false; fireEvent.click(screen.getByRole("button", { name: "重新读取对话" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "发送给AI导演" })).toBeEnabled());
  });

  it("polls durable pending replies, pauses when closed, and reads the completed reply on reopening", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    chat.pending_job_id = jid; chat.last_job_id = jid;
    renderWidget(); await open(); await screen.findByText(/导演正在结合本期内容思考/);
    const initial = mocks.api.mock.calls.filter(([path]) => path.endsWith("/director/chat")).length;
    await act(async () => { vi.advanceTimersByTime(1500); });
    expect(mocks.api.mock.calls.filter(([path]) => path.endsWith("/director/chat"))).toHaveLength(initial + 1);
    fireEvent.click(screen.getByRole("button", { name: "关闭导演对话" }));
    const closed = mocks.api.mock.calls.length;
    await act(async () => { vi.advanceTimersByTime(4500); });
    expect(mocks.api).toHaveBeenCalledTimes(closed);
    chat = { ...chat, pending_job_id: null, messages: [{ id: "ready", role: "assistant", source: "model", text: "先保留这段完整对话，再接反应镜头。", created_at: "2026-11-13" }] };
    await open(); expect(await screen.findByText("先保留这段完整对话，再接反应镜头。")).toBeVisible();
    expect(screen.queryByText(/导演正在结合本期内容思考/)).not.toBeInTheDocument();
  });

  it("ignores an old episode's late history response after switching context", async () => {
    let resolve: (state: DirectorChatState) => void = () => {};
    mocks.api.mockImplementation((path: string, options: Options = {}) => path === `/episodes/${eid}/director/chat`
      ? new Promise(done => { resolve = done; }) : service(path, options));
    const view = renderWidget(); await open();
    route(`/episodes/${otherId}`, "?step=materials"); view.rerender(<FloatingDirector ownerId="owner-a" status={status} allowed />);
    await waitFor(() => expect(mocks.api).toHaveBeenCalledWith(`/episodes/${otherId}/director/chat`, expect.anything()));
    await act(async () => resolve({ ...blank(), messages: [{ id: "late", role: "assistant", source: "model", text: "上一期的私密回复", created_at: "2026-11-13" }] }));
    expect(screen.queryByText("上一期的私密回复")).not.toBeInTheDocument();
  });

  it("keeps a new owner's draft when a previous owner's accepted POST completes late", async () => {
    let resolve: (value: unknown) => void = () => {};
    mocks.api.mockImplementation((path: string, options: Options = {}) => options.method === "POST" ? new Promise(done => { resolve = done; }) : service(path, options));
    const view = renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "旧账号的问题" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    view.rerender(<FloatingDirector ownerId="owner-b" status={status} allowed />); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "新账号的草稿" } });
    await act(async () => resolve(job));
    view.unmount(); renderWidget({ ownerId: "owner-b" });
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("新账号的草稿");
  });
  it("uses the same sprite to open its menu and closes layers before the conversation", async () => {
    renderWidget(); const sprite = screen.getByRole("button", { name: "打开AI导演" }); await open();
    expect(screen.getByRole("button", { name: "打开导演功能" })).toBe(sprite);
    fireEvent.click(sprite);
    expect(screen.getByRole("button", { name: "换一期节目" })).toBeVisible();
    expect(screen.getByRole("button", { name: "聊聊这一版" })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole("button", { name: "聊聊这一版" }), { key: "Escape" });
    expect(screen.queryByRole("button", { name: "换一期节目" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "在场导演" })).toBeInTheDocument();
    expect(sprite).toHaveFocus();
    fireEvent.keyDown(sprite, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  });

  it("fills shortcuts and the sprite feedback action into the existing draft without sending", async () => {
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "先保留原声" } });
    fireEvent.click(await screen.findByRole("button", { name: "旁白短一点" }));
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("先保留原声\n旁白再短一点，语气干脆些。");
    await openDirectorMenu("聊聊这一版");
    await waitFor(() => expect(screen.getByLabelText("和AI导演对话")).toHaveValue("先保留原声\n旁白再短一点，语气干脆些。\n这版有哪些地方可以改得更好？"));
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(0);
  });

  it("keeps an uncertain request intact when the menu proposes more feedback", async () => {
    mocks.api.mockImplementation((path: string, options: Options = {}) => {
      if (options.method === "POST") throw new AppError("NETWORK_ERROR", "这条发送尚不确定", 0, true);
      return service(path, options);
    });
    renderWidget(); await open();
    fireEvent.change(screen.getByLabelText("和AI导演对话"), { target: { value: "保留这句" } });
    fireEvent.click(screen.getByRole("button", { name: "发送给AI导演" }));
    await screen.findByText("这条发送尚不确定");
    await openDirectorMenu("聊聊这一版");
    await screen.findByText("先核对上一条发送结果，再补充想法。");
    expect(screen.getByLabelText("和AI导演对话")).toHaveValue("保留这句");
    expect(mocks.api.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  });

  it("keeps the covered chat inert and returns focus when closing the episode picker", async () => {
    renderWidget(); await open(); await openDirectorMenu("换一期节目");
    const input = screen.getByLabelText("和AI导演对话");
    expect(input.closest(".director-mode-body")).toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "返回导演对话" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "返回导演对话" }));
    expect(input.closest(".director-mode-body")).not.toHaveAttribute("inert");
    expect(screen.getByRole("button", { name: "打开导演功能" })).toHaveFocus();
  });

  it("does not leave a dirty episode for another one without the existing leave confirmation", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderWidget(); await open();
    act(() => window.dispatchEvent(new CustomEvent("episode:dirty", { detail: true })));
    await chooseEpisode(otherId);
    expect(confirm).toHaveBeenCalled(); expect(mocks.push).not.toHaveBeenCalled();
    expect(screen.getByRole("region", { name: "选择导演节目" })).toBeInTheDocument();
    confirm.mockReturnValue(true);
    fireEvent.change(screen.getByLabelText("选择导演对话的节目"), { target: { value: otherId } });
    expect(mocks.push).toHaveBeenCalledWith(`/episodes/${otherId}?step=materials`);
    confirm.mockRestore();
  });

});

it('shows exact subtitle changes and only applies after the explicit candidate button',async()=>{
 chat.messages=[{id:'m1',role:'assistant',text:'先回听这句，再决定改字。',source:'model',created_at:'2026-11-16'}];
 chat.proposals=[{id:pid,message_id:'m1',kind:'revise_subtitles',base_version_id:vid,title:'校对一句',explanation:'只改字幕',status:'pending',subtitle_changes:[{before:'喝了',after:'不喝了',start:2.1,end:3.4}]}];
 renderWidget();await open();
 expect(await screen.findByText('原文：喝了')).toBeVisible();expect(screen.getByText('建议：不喝了')).toBeVisible();
 expect(mocks.api.mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(0);
 fireEvent.click(screen.getByRole('button',{name:'回听这处'}));
 expect(screen.getByRole('dialog',{name:'回听建议对应的原话'})).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'关闭弹窗'}));
 fireEvent.click(screen.getByRole('button',{name:'确认改字并生成新版'}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/actions/${pid}`,{method:'POST',body:{confirm:true}}));
});

it('reviews a recut in place and keeps the base until explicitly accepted',async()=>{
 chat.messages=[{id:'cut-message',role:'assistant',text:'先留完整这一句。',source:'model',created_at:'2026-11-16'}];
 chat.proposals=[{id:pid,message_id:'cut-message',kind:'revise_cut',base_version_id:vid,title:'收紧开头',explanation:'第二段不进入新版',status:'pending',before_seconds:20,after_seconds:12,cut_changes:[{shot:1,start:1,end:13,reason:'保留完整回答',removed:false},{shot:2,start:13,end:20,reason:'这段不放入新版',removed:true}]}];
 renderWidget();await open();
 expect(await screen.findByText('镜头内容约 20 秒 → 12 秒')).toBeVisible();
 expect(screen.getByRole('button',{name:'按这个剪法生成新版'})).toBeVisible();
 fireEvent.click(screen.getByText('查看剪法与影响'));
 expect(screen.getByText('不放入新版 · 原版第2段')).toBeVisible();
 expect(mocks.api.mock.calls.filter(([,o])=>o?.method==='POST')).toHaveLength(0);
 fireEvent.click(screen.getAllByRole('button',{name:'看看这一段'})[0]);
 expect(screen.getByRole('dialog',{name:'查看建议对应的原片段'})).toBeInTheDocument();
 fireEvent.click(screen.getByRole('button',{name:'关闭弹窗'}));
 fireEvent.click(screen.getByRole('button',{name:'按这个剪法生成新版'}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/actions/${pid}`,{method:'POST',body:{confirm:true}}));
});

function requestCue(versionId=vid,cueId="e".repeat(32)) {
 act(()=>window.dispatchEvent(new CustomEvent("director:review-cue",{detail:{versionId,cueId,label:"这句字幕 · 12—14秒"}})));
}
it("opens a specific review in the same director without sending until asked",async()=>{
 renderWidget();requestCue();
 await waitFor(()=>expect(screen.getByLabelText("和AI导演对话")).toHaveValue("帮我看看这句字幕，有必要改吗？先给建议，不直接改片。"));
 expect(screen.getByText("正在讨论：这句字幕 · 12—14秒")).toBeVisible();
 expect(mocks.api.mock.calls.some(([,o])=>o?.method==="POST")).toBe(false);
 fireEvent.click(screen.getByRole("button",{name:"发送给AI导演"}));
 await waitFor(()=>expect(mocks.api).toHaveBeenCalledWith(`/episodes/${eid}/director/chat`,expect.objectContaining({method:"POST",body:expect.objectContaining({version_id:vid,audio_focus_cue_id:"e".repeat(32)})})));
 await waitFor(()=>expect(screen.getByLabelText("和AI导演对话")).toHaveValue(""));
 expect(screen.queryByText("正在讨论：这句字幕 · 12—14秒")).not.toBeInTheDocument();
});
it("does not overwrite an existing draft or retarget it to a different version",async()=>{
 renderWidget();await open();fireEvent.change(screen.getByLabelText("和AI导演对话"),{target:{value:"保留这条草稿"}});
 requestCue();expect(await screen.findByText(/输入框已有内容/)).toBeVisible();
 expect(screen.getByLabelText("和AI导演对话")).toHaveValue("保留这条草稿");
 fireEvent.change(screen.getByLabelText("和AI导演对话"),{target:{value:""}});requestCue();
 await waitFor(()=>expect(screen.getByLabelText("和AI导演对话")).not.toHaveValue(""));
 act(()=>{window.history.replaceState(null,"",`/episodes/${eid}?step=preview&version=${otherId}`);window.dispatchEvent(new Event("episode:location"));});
 fireEvent.click(screen.getByRole("button",{name:"发送给AI导演"}));
 expect(await screen.findByText(/已切换版本/)).toBeVisible();
 expect(mocks.api.mock.calls.some(([,o])=>o?.method==="POST")).toBe(false);
});
it("retains cue binding with a draft across remount and clears it when the draft is emptied",async()=>{
 const view=renderWidget();requestCue();
 await waitFor(()=>expect(screen.getByLabelText("和AI导演对话")).not.toHaveValue(""));
 view.unmount();renderWidget();
 expect(await screen.findByText("正在讨论：这句字幕 · 12—14秒")).toBeVisible();
 fireEvent.change(screen.getByLabelText("和AI导演对话"),{target:{value:""}});
 expect(screen.queryByText("正在讨论：这句字幕 · 12—14秒")).not.toBeInTheDocument();
 fireEvent.change(screen.getByLabelText("和AI导演对话"),{target:{value:"换个话题"}});
 await waitFor(()=>expect(screen.getByRole("button",{name:"发送给AI导演"})).toBeEnabled());
 fireEvent.click(screen.getByRole("button",{name:"发送给AI导演"}));
 await waitFor(()=>expect(mocks.api.mock.calls.find(([,o])=>o?.method==="POST")?.[1]?.body).not.toHaveProperty("audio_focus_cue_id"));
});
it("ignores cue requests for another version",()=>{
 renderWidget();requestCue(otherId);
 expect(screen.getByRole("button",{name:"打开AI导演"})).toBeVisible();
 expect(mocks.api).not.toHaveBeenCalled();
});

it("keeps a paid action review in the same card and polls to its result without navigation", async()=>{
  chat.messages=[{id:"review-message",role:"assistant",text:"先看清动作，再决定怎么剪。",source:"model",created_at:"2026-11-17"}];
  chat.proposals=[{id:pid,message_id:"review-message",kind:"review_sequence",title:"查看这段动作",explanation:"先看过程，不直接剪。",status:"pending",start:11,end:34}];
  mocks.api.mockImplementation(async(path:string,options:Options={})=>{
    if(path.endsWith(`/director/actions/${pid}`)) {
      chat.proposals=chat.proposals.map(p=>({...p,status:"applied",review_state:"queued"}));
      return {ok:true,version_id:vid,job_id:jid};
    }
    return service(path,options);
  });
  renderWidget();await open();
  expect(await screen.findByText("查看成片 11—34 秒")).toBeVisible();
  expect(screen.getByText(/发送这段画面的抽帧/)).toBeVisible();
  expect(mocks.api.mock.calls.some(([,o])=>o?.method==="POST")).toBe(false);
  fireEvent.click(screen.getByRole("button",{name:"查看这段动作"}));
  expect(await screen.findByText("已排队，稍后开始看这段。")).toBeVisible();
  expect(mocks.push).not.toHaveBeenCalled();
  chat.proposals=chat.proposals.map(p=>({...p,review_state:"succeeded",review_summary:"画面里人物先抬手，再低下头。"}));
  await waitFor(()=>expect(screen.getByText("画面里人物先抬手，再低下头。")).toBeVisible(),{timeout:3000});
  expect(screen.getByText(/抽样观察，不含声音/)).toBeVisible();
  expect(mocks.api.mock.calls.filter(([,o])=>o?.method==="POST")).toHaveLength(1);
});

it("restores a failed observation without offering a blind paid retry",async()=>{
  chat.messages=[{id:"failed-review",role:"assistant",text:"先看过程。",source:"model",created_at:"2026-11-17"}];
  chat.proposals=[{id:pid,message_id:"failed-review",kind:"review_sequence",title:"查看这段动作",explanation:"先看过程。",status:"applied",start:1,end:3,review_state:"failed",review_summary:"本次没有看完，请先核对费用。"}];
  renderWidget();await open();
  expect(await screen.findByText("本次没有看完，请先核对费用。")).toBeVisible();
  expect(screen.queryByRole("button",{name:"查看这段动作"})).not.toBeInTheDocument();
  expect(mocks.api.mock.calls.some(([,o])=>o?.method==="POST")).toBe(false);
});
