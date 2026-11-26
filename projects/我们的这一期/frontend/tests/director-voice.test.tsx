import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DirectorVoice, recordingMime } from "@/components/director-voice";
import { AppError } from "@/lib/api";
import type { DirectorVoice as VoiceRecord, DirectorVoiceStatus } from "@/lib/director-voice";

const mocks = vi.hoisted(() => ({ api: vi.fn(), upload: vi.fn(), stopTrack: vi.fn(), getUserMedia: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: mocks.api, uploadDirectorVoice: mocks.upload,
  AppError: class extends Error { constructor(public code: string, message: string, public status = 0, public uncertain = false) { super(message); } } }));
const eid = "a".repeat(32), id = "b".repeat(32), jid = "c".repeat(32);
const status: DirectorVoiceStatus = { can_record: true, reason: "", provider: "MiniMax", model: "asr-1.0", mode: "cloud", max_seconds: 60, max_bytes: 12582912 };
const ready: VoiceRecord = { id, job_id: jid, state: "succeeded", text: "保留完整原声，旁白少一点。", error: null, mode: "cloud" };
class Recorder {
  static items: Recorder[] = [];
  static isTypeSupported = vi.fn((value: string) => value.startsWith("audio/webm"));
  state = "inactive";
  mimeType: string;
  ondataavailable: ((value: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(_stream: unknown, options: { mimeType: string }) { this.mimeType = options.mimeType; Recorder.items.push(this); }
  start() { this.state = "recording"; }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["synthetic-recording-fixture"], { type: this.mimeType }) });
    this.onstop?.();
  }
}
function view(extra: Partial<Parameters<typeof DirectorVoice>[0]> = {}) {
  return render(<DirectorVoice episodeId={eid} active onTranscript={() => true} {...extra} />);
}
async function clickMicrophone() {
  const mic = await screen.findByRole("button", { name: "语音输入" });
  await waitFor(() => expect(mic).toBeEnabled()); fireEvent.click(mic);
}
async function startRecording() { await clickMicrophone(); await screen.findByText(/正在听 ·/); }
beforeEach(() => {
  mocks.api.mockReset(); mocks.upload.mockReset(); mocks.stopTrack.mockReset(); mocks.getUserMedia.mockReset(); Recorder.items = [];
  Recorder.isTypeSupported.mockImplementation(value => value.startsWith("audio/webm"));
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: mocks.getUserMedia } });
  vi.stubGlobal("MediaRecorder", Recorder);
  mocks.getUserMedia.mockResolvedValue({ getTracks: () => [{ stop: mocks.stopTrack }] });
  mocks.api.mockImplementation(async (path: string) => {
    if (path.endsWith("/status")) return { ...status };
    if (path.endsWith("/voice")) return { items: [] };
    if (path.includes("/by-client/")) throw new AppError("NOT_FOUND", "未找到", 404);
    if (path.endsWith(id)) return ready;
    throw new Error("unexpected voice request");
  });
  mocks.upload.mockResolvedValue(ready);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe("director voice input", () => {
  it("uses the compact microphone and still requires consent before returning text to the shared composer", async () => {
    const onTranscript = vi.fn(() => true); const rendered = view({ compact: true, onTranscript });
    await waitFor(() => expect(screen.getByRole("button", { name: "语音输入" })).toBeEnabled());
    expect(rendered.container.querySelector(".director-voice-tray")).toBeNull();
    expect(mocks.getUserMedia).not.toHaveBeenCalled();
    await startRecording(); fireEvent.click(screen.getByRole("button", { name: "结束录音" }));
    expect(screen.getByText(/供应商留存期限未知/)).toBeVisible();
    expect(mocks.upload).not.toHaveBeenCalled(); expect(onTranscript).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" }));
    await screen.findByText("文字已放到输入框，可修改后发送");
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith(ready.text);
  });

  it("opens persisted voice history from the compact menu without filling text until a record is chosen", async () => {
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? status : { items: [ready] });
    const onTranscript = vi.fn(() => true), onHistoryClose = vi.fn();
    const props = { episodeId: eid, active: true, compact: true, onTranscript, onHistoryClose };
    const rendered = render(<DirectorVoice {...props} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "语音输入" })).toBeEnabled());
    expect(screen.queryByRole("region", { name: "最近语音" })).not.toBeInTheDocument();
    expect(onTranscript).not.toHaveBeenCalled();
    rendered.rerender(<DirectorVoice {...props} historyOpen />);
    expect(screen.getByRole("region", { name: "最近语音" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /保留完整原声，旁白少一点/ }));
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith(ready.text);
    expect(onHistoryClose).toHaveBeenCalledOnce();
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
    rendered.rerender(<DirectorVoice {...props} />);
    expect(screen.queryByRole("region", { name: "最近语音" })).not.toBeInTheDocument();
  });

  it("stops recording when history covers the controls, retaining the unsent capture on return", async () => {
    const props = { episodeId: eid, active: true, compact: true, onTranscript: () => true };
    const rendered = render(<DirectorVoice {...props} />);
    await startRecording();
    rendered.rerender(<DirectorVoice {...props} historyOpen />);
    expect(mocks.stopTrack).toHaveBeenCalled();
    expect(Recorder.items[0].state).toBe("inactive");
    expect(mocks.upload).not.toHaveBeenCalled();
    rendered.rerender(<DirectorVoice {...props} />);
    expect(screen.getByRole("button", { name: "同意并转成文字" })).toBeVisible();
    expect(screen.getByText(/已录 .*未发送/)).toBeVisible();
  });

  it("offers a way back from an empty compact history without starting a recording", async () => {
    const onHistoryClose = vi.fn(); view({ compact: true, historyOpen: true, onHistoryClose });
    await screen.findByText(/还没有语音记录/);
    fireEvent.click(screen.getByRole("button", { name: "返回导演对话" }));
    expect(onHistoryClose).toHaveBeenCalledOnce(); expect(mocks.getUserMedia).not.toHaveBeenCalled();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("starts only after the microphone click and fills the one parent composer after consent without sending chat", async () => {
    const onTranscript = vi.fn(() => true), onBusyChange = vi.fn(); view({ onTranscript, onBusyChange });
    await waitFor(() => expect(screen.getByRole("button", { name: "语音输入" })).toBeEnabled());
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    await startRecording();
    expect(onBusyChange).toHaveBeenLastCalledWith(true);
    expect(mocks.getUserMedia).toHaveBeenCalledWith(expect.objectContaining({ audio: expect.any(Object), video: false }));
    expect(mocks.upload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    expect(mocks.stopTrack).toHaveBeenCalled();
    expect(screen.getByText(/供应商留存期限未知/)).toBeVisible();
    expect(mocks.upload).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" }));
    await screen.findByText("文字已放到输入框，可修改后发送");
    expect(mocks.upload).toHaveBeenCalledWith(eid, expect.any(File), expect.objectContaining({ confirm_send: true, accept_unknown_retention: true }), expect.any(Function), expect.any(AbortSignal));
    expect(onTranscript).toHaveBeenCalledExactlyOnceWith(ready.text);
    // Parent notification runs in an effect after the ready text is committed.
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
    expect(mocks.api.mock.calls.some(([path]) => path.endsWith("/chat"))).toBe(false);
  });

  it("stops the microphone when closed, and never uploads cancelled or background recordings", async () => {
    const props = { episodeId: eid, active: true, onTranscript: () => true };
    const rendered = render(<DirectorVoice {...props} />);
    await startRecording();
    rendered.rerender(<DirectorVoice {...props} active={false} />);
    expect(mocks.stopTrack).toHaveBeenCalledOnce(); expect(Recorder.items[0].state).toBe("inactive");
    expect(mocks.upload).not.toHaveBeenCalled();
    rendered.rerender(<DirectorVoice {...props} />);
    await startRecording(); await waitFor(() => expect(Recorder.items).toHaveLength(2));
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(mocks.stopTrack).toHaveBeenCalledTimes(2); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("stops a late permission response after closing before the user grants permission", async () => {
    let resolve!: (value: unknown) => void;
    mocks.getUserMedia.mockImplementation(() => new Promise(done => { resolve = done; }));
    const props = { episodeId: eid, active: true, onTranscript: () => true };
    const rendered = render(<DirectorVoice {...props} />);
    await clickMicrophone(); await screen.findByText("请允许麦克风");
    rendered.rerender(<DirectorVoice {...props} active={false} />);
    await act(async () => resolve({ getTracks: () => [{ stop: mocks.stopTrack }] }));
    expect(mocks.stopTrack).toHaveBeenCalledOnce(); expect(Recorder.items).toHaveLength(0); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("provides a usable text fallback after permission denial", async () => {
    mocks.getUserMedia.mockRejectedValue(new DOMException("denied", "NotAllowedError"));
    const onBusyChange = vi.fn(); view({ onBusyChange }); await clickMicrophone();
    await screen.findByText(/麦克风未获允许/);
    expect(onBusyChange).toHaveBeenLastCalledWith(false); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("does not request a microphone when the current transcription service is unavailable", async () => {
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? { ...status, can_record: false, reason: "本期语音尚未配置" } : { items: [] });
    view(); await screen.findByText("本期语音尚未配置");
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("selects the supported Safari MP4 format instead of forcing WebM", async () => {
    Recorder.isTypeSupported.mockImplementation(value => value === "audio/mp4");
    expect(recordingMime()).toBe("audio/mp4");
    view(); await startRecording(); fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" }));
    await screen.findByText("文字已放到输入框，可修改后发送");
    const file = mocks.upload.mock.calls[0][1] as File;
    expect(file.name).toBe("director-voice.m4a"); expect(file.type).toBe("audio/mp4");
  });

  it("stops before 60 seconds to allow container tail frames without automatically sending it", async () => {
    view(); await startRecording();
    const now = Date.now(); vi.spyOn(Date, "now").mockReturnValue(now + 59010);
    await screen.findByText(/已录 .* 未发送/, {}, { timeout: 1500 });
    expect(mocks.stopTrack).toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });

  it("refuses a recording larger than the local bound without uploading it", async () => {
    view(); await startRecording();
    act(() => Recorder.items[0].ondataavailable?.({ data: { size: 12582913 } as Blob }));
    expect(screen.getByText(/超过12MB/)).toBeVisible(); expect(mocks.stopTrack).toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("reconciles an uncertain upload before offering an identical client-ID retry", async () => {
    mocks.upload.mockRejectedValueOnce(new AppError("NETWORK_ERROR", "结果不确定", 0, true)).mockResolvedValue(ready);
    view(); await startRecording(); fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" }));
    await screen.findByText("结果不确定"); expect(mocks.upload).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: "同意并转成文字" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "核对提交状态" }));
    await screen.findByText(/服务端没有这段记录/);
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" }));
    await screen.findByText("文字已放到输入框，可修改后发送");
    expect(mocks.upload.mock.calls[1][2]).toEqual(mocks.upload.mock.calls[0][2]);
  });

  it("restores a saved pending transcription without recording or sending again", async () => {
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? status : path.endsWith(id) ? ready : { items: [{ ...ready, state: "running", text: null }] });
    view(); await screen.findByText("转写中…");
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
    await screen.findByText("文字已放到输入框，可修改后发送", {}, { timeout: 2500 });
  });

  it("retains a transcript if the chat has an uncertain prior send instead of overwriting it", async () => {
    const onTranscript = vi.fn(() => false); view({ onTranscript });
    await startRecording(); fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" })); await screen.findByText("转写文字已保存");
    expect(screen.getByText(/输入框还有待核对的发送/)).toBeVisible();
    expect(screen.getAllByText(ready.text!)[0]).toBeVisible();
    onTranscript.mockReturnValue(true); fireEvent.click(screen.getByRole("button", { name: "放入输入框" }));
    expect(screen.getByText("文字已放到输入框，可修改后发送")).toBeVisible();
    expect(onTranscript).toHaveBeenCalledTimes(2);
  });

  it("does not release a new recording when an older cancelled permission request rejects", async () => {
    let reject!: (cause: unknown) => void;
    mocks.getUserMedia.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
    const props = { episodeId: eid, active: true, onTranscript: () => true };
    const rendered = render(<DirectorVoice {...props} />);
    await clickMicrophone(); await screen.findByText("请允许麦克风");
    rendered.rerender(<DirectorVoice {...props} active={false} />);
    rendered.rerender(<DirectorVoice {...props} />);
    await startRecording();
    await act(async () => reject(new DOMException("old request denied", "NotAllowedError")));
    expect(mocks.stopTrack).not.toHaveBeenCalled(); expect(Recorder.items[0].state).toBe("recording");
    expect(screen.getByText(/正在听 ·/)).toBeVisible();
  });

  it("never starts a microphone if the status response arrives after switching to the background", async () => {
    let resolve!: (value: unknown) => void;
    mocks.api.mockImplementation((path: string) => path.endsWith("/status") ? new Promise(done => { resolve = done; }) : Promise.resolve({ items: [] }));
    view();
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await act(async () => resolve(status));
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(Recorder.items).toHaveLength(0);
  });

  it("preserves a submitted job and its finished transcript while switching to the background", async () => {
    mocks.upload.mockResolvedValue({ ...ready, state: "running", text: null });
    view(); await startRecording(); fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" })); await screen.findByText("转写中…");
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(screen.getByText("转写中…")).toBeVisible();
    expect(screen.queryByRole("button", { name: "同意并转成文字" })).not.toBeInTheDocument();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await screen.findByText("文字已放到输入框，可修改后发送", {}, { timeout: 2500 });
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(screen.getByText("文字已放到输入框，可修改后发送")).toBeVisible(); expect(mocks.upload).toHaveBeenCalledOnce();
  });

  it("makes already finished transcripts discoverable after a fresh page mount", async () => {
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? status : { items: [ready] });
    const onTranscript = vi.fn(() => true); view({ onTranscript }); await screen.findByText("最近语音 · 1");
    fireEvent.click(screen.getByText("最近语音 · 1"));
    fireEvent.click(screen.getByRole("button", { name: /保留完整原声，旁白少一点/ }));
    expect(onTranscript).toHaveBeenCalledWith(ready.text); expect(mocks.getUserMedia).not.toHaveBeenCalled();
    expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("allows a new recording after a failed transcription instead of trapping the user", async () => {
    const failed = { ...ready, state: "failed", text: null, error: { code: "MODEL_FAILED", message: "这次转写未完成" } };
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? status : { items: [failed] });
    view(); await screen.findByText("最近语音 · 1");
    fireEvent.click(screen.getByText("最近语音 · 1")); fireEvent.click(screen.getByRole("button", { name: /转写未完成/ }));
    expect(screen.getByText("这次转写未完成")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重新录一句" }));
    await screen.findByText(/正在听 ·/); expect(mocks.getUserMedia).toHaveBeenCalledOnce(); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("falls back to typing on an insecure page without requesting permission", async () => {
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });
    view(); await clickMicrophone(); await screen.findByText(/当前页面无法使用麦克风/);
    expect(mocks.getUserMedia).not.toHaveBeenCalled(); expect(mocks.upload).not.toHaveBeenCalled();
  });

  it("fills each completed voice once across reopen, while explicit history retrieval can fill it again", async () => {
    const onTranscript = vi.fn(() => true), props = { episodeId: eid, active: true, onTranscript };
    const rendered = render(<DirectorVoice {...props} />);
    await startRecording(); fireEvent.click(screen.getByRole("button", { name: "说完了" }));
    fireEvent.click(screen.getByRole("button", { name: "同意并转成文字" })); await screen.findByText("文字已放到输入框，可修改后发送");
    expect(onTranscript).toHaveBeenCalledOnce();
    mocks.api.mockImplementation(async (path: string) => path.endsWith("/status") ? status : { items: [ready] });
    rendered.rerender(<DirectorVoice {...props} active={false} />);
    rendered.rerender(<DirectorVoice {...props} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "语音输入" })).toBeEnabled());
    expect(onTranscript).toHaveBeenCalledOnce(); expect(mocks.getUserMedia).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByText("最近语音 · 1"));
    fireEvent.click(screen.getByRole("button", { name: /保留完整原声，旁白少一点/ }));
    expect(onTranscript).toHaveBeenCalledTimes(2); expect(mocks.upload).toHaveBeenCalledOnce();
  });
});
