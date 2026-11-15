"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { ArrowRight, RefreshCw } from "lucide-react";
import { Button, Dialog, MediaAccessContext, Notice, useMediaAccess } from "@/components/ui";
import { api, AppError, checkExport, exportUrl, uploadAsset } from "@/lib/api";
import type { Asset, Direction, DirectorState, Episode, Evidence, Impact, Job, Packaging, Status, Version } from "@/lib/types";
import { DirectorDirections, DirectorGuideDock, hasCurrentDirections, type DirectorMessageInput } from "./studio/director-guide";
import styles from "./studio/workspace.module.css";
import { EpisodeRetention } from "./episode-retention";
import { DirectionPanel, EditorPanel, MaterialsPanel, PreviewPanel } from "./studio/panels";
import { readWorkspaceDraft, writeWorkspaceDraft } from "./studio/draft-cache";
import { blankDraft, hasNarratedIntro, costUncertain, draftFromVersion, draftProblem, evidenceProblem, isActiveJob, modelJob,
  segmentFromAsset, steps, stepTitles, type Draft, type Source, type Step, type UploadRow } from "./studio/model";

type Feedback = { text: string; error?: boolean } | null;
type Confirmation = { title: string; body: ReactNode; label: string; run: () => Promise<boolean> } | null;
const pathId = encodeURIComponent;
const errorText = (error: unknown) => error instanceof Error ? error.message : "操作没有完成，请刷新核对当前状态。";

