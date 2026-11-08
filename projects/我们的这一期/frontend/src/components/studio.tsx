"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { ArrowRight, RefreshCw } from "lucide-react";
import { Button, Dialog, MediaAccessContext, Notice, useMediaAccess } from "@/components/ui";
import { api, AppError, checkExport, exportUrl, uploadAsset } from "@/lib/api";
import type { Asset, Direction, Episode, Evidence, Impact, Job, Packaging, Status, Version } from "@/lib/types";
import { DirectionPanel, EditorPanel, MaterialsPanel, PreviewPanel } from "./studio/panels";
import { readWorkspaceDraft, writeWorkspaceDraft } from "./studio/draft-cache";
import { blankDraft, costUncertain, draftFromVersion, draftProblem, evidenceProblem, isActiveJob, modelJob,
  segmentFromAsset, steps, stepTitles, type Draft, type Source, type Step, type UploadRow } from "./studio/model";

type Feedback = { text: string; error?: boolean } | null;
type Confirmation = { title: string; body: ReactNode; label: string; run: () => Promise<boolean> } | null;
const pathId = encodeURIComponent;
const errorText = (error: unknown) => error instanceof Error ? error.message : "操作没有完成，请刷新核对当前状态。";

function StudioWorkspace({ episodeId }: { episodeId: string }) {
  const [restored] = useState(() => readWorkspaceDraft(episodeId));
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
  const [packaging, setPackaging] = useState<Packaging>("light");
  const [uploads, setUploads] = useState<UploadRow[]>([]);
  const [busy, setBusy] = useState("");
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [uncertain, setUncertain] = useState(false);
  const [watched, setWatched] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
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

  const demo = status?.model_mode === "demo";
  const workflow = !!status && ["cloud", "demo"].includes(status.model_mode) && (status.workflow_enabled ?? status.cloud_enabled);
  const blocked = !!busy || uncertain || !mediaFresh || !mediaAccess.allowed;
  const version = episode?.versions.find(item => item.id === selectedVersion);
  const running = episode?.jobs.some(isActiveJob) ?? false;
  const dirty = draftDirty || evidenceDirty;
  const viewKey = version ? `${version.id}:${version.media_url}:${version.state}` : "";

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
      throw error;
    }
    if (!mounted.current || pageEpoch !== epoch.current || generation !== readSequence.current) return;
    putEpisode(value);
    setMediaFresh(!document.hidden);
    setInitialError("");
  }, [episodeId, putEpisode, expireSession]);

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
      setAcknowledged(false);
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
      setWatched(null); setAcknowledged(false);
      if (document.hidden) { readSequence.current++; readController.current?.abort(); }
      if (!document.hidden) void tick();
    };
    if (running && !document.hidden) timer = setTimeout(tick, 2000);
    document.addEventListener("visibilitychange", visible);
    return () => { disposed = true; clearTimeout(timer); document.removeEventListener("visibilitychange", visible); };
  }, [running, refresh]);

  function navigate(next: Step, versionId = currentVersion.current) {
    const url = new URL(window.location.href);
    url.searchParams.set("step", next);
    if (versionId) url.searchParams.set("version", versionId); else url.searchParams.delete("version");
    window.history.pushState(null, "", url.pathname + url.search);
    window.dispatchEvent(new Event("episode:location"));
    setStep(next);
    if (versionId !== currentVersion.current) { setWatched(null); setAcknowledged(false); }
    currentVersion.current = versionId;
    setSelectedVersion(versionId);
  }
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
    return demo ? `${action}只使用固定测试结果或合成提示音，不理解画面、不识别原声、不调用模型、不外发、不扣费。` : `${action}会向已准入模型发送所需的素材或文案。与修改、重试共用本期${episode?.budget.limit ?? 3}元及首批${episode?.budget.total_limit ?? 30}元上限，结果仍须人工核对。`;
  }

  async function upload(files: File[]) {
    if (working.current || uncertain) return;
    const controller = new AbortController();
    uploadController.current = controller;
    const rows: UploadRow[] = files.map(file => ({ name: file.name, sent: 0, total: file.size, state: "waiting" }));
    setUploads(rows.map(item => ({ ...item })));
    await perform("上传与媒体校验中", async () => {
      let successes = 0;
      let unknown = false;
      const update = () => { if (mounted.current && !controller.signal.aborted) setUploads(rows.map(item => ({ ...item }))); };
      for (let index = 0; index < files.length; index++) {
        if (controller.signal.aborted) break;
        rows[index].state = "uploading"; update();
        try {
          await uploadAsset(episodeId, files[index], (sent, total) => {
            rows[index].sent = sent; rows[index].total = total || files[index].size;
            rows[index].state = sent >= (total || files[index].size) ? "processing" : "uploading"; update();
          }, controller.signal);
          rows[index].state = "done"; successes++;
        } catch (error) {
          if (controller.signal.aborted) break;
          rows[index].state = "failed"; rows[index].error = errorText(error);
          if (error instanceof AppError && error.uncertain) unknown = true;
          if (error instanceof AppError && error.status === 401) { expireSession(); update(); break; }
        }
        update();
      }
      if (mounted.current && !controller.signal.aborted) {
        setFeedback({ text: `已接收${successes}／${files.length}份。${successes === files.length ? "素材已保存，可继续核对或人工选段。" : "成功素材已保留；未接收项请逐个核对原因。"}${unknown ? "部分请求结果未确定，请刷新核对素材清单后再决定是否重传。" : ""}`, error: successes !== files.length });
        if (unknown) setUncertain(true);
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
  function saveDraft() {
    if (!episode) return;
    const problem = draftProblem(draft, episode);
    if (problem) { setFeedback({ text: problem, error: true }); return; }
    void perform("保存新版本", async isCurrent => {
      const saved = await api<Version>(`/episodes/${pathId(episodeId)}/versions`, { method: "POST", body: {
        ...draft, title: draft.title.trim(), narration_text: draft.packaging === "standard" ? draft.narration_text.trim() : "",
      } });
      if (!isCurrent()) return;
      setDraftDirty(false); setDraft(previous => ({ ...previous, base_version_id: saved.id }));
      setWatched(null); setAcknowledged(false); navigate("preview", saved.id);
    }, "新版本已保存；现在可合成并预览，旧版本保留。");
  }
  function analyze() {
    if (!workflow) return;
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
    ask(demo ? "确认固定演示占位？" : "保存并确认这份来源核对？", <><p>{demo ? "这些文字是固定占位，不是画面理解或真实转写。这次确认只验收流程。" : "请确认已回看相关原片，保留文字有来源支持；不确定内容已修正或移除。确认记录你的核对结论，不自动证明模型正确。"}</p><p>依据更新后，旧方向和依赖版本需重新核对。{!evidence.length && "当前清单为空，不能据此请求方向。"}</p></>, () => perform("保存核对结果", async isCurrent => {
      const value = await api<Episode>(`/episodes/${pathId(episodeId)}/evidence`, { method: "PUT", body: { revision: evidenceRevision, items: evidence.map(item => ({ ...item, note: item.note.trim() })), confirmed: true } });
      if (!isCurrent()) return;
      dirtyEvidence.current = false; setEvidenceDirty(false); putEpisode(value);
    }, "核对结果已保存。"), demo ? "已知是占位，确认演示" : "已回看来源，确认保存");
  }
  function directions() {
    ask(demo ? "取得固定演示方向？" : "根据已确认证据提出方向？", <p>{modelExplanation("方向提案")}</p>, () => perform("提交方向提案", async () => { await api(`/episodes/${pathId(episodeId)}/directions`, { method: "POST", body: {} }); }, "任务已提交，完成后再选择方向。"));
  }
  function selectDirection(direction: Direction) {
    if (!episode || episode.directions_revision == null) return;
    if (draftDirty && !window.confirm("这个方向会建立并载入新的编辑草案，替换当前未保存编辑。已保存版本保留。继续吗？")) return;
    void perform("建立方向草案", async isCurrent => {
      const saved = await api<Version>(`/episodes/${pathId(episodeId)}/direction`, { method: "POST", body: { direction_id: direction.id, revision: episode.directions_revision, packaging } });
      if (!isCurrent()) return;
      setDraft({ ...draftFromVersion(saved), title: saved.title }); setDraftDirty(false);
      setWatched(null); setAcknowledged(false); navigate("edit", saved.id);
      if (saved.segments[0]) showSource(saved.segments[0].asset_id, saved.segments[0].start, saved.segments[0].end);
    }, demo ? "固定演示草案已建立，请核对占位内容；这不是AI效果验证。" : "草案已建立。可逐段核对、修改，再保存新版本。");
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
    void perform("提交本机合成", async isCurrent => { await api(`/versions/${pathId(version.id)}/render`, { method: "POST", body: {} }); if (isCurrent()) { setWatched(null); setAcknowledged(false); } }, "合成任务已提交，完成后请预览这一版。");
  }
  function confirmVersion() {
    if (!version || watched !== viewKey || !acknowledged) return;
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

  if (!episode) return <section className="panel" aria-busy={!initialError}><h1>打开这一期</h1>{initialError ? <><Notice tone="error">{initialError}</Notice><Button onClick={() => { void refresh().catch(error => setInitialError(errorText(error))); }}>重新读取</Button></> : <p role="status">正在读取已保存的素材和版本…</p>}</section>;
  return <MediaAccessContext.Provider value={{ allowed: mediaAccess.allowed && mediaFresh, revision: mediaAccess.revision, revalidate: async () => { await mediaAccess.revalidate?.(); await refresh(); } }}><div className="studio" aria-busy={!!busy}>
    <header className="studio-heading"><div><span className="eyebrow">我们的这一期 / 制作现场</span><h1>{episode.title}</h1>{episode.background && <p className="muted">{episode.background}</p>}</div><Button variant="ghost" disabled={!!busy} onClick={() => { void refresh().then(() => setFeedback({ text: "已重新核对服务端状态，未保存编辑仍保留。" })).catch(error => setFeedback({ text: errorText(error), error: true })); }}><RefreshCw size={17} />刷新状态</Button></header>
    <nav className="step-nav" aria-label="本期制作步骤">{steps.map((item, index) => <button key={item} className={step === item ? "active" : ""} aria-current={step === item ? "step" : undefined} disabled={!!busy} onClick={() => navigate(item)}><span>{String(index + 1).padStart(2, "0")}</span>{stepTitles[item]}<ArrowRight size={16} aria-hidden="true" /></button>)}</nav>
    {dirty && <p className="dirty-note">有未保存的{draftDirty && evidenceDirty ? "编辑草案与来源核对" : draftDirty ? "编辑草案" : "来源核对"}。切换步骤会保留，离开页面前请保存。</p>}
    {restored && dirty && <Notice>已从当前标签页内存恢复未保存草案，尚未提交。原有版本与核对修订号保持不变；请先核对当前来源是否仍可用。刷新浏览器或退出登录会清除此临时副本。</Notice>}
    {busy && <Notice><span role="status">{busy}…</span></Notice>}
    {feedback && <Notice tone={feedback.error ? "error" : "info"}>{feedback.text}</Notice>}
    {!mediaFresh && <Notice>媒体与操作已暂停，未保存编辑仍保留。请等待状态核对；读取失败时使用“刷新状态”重试。</Notice>}
    {uncertain && <Notice tone="error"><p>上次请求结果未确定，操作暂时锁定，避免重复写入。请先刷新并核对素材、版本与任务。</p><Button onClick={() => { ask("已核对服务端记录？", <p>请确认没有重复上传、重复版本或仍在处理的同一任务。解除锁定不会重发上次请求。</p>, async () => { await refresh(); setUncertain(false); return true; }, "已核对，解除操作锁定"); }}>我已核对当前记录</Button></Notice>}
    {status?.media_ready === false && <Notice tone="error">本机媒体工具尚未就绪，上传探测与合成可能失败；请先恢复本机媒体服务。</Notice>}
    {selectedVersion && !version && step === "preview" && <Notice tone="error">地址指定的版本当前不可用。请从本期版本中明确选择，不会自动导出其他版本。</Notice>}
    {step === "materials" && <MaterialsPanel episode={episode} source={source} busy={blocked} uploads={uploads} onUpload={files => { void upload(files); }} onSource={asset => showSource(asset.id)} onAdd={add} onDelete={asset => { void prepareDelete(asset); }} onCleanup={cleanup} onNext={() => navigate("direction")} />}
    {step === "direction" && <DirectionPanel episode={episode} source={source} evidence={evidence} revision={evidenceRevision} dirty={evidenceDirty} busy={blocked} demo={demo} workflow={workflow} reason={status?.cloud_reason || "运行状态尚未确定，模型操作保持禁用。"} packaging={packaging} onPackaging={setPackaging} onSource={showSource} onEvidence={editEvidence} onReload={reloadEvidence} onConfirm={confirmEvidence} onAnalyze={analyze} onDirections={directions} onSelect={selectDirection} onManual={() => navigate("edit")} />}
    {step === "edit" && <EditorPanel episode={episode} source={source} draft={draft} busy={blocked} demo={demo} onChange={editDraft} onSave={saveDraft} onSource={showSource} onMaterials={() => navigate("materials")} />}
    {step === "preview" && <PreviewPanel episode={episode} version={version} busy={blocked} demo={demo} workflow={workflow} mediaReady={status?.media_ready === true} watched={watched === viewKey} acknowledged={acknowledged} onWatched={() => { setWatched(viewKey); setAcknowledged(false); }} onAcknowledged={setAcknowledged} onVersion={id => { setWatched(null); setAcknowledged(false); navigate("preview", id); }} onEdit={loadEditor} onRender={render} onNarration={narration} onConfirm={confirmVersion} onDownload={download} />}
    <section className="panel jobs-panel"><div className="section-title"><h2>制作进度</h2><span className="muted">{running ? "读取真实任务状态" : "当前没有运行中任务"}</span></div>{!episode.jobs.length && <p className="muted">开始分析、生成音频或合成后，任务与失败原因会保存在这里。</p>}
      <ul className="job-list">{[...episode.jobs].reverse().map(job => <li key={job.id}><div className="toolbar"><strong>{({ analysis: demo ? "固定观察与文字" : "素材分析", directions: demo ? "固定演示方向" : "导演方向", narration: demo ? "非人声提示音" : "系统旁白", render: "本机视频合成" }[job.kind] || "已保存任务")}</strong><span className="chip">{({ queued: "排队中", running: "处理中", succeeded: "已完成", failed: "失败" }[job.state] || "状态待核对")}</span><span>{job.stage}</span></div>{job.error && <Notice tone="error">{job.error.message}{job.error.code === "INTERRUPTED" && " 中断前成功结果已保存，可从适用检查点继续。"}{costUncertain(job.error.code) && " 预算继续保留，费用核实前不能重试。"}</Notice>}{job.state === "failed" && modelJob(job) && !costUncertain(job.error?.code) && <Button disabled={blocked || !workflow} onClick={() => retry(job)}>重试这一失败步骤</Button>}</li>)}</ul>
      <details className="budget-detail"><summary>本期预算与占用</summary><p>本期已计费 ¥{episode.budget.spent.toFixed(2)} · 预留 ¥{episode.budget.reserved.toFixed(2)}／上限 ¥{episode.budget.limit.toFixed(2)}</p><p>首批已计费 ¥{episode.budget.total_spent.toFixed(2)} · 预留 ¥{episode.budget.total_reserved.toFixed(2)}／上限 ¥{episode.budget.total_limit.toFixed(2)}</p>{demo && <p>固定演示不产生真实模型消费；预留不等于实际扣费。</p>}</details>
    </section>
    <section className="panel"><h2>这一期，和朋友一起</h2><p className="muted">邀请本人补充片段、确认采访表达，或把已确认的一版留给受邀者观看。</p><div className="toolbar"><Link className="button secondary" href={`/episodes/${episodeId}/collaboration`} onClick={event => { if ((dirty || busy || uncertain) && !window.confirm("还有未保存修改或待核对结果。离开会保留已保存内容；是否继续？")) event.preventDefault(); }}>朋友供材与采访 ↗</Link><Link className="button secondary" href={`/episodes/${episodeId}/sharing`} onClick={event => { if ((dirty || busy || uncertain) && !window.confirm("还有未保存修改或待核对结果。离开会保留已保存内容；是否继续？")) event.preventDefault(); }}>私密分享与撤回 ↗</Link></div></section>
    <Dialog open={!!confirmation} onClose={() => { if (!busy) setConfirmation(null); }} title={confirmation?.title || "确认操作"}>
      {confirmation && <>{confirmation.body}{feedback?.error && <Notice tone="error">{feedback.text}</Notice>}<div className="toolbar"><Button disabled={!!busy} onClick={() => setConfirmation(null)}>取消</Button><Button variant="primary" busy={!!busy} disabled={!!busy} onClick={() => { const current = confirmation; void current.run().then(ok => { if (ok && mounted.current) setConfirmation(null); }).catch(error => setFeedback({ text: errorText(error), error: true })); }}>{confirmation.label}</Button></div></>}
    </Dialog>
  </div></MediaAccessContext.Provider>;
}

export default function Studio({ episodeId }: { episodeId: string }) {
  return <StudioWorkspace key={episodeId} episodeId={episodeId} />;
}
