"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, Mic, RotateCcw, Square, X } from "lucide-react";
import { api, AppError, uploadDirectorVoice } from "@/lib/api";
import type { DirectorVoice as VoiceRecord, DirectorVoiceStatus } from "@/lib/director-voice";

// Stop a little before the decoded 60-second server limit to allow encoder tail frames.
const MAX_SECONDS = 60, STOP_AT_MS = 59000, MAX_BYTES = 12 * 1024 * 1024;
type Phase = "idle" | "permission" | "recording" | "captured" | "uploading" | "waiting" | "ready" | "error";
type Capture = { file: File; clientId: string; seconds: number };
type Props = {
  episodeId: string; active: boolean; onTranscript: (text: string) => boolean; onBusyChange?: (busy: boolean) => void;
  compact?: boolean; historyOpen?: boolean; onHistoryClose?: () => void;
};
const message = (error: unknown) => error instanceof Error ? error.message : "语音暂时没能连接，请改用文字或重试。";
export function recordingMime(): string | null {
  if (typeof MediaRecorder === "undefined") return null;
  for (const mime of ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"]) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return null;
}

export function DirectorVoice({ episodeId, active, onTranscript, onBusyChange, compact = false, historyOpen = false, onHistoryClose }: Props) {
  const [status, setStatus] = useState<DirectorVoiceStatus | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);
  const [capture, setCapture] = useState<Capture | null>(null);
  const [record, setRecord] = useState<VoiceRecord | null>(null);
  const [history, setHistory] = useState<VoiceRecord[]>([]);
  const [transcript, setTranscript] = useState("");
  const [delivered, setDelivered] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [progress, setProgress] = useState(0);
  const stream = useRef<MediaStream | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const chunks = useRef<Blob[]>([]);
  const bytes = useRef(0);
  const started = useRef(0);
  const ticker = useRef<ReturnType<typeof setInterval> | null>(null);
  const generation = useRef(0);
  const mounted = useRef(false);
  const enabled = useRef(active);
  const busy = useRef(false);
  const requestingMicrophone = useRef(false);
  const transcriptCallback = useRef(onTranscript);
  const deliveredIds = useRef(new Set<string>());
  const savedCapture = useRef<Capture | null>(null);
  const currentRecord = useRef<VoiceRecord | null>(null);
  const unknown = useRef(false);
  const upload = useRef<AbortController | null>(null);
  const historyCloseButton = useRef<HTMLButtonElement>(null);
  const path = `/episodes/${episodeId}/director/voice`;
  useEffect(() => { transcriptCallback.current = onTranscript; }, [onTranscript]);
  const blocking = active && (["permission", "recording", "captured", "uploading", "waiting"].includes(phase) || uncertain);
  useEffect(() => { onBusyChange?.(blocking); }, [blocking, onBusyChange]);
  useEffect(() => () => { onBusyChange?.(false); }, [onBusyChange]);
  useEffect(() => { if (active && compact && historyOpen) historyCloseButton.current?.focus(); }, [active, compact, historyOpen]);

  const release = useCallback(() => {
    if (ticker.current) clearInterval(ticker.current);
    ticker.current = null;
    stream.current?.getTracks().forEach(track => track.stop()); stream.current = null;
  }, []);
  const cancelRecording = useCallback(() => {
    generation.current++;
    requestingMicrophone.current = false;
    const current = recorder.current; recorder.current = null;
    if (current) { current.ondataavailable = null; current.onstop = null; current.onerror = null; if (current.state !== "inactive") current.stop(); }
    chunks.current = []; bytes.current = 0; release();
  }, [release]);

  const fillDraft = useCallback((value: VoiceRecord, explicitlyRestore = false) => {
    setDelivered(false);
    if (!value.text?.trim()) return;
    if (!explicitlyRestore && deliveredIds.current.has(value.id)) { setDelivered(true); return; }
    if (!enabled.current || document.hidden) return;
    if (transcriptCallback.current(value.text.trim())) {
      deliveredIds.current.add(value.id); setDelivered(true); setError("");
    } else {
      setDelivered(false); setError("输入框还有待核对的发送，或合并后超过2000字。文字已保存，可稍后取回。");
    }
  }, []);
  const showRecord = useCallback((value: VoiceRecord, explicitlyRestore = false) => {
    currentRecord.current = value; setRecord(value); unknown.current = false; setUncertain(false); setError("");
    setHistory(items => [value, ...items.filter(item => item.id !== value.id)].slice(0, 20));
    if (value.state === "succeeded") { setTranscript(value.text || ""); setPhase("ready"); fillDraft(value, explicitlyRestore); }
    else if (value.state === "failed") { setPhase("error"); setError(value.error?.message || "转写未完成，请检查记录后再试。"); }
    else setPhase("waiting");
  }, [fillDraft]);

  const stop = useCallback(() => {
    const current = recorder.current;
    if (current && current.state !== "inactive") current.stop();
    release();
  }, [release]);

  useEffect(() => {
    if (!active || !compact || !historyOpen) return;
    if (recorder.current) stop();
    else if (requestingMicrophone.current) { cancelRecording(); setPhase("idle"); }
  }, [active, compact, historyOpen, stop, cancelRecording]);

  const begin = useCallback(async () => {
    if (!enabled.current || document.hidden || busy.current || unknown.current || (currentRecord.current && ["queued", "running"].includes(currentRecord.current.state))) return;
    if (!navigator.mediaDevices?.getUserMedia || window.isSecureContext === false) { setPhase("error"); setError("当前页面无法使用麦克风。请在本机浏览器或 HTTPS 页面打开，也可以先改用打字。"); return; }
    const mime = recordingMime();
    if (!mime) { setPhase("error"); setError("这个浏览器暂不支持录音，请用 Safari 或 Chrome，或改用打字。"); return; }
    cancelRecording(); const token = generation.current;
    requestingMicrophone.current = true;
    setPhase("permission"); setError(""); setSeconds(0); setCapture(null); savedCapture.current = null;
    setRecord(null); currentRecord.current = null; setTranscript(""); setDelivered(false);
    try {
      const input = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      if (!mounted.current || !enabled.current || document.hidden || token !== generation.current) { input.getTracks().forEach(track => track.stop()); return; }
      requestingMicrophone.current = false;
      stream.current = input;
      const media = new MediaRecorder(input, { mimeType: mime, audioBitsPerSecond: 64000 }); recorder.current = media;
      chunks.current = []; bytes.current = 0; started.current = Date.now();
      media.ondataavailable = event => {
        if (token !== generation.current || !event.data.size) return;
        bytes.current += event.data.size;
        if (bytes.current > MAX_BYTES) { cancelRecording(); setPhase("error"); setError("这段录音超过12MB，请分成更短的一句话。尚未上传。"); return; }
        chunks.current.push(event.data);
      };
      media.onerror = () => { cancelRecording(); setPhase("error"); setError("录音被中断，尚未上传。请重新录制或改用打字。"); };
      media.onstop = () => {
        if (!mounted.current || !enabled.current || token !== generation.current) return;
        release(); recorder.current = null;
        const blob = new Blob(chunks.current, { type: media.mimeType || mime }); chunks.current = [];
        if (!blob.size) { setPhase("error"); setError("没有录到声音，请检查麦克风后重试。"); return; }
        const extension = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
        const value = { file: new File([blob], `director-voice.${extension}`, { type: blob.type }), clientId: crypto.randomUUID(), seconds: Math.min(MAX_SECONDS, Math.max(1, Math.ceil((Date.now() - started.current) / 1000))) };
        savedCapture.current = value; setCapture(value); setPhase("captured");
      };
      media.start(250); setPhase("recording");
      ticker.current = setInterval(() => {
        const elapsed = Math.floor((Date.now() - started.current) / 1000); setSeconds(Math.min(elapsed, MAX_SECONDS));
        if (Date.now() - started.current >= STOP_AT_MS) stop();
      }, 250);
    } catch (cause) {
      if (!mounted.current || token !== generation.current) return;
      release();
      requestingMicrophone.current = false;
      const name = cause instanceof DOMException ? cause.name : "";
      setPhase("error"); setError(name === "NotAllowedError" ? "麦克风未获允许。可以在地址栏开启麦克风权限后再试，也可以直接打字。"
        : name === "NotFoundError" ? "没有找到可用的麦克风，请接入设备或改用打字。" : "麦克风暂时不能录音，请关闭其他占用麦克风的应用后再试。");
    }
  }, [cancelRecording, release, stop]);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; enabled.current = false; cancelRecording(); upload.current?.abort(); }; }, [cancelRecording]);
  useEffect(() => {
    enabled.current = active;
    if (!active) { cancelRecording(); upload.current?.abort(); return; }
    const controller = new AbortController(); let current = true;
    const leavePage = () => { if (recorder.current || requestingMicrophone.current) { cancelRecording(); setPhase(savedCapture.current ? "captured" : "idle"); setError("录音已停止，尚未自动上传。回来后点麦克风重新开始。"); } };
    const visible = () => { if (document.hidden) leavePage(); };
    document.addEventListener("visibilitychange", visible);
    window.addEventListener("pagehide", leavePage);
    void Promise.all([api<DirectorVoiceStatus>(path + "/status", { signal: controller.signal }), api<{ items: VoiceRecord[] }>(path, { signal: controller.signal })])
      .then(([ready, list]) => {
        if (!current) return;
        setStatus(ready); setHistory(list.items);
        if (unknown.current && savedCapture.current) return;
        const pending = list.items.find(item => ["queued", "running"].includes(item.state));
        if (pending) { showRecord(pending); return; }
        if (currentRecord.current) {
          const restored = list.items.find(item => item.id === currentRecord.current?.id); if (restored) showRecord(restored); return;
        }
        if (savedCapture.current) { setPhase("captured"); return; }
        if (!ready.can_record) { setPhase("error"); setError(ready.reason); return; }
        setPhase("idle");
      }).catch(cause => { if (current && !controller.signal.aborted) { setPhase("error"); setError(message(cause)); } });
    return () => { current = false; controller.abort(); document.removeEventListener("visibilitychange", visible); window.removeEventListener("pagehide", leavePage); cancelRecording(); };
  }, [active, path, cancelRecording, showRecord]);
  useEffect(() => {
    if (!active || !record || !["queued", "running"].includes(record.state)) return;
    let current = true; const controller = new AbortController();
    const timer = setInterval(() => {
      if (document.hidden) return;
      void api<VoiceRecord>(`${path}/${record.id}`, { signal: controller.signal }).then(value => { if (current) showRecord(value); })
        .catch(cause => { if (current && !controller.signal.aborted) setError(message(cause)); });
    }, 1500);
    return () => { current = false; clearInterval(timer); controller.abort(); };
  }, [active, record, path, showRecord]);

  async function reconcile() {
    const saved = savedCapture.current;
    if (!saved || busy.current || !active) return;
    busy.current = true; setError("");
    try { showRecord(await api<VoiceRecord>(`${path}/by-client/${saved.clientId}`)); }
    catch (cause) {
      if (!mounted.current) return;
      if (cause instanceof AppError && cause.status === 404) { unknown.current = false; setUncertain(false); setPhase("captured"); setError("服务端没有这段记录，录音仍保留；可重新点转成文字，不会自动重传。"); }
      else setError(message(cause));
    } finally { busy.current = false; }
  }
  async function submit() {
    if (!active || busy.current || !capture || uncertain || !status?.can_record) return;
    busy.current = true; setPhase("uploading"); setError(""); setProgress(0);
    const controller = new AbortController(); upload.current = controller;
    try {
      const value = await uploadDirectorVoice(episodeId, capture.file, { client_id: capture.clientId, confirm_send: true, accept_unknown_retention: true },
        (sent, total) => { if (mounted.current && total) setProgress(Math.round(sent / total * 100)); }, controller.signal);
      if (mounted.current) showRecord(value);
    } catch (cause) {
      if (!mounted.current) return;
      unknown.current = cause instanceof AppError && cause.uncertain; setUncertain(unknown.current); setPhase(unknown.current ? "error" : "captured"); setError(message(cause));
    } finally { busy.current = false; upload.current = null; }
  }
  function cancel() {
    cancelRecording(); savedCapture.current = null; setCapture(null); setSeconds(0); setPhase("idle"); setError("录音已取消，没有上传。");
  }
  const listening = phase === "recording";
  const canBegin = phase === "idle" || (phase === "error" && !uncertain) || phase === "ready";
  const showTray = phase !== "idle" || !!error || uncertain;
  const historyItems = <ul>{history.map((item, index) => <li key={item.id}><button type="button" disabled={phase === "uploading" || uncertain} onClick={() => {
    cancelRecording(); savedCapture.current = null; setCapture(null); showRecord(item, true); onHistoryClose?.();
  }}><span>{item.state === "succeeded" ? item.text || "没有识别到文字" : item.state === "failed" ? "转写未完成" : "正在转写"}</span><small>{item.mode === "demo" ? "演示 · " : ""}记录 {history.length - index}</small></button></li>)}</ul>;
  return <div className={`director-voice-inline${compact ? " compact" : ""}`} hidden={!active}>
    {compact && <button type="button" className={`director-voice-mic${listening ? " recording" : ""}`} disabled={!status?.can_record || (!canBegin && !listening)} inert={historyOpen} aria-hidden={historyOpen || undefined}
      onClick={() => { if (listening) stop(); else void begin(); }} aria-label={listening ? "结束录音" : record?.state === "failed" ? "重新录一句" : "语音输入"}>
      {listening ? <Square size={19} fill="currentColor" /> : ["permission", "uploading", "waiting"].includes(phase) ? <LoaderCircle className="director-chat-spin" size={23} /> : <Mic size={23} />}
    </button>}
    {(!compact || showTray) && <div className={compact ? "director-voice-tray" : undefined} inert={compact && historyOpen} aria-hidden={compact && historyOpen || undefined}>
    <div className="director-voice-row">
      {!compact && canBegin && <button type="button" className="director-voice-mic" disabled={!status?.can_record} onClick={() => void begin()} aria-label={record?.state === "failed" ? "重新录一句" : "语音输入"}><Mic size={18} /><span>{record?.state === "failed" ? "重新录一句" : "语音"}</span></button>}
      {(phase === "permission" || phase === "uploading" || phase === "waiting") && <LoaderCircle className="director-chat-spin" size={15} aria-label="语音处理中" />}
      <span className="director-voice-status" role="status">{listening ? `正在听 · ${seconds} 秒` : phase === "permission" ? "请允许麦克风" : phase === "captured" ? `已录 ${capture?.seconds || 0} 秒 · 未发送` : phase === "uploading" ? `上传中 ${progress}%` : phase === "waiting" ? "转写中…" : phase === "ready" ? delivered ? "文字已放到输入框，可修改后发送" : "转写文字已保存" : ""}</span>
      {listening && <><button className="director-voice-stop" type="button" onClick={stop}><Square size={13} fill="currentColor" />说完了</button><button className="director-voice-link" type="button" onClick={cancel}>取消录音</button></>}
      {phase === "permission" && <button className="director-voice-link" type="button" onClick={cancel}>取消</button>}
      {phase === "captured" && <><button className="director-chat-primary" type="button" onClick={() => void submit()} disabled={!status?.can_record || uncertain}>同意并转成文字</button><button className="director-voice-link" type="button" onClick={() => void begin()} disabled={uncertain || !status?.can_record} aria-label="重新录制"><RotateCcw size={14} /></button><button className="director-voice-link" type="button" onClick={cancel}>取消</button></>}
      {uncertain && <button className="director-voice-link" type="button" onClick={() => void reconcile()}>核对提交状态</button>}
    </div>
    {phase === "captured" && <details className="director-voice-consent"><summary>本段将交给 MiniMax，供应商留存期限未知</summary><p>点击“同意并转成文字”仅授权本段录音转写，计入本期预算。应用内记录随本期到期删除；没提交的录音刷新后不保留。</p></details>}
    {error && <p className="director-voice-error" role="alert">{error}</p>}
    {phase === "ready" && !delivered && <div className="director-voice-recovery"><p>{transcript}</p><button className="director-voice-link" type="button" disabled={!transcript.trim()} onClick={() => { if (record) fillDraft(record, true); }}>放入输入框</button></div>}
    {phase === "ready" && record?.mode === "demo" && <small>固定演示文本，非真实语音识别。</small>}
    </div>}
    {compact ? historyOpen && <section className="director-voice-history-sheet" aria-label="最近语音">
      <header><h3>最近语音</h3><button ref={historyCloseButton} type="button" className="director-chat-icon" onClick={onHistoryClose} aria-label="返回导演对话"><X size={18} /></button></header>
      {history.length ? historyItems : <p>还没有语音记录。转过的文字会保留在这里。</p>}
    </section> : !!history.length && <details className="director-voice-history"><summary>最近语音 · {history.length}</summary>{historyItems}</details>}
  </div>;
}
