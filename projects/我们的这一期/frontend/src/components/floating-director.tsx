"use client";

import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ArrowRight, ArrowUp, ChevronDown, Clock3, LoaderCircle, Minus, RefreshCw, Repeat2, X } from "lucide-react";
import { Button, Dialog, MediaPlayer } from "./ui";
import { DirectorOrb } from "./director-orb";
import { DirectorVoice } from "./director-voice";
import { DirectorReply } from "./director-reply";
import { api, AppError } from "@/lib/api";
import type { Episode, Episodes, Job, Status } from "@/lib/types";
import type { DirectorChatActionResult, DirectorChatContext, DirectorChatProposal, DirectorChatState } from "@/lib/director-chat";

type SendRequest = DirectorChatContext & { client_id: string; text: string; confirm_send: true };
type ReviewFocus = { versionId: string; cueId: string; label: string };
type ReviewRequest = ReviewFocus & { id: string };
type UnsentDraft = { text: string; request?: SendRequest; reviewFocus?: ReviewFocus };
type Memory = { owner: string; open: boolean; episodeId: string; drafts: Map<string, UnsentDraft> };
let memory: Memory | null = null;

/** Only unsent text and UI position live in memory. Server history is never cached here. */
export function clearDirectorChatMemory() { memory = null; }
function memoryFor(owner: string): Memory {
  if (!memory || memory.owner !== owner) memory = { owner, open: false, episodeId: "", drafts: new Map() };
  return memory;
}
function rememberDraft(owner: string, episodeId: string, value: UnsentDraft) {
  const current = memoryFor(owner);
  current.drafts.set(episodeId, value);
  if (current.drafts.size > 30) current.drafts.delete(current.drafts.keys().next().value!);
}
const errorText = (error: unknown) => error instanceof Error ? error.message : "暂时没能连接导演，请稍后重试。";
const episodeFromPath = (pathname: string) => /^\/episodes\/([a-f0-9]{32})(?:\/|$)/.exec(pathname)?.[1] || "";
function routeContext(pathname: string): DirectorChatContext {
  const query = new URLSearchParams(window.location.search);
  const rawStep = query.get("step");
  const step = pathname.endsWith("/collaboration") ? "collaboration" : pathname.endsWith("/sharing") ? "sharing"
    : episodeFromPath(pathname) ? (["materials", "direction", "edit", "preview"].includes(rawStep || "") ? rawStep as DirectorChatContext["step"] : "materials") : "home";
  const version = query.get("version");
  return { step, ...(version && /^[a-f0-9]{32}$/.test(version) ? { version_id: version } : {}) };
}

export type FloatingDirectorProps = { ownerId: string; status: Status | null; allowed: boolean };
export function FloatingDirector(props: FloatingDirectorProps) {
  return <DirectorWidget key={props.ownerId} {...props} />;
}