function StudioWorkspace({ episodeId }: { episodeId: string }) {
  const [restored] = useState(() => readWorkspaceDraft(episodeId));
  const [retired, setRetired] = useState(false);
  const [episode, setEpisode] = useState<Episode | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [step, setStep] = useState<Step>("materials");
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [source, setSource] = useState<Source | null>(null);
  const [draft, setDraft] = useState<Draft>(() => restored?.draft || blankDraft());
  const [draftDirty, setDraftDirty] = useState(!!restored?.draft);
  const [evidence, setEvidence] = useState<Evidence[]>(() => restored?.evidence?.items || []);
  const [evidenceRevision, setEvidenceRevision] = useState(restored?.evidence?.revision || 0);
  const [evidenceDirty, setEvidenceDirty] = useState(!!restored?.evidence);
  const [directorDirty, setDirectorDirty] = useState(false);
  const directorRequest = useRef<{ signature: string; revision: number; client_id: string } | null>(null);
  const [packaging, setPackaging] = useState<Packaging>("variety_v2");
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [watched, setWatched] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState<string | null>(null);
  const [initialError, setInitialError] = useState("");
  const [mediaFresh, setMediaFresh] = useState(false);
  const mediaAccess = useMediaAccess();
  const mounted = useRef(false);
  const epoch = useRef(0);
  const readSequence = useRef(0);
  const readController = useRef<AbortController | null>(null);
  const uploadController = useRef<AbortController | null>(null);
  const working = useRef(false);
  const dirtyEvidence = useRef(!!restored?.evidence);
  const currentEpisode = useRef<Episode | null>(null);
  const currentVersion = useRef<string | null>(null);
  const initialized = useRef(false);
  const expired = useRef(false);

  const retire = useCallback(() => {
    epoch.current++; readController.current?.abort();
    setRetired(true); setEpisode(null); currentEpisode.current = null;
    setDraft(blankDraft()); setEvidence([]); setDraftDirty(false); setEvidenceDirty(false);
    setSource(null); setSelectedVersion(null); setUploads([]); setConfirmation(null);
    dirtyEvidence.current = false; setMediaFresh(false);
    uploadController.current?.abort();
    writeWorkspaceDraft(episodeId, {});
  }, [episodeId, setConfirmation]);

  const demo = status?.model_mode === "demo";
  const workflow = !!status && ["cloud", "demo"].includes(status.model_mode) && (episode?.workflow?.enabled ?? status.workflow_enabled ?? status.cloud_enabled);
  const blocked = !!busy || uncertain || !mediaFresh || !mediaAccess.allowed;
  const version = episode?.versions.find(item => item.id === selectedVersion);
  const running = episode?.jobs.some(isActiveJob) ?? false;
  const dirty = draftDirty || evidenceDirty || directorDirty;
  const viewKey = version ? `${version.id}:${version.media_url}:${version.state}:${version.evidence_revision}:${episode?.evidence_revision}` : "";

  const expireSession = useCallback(() => {
    expired.current = true;
    currentEpisode.current = null;
    setEpisode(null);
    setInitialError("登录已过期，请重新登录后打开这一期。");
  }, []);

  const putEpisode = useCallback((value: Episode) => {
    currentEpisode.current = value;
    setEpisode(value);
    if (!dirtyEvidence.current) {
      setEvidence(value.evidence.map(item => ({ ...item })));
      setEvidenceRevision(value.evidence_revision);
      setEvidenceDirty(false);
    }
    setSource(previous => previous && value.assets.some(asset => asset.id === previous.id) ? previous : value.assets[0] ? { id: value.assets[0].id, start: 0, sequence: 0 } : null);
    if (!initialized.current) {
      const query = new URLSearchParams(window.location.search);
      const requestedStep = query.get("step");
      setStep(steps.includes(requestedStep as Step) ? requestedStep as Step : "materials");
      const selected = query.get("version") || value.versions.at(-1)?.id || null;
      currentVersion.current = selected;
      setSelectedVersion(selected);
      const saved = requestedStep === "edit" ? value.versions.find(item => item.id === selected) : undefined;
      if (!restored?.draft) setDraft(saved ? { ...draftFromVersion(saved), title: saved.title } : { ...blankDraft(), title: `${value.title} · 第一版`.slice(0, 120) });
      initialized.current = true;
    }
  }, [restored]);

  useEffect(() => {
    writeWorkspaceDraft(episodeId, { draft: draftDirty ? draft : undefined, evidence: evidenceDirty ? { items: evidence, revision: evidenceRevision } : undefined });
  }, [episodeId, draft, draftDirty, evidence, evidenceDirty, evidenceRevision]);

  const refresh = useCallback(async () => {
    if (expired.current) throw new AppError("AUTH_REQUIRED", "登录已过期，请重新登录。", 401);
    const generation = ++readSequence.current;
    const pageEpoch = epoch.current;
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    let value: Episode;
    try { value = await api<Episode>(`/episodes/${pathId(episodeId)}`, { signal: controller.signal }); }
    catch (error) {
      if (mounted.current && pageEpoch === epoch.current && generation === readSequence.current && !(error instanceof Error && error.name === "AbortError")) setMediaFresh(false);
      if (mounted.current && pageEpoch === epoch.current && error instanceof AppError && error.status === 401) expireSession();
      if (mounted.current && pageEpoch === epoch.current && error instanceof AppError && error.code === 'EPISODE_EXPIRED') retire();
      throw error;
    }
    if (!mounted.current || pageEpoch !== epoch.current || generation !== readSequence.current) return;
    putEpisode(value);
    setMediaFresh(!document.hidden);
    setInitialError("");
  }, [episodeId, putEpisode, expireSession, retire]);

  useEffect(() => {
    mounted.current = true;
    epoch.current++;
    initialized.current = false;
    const pageEpoch = epoch.current;
    const statusController = new AbortController();
    void api<Status>("/status", { signal: statusController.signal }).then(value => { if (mounted.current && pageEpoch === epoch.current) setStatus(value); }).catch(error => {
      if (!statusController.signal.aborted && mounted.current) setFeedback({ text: `运行状态未取得：${errorText(error)}。模型操作保持禁用。`, error: true });
    });
    void refresh().catch(error => { if (mounted.current && pageEpoch === epoch.current) setInitialError(errorText(error)); });
    return () => {
      mounted.current = false;
      epoch.current = pageEpoch + 1;
      statusController.abort();
      readController.current?.abort();
      uploadController.current?.abort();
      window.dispatchEvent(new Event("episode:dirty-release"));
    };
  }, [episodeId, refresh]);

  useEffect(() => {
    window.dispatchEvent(new CustomEvent("episode:dirty", { detail: dirty || !!busy }));
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty || working.current) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", beforeUnload);
    return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty, busy]);

  useEffect(() => {
    const handlePopState = () => {
      const query = new URLSearchParams(window.location.search);
      const candidate = query.get("step");
      setStep(steps.includes(candidate as Step) ? candidate as Step : "materials");
      const next = query.get("version") || currentVersion.current;
      currentVersion.current = next;
      setSelectedVersion(next);
      setWatched(null);
      setAcknowledged(null);
    };
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let disposed = false;
    let consecutiveErrors = 0;
    let pollGeneration = 0;
    const tick = async () => {
      if (disposed || document.hidden) return;
      if (working.current) { timer = setTimeout(tick, 2000); return; }
      const generation = ++pollGeneration;
      let nextDelay = 2000;
      try {
        await refresh();
        if (disposed || generation !== pollGeneration) return;
        consecutiveErrors = 0;
      } catch (error) {
        if (disposed || generation !== pollGeneration) return;
        if (!(error instanceof Error && error.name === "AbortError")) {
          consecutiveErrors = Math.min(consecutiveErrors + 1, 5);
          nextDelay = Math.min(2000 * 2 ** (consecutiveErrors - 1), 30000);
          setFeedback({ text: `状态更新未完成：${errorText(error)}。可手动刷新核对。`, error: true });
        }
      }
      if (!disposed && !document.hidden && currentEpisode.current?.jobs.some(isActiveJob)) timer = setTimeout(tick, nextDelay);
    };
    const visible = () => {
      clearTimeout(timer);
      pollGeneration++;
      consecutiveErrors = 0;
      setMediaFresh(false);
      // Recheck access on return, but an unchanged video's completed preview
      // is still useful feedback. Consent itself must be given again.
      setAcknowledged(null);
      if (document.hidden) { readSequence.current++; readController.current?.abort(); }
      if (!document.hidden) void tick();
    };
    if (running && !document.hidden) timer = setTimeout(tick, 2000);
    document.addEventListener("visibilitychange", visible);
    return () => { disposed = true; clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [running, refresh]);

  const navigate = useCallback((next: Step, versionId = currentVersion.current) => {
    const url = new URL(window.location.href);
    url.searchParams.set("step", next);
    if (versionId) url.searchParams.set("version", versionId); else url.searchParams.delete("version");
    window.history.pushState(null, "", url.pathname + url.search);
    window.dispatchEvent(new Event("episode:location"));
    setStep(next);
    if (versionId !== currentVersion.current) { setWatched(null); setAcknowledged(null); }
    currentVersion.current = versionId;
    setSelectedVersion(versionId);
    // A new task starts at its own controls, never at the previous page's scroll depth.
    window.requestAnimationFrame(() => document.getElementById("studio-current-task")?.scrollIntoView?.({ block: "start", behavior: "instant" }));
  }, []);
  useEffect(() => {
    const updated = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string }>).detail;
      if (detail?.episodeId !== episodeId || working.current) return;
      void refresh().catch(error => { if (!(error instanceof Error && error.name === "AbortError")) setFeedback({ text: errorText(error), error: true }); });
    };
    const requested = (event: Event) => {
      const detail = (event as CustomEvent<{ episodeId?: string; step?: Step; versionId?: string }>).detail;
      if (detail?.episodeId !== episodeId || !steps.includes(detail.step as Step)) return;
      event.preventDefault();
      if (working.current || uncertain) {
        setFeedback({ text: "当前操作还未完成，请核对结果后再采用导演建议。", error: true }); return;
      }
      const changesVersion = !!detail.versionId && detail.versionId !== currentVersion.current;
      if (changesVersion && draftDirty && !window.confirm("打开导演建议的新草案会替换当前未保存编辑；已有版本保留。继续吗？")) return;
      working.current = true; setBusy("载入导演建议");
      void refresh().then(() => {
        if (!mounted.current) return;
        const target = detail.versionId ? currentEpisode.current?.versions.find(item => item.id === detail.versionId) : undefined;
        if (detail.versionId && !target) {
          setFeedback({ text: "导演建议的版本已不可用，请重新核对。", error: true }); return;
        }
        if (changesVersion && target) { setDraft({ ...draftFromVersion(target), title: target.title }); setDraftDirty(false); }
        navigate(detail.step!, detail.versionId || currentVersion.current);
      }).catch(error => setFeedback({ text: errorText(error), error: true })).finally(() => {
        working.current = false; if (mounted.current) setBusy("");
      });
    };
    window.addEventListener("director:updated", updated);
    window.addEventListener("director:navigate", requested);
    return () => { window.removeEventListener("director:updated", updated); window.removeEventListener("director:navigate", requested); };
  }, [episodeId, refresh, navigate, draftDirty, uncertain]);
  function editDraft(value: Draft) { setDraft(value); setDraftDirty(true); }
  function editEvidence(value: Evidence[]) { dirtyEvidence.current = true; setEvidenceDirty(true); setEvidence(value); }
  function showSource(id: string, start = 0, end?: number) {
    if (!currentEpisode.current?.assets.some(item => item.id === id)) { setFeedback({ text: "来源已不可用，请刷新核对。", error: true }); return; }
    setSource(previous => ({ id, start, end, sequence: (previous?.sequence || 0) + 1 }));
  }
  async function perform(label: string, operation: (isCurrent: () => boolean) => Promise<void>, message?: string): Promise<boolean> {
    if (working.current || uncertain) return false;
    working.current = true;
    setBusy(label);
    setFeedback(null);
    const pageEpoch = epoch.current;
    const isCurrent = () => mounted.current && pageEpoch === epoch.current;
    try {
      await operation(isCurrent);
      if (!isCurrent()) return false;
      try { await refresh(); } catch (error) {
        if (!isCurrent()) return false;
        setUncertain(true);
        setFeedback({ text: `操作已返回成功，但最新状态未能读取：${errorText(error)}。请刷新核对，不必重复提交刚才的操作。`, error: true });
        return true;
      }
      if (message) setFeedback({ text: message });
      return true;
    } catch (error) {
      if (mounted.current && pageEpoch === epoch.current) {
        if (error instanceof AppError && error.status === 401) expireSession();
        const unknown = error instanceof AppError && error.uncertain && error.code !== "DELETION_PENDING";
        if (unknown) setUncertain(true);
        setFeedback({ text: `${errorText(error)}${unknown ? " 请求结果可能已写入，先刷新核对已有任务和版本，不要直接重复提交。" : ""}`, error: true });
        try { await refresh(); } catch { /* Keep the original action failure visible. */ }
      }
      return false;
    } finally {
      working.current = false;
      if (mounted.current && pageEpoch === epoch.current) setBusy("");
    }
  }
  function ask(title: string, body: ReactNode, run: () => Promise<boolean>, label = "确认继续") {
    setFeedback(null);
    setConfirmation({ title, body, label, run });
  }
  function modelExplanation(action: string) {
    return demo ? `${action}只使用固定测试结果或合成提示音，不理解画面、不识别原声、不调用模型、不外发、不扣费。` : `${action}会向${episode?.workflow?.enabled ? "本期已授权的DeepSeek或MiniMax" : "已准入模型"}发送所需的素材或文案。与修改、重试共用本期${episode?.budget.limit ?? 3}元及首批${episode?.budget.total_limit ?? 30}元上限，结果仍须人工核对。`;
  }

  async function upload(files: File[]) {
    if (working.current || uncertain || !files.length) return;
    const controller = new AbortController();
    uploadController.current = controller;
    const rows: UploadRow[] = files.map(file => ({ name: file.name, sent: 0, total: file.size, state: "waiting" }));
    const history = uploads;
    setUploads([...history, ...rows.map(item => ({ ...item }))]);
    await perform("上传与媒体校验中", async isCurrent => {
      let successes = 0;
      let unknown = false;
      let refreshFailed = false;
      const active = () => isCurrent() && !controller.signal.aborted;
      const update = () => { if (active()) setUploads([...history, ...rows.map(item => ({ ...item }))]); };
      for (let index = 0; index < files.length; index++) {
        if (!active() || document.hidden) break;
        rows[index].state = "uploading"; update();
        try {
          await uploadAsset(episodeId, files[index], (sent, total) => {
            rows[index].sent = sent; rows[index].total = total || files[index].size;
            rows[index].state = sent >= (total || files[index].size) ? "processing" : "uploading"; update();
          }, controller.signal);
          if (!active()) break;
          rows[index].state = "done"; successes++;
        } catch (error) {
          if (!active()) break;
          rows[index].state = "failed"; rows[index].error = errorText(error);
          if (error instanceof AppError && error.uncertain) { unknown = true; rows[index].state = "unknown"; update(); break; }
          if (error instanceof AppError && error.status === 401) { expireSession(); update(); break; }
        }
        update();
        if (rows[index].state === "done") {
          // Publish each saved asset before starting the next file. A failed read
          // does not turn an acknowledged upload into a failed upload or retry it.
          try { await refresh(); }
          catch { refreshFailed = true; break; }
        }
      }
      if (active()) {
        for (const row of rows) if (row.state === "waiting") { row.state = "skipped"; row.error = "本次尚未发送；核对已保存素材后，可重新选择这一份。"; }
        update();
        setFeedback({ text: `已接收${successes}／${files.length}份。${successes === files.length ? "素材已保存，可继续核对或人工选段。" : "成功素材已保留；未接收项请逐个核对原因。"}${unknown ? "部分请求结果未确定，请刷新核对素材清单后再决定是否重传。" : ""}${refreshFailed ? "已保存素材的最新列表暂未读回，请刷新核对，不要重复上传已保存项。" : ""}`, error: successes !== files.length || unknown || refreshFailed });
        if (unknown || refreshFailed) setUncertain(true);
      }
    });
  }
  async function prepareDelete(asset: Asset) {
    if (working.current || uncertain) return;
    const ok = await perform("核对删除影响", async isCurrent => {
      const impact = await api<Impact>(`/assets/${pathId(asset.id)}/impact`);
      if (!isCurrent()) return;
      ask(`删除“${asset.filename}”？`, <><p>{impact.message}</p><p>影响 {impact.versions.length} 个版本、{impact.shares.length} 条仍有效或受限的分享。它们会停止使用该来源。</p>{impact.versions.length > 0 && <ul>{impact.versions.map(id => <li key={id}>{currentEpisode.current?.versions.find(item => item.id === id)?.title || "关联版本"}</li>)}</ul>}<p>删除本应用保存的副本，不改外部源文件；此操作不可撤销。</p></>, () => perform("正在删除应用副本", async () => { await api(`/assets/${pathId(asset.id)}?confirm=true`, { method: "DELETE" }); }, "删除已完成，依赖内容已重新核对。"), "确认删除应用副本");
    });
    return ok;
  }
  function cleanup(id: string, filename: string) {
    ask("重试已经确认的删除？", <p>仅继续清理“{filename}”尚未清理完成的应用副本，外部原片不受影响。</p>, () => perform("继续清理应用副本", async () => { await api(`/assets/${pathId(id)}?confirm=true`, { method: "DELETE" }); }, "应用副本清理已完成。"), "重试清理");
  }
  function add(asset: Asset) {
    if (draft.segments.length >= 30) { setFeedback({ text: "一个版本最多30个片段，请先调整现有选段。", error: true }); return; }
    editDraft({ ...draft, segments: [...draft.segments, segmentFromAsset(asset)] });
    showSource(asset.id);
    setFeedback({ text: `已把“${asset.filename}”加入编辑草案，尚未保存。` });
  }
  function loadEditor(item: Version, without = false) {
    if (draftDirty && !window.confirm("当前编辑草案尚未保存。载入此版本会替换草案，已保存版本保留。继续吗？")) return;
    setDraft(draftFromVersion(item, without));
    setDraftDirty(true);
    navigate("edit", item.id);
    if (item.segments[0]) showSource(item.segments[0].asset_id, item.segments[0].start, item.segments[0].end);
  }
  async function saveSubtitles(base: Version, segments: Version["segments"]): Promise<boolean> {
    if (blocked || !episode || currentVersion.current !== base.id) return false;
    return perform("保存字幕并生成新版", async isCurrent => {
      const saved = await api<Version>(`/episodes/${pathId(episodeId)}/versions`, { method: "POST", body: {
        base_version_id: base.id, title: `${base.title.slice(0, 110)} · 字幕校对`, segments,
        packaging: base.packaging, narration_text: base.narration_text,
      } });
      if (!isCurrent()) return;
      // The POST already persisted this version. Display that response before
      // navigating; a slower render request must not look like a missing version.
      const current = currentEpisode.current;
      if (current) putEpisode({ ...current, versions: [...current.versions.filter(item => item.id !== saved.id), saved] });
      setWatched(null); setAcknowledged(null); navigate("preview", saved.id);
      await api(`/versions/${pathId(saved.id)}/render`, { method: "POST", body: {} });
    }, "字幕已另存；原版本保留。");
  }
  function saveDraft() {
    if (!episode) return;
    const problem = draftProblem(draft, episode);
    if (problem) { setFeedback({ text: problem, error: true }); return; }
    void perform("保存新版本", async isCurrent => {
      const saved = await api<Version>(`/episodes/${pathId(episodeId)}/versions`, { method: "POST", body: {
        ...draft, title: draft.title.trim(), narration_text: hasNarratedIntro(draft.packaging) ? draft.narration_text.trim() : "",
      } });
      if (!isCurrent()) return;
      setDraftDirty(false); setDraft(previous => ({ ...previous, base_version_id: saved.id }));
      setWatched(null); setAcknowledged(null); navigate("preview", saved.id);
    }, "新版本已保存；现在可合成并预览，旧版本保留。");
  }
  function continueToPreview() {
    if (!episode || blocked || working.current) return;
    const problem = draftProblem(draft, episode);
    if (problem) { setFeedback({ text: problem, error: true }); return; }
    const hasVoice = hasNarratedIntro(draft.packaging) && !!draft.narration_text.trim();
    if (!hasVoice && status?.media_ready !== true) {
      setFeedback({ text: "媒体服务尚未就绪，请刷新状态后重试。", error: true }); return;
    }
    void perform(hasVoice ? "准备旁白确认" : "准备预览", async isCurrent => {
      // Preview selection can change while this editor's draft stays mounted.
      // Reuse only the version whose content is actually shown in the editor.
      let target = episode.versions.find(item => item.id === draft.base_version_id);
      if (draftDirty || !target) {
        target = await api<Version>(`/episodes/${pathId(episodeId)}/versions`, { method: "POST", body: {
          ...draft, title: draft.title.trim(), narration_text: hasVoice ? draft.narration_text.trim() : "",
        } });
        if (!isCurrent()) return;
        setDraftDirty(false); setDraft(previous => ({ ...previous, base_version_id: target!.id }));
      }
      if (!isCurrent()) return;
      // Publish the saved ID before render. A failed render retries this version,
      // and an unknown response uses perform's existing reconciliation lock.
      setWatched(null); setAcknowledged(null); navigate("preview", target.id);
      if (!hasVoice && ["draft", "failed"].includes(target.state)) {
        await api(`/versions/${pathId(target.id)}/render`, { method: "POST", body: {} });
        if (!isCurrent()) return;
      }
    });
  }
  async function saveDirectorMessage(payload: DirectorMessageInput): Promise<boolean> {
    const current = currentEpisode.current;
    if (!current?.director || blocked || working.current) return false;
    const signature = JSON.stringify(payload);
    const existing = directorRequest.current;
    if (!existing || existing.signature !== signature) {
      directorRequest.current = { signature, revision: current.director.revision, client_id: crypto.randomUUID() };
    }
    const request = directorRequest.current!;
    // An acknowledged or subsequently observed client ID must never be resent
    // under a fresh key after a network failure.
    if (current.director.messages.some(message => message.id === request.client_id)) {
      directorRequest.current = null;
      setFeedback({ text: "这条要求已经保存，下一轮方案会带上。" });
      return true;
    }
    return perform("保存给导演的要求", async isCurrent => {
      let state: DirectorState;
      try {
        state = await api<DirectorState>(`/episodes/${pathId(episodeId)}/director/messages`, { method: "POST", body: {
          ...payload, revision: request.revision, client_id: request.client_id,
        } });
      } catch (error) {
        if (!(error instanceof AppError && error.uncertain)) directorRequest.current = null;
        throw error;
      }
      if (!isCurrent()) return;
      directorRequest.current = null;
      if (currentEpisode.current) putEpisode({ ...currentEpisode.current, director: state });
    }, "已保存。生成下一轮方向时会带上这些要求，已有成片保留。");
  }
  function analyze() {
    if (!workflow) return;
    if (episode?.workflow?.analysis_cached) {
      void perform("载入已完成分析", async () => {
        await api(`/episodes/${pathId(episodeId)}/analysis`, { method: "POST", body: {} });
      }, "已复用本期分析，无需重复发送素材或收费；旧版本保留。");
      return;
    }
    ask(demo ? "取得固定演示占位？" : "分析当前获授权素材？", <><p>{modelExplanation("素材分析")}</p><p>重新分析会使旧依据和依赖版本失效。{evidenceDirty ? "未保存的核对修改也将被新结果替换。" : ""}</p></>, () => perform("提交素材分析任务", async isCurrent => {
      await api(`/episodes/${pathId(episodeId)}/analysis`, { method: "POST", body: {} });
      if (!isCurrent()) return;
      dirtyEvidence.current = false; setEvidenceDirty(false);
    }, demo ? "固定演示任务已提交；结果不代表真实分析。" : "分析任务已提交，完成后先核对来源。"), demo ? "确认只生成固定占位" : "确认发送并分析");
  }
  function confirmEvidence() {
    if (!episode) return;
    if (evidenceRevision !== episode.evidence_revision) { setFeedback({ text: "服务端核对版本已更新，请重新载入再确认。", error: true }); return; }
    const problem = evidenceProblem(evidence, episode);
    if (problem) { setFeedback({ text: problem, error: true }); return; }
    void perform("保存内容确认", async isCurrent => {
      const value = await api<Episode>(`/episodes/${pathId(episodeId)}/evidence`, { method: "PUT", body: { revision: evidenceRevision, items: evidence.map(item => ({ ...item, note: item.note.trim() })), confirmed: true, confirmation_mode: "overview" } });
      if (!isCurrent()) return;
      dirtyEvidence.current = false; setEvidenceDirty(false); putEpisode(value);
    }, evidence.length ? "内容概览已确认，可以继续选方向。" : "已保存为空；补充内容后再生成方向。");
  }
  function directions() {
    ask(demo ? "取得固定演示方向？" : "根据已确认证据提出方向？", <p>{modelExplanation("方向提案")}</p>, () => perform("提交方向提案", async () => { await api(`/episodes/${pathId(episodeId)}/directions`, { method: "POST", body: {} }); }, "任务已提交，完成后再选择方向。"));
  }
  function selectDirection(direction: Direction, autoRenderOriginal = false) {
    if (!episode || episode.directions_revision == null) return;
    if (episode.director && !episode.director.directions_current) {
      setFeedback({ text: "要求已更新，请先取得新方向；已有成片仍可查看。", error: true }); return;
    }
    if (draftDirty && !window.confirm("这个方向会建立并载入新的编辑草案，替换当前未保存编辑。已保存版本保留。继续吗？")) return;
    void perform("建立方向草案", async isCurrent => {
      const saved = await api<Version>(`/episodes/${pathId(episodeId)}/direction`, { method: "POST", body: { direction_id: direction.id, revision: episode.directions_revision, brief_revision: episode.director?.revision ?? 0, packaging,
        ...(autoRenderOriginal ? { auto_render_original: true, client_id: crypto.randomUUID() } : {}) } });
      if (!isCurrent()) return;
      setDraft({ ...draftFromVersion(saved), title: saved.title }); setDraftDirty(false);
      setWatched(null); setAcknowledged(null); navigate(autoRenderOriginal ? "preview" : "edit", saved.id);
      if (!autoRenderOriginal && saved.segments[0]) showSource(saved.segments[0].asset_id, saved.segments[0].start, saved.segments[0].end);
    }, autoRenderOriginal ? "导演正在用现场原声出初稿，完成后会自动检查。" : demo ? "固定演示草案已建立，请核对占位内容；这不是AI效果验证。" : "拍法已选好，接下来看看声音和包装。");
  }
  function reloadEvidence() {
    if (evidenceDirty && !window.confirm("重新载入会丢弃当前未保存的证据文字、时间和移除修改。继续吗？")) return;
    dirtyEvidence.current = false; setEvidenceDirty(false);
    void refresh().catch(error => setFeedback({ text: errorText(error), error: true }));
  }
  function narration() {
    if (!version || !workflow) return;
    ask(demo ? "确认生成非人声提示音？" : "确认这版旁白文案与系统音色？", <><p className="narration-copy">{version.narration_text}</p><p>{modelExplanation(demo ? "演示音频" : "系统旁白")}</p><p>{demo ? "不会朗读上述文字。" : "不冒充出镜者原话，不克隆真人声纹。"}独立片头不覆盖真人原声。</p></>, () => perform("提交本版本音频任务", async () => { await api(`/versions/${pathId(version.id)}/narration`, { method: "POST", body: { confirm_text: true } }); }, demo ? "非人声提示音任务已提交。" : "旁白任务已提交。"));
  }
  function render() {
    if (!version) return;
    void perform("提交本机合成", async isCurrent => { await api(`/versions/${pathId(version.id)}/render`, { method: "POST", body: {} }); if (isCurrent()) { setWatched(null); setAcknowledged(null); } }, "合成任务已提交，完成后请预览这一版。");
  }
  function confirmVersion() {
    if (!version || blocked || version.state !== "ready" || !version.media_url || acknowledged !== viewKey) return;
    ask(`确认“${version.title}”？`, <p>确认只绑定当前版本。你已完整预览，画面、原声和文字符合表达，并有权使用素材。{demo && "演示占位与合成提示音不代表真实AI效果，素材许可要求仍需遵守。"}</p>, () => perform("确认当前版本", async () => { await api(`/versions/${pathId(version.id)}/confirm`, { method: "POST", body: {} }); }, "此版本已确认，可导出这一版。"), "确认此版本");
  }
  function download() {
    if (!version || version.state !== "confirmed") return;
    const versionId = version.id;
    void perform("核对这一版的导出权限", async isCurrent => {
      await checkExport(versionId);
      if (!isCurrent() || currentVersion.current !== versionId) return;
      const anchor = document.createElement("a");
      anchor.href = exportUrl(versionId);
      anchor.download = "";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    }, "已将这一版交给浏览器下载，请查看下载列表。是否下载完成以浏览器为准。");
  }
  function retry(job: Job) {
    if (costUncertain(job.error?.code)) return;
    ask(demo ? "重试固定演示步骤？" : "从已保存检查点重试？", <><p>{modelExplanation("重试")}</p><p>费用未确定时不能继续重试；成功步骤由服务端核对复用。</p></>, () => perform("提交失败步骤重试", async () => { await api(`/jobs/${pathId(job.id)}/retry`, { method: "POST", body: {} }); }, "重试请求已提交，请查看真实任务状态。"));
  }

  if (retired) return <EpisodeRetention episodeId={episodeId} retired onRetired={retire} />;
  if (!episode) return <section className="panel" aria-busy={!initialError}><h1>打开这一期</h1>{initialError ? <><Notice tone="error">{initialError}</Notice><Button onClick={() => { void refresh().catch(error => setInitialError(errorText(error))); }}>重新读取</Button></> : <p role="status">正在读取已保存的素材和版本…</p>}</section>;
  return <MediaAccessContext.Provider value={{ allowed: mediaAccess.allowed && mediaFresh, revision: mediaAccess.revision, revalidate: async () => { await mediaAccess.revalidate?.(); await refresh(); } }}><div className={`studio ${styles.workspace}`} aria-busy={!!busy}>
    <header className="studio-heading"><div><span className="eyebrow">在场 / 制作现场</span><h1>{episode.title}</h1></div><div className={styles.tools}><Button variant="ghost" disabled={!!busy} onClick={() => { void refresh().then(() => setFeedback({ text: "状态已更新。" })).catch(error => setFeedback({ text: errorText(error), error: true })); }}><RefreshCw size={17} />刷新状态</Button><Button variant="ghost" onClick={() => setSettingsOpen(true)}>本期设置</Button></div></header>
    <nav id="studio-current-task" className="step-nav" aria-label="本期制作步骤">{steps.map((item, index) => <button key={item} className={step === item ? "active" : ""} aria-current={step === item ? "step" : undefined} disabled={!!busy} onClick={() => navigate(item)}><span>{String(index + 1).padStart(2, "0")}</span>{stepTitles[item]}<ArrowRight size={16} aria-hidden="true" /></button>)}</nav>
    {dirty && <p className="dirty-note">有未保存的{directorDirty ? "导演补充要求" : draftDirty && evidenceDirty ? "编辑草案与来源核对" : draftDirty ? "编辑草案" : "来源核对"}。</p>}
    {restored && dirty && <Notice>已从当前标签页内存恢复未保存草案，尚未提交。原有版本与核对修订号保持不变；请先核对当前来源是否仍可用。刷新浏览器或退出登录会清除此临时副本。</Notice>}
    {busy && <Notice><span role="status">{busy}…</span></Notice>}
    {feedback && (feedback.error ? <Notice tone="error">{feedback.text}</Notice> : <p className={styles.feedback} role="status">{feedback.text}</p>)}
    {!mediaFresh && <Notice>媒体与操作已暂停，未保存编辑仍保留。请等待状态核对；读取失败时使用“刷新状态”重试。</Notice>}
    {uncertain && <Notice tone="error"><p>上次请求结果未确定，操作暂时锁定，避免重复写入。请先刷新并核对素材、版本与任务。</p><Button onClick={() => { ask("已核对服务端记录？", <p>请确认没有重复上传、重复版本或仍在处理的同一任务。解除锁定不会重发上次请求。</p>, async () => { await refresh(); setUncertain(false); return true; }, "已核对，解除操作锁定"); }}>我已核对当前记录</Button></Notice>}
    {status?.media_ready === false && <Notice tone="error">本机媒体工具尚未就绪，上传探测与合成可能失败；请先恢复本机媒体服务。</Notice>}
    {selectedVersion && !version && step === "preview" && <Notice tone="error">地址指定的版本当前不可用。请从本期版本中明确选择，不会自动导出其他版本。</Notice>}
    {step === "materials" && <MaterialsPanel episode={episode} source={source} busy={blocked} uploads={uploads} onUpload={files => { void upload(files); }} onSource={asset => showSource(asset.id)} onAdd={add} onDelete={asset => { void prepareDelete(asset); }} onCleanup={cleanup} onNext={() => navigate("direction")} />}
    {step === "direction" && <>
      <DirectorDirections episode={episode} busy={blocked || directorDirty || evidenceDirty || running} demo={demo} onSelect={selectDirection} onAutoSelect={direction => selectDirection(direction, true)} />
      {hasCurrentDirections(episode) && (directorDirty || evidenceDirty) && <Notice>先保存下方的要求或素材修改，再选择方案。</Notice>}
      <details className="director-details" open={!hasCurrentDirections(episode) || evidenceDirty}><summary>{hasCurrentDirections(episode) ? "查看素材依据" : "核对素材内容"}</summary><DirectionPanel evidenceOnly={hasCurrentDirections(episode)} episode={episode} source={source} evidence={evidence} revision={evidenceRevision} dirty={evidenceDirty} busy={blocked} demo={demo} workflow={workflow} reason={episode.workflow?.reason || status?.cloud_reason || "运行状态尚未确定，模型操作保持禁用。"} packaging={packaging} onPackaging={setPackaging} onSource={showSource} onEvidence={editEvidence} onReload={reloadEvidence} onConfirm={confirmEvidence} onAnalyze={analyze} onDirections={directions} onSelect={selectDirection} onManual={() => navigate("edit")} /></details>
    </>}
    {step === "edit" && <EditorPanel episode={episode} source={source} draft={draft} busy={blocked} demo={demo} guided={!!episode.director} feedbackError={feedback?.error ? feedback.text : undefined}
      onContinue={continueToPreview}
      onChange={editDraft} onSave={saveDraft} onSource={showSource} onMaterials={() => navigate("materials")} />}
    {step === "preview" && <PreviewPanel episode={episode} version={version} busy={blocked} demo={demo} workflow={workflow} mediaReady={status?.media_ready === true} watched={watched === viewKey} acknowledged={acknowledged === viewKey} onWatched={() => setWatched(viewKey)} onAcknowledged={value => setAcknowledged(value ? viewKey : null)} onVersion={id => { setWatched(null); setAcknowledged(null); navigate("preview", id); }} onEdit={loadEditor} onRender={render} onNarration={narration} onConfirm={confirmVersion} onDownload={download} onSubtitles={saveSubtitles} onRunControl={action => { if (version) void perform("更新导演委托", async () => { await api(`/versions/${pathId(version.id)}/director-run/${action}`, { method: "POST", body: {} }); }, action === "pause" ? "已请求暂停，完成的结果会保留。" : action === "cancel" ? "已请求结束委托，已有结果会保留。" : "导演已继续制作。"); }} onVisualCheck={() => { if (version) void perform("导演检查画面", async () => { await api(`/versions/${pathId(version.id)}/visual-review`, { method: "POST", body: {} }); }, "导演开始看抽样画面，候选保持不变。"); }} onCheck={() => { if (version) void perform("检查画面与声音", async () => { await api(`/versions/${pathId(version.id)}/review`, { method: "POST", body: {} }); }, "检查已开始，无需重新生成视频。"); }} />}
    <DirectorGuideDock episode={episode} step={step} version={version} busy={blocked} canGenerate={workflow} pending={running} demo={demo}
      onMessage={saveDirectorMessage} onDirtyChange={setDirectorDirty} onNavigate={navigate}
      onGenerate={kind => kind === "analysis" ? analyze() : directions()} onSelect={selectDirection} />
    <details className={`panel jobs-panel ${styles.records}`} open={running || episode.jobs.at(-1)?.state === "failed" ? true : undefined}><summary>制作记录 <span>{running ? "正在处理…" : `${episode.jobs.length} 条`}</span></summary><div className="section-title"><h2>制作进度</h2><span className="muted">{running ? "读取真实任务状态" : "当前没有运行中任务"}</span></div>{!episode.jobs.length && <p className="muted">开始分析、生成音频或合成后，任务与失败原因会保存在这里。</p>}
      <ul className="job-list">{[...episode.jobs].reverse().map(job => <li key={job.id}><div className="toolbar"><strong>{({ director_chat: demo ? "固定对话演示" : "导演对话", analysis: demo ? "固定观察与文字" : "素材分析", directions: demo ? "固定演示方向" : "导演方向", narration: demo ? "非人声提示音" : "系统旁白", render: "本机视频合成", technical_review: "画面与声音检查", visual_review: "导演画面审片", director_run: "导演制作委托" }[job.kind] || "已保存任务")}</strong><span className="chip">{({ queued: "排队中", running: "处理中", succeeded: "已完成", failed: "失败", active: "执行中", pausing: "正在暂停", paused: "已暂停", blocked: "已停止", awaiting_user: "待预览", cancelled: "已结束", cancelling: "正在结束" }[job.state] || "状态待核对")}</span><span>{job.stage}</span></div>{job.error && <Notice tone="error">{job.error.message}{job.error.code === "INTERRUPTED" && " 中断前成功结果已保存，可从适用检查点继续。"}{costUncertain(job.error.code) && " 预算继续保留，费用核实前不能重试。"}</Notice>}{job.state === "failed" && modelJob(job) && !costUncertain(job.error?.code) && <Button disabled={blocked || !workflow} onClick={() => retry(job)}>重试这一失败步骤</Button>}</li>)}</ul>
      <details className="budget-detail"><summary>本期预算与占用</summary><p>本期已计费 ¥{episode.budget.spent.toFixed(2)} · 预留 ¥{episode.budget.reserved.toFixed(2)}／上限 ¥{episode.budget.limit.toFixed(2)}</p><p>首批已计费 ¥{episode.budget.total_spent.toFixed(2)} · 预留 ¥{episode.budget.total_reserved.toFixed(2)}／上限 ¥{episode.budget.total_limit.toFixed(2)}</p>{demo && <p>固定演示不产生真实模型消费；预留不等于实际扣费。</p>}</details>
    </details>
    <Dialog open={settingsOpen} title="本期设置" onClose={() => setSettingsOpen(false)}>
      {episode.background && <p>{episode.background}</p>}
      <EpisodeRetention episodeId={episodeId} initial={episode.retention} onRetired={retire} />
      <div className="toolbar"><Link className="button secondary" href={`/episodes/${episodeId}/collaboration`} onClick={event => { if ((dirty || busy || uncertain) && !window.confirm("还有未保存修改或待核对结果。离开会保留已保存内容；是否继续？")) event.preventDefault(); }}>朋友供材与采访 ↗</Link><Link className="button secondary" href={`/episodes/${episodeId}/sharing`} onClick={event => { if ((dirty || busy || uncertain) && !window.confirm("还有未保存修改或待核对结果。离开会保留已保存内容；是否继续？")) event.preventDefault(); }}>私密分享与撤回 ↗</Link></div>
    </Dialog>
    <Dialog open={!!confirmation} onClose={() => { if (!busy) setConfirmation(null); }} title={confirmation?.title || "确认操作"}>
      {confirmation && <>{confirmation.body}{feedback?.error && <Notice tone="error">{feedback.text}</Notice>}<div className="toolbar"><Button disabled={!!busy} onClick={() => setConfirmation(null)}>取消</Button><Button variant="primary" busy={!!busy} disabled={!!busy} onClick={() => { const current = confirmation; void current.run().then(ok => { if (ok && mounted.current) setConfirmation(null); }).catch(error => setFeedback({ text: errorText(error), error: true })); }}>{confirmation.label}</Button></div></>}
    </Dialog>
  </div></MediaAccessContext.Provider>;
}

export default function Studio({ episodeId }: { episodeId: string }) {
  return <StudioWorkspace key={episodeId} episodeId={episodeId} />;
}