function DirectorWidget({ ownerId, status, allowed }: FloatingDirectorProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(() => memoryFor(ownerId).open);
  const [chosenId, setChosenId] = useState(() => memoryFor(ownerId).episodeId);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [listing, setListing] = useState(false);
  const [listError, setListError] = useState("");
  const [theme, setTheme] = useState("");
  const [creating, setCreating] = useState(false);
  const [createUnknown, setCreateUnknown] = useState(false);
  const [context, setContext] = useState<DirectorChatContext>({ step: "home" });
  const [refreshTick, setRefreshTick] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [utility, setUtility] = useState<"" | "episodes" | "voices">("");
  const [feedbackCue, setFeedbackCue] = useState(0);
  const [reviewRequest, setReviewRequest] = useState<ReviewRequest | null>(null);
  const consumeReview = useCallback(() => setReviewRequest(null), []);
  const launcher = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const utilityClose = useRef<HTMLButtonElement>(null);
  const dock = useRef<HTMLDivElement>(null);
  const dirty = useRef(false);
  const creatingNow = useRef(false);
  const id = useId();
  const routeEpisode = episodeFromPath(pathname);
  const episodeId = routeEpisode || chosenId;

  useEffect(() => {
    const review = (event: Event) => {
      const value = (event as CustomEvent<ReviewFocus>).detail;
      if (!allowed || !routeEpisode || !value || value.versionId !== routeContext(window.location.pathname).version_id
          || !/^[a-f0-9]{32}$/.test(value.cueId) || typeof value.label !== "string" || value.label.length > 80) return;
      setReviewRequest({...value,id:crypto.randomUUID()});
      setOpen(true); setMenuOpen(false); setUtility("");
    };
    window.addEventListener("director:review-cue", review);
    return () => window.removeEventListener("director:review-cue", review);
  }, [allowed, routeEpisode]);
  useEffect(() => {
    const update = () => setContext(routeContext(window.location.pathname));
    update();
    window.addEventListener("episode:location", update);
    window.addEventListener("popstate", update);
    return () => { window.removeEventListener("episode:location", update); window.removeEventListener("popstate", update); };
  }, [pathname]);
  useEffect(() => {
    const update = (event: Event) => { dirty.current = Boolean((event as CustomEvent<boolean>).detail); };
    const clear = () => { clearDirectorChatMemory(); setOpen(false); setMenuOpen(false); setUtility(""); setChosenId(""); setEpisodes([]); };
    window.addEventListener("episode:dirty", update);
    window.addEventListener("episode:auth-expired", clear);
    window.addEventListener("director:reset", clear);
    return () => { window.removeEventListener("episode:dirty", update); window.removeEventListener("episode:auth-expired", clear); window.removeEventListener("director:reset", clear); };
  }, []);
  useEffect(() => {
    const current = memoryFor(ownerId); current.open = open; current.episodeId = episodeId;
  }, [ownerId, open, episodeId]);
  useEffect(() => {
    if (!open || !allowed) return;
    closeButton.current?.focus();
  }, [open, allowed]);
  useEffect(() => {
    if (!open || !allowed) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented && dock.current?.contains(document.activeElement)) {
        event.preventDefault();
        if (menuOpen) setMenuOpen(false);
        else if (utility) setUtility("");
        else setOpen(false);
        launcher.current?.focus();
      }
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [open, allowed, menuOpen, utility]);
  useEffect(() => {
    if (!menuOpen) return;
    menu.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
    const dismiss = (event: PointerEvent) => {
      if (!menu.current?.contains(event.target as Node) && !launcher.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [menuOpen]);
  useEffect(() => { if (utility === "episodes") utilityClose.current?.focus(); }, [utility]);
  useEffect(() => {
    const viewport = window.visualViewport;
    const fit = () => {
      const element = dock.current;
      if (!element) return;
      if (viewport && window.innerHeight - viewport.height > 120) {
        element.style.setProperty("--director-available-height", `${Math.max(180, viewport.height - 28)}px`);
        element.style.bottom = `${Math.max(12, window.innerHeight - viewport.height - viewport.offsetTop + 12)}px`;
      } else { element.style.removeProperty("--director-available-height"); element.style.removeProperty("bottom"); }
    };
    fit(); viewport?.addEventListener("resize", fit); viewport?.addEventListener("scroll", fit);
    return () => { viewport?.removeEventListener("resize", fit); viewport?.removeEventListener("scroll", fit); };
  }, []);
  useEffect(() => {
    if (!open || !allowed) return;
    const controller = new AbortController();
    let current = true;
    queueMicrotask(() => {
      if (!current) return;
      setListing(true); setListError("");
      api<Episodes>("/episodes", { signal: controller.signal }).then(result => {
        if (!current) return;
        setEpisodes(result.items);
        if (!routeEpisode && chosenId && !result.items.some(item => item.id === chosenId)) setChosenId("");
      }).catch(error => { if (current && !controller.signal.aborted) setListError(errorText(error)); })
        .finally(() => { if (current) setListing(false); });
    });
    return () => { current = false; controller.abort(); };
  }, [allowed, open, routeEpisode, chosenId, refreshTick]);

  function close() { setOpen(false); setMenuOpen(false); setUtility(""); launcher.current?.focus(); }
  function closeUtility() { setUtility(""); launcher.current?.focus(); }
  function launch() {
    if (open) { setMenuOpen(previous => !previous); return; }
    setOpen(true);
  }
  function showUtility(value: "episodes" | "voices") { setMenuOpen(false); setUtility(value); }
  function chooseEpisode(nextId: string) {
    if (routeEpisode && nextId !== routeEpisode) {
      if (dirty.current && !window.confirm("当前页面有未保存的修改。切换节目？已保存内容会保留。")) return;
      router.push(nextId ? `/episodes/${nextId}?step=materials` : "/");
    }
    setChosenId(nextId); setCreateUnknown(false); setUtility(""); launcher.current?.focus();
  }
  function navigate(result: DirectorChatActionResult) {
    if (!result.next_step || !episodeId) return;
    const event = new CustomEvent("director:navigate", { cancelable: true, detail: {
      episodeId, step: result.next_step, versionId: result.version_id || undefined,
    } });
    window.dispatchEvent(event);
    if (event.defaultPrevented) return;
    if (dirty.current && !window.confirm("当前页面有未保存的修改。离开前往导演建议的位置？已保存内容会保留。")) return;
    const query = new URLSearchParams({ step: result.next_step });
    if (result.version_id) query.set("version", result.version_id);
    router.push(`/episodes/${episodeId}?${query}`);
  }
  async function create(event: FormEvent) {
    event.preventDefault();
    if (!allowed || creatingNow.current || !theme.trim() || createUnknown) return;
    creatingNow.current = true; setCreating(true); setListError("");
    try {
      const episode = await api<Episode>("/episodes", { method: "POST", body: { title: theme.trim(), background: theme.trim() } });
      setEpisodes(previous => [...previous, episode]); setChosenId(episode.id); setTheme("");
      window.dispatchEvent(new CustomEvent("director:updated", { detail: { episodeId: episode.id } }));
    } catch (error) {
      setListError(errorText(error));
      if (error instanceof AppError && error.uncertain) { setCreateUnknown(true); setRefreshTick(value => value + 1); }
    } finally { creatingNow.current = false; setCreating(false); }
  }
  const selected = episodes.find(episode => episode.id === episodeId);
  const visible = open && allowed;
  return <div ref={dock} className={`director-dock${visible ? " open" : ""}`}>
    <aside ref={panel} id={id} role="dialog" aria-modal="false" aria-labelledby={`${id}-title`} aria-hidden={!visible}
      className="director-chat-panel" inert={!visible}>
      <header className="director-chat-header">
        <div className="director-chat-heading"><h2 id={`${id}-title`}>在场导演</h2>
          <button type="button" className="director-chat-episode" onClick={() => showUtility("episodes")} aria-label="切换导演对话的节目">
            <span>{selected?.title || (routeEpisode ? "当前节目" : "选择一期节目")}</span><ChevronDown size={12} aria-hidden="true" />
          </button>
        </div>
        <button ref={closeButton} type="button" className="director-chat-icon" aria-label="关闭导演对话" onClick={close}><Minus size={20} /></button>
      </header>
      {status?.model_mode === "demo" && <p className="director-chat-demo">独立演示 · 固定回复，非真实 AI</p>}
      {listError && <div className="director-chat-notice" role="alert">{listError}<button type="button" onClick={() => setRefreshTick(value => value + 1)} disabled={listing}>刷新节目列表</button></div>}
      {episodeId ? <div key={`${ownerId}:${episodeId}`} className="director-mode-body" inert={utility === "episodes"}>
        <DirectorConversation ownerId={ownerId} episodeId={episodeId} active={visible && utility !== "episodes"} context={context}
          budget={selected?.budget} onNavigate={navigate} feedbackCue={feedbackCue} reviewRequest={reviewRequest} onReviewHandled={consumeReview}
          historyOpen={visible && utility === "voices"} onHistoryClose={closeUtility} />
      </div> : <div className="director-chat-start" inert={!!utility}>
        {listing ? <p role="status">正在找回你的节目…</p> : <><h3>这一期，先聊个主题</h3><p>选一期已有节目，或说个主题新开一期。</p>
          {!!episodes.length && <button type="button" className="director-chat-existing" onClick={() => showUtility("episodes")}>选择已有节目<ArrowRight size={14} /></button>}
          <form onSubmit={create}><label htmlFor={`${id}-theme`}>新一期的主题</label><input id={`${id}-theme`} value={theme} maxLength={120} onChange={event => setTheme(event.target.value)} placeholder="比如：说好今晚不点外卖" disabled={creating || createUnknown} />
            <button className="director-chat-primary" type="submit" disabled={!theme.trim() || creating || createUnknown}>{creating ? "正在新建…" : "新建这期，开始聊"}<ArrowRight size={16} /></button></form>
          {createUnknown && <div role="status"><p>创建结果尚未确认，请先刷新并从节目列表选取已创建的节目，避免重复新建。</p>{!listing && !listError && !episodes.some(episode => episode.title === theme.trim()) && <button type="button" className="director-chat-primary" onClick={() => setCreateUnknown(false)}>已核对没有创建，允许重试</button>}</div>}
          <small>新建只保存主题，不会自动调用模型。</small></>}
      </div>}
      {utility === "episodes" && <section className="director-utility-sheet" aria-label="选择导演节目">
        <header><h3>这一回，聊哪一期？</h3><button ref={utilityClose} type="button" aria-label="返回导演对话" onClick={closeUtility}><X size={18} /></button></header>
        <p>各期对话分别保留。</p>
        {listing && <p role="status">正在找回你的节目…</p>}
        <label className="sr-only" htmlFor={`${id}-episodes`}>选择导演对话的节目</label>
        <select id={`${id}-episodes`} value={episodeId} disabled={listing || creating} onChange={event => chooseEpisode(event.target.value)}>
          <option value="">新开一期</option>{episodes.map(episode => <option key={episode.id} value={episode.id}>{episode.title}</option>)}
        </select>
        {!listing && !episodes.length && <p>还没有节目，可以先说个主题。</p>}
      </section>}
      {menuOpen && <div ref={menu} id={`${id}-menu`} className="director-orb-menu" aria-label="导演功能">
        <p>这一期，我在</p>
        <button type="button" disabled={!episodeId} onClick={() => { setMenuOpen(false); setUtility(""); setFeedbackCue(value => value + 1); }}>聊聊这一版<ArrowRight size={15} /></button>
        <button type="button" onClick={() => showUtility("episodes")}>换一期节目<Repeat2 size={15} /></button>
        <button type="button" disabled={!episodeId} onClick={() => showUtility("voices")}>最近语音<Clock3 size={15} /></button>
        <button type="button" onClick={close}>收起导演<Minus size={15} /></button>
      </div>}
    </aside>
    <button ref={launcher} type="button" className={`director-fab${visible ? " active" : ""}`} aria-label={visible ? "打开导演功能" : "打开AI导演"}
      aria-expanded={visible ? menuOpen : false} aria-controls={visible ? `${id}-menu` : id} aria-haspopup={visible ? undefined : "dialog"}
      disabled={!allowed} onClick={launch} title={visible ? "导演功能" : "点我，和导演说句话"}>
      <DirectorOrb /><span className="director-fab-hint">导演在这儿</span>
    </button>
  </div>;
}

type ConversationProps = { ownerId: string; episodeId: string; active: boolean; context: DirectorChatContext; budget?: Episode["budget"]; onNavigate: (result: DirectorChatActionResult) => void; feedbackCue: number; reviewRequest: ReviewRequest | null; onReviewHandled: () => void; historyOpen: boolean; onHistoryClose: () => void };
function DirectorConversation({ ownerId, episodeId, active, context, budget, onNavigate, feedbackCue, reviewRequest, onReviewHandled, historyOpen, onHistoryClose }: ConversationProps) {
  const [text, setText] = useState(() => memoryFor(ownerId).drafts.get(episodeId)?.text || "");
  const [state, setState] = useState<DirectorChatState | null>(null);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [busy, setBusy] = useState("");
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [reviewFocus, setReviewFocus] = useState<ReviewFocus | undefined>(() => memoryFor(ownerId).drafts.get(episodeId)?.reviewFocus);
  const seenReview = useRef<string | null>(null);
  const [hasRequest, setHasRequest] = useState(() => !!memoryFor(ownerId).drafts.get(episodeId)?.request);
  const request = useRef<SendRequest | undefined>(memoryFor(ownerId).drafts.get(episodeId)?.request);
  const working = useRef(false);
  const alive = useRef(false);
  const enabled = useRef(active);
  const readGeneration = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const history = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const seenFeedback = useRef(feedbackCue);
  const inputId = useId();
  const path = `/episodes/${episodeId}/director/chat`;
  const invalidateRead = useCallback(() => { readGeneration.current++; readController.current?.abort(); }, []);
  const load = useCallback(async () => {
    if (!enabled.current || !alive.current) return;
    const generation = ++readGeneration.current;
    readController.current?.abort(); const controller = new AbortController(); readController.current = controller;
    setLoading(true);
    try {
      const value = await api<DirectorChatState>(path, { signal: controller.signal });
      if (!alive.current || generation !== readGeneration.current) return;
      setState(value); setReadError("");
      const sent = request.current;
      if (sent && value.messages.some(message => message.role === "user" && message.client_id === sent.client_id)) {
        const draft = memoryFor(ownerId).drafts.get(episodeId)?.text || "";
        const retained = draft.trim() === sent.text ? "" : draft;
        rememberDraft(ownerId, episodeId, { text: retained }); request.current = undefined; setHasRequest(false);
        setText(retained); setReviewFocus(undefined); setActionError("");
      }
    } catch (error) {
      if (alive.current && generation === readGeneration.current && !controller.signal.aborted) setReadError(errorText(error));
    } finally { if (alive.current && generation === readGeneration.current) setLoading(false); }
  }, [path, ownerId, episodeId]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; invalidateRead(); }; }, [invalidateRead]);
  useEffect(() => {
    const clear = () => { readGeneration.current++; readController.current?.abort(); request.current = undefined; setHasRequest(false); setState(null); setText(""); setReviewFocus(undefined); setReadError(""); setActionError(""); };
    window.addEventListener("episode:auth-expired", clear); window.addEventListener("director:reset", clear);
    return () => { window.removeEventListener("episode:auth-expired", clear); window.removeEventListener("director:reset", clear); };
  }, []);
  useEffect(() => {
    enabled.current = active;
    if (!active) { invalidateRead(); return; }
    let current = true;
    queueMicrotask(() => { if (current) void load(); });
    const update = (event: Event) => { if ((event as CustomEvent<{ episodeId?: string }>).detail?.episodeId === episodeId) void load(); };
    window.addEventListener("director:updated", update);
    return () => { current = false; enabled.current = false; window.removeEventListener("director:updated", update); invalidateRead(); };
  }, [active, load, episodeId, invalidateRead]);
  const reviewing = !!state?.proposals.some(p => p.kind === "review_sequence" && ["queued", "running"].includes(p.review_state ?? ""));
  useEffect(() => {
    if (!active || (!state?.pending_job_id && !reviewing) || readError) return;
    const timer = window.setInterval(() => { void load(); }, 1500);
    return () => window.clearInterval(timer);
  }, [active, state?.pending_job_id, reviewing, readError, load]);
  useEffect(() => { if (active && history.current) history.current.scrollTop = history.current.scrollHeight; }, [active, state?.messages.length]);
  useEffect(() => {
    if (!text.trim()) return;
    const leave = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", leave);
    return () => window.removeEventListener("beforeunload", leave);
  }, [text]);
  const appendTranscript = useCallback((value: string) => {
    if (!alive.current || !enabled.current || working.current || request.current || memory?.owner !== ownerId) return false;
    const draft = memoryFor(ownerId).drafts.get(episodeId);
    if (draft?.request) return false;
    const combined = draft?.text.trim() ? draft.text.trim() + "\n" + value : value;
    if (Array.from(combined).length > 2000) return false;
    rememberDraft(ownerId, episodeId, { text: combined, reviewFocus: draft?.reviewFocus });
    setText(combined); setActionError(""); input.current?.focus();
    return true;
  }, [ownerId, episodeId]);
  useEffect(() => {
    if (seenFeedback.current === feedbackCue) return;
    seenFeedback.current = feedbackCue;
    queueMicrotask(() => {
      if (!alive.current || !enabled.current) return;
      if (!appendTranscript("这版有哪些地方可以改得更好？")) setActionError("先核对上一条发送结果，再补充想法。");
    });
  }, [feedbackCue, appendTranscript]);
  useEffect(() => {
    if (!active || !reviewRequest || seenReview.current === reviewRequest.id) return;
    seenReview.current = reviewRequest.id;
    queueMicrotask(() => {
      onReviewHandled();
      if (!alive.current || !enabled.current || reviewRequest.versionId !== context.version_id) return;
      const draft = memoryFor(ownerId).drafts.get(episodeId);
      if (working.current || request.current || draft?.text.trim()) {
        setActionError("输入框已有内容，先发送或清空，再点「让导演看这句」。"); return;
      }
      const focus = {versionId:reviewRequest.versionId,cueId:reviewRequest.cueId,label:reviewRequest.label};
      const question = "帮我看看这句字幕，有必要改吗？先给建议，不直接改片。";
      rememberDraft(ownerId,episodeId,{text:question,reviewFocus:focus});
      setReviewFocus(focus); setText(question); setActionError(""); input.current?.focus();
    });
  }, [active, reviewRequest, context.version_id, ownerId, episodeId, onReviewHandled]);
  useEffect(() => {
    const element = input.current;
    if (!element) return;
    const fit = () => { element.style.height = "40px"; if (text) element.style.height = `${Math.max(40, Math.min(96, element.scrollHeight))}px`; };
    fit();
    if (typeof ResizeObserver === "undefined") return;
    let width = element.clientWidth;
    const observer = new ResizeObserver(() => { if (width !== element.clientWidth) { width = element.clientWidth; fit(); } });
    observer.observe(element);
    return () => observer.disconnect();
  }, [text, active]);
  function edit(value: string) {
    if (working.current || request.current) return;
    setText(value); setActionError("");
    rememberDraft(ownerId, episodeId, { text: value, request: request.current, reviewFocus: value.trim() ? reviewFocus : undefined });
    if (!value.trim()) setReviewFocus(undefined);
  }
  const pending = !!state?.pending_job_id;
  const blocked = !active || !!busy || voiceBusy || pending || reviewing || !!readError;
  async function send(event: FormEvent) {
    event.preventDefault();
    if (working.current || blocked || !state?.can_chat || !text.trim() || Array.from(text).length > 2000) return;
    if (reviewFocus && reviewFocus.versionId !== context.version_id && !request.current) { setActionError("已切换版本，请清空这条提问后在当前版本重新选择字幕。"); return; }
    const submitted = text.trim();
    if (!request.current || request.current.text !== submitted) request.current = { ...context, ...(reviewFocus ? {audio_focus_cue_id:reviewFocus.cueId} : {}), client_id: crypto.randomUUID(), text: submitted, confirm_send: true };
    const payload = request.current;
    setHasRequest(true);
    rememberDraft(ownerId, episodeId, { text, request: payload, reviewFocus });
    working.current = true; setBusy("send"); setActionError("");
    try {
      const job = await api<Job>(path, { method: "POST", body: payload });
      let retained = "";
      if (memory?.owner === ownerId) {
        const current = memoryFor(ownerId).drafts.get(episodeId)?.text || "";
        retained = current.trim() === payload.text ? "" : current;
        rememberDraft(ownerId, episodeId, { text: retained });
      }
      request.current = undefined;
      if (!alive.current) return;
      setHasRequest(false);
      setText(retained); setReviewFocus(undefined); setState(previous => previous ? { ...previous, pending_job_id: ["queued", "running"].includes(job.state) ? job.id : null, last_job_id: job.id, last_error: job.error } : previous);
      await load(); input.current?.focus();
    } catch (error) {
      if (!alive.current) return;
      setActionError(errorText(error));
      if (!(error instanceof AppError && error.uncertain)) { request.current = undefined; setHasRequest(false); rememberDraft(ownerId, episodeId, { text, reviewFocus }); }
      await load();
    } finally { working.current = false; if (alive.current) setBusy(""); }
  }
  async function retry() {
    if (working.current || blocked || !state?.last_job_id) return;
    working.current = true; setBusy("retry"); setActionError("");
    try { await api<Job>(`/jobs/${state.last_job_id}/retry`, { method: "POST", body: {} }); await load(); }
    catch (error) { if (alive.current) setActionError(errorText(error)); await load(); }
    finally { working.current = false; if (alive.current) setBusy(""); }
  }
  async function apply(proposal: DirectorChatProposal) {
    if (working.current || blocked || proposal.status !== "pending") return;
    working.current = true; setBusy(proposal.id); setActionError("");
    try {
      const result = await api<DirectorChatActionResult>(`/episodes/${episodeId}/director/actions/${proposal.id}`, { method: "POST", body: { confirm: true } });
      if (!alive.current) return;
      await load();
      window.dispatchEvent(new CustomEvent("director:updated", { detail: { episodeId } }));
      if (proposal.kind !== "review_sequence") onNavigate(result);
    } catch (error) { if (alive.current) setActionError(errorText(error)); await load(); }
    finally { working.current = false; if (alive.current) setBusy(""); }
  }
  const labels: Record<DirectorChatProposal["kind"], string> = { revise_directions: "确认要求并生成新方案", revise_narration: "确认文案，另存旁白草案", revise_subtitles: "确认改字并生成新版", revise_cut: "按这个剪法生成新版", review_sequence: "查看这段动作", navigate: "去看看" };
  const cost = `本期上限${budget?.limit ?? 3}元，累计${budget?.total_limit ?? 30}元，含修改和重试。`;
  return <div className="director-conversation-body" aria-busy={loading || !!busy || pending}>
    <div ref={history} className={`director-chat-history${state && !state.messages.length ? " empty" : ""}`} aria-label="导演对话记录" inert={historyOpen}>
      {!state && loading && <p className="director-chat-loading" role="status">正在取回这期的对话…</p>}
      {state && !state.messages.length && <div className="director-chat-empty"><h3>这一期，想怎么拍？<span aria-hidden="true">✳</span></h3><p>说个想法，剩下的我们一起理。</p><div className="director-chat-suggestions">
        {[["帮我找故事", "帮我从这些素材里找到一个连贯的故事。"], ["这版怎么改", "这版有哪些地方可以改得更好？"], ["旁白短一点", "旁白再短一点，语气干脆些。"], ["剪得更热闹", "我希望剪得更热闹，但保留完整原声。"]].map(([label, value]) => <button type="button" key={label} disabled={blocked || hasRequest} onClick={() => appendTranscript(value)}>{label}</button>)}
      </div></div>}
      {state?.messages.map(message => <article className={`director-chat-message ${message.role}`} key={message.id}>
        <span className="director-chat-speaker">{message.role === "user" ? "你" : message.source === "demo" ? "演示回复" : "AI 导演"}</span>
        {message.role === "assistant" ? <DirectorReply text={message.text} /> : <p>{message.text}</p>}
        {state.proposals.filter(proposal => proposal.message_id === message.id).map(proposal => <section className="director-action-card" key={proposal.id} aria-label={`导演建议：${proposal.title}`}>
          <span className="director-action-label">{proposal.kind === "review_sequence" && proposal.status === "applied" ? "动作观察" : proposal.kind === "navigate" ? "下一步" : "等你确认，再动手"}</span><h3>{proposal.title}</h3>{proposal.kind !== "revise_cut" && <p>{proposal.explanation}</p>}
          {proposal.requirement && <blockquote>{proposal.requirement}</blockquote>}{proposal.narration_text != null && <blockquote aria-label="建议的新旁白">{proposal.narration_text || "移除旁白，保留现场原声。"}</blockquote>}
          {proposal.kind === "revise_cut" && <><p>镜头内容约 {Math.round(proposal.before_seconds ?? 0)} 秒 → {Math.round(proposal.after_seconds ?? 0)} 秒</p><p>保留 {Array.from(new Set(proposal.cut_changes?.filter(c=>!c.removed).map(c=>c.shot))).join("、")} 段{proposal.cut_changes?.some(c=>c.removed) ? "，部分画面与原声不进入新版" : "，按原顺序衔接"}</p><details><summary>查看剪法与影响</summary><p>{proposal.explanation}</p><div aria-label="镜头调整安排">{proposal.cut_changes?.map((change,index)=><blockquote key={index}><small>{change.removed ? "不放入新版" : "保留"} · 原版第{change.shot}段</small><p>{change.reason}</p>{proposal.base_version_id && <SubtitleProposalReplay versionId={proposal.base_version_id} start={change.start} end={change.end} disabled={blocked || proposal.status === "stale"} cut />}</blockquote>)}</div></details><small>另存候选，旧版保留，不新增模型费用。</small></>}
          {proposal.kind === "revise_subtitles" && <><div aria-label="字幕修改对照">{proposal.subtitle_changes?.map((change, index) => <blockquote key={index}><small>{Math.floor(change.start)}—{Math.ceil(change.end)}秒</small><p>原文：{change.before}</p><p>建议：{change.after}</p>{proposal.base_version_id && <SubtitleProposalReplay versionId={proposal.base_version_id} start={change.start} end={change.end} disabled={blocked || proposal.status === "stale"} />}</blockquote>)}</div><small>先回听确认文字。只改字幕并另存合成，原片和原声保留，不新增模型费用。</small></>}
          {proposal.kind === "review_sequence" && <><p>查看成片 {Math.floor(proposal.start ?? 0)}—{Math.ceil(proposal.end ?? 0)} 秒</p>
            {proposal.status === "pending" ? <small>发送这段画面的抽帧给模型，只看动作，不改片。{cost}</small> : proposal.status === "applied" && <div role="status">
              <p>{proposal.review_state === "queued" ? "已排队，稍后开始看这段。" : proposal.review_state === "running" ? "导演正在看这段动作…" : proposal.review_summary || "正在取回观察结果…"}</p>
              {proposal.review_state === "succeeded" && <small>抽样观察，不含声音；可以接着问导演怎么剪。</small>}
            </div>}</>}
          {proposal.kind === "revise_directions" && <small>会按以上要求重新生成方案。{cost}</small>}
          {proposal.kind === "revise_narration" && <small>另存为新草案；确认旁白声音与合成在下一步。</small>}
          {proposal.status === "pending" ? <button type="button" className="director-chat-primary" disabled={blocked} onClick={() => void apply(proposal)}>{busy === proposal.id ? "正在处理…" : labels[proposal.kind]}<ArrowRight size={15} /></button>
            : (proposal.kind !== "review_sequence" || proposal.status === "stale") && <span className="director-action-result">{proposal.status === "applied" ? "已按这条建议处理" : "节目已更新，这条建议不再适用"}</span>}
        </section>)}
      </article>)}
      {pending && <p className="director-chat-thinking" role="status"><LoaderCircle size={15} className="director-chat-spin" />导演正在结合本期内容思考…</p>}
      {state?.last_error && !pending && <div className="director-chat-notice" role="alert"><p>{state.last_error.message}</p><button type="button" disabled={blocked || !state.last_job_id} onClick={() => void retry()}>重新让导演回答</button></div>}
    </div>
    {(readError || actionError) && <div className="director-chat-notice" role="alert" inert={historyOpen}><p>{readError || actionError}</p><button type="button" disabled={loading || !active} onClick={() => void load()}><RefreshCw size={13} />重新读取对话</button></div>}
    {state && !state.can_chat && <p className="director-chat-unavailable" inert={historyOpen}>{state.reason || "真实导演对话暂未就绪，请先完成页面提示的配置。"}</p>}
    <form className="director-chat-composer" onSubmit={send}>
      {reviewFocus && <p className="director-chat-focus" role="status">正在讨论：{reviewFocus.label}</p>}
      <div className="director-chat-input-pill" inert={historyOpen}>
        <label className="sr-only" htmlFor={inputId}>和AI导演对话</label>
        <textarea ref={input} id={inputId} value={text} maxLength={2000} rows={1} onChange={event => edit(event.target.value)} disabled={!active || !!busy || hasRequest}
          placeholder="聊个想法，或直接说…" onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !window.matchMedia("(max-width: 680px)").matches) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
        <button className={`director-chat-send${text.trim() ? " has-text" : ""}`} type="submit" aria-label="发送给AI导演" title="发送" disabled={blocked || !state?.can_chat || !text.trim() || Array.from(text).length > 2000}>{busy === "send" ? <LoaderCircle size={18} className="director-chat-spin" /> : <ArrowUp size={18} />}</button>
      </div>
      <DirectorVoice episodeId={episodeId} active={active} onTranscript={appendTranscript} onBusyChange={setVoiceBusy}
        compact historyOpen={historyOpen} onHistoryClose={onHistoryClose} />
      {hasRequest && !busy && <p className="director-chat-disclosure" inert={historyOpen}>上一条发送结果待核对。可重新读取对话，或原样重试。</p>}
      <details className="director-chat-disclosure" inert={historyOpen}><summary>发送说明</summary><p>{state?.mode === "demo" ? "演示只返回固定内容，不理解素材、不调用模型。" : `发送会将本期已确认的素材说明和对话交给AI导演。${cost}`}</p></details>
    </form>
  </div>;
}


function SubtitleProposalReplay({ versionId, start, end, disabled, cut = false }: { versionId: string; start: number; end: number; disabled: boolean; cut?: boolean }) {
  const [open, setOpen] = useState(false);
  return <><Button disabled={disabled} onClick={() => setOpen(true)}>{cut ? "看看这一段" : "回听这处"}</Button>
    <Dialog open={open && !disabled} onClose={() => setOpen(false)} title={cut ? "查看建议对应的原片段" : "回听建议对应的原话"}>
      <MediaPlayer src={`/api/v1/versions/${versionId}/media`} title={cut ? "镜头建议片段" : "字幕建议原话"} start={start} end={end} />
    </Dialog></>;
}
