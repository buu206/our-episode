"use client";

import { OutputCheck } from "./output-check";
import { FinishingEditor } from "./finishing-editor";
import { FinishingRehearsal } from "./finishing-rehearsal";
import { DirectorRun, runIsLive, type RunAction } from "./director-run";
import { SubtitleEditor, type SubtitleReviewTarget } from "./subtitle-editor";
import type { Segment } from "@/lib/types";
import previewStyles from "./preview.module.css";

import { useRef, useState } from "react";
import { ArrowDown, ArrowUp, CheckCircle2, Film, Plus, Trash2, Upload, X } from "lucide-react";
import { Button, Dialog, MediaPlayer, Notice } from "@/components/ui";
import type { Asset, Direction, Episode, Evidence, Packaging, Version } from "@/lib/types";
import { bytes, clockTime, hasNarratedIntro, editedSecond, editedDuration, draftProblem, wholeSecond, originName, versionState, type Draft, type Source, type UploadRow } from "./model";

import editorStyles from "./editor-plan.module.css";

type SourceProps = { episode: Episode; source: Source | null };
export function SourcePreviewDialog({ episode, source, onClose }: SourceProps & { onClose: () => void }) {
  const asset = episode.assets.find(item => item.id === source?.id);
  return <Dialog open={!!asset && !!source} onClose={onClose} title="原片预览" className={`source-preview-dialog${asset && asset.width < asset.height ? " portrait-preview" : ""}`}>
    {asset && source && <><MediaPlayer key={`${asset.id}-${source.sequence}`} src={asset.media_url} title={asset.filename} start={source.start} end={source.end} />
      <p className="muted">{source.end != null ? `${wholeSecond(source.start, "start")}—${wholeSecond(source.end, "end")} 秒 · ` : ""}{asset.width}×{asset.height} · 完整画幅，保留原声</p>
      <div className="dialog-actions"><Button onClick={onClose}>关闭预览</Button></div></>}
  </Dialog>;
}
export function MaterialsPanel({ episode, source, busy, uploads, onUpload, onSource, onAdd, onDelete, onCleanup, onNext }: SourceProps & {
  busy: boolean; uploads: UploadRow[]; onUpload: (files: File[]) => void; onSource: (asset: Asset) => void;
  onAdd: (asset: Asset) => void; onDelete: (asset: Asset) => void; onCleanup: (id: string, filename: string) => void; onNext: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [selectionError, setSelectionError] = useState("");
  const [previewId, setPreviewId] = useState<string | null>(null);
  const quota = episode.quota;
  function selectFiles(selected: File[]) {
    const next = [...files];
    for (const file of selected) {
      if (!/\.(mp4|mov)$/i.test(file.name) || !file.size) { setSelectionError("请选择非空的 MP4 / MOV 视频，已选择的素材仍保留。"); return; }
      if (!next.some(item => item.name === file.name && item.size === file.size && item.lastModified === file.lastModified)) next.push(file);
    }
    if ((quota?.original_count ?? episode.assets.length) + next.length > (quota?.max_count ?? 8)) { setSelectionError("本期最多 8 段视频，已选择的素材仍保留。"); return; }
    if ((quota?.total_bytes ?? 0) + next.reduce((sum, file) => sum + file.size, 0) > (quota?.max_bytes ?? 1024 ** 3)) { setSelectionError("本期素材合计不能超过 1 GiB，已选择的素材仍保留。"); return; }
    setFiles(next); setSelectionError("");
  }
  return <><div className="studio-grid">
    <div className="stack">
      <section className="panel"><div className="section-title"><span className="eyebrow">01 / 开场素材</span><h2>把这次的小事带过来</h2></div>
        <p className="muted">选择本次愿意提供的MP4／MOV，只上传本人或已获得同意的成年人素材。</p>
        <form className="upload-zone" onSubmit={event => { event.preventDefault(); if (files.length) { onUpload(files); setFiles([]); event.currentTarget.reset(); } }}>
          <label className="field"><span><Upload size={18} aria-hidden="true" /> 选择视频文件</span><input type="file" accept=".mp4,.mov,video/mp4,video/quicktime" multiple disabled={busy} onChange={event => { selectFiles(Array.from(event.target.files || [])); event.currentTarget.value = ""; }} /></label>
          <p>可以分次选择，素材会累加。点击上传后，逐份保存到本期。</p>
          {!!files.length && <div className="pending-files"><strong>待上传 {files.length} 份</strong><ul aria-label="待上传素材">{files.map((file, index) => <li key={`${file.name}:${file.size}:${file.lastModified}`}><span><strong>{file.name}</strong><small>{bytes(file.size)}</small></span><Button variant="ghost" disabled={busy} aria-label={`移除待上传的${file.name}`} onClick={() => { setFiles(previous => previous.filter((_, i) => i !== index)); setSelectionError(""); }}><X size={18} /></Button></li>)}</ul></div>}
          {selectionError && <Notice tone="error">{selectionError}</Notice>}
          <Button type="submit" variant="primary" disabled={busy || !files.length}>上传{files.length ? `这${files.length}份素材` : "所选素材"}</Button>
        </form>
        {quota ? <p className="quota-summary">原始素材 {quota.original_count}／{quota.max_count}份 · {clockTime(quota.total_seconds)}／{clockTime(quota.max_seconds)} · {bytes(quota.total_bytes)}／{bytes(quota.max_bytes)}<span className="muted">（包含仍留存的协作草稿与撤回素材）</span></p> : <Notice>服务尚未提供共享配额数据；以上传接口的实际校验为准。</Notice>}
        {!!uploads.length && <ul className="upload-list" aria-live="polite">{uploads.map((upload, index) => <li key={`${index}-${upload.name}`}>
          <strong>{upload.name}</strong><span>{({ waiting: "等待上传", uploading: "正在传输", processing: "已传输，正在校验媒体", done: "已保存到本期", failed: "未接收，请核对原因", unknown: "接收结果待核对", skipped: "未上传" })[upload.state]}</span>
          {upload.state === "uploading" && <progress aria-label={`${upload.name}传输进度`} value={upload.sent} max={upload.total || 1} />}
          {upload.error && <p className="error-text">{upload.error}</p>}
        </li>)}</ul>}
      </section>
    </div>
    <div className="stack">
      <section className="panel"><div className="section-title"><h2>本期素材</h2><span className="chip">{episode.assets.length}份可用</span></div>
        {!episode.assets.length && <div className="empty-state"><Film size={32} aria-hidden="true" /><h3>这一期，还等你开场。</h3><p>先上传一段视频，就可以开始；不用等齐所有朋友。</p></div>}
        <div className="asset-grid">{episode.assets.map((asset, index) => <article key={asset.id} className={`asset-card${source?.id === asset.id ? " selected" : ""}`}>
          <span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{asset.filename}</h3>
          <p className="asset-saved"><CheckCircle2 size={15} aria-hidden="true" />已保存 · 可预览</p>
          <p className="muted">{clockTime(asset.duration)} · {asset.width}×{asset.height} · {asset.has_audio ? "含原声" : "无音轨"}</p>
          {asset.source_kind !== "original" && <span className="chip">{asset.source_kind === "interview" ? "事后采访" : "朋友供材"} · {asset.contributor || "参与者"}</span>}
          <div className="toolbar"><Button disabled={busy} aria-haspopup="dialog" onClick={() => { onSource(asset); setPreviewId(asset.id); }}>预览原片</Button><Button disabled={busy} onClick={() => onAdd(asset)}><Plus size={16} aria-hidden="true" />加入选段</Button><Button variant="ghost" disabled={busy} onClick={() => onDelete(asset)} aria-label={`删除${asset.filename}`}><Trash2 size={16} aria-hidden="true" />删除</Button></div>
        </article>)}</div>
      </section>
      {episode.pending_cleanup == null ? <Notice>暂未取得待清理状态，可刷新后再核对。</Notice> : episode.pending_cleanup.length > 0 && <section className="panel"><h2>删除尚未清理完成</h2><Notice>这些素材已限制使用，应用副本尚未清理完成。可重试清理，只处理已确认的删除。</Notice>{episode.pending_cleanup.map(item => <div className="toolbar" key={item.asset_id}><span>{item.filename}</span><Button disabled={busy} onClick={() => onCleanup(item.asset_id, item.filename)}>重试清理</Button></div>)}</section>}
      <Button variant="primary" disabled={busy || !episode.assets.length} onClick={onNext}>去核对来源与方向 →</Button>
    </div>
    </div>
    <SourcePreviewDialog episode={episode} source={previewId ? { id: previewId, start: 0, sequence: 0 } : null} onClose={() => setPreviewId(null)} />
  </>;
}

export function PackagingField({ value, onChange, disabled, id }: { value: Packaging; onChange: (value: Packaging) => void; disabled?: boolean; id: string }) {
  return <label className="field" htmlFor={id}><span id={`${id}-label`}>节目包装</span><select id={id} aria-labelledby={`${id}-label`} value={value} disabled={disabled} onChange={event => onChange(event.target.value as Packaging)}>
    <option value="variety_v2">综艺节奏 · 重点花字与分拍动效</option><option value="variety">基础综艺 · 全句花字与贴纸</option><option value="standard">标准包装 · 静态花字，可加独立片头</option><option value="light">轻包装 · 仅标题与原声</option><option value="original">原声为主 · 不加标题花字和旁白</option>
  </select></label>;
}

export function DirectionPanel({ episode, evidence, revision, dirty, busy, demo, workflow, reason, packaging, onPackaging, onSource, onEvidence, onReload, onConfirm, onAnalyze, onDirections, onSelect, onManual, evidenceOnly = false }: SourceProps & {
  evidence: Evidence[]; revision: number; dirty: boolean; busy: boolean; demo: boolean; workflow: boolean; reason: string; evidenceOnly?: boolean;
  packaging: Packaging; onPackaging: (value: Packaging) => void; onSource: (assetId: string, start: number, end: number) => void;
  onEvidence: (items: Evidence[]) => void; onReload: () => void; onConfirm: () => void; onAnalyze: () => void; onDirections: () => void; onSelect: (direction: Direction) => void; onManual: () => void;
}) {
  const [detailsOpen, setDetailsOpen] = useState(dirty);
  const [preview, setPreview] = useState<Source | null>(null);
  const working = episode.jobs.some(job => ["analysis", "directions"].includes(job.kind) && ["queued", "running"].includes(job.state));
  const stale = revision !== episode.evidence_revision;
  const confirmed = !dirty && !stale && !!evidence.length && episode.analysis_state === "confirmed" && episode.evidence_confirmed_revision === episode.evidence_revision;
  const validDirections = confirmed && episode.directions_revision === episode.evidence_revision;
  function change(index: number, patch: Partial<Evidence>) { onEvidence(evidence.map((item, i) => i === index ? { ...item, ...patch } : item)); }
  function previewSource(id: string, start: number, end: number) {
    onSource(id, start, end); setPreview(previous => ({ id, start, end, sequence: (previous?.sequence || 0) + 1 }));
  }
  return <><div className={`studio-grid review-layout${evidenceOnly ? " review-only" : ""}`}>
    <section className="panel review-panel"><div className="section-title"><span className="eyebrow">02 / 快速确认</span><h2>{demo ? "试走一次内容确认" : confirmed ? "本期内容已确认" : "先看看，有没有理解错"}</h2></div>
      <p className="review-intro">{demo ? "以下是固定演示占位，不是真实画面理解或原声转写。确认只表示继续演示。" : "浏览素材概览，有明显不对的地方再展开修改。确认后继续选方向，成片导出前还会请你完整看一遍。"}</p>
      <div className="review-stats"><span>{episode.assets.length} 份素材</span><span>共 {Math.ceil(episode.assets.reduce((sum, asset) => sum + asset.duration, 0))} 秒</span><span>{confirmed ? "已确认" : "待你确认"}</span></div>
      {!!evidence.length && <div className="review-overview" aria-label="本期素材概览">{episode.assets.map((asset, index) => {
        const visual = evidence.find(item => item.asset_id === asset.id && item.kind !== "speech");
        const speech = evidence.find(item => item.asset_id === asset.id && item.kind === "speech");
        return <article className="review-summary" key={asset.id}><div className="review-summary-heading"><span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{asset.filename}</h3><span className="muted">{Math.ceil(asset.duration)} 秒</span></div>
          <div className="review-excerpts"><div><span className="eyebrow">画面摘录</span><p>{visual?.note || "暂无画面描述，可按需看看原片。"}</p></div><div><span className="eyebrow">原声摘录</span><p>{speech ? `“${speech.note}”` : asset.has_audio ? "暂无可用转写，可按需听听原声。" : "这份素材没有原声音轨。"}</p></div></div>
          <Button variant="ghost" disabled={busy} aria-haspopup="dialog" onClick={() => previewSource(asset.id, 0, asset.duration)}>看看原片</Button>
        </article>;
      })}</div>}
      {!evidence.length && <div className="empty-state"><Film size={30} /><p>{working ? "正在整理素材，完成后会显示概览。" : "先分析素材，看看这一期有什么。"}</p><Button variant="primary" disabled={busy || working || !workflow || !episode.assets.length} onClick={onAnalyze}>{demo ? "取得固定演示占位" : "分析当前素材"}</Button></div>}
      {stale && <Notice tone="error">来源核对已在服务端更新。当前未保存文字仍保留，请重新载入后核对。</Notice>}
      {dirty && <p className="dirty-note">有未保存的修改。保存确认后，旧方向和依赖版本需要重新核对。</p>}
      {(!!evidence.length || dirty) && <div className="review-confirmation">{confirmed ? <p className="review-confirmed"><CheckCircle2 size={19} aria-hidden="true" />已确认，可以继续选择方向。</p> : <><Button variant="primary" disabled={busy || working || stale || !["review", "confirmed"].includes(episode.analysis_state)} onClick={onConfirm}>{demo ? "确认演示概览" : "确认内容概览"}</Button><p className="muted">{evidence.length ? "确认用于继续创作，不代表逐条原话已经核准。" : "保存为空后，暂不能生成方向。"}</p></>}</div>}
      <div className="review-details-toggle"><Button variant="ghost" aria-expanded={detailsOpen} aria-controls="review-details" onClick={() => setDetailsOpen(!detailsOpen)}>{detailsOpen ? "收起详细内容" : `查看与修改详细内容（${evidence.length}）`}</Button></div>
      {detailsOpen && <div id="review-details"><p className="muted">需要时再回看、修改或移除。时间框按整秒显示；原始定位精度会保留。</p>
        <div className="evidence-list">{evidence.map((item, index) => { const asset = episode.assets.find(asset => asset.id === item.asset_id); return <article className="evidence-card" key={item.id}>
          <div className="section-title"><h3>{index + 1}. {asset?.filename || "来源已不可用"}</h3><span className="chip">{demo ? "演示占位" : item.kind === "speech" ? "原声转写" : "画面描述"}</span></div>
          <div className="toolbar"><Button disabled={busy || !asset} aria-haspopup="dialog" onClick={() => previewSource(item.asset_id, item.start, item.end)}>回看此处</Button><Button variant="ghost" disabled={busy} onClick={() => onEvidence(evidence.filter((_, i) => index !== i))}>移除不可靠内容</Button></div>
          <div className="field-row">{(["start", "end"] as const).map(key => <label className="field" key={key}>{key === "start" ? "开始" : "结束"}（秒）<input type="number" step="1" min="0" max={asset ? Math.ceil(asset.duration) : undefined} value={wholeSecond(item[key], key)} disabled={busy} onChange={event => change(index, { [key]: editedSecond(event.target.value, key, asset?.duration) })} /></label>)}</div>
          <label className="field">{demo ? "演示文字（非真实转写或观察）" : item.kind === "speech" ? "核对后的文字，不确定的原话请移除" : "只保留原片能支持的描述"}<textarea maxLength={2000} value={item.note} disabled={busy} onChange={event => change(index, { note: event.target.value })} /></label>
        </article>; })}</div>
        <div className="toolbar"><Button disabled={busy} onClick={onReload}>重新载入证据</Button><Button disabled={busy || working || !workflow || !episode.assets.length} onClick={onAnalyze}>{demo ? "重新取得演示占位" : episode.workflow?.analysis_cached ? "载入已有分析" : "重新分析当前素材"}</Button></div>
        <details className="coverage"><summary>查看分析说明</summary>{episode.analysis_notes.map((note, index) => <p className="muted" key={index}>{note}</p>)}{episode.analysis_coverage.map(item => <p key={item.asset_id}>{episode.assets.find(asset => asset.id === item.asset_id)?.filename || "来源素材"}：{demo ? "演示未理解画面。" : `抽帧时刻 ${item.sample_times.map(clockTime).join("、") || "无"}；采样点之间未连续观察。未连续覆盖：${item.unsampled_intervals.map(span => `${clockTime(span.start)}—${clockTime(span.end)}`).join("，") || "见采样说明"}`}</p>)}</details>
      </div>}
    </section>
    {!evidenceOnly && <section className="panel direction-panel"><div className="section-title"><span className="eyebrow">本期怎么讲</span><h2>{demo ? "固定方向，试着选一版" : "挑一个有依据的方向"}</h2></div>
      {!workflow && <Notice>{reason || "AI尚未启用。你可以使用人工选段与本机合成。"}</Notice>}
      <PackagingField id="direction-packaging" value={packaging} onChange={onPackaging} disabled={busy} />
      <Button variant="primary" disabled={busy || working || !workflow || !confirmed} onClick={onDirections}>{demo ? "取得固定演示方向" : "根据已确认证据提出方向"}</Button>
      {!episode.directions.length && <div className="empty-state"><h3>{confirmed ? "素材就绪，接下来选方向。" : "先确认概览，再看看怎么讲。"}</h3><p>{confirmed ? "可请求提案，也可以自己安排片段。" : "不必逐条回看，有疑问时再展开详细内容。"}</p></div>}
      <div className="direction-list">{episode.directions.map((direction, index) => <article key={direction.id} className="direction-card"><span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{direction.title}</h3><p>{direction.summary}</p>{episode.selected_direction === direction.id && <span className="chip">已选过此方向</span>}<p className="muted">这个方向依据的片段</p><ul>{direction.evidence.map((span, i) => <li key={i}><Button variant="ghost" disabled={busy || !episode.assets.some(asset => asset.id === span.asset_id)} aria-haspopup="dialog" onClick={() => previewSource(span.asset_id, span.start, span.end)}>{episode.assets.find(asset => asset.id === span.asset_id)?.filename || "来源不可用"} · {wholeSecond(span.start, "start")}—{wholeSecond(span.end, "end")} 秒</Button><p className="muted">{span.note}</p></li>)}</ul><Button variant="primary" disabled={busy || working || !validDirections} onClick={() => onSelect(direction)}>选这个方向，建立编辑草案</Button>{!validDirections && <p className="muted">依据已变化或尚未确认，请重新确认内容后取得方向。</p>}</article>)}</div>
      <Button variant="ghost" disabled={busy} onClick={onManual}>直接人工选段</Button>
    </section>}</div>
    <SourcePreviewDialog episode={episode} source={preview} onClose={() => setPreview(null)} />
  </>;
}

export function EditorPanel({ episode, draft, busy, demo, guided = false, onContinue, onChange, onSave, onSource, onMaterials, feedbackError }: SourceProps & {
  draft: Draft; busy: boolean; demo: boolean; onChange: (draft: Draft) => void; onSave: () => void;
  guided?: boolean; onContinue?: () => void; feedbackError?: string;
  onSource: (id: string, start: number, end: number) => void; onMaterials: () => void;
}) {
  const [preview, setPreview] = useState<Source | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [voiceEntry, setVoiceEntry] = useState<{ key: string; enabled: boolean } | null>(null);
  const voiceBackup = useRef<{ key: string; text: string } | null>(null);
  const draftKey = `${episode.id}:${draft.base_version_id || "new"}`;
  const voiceEnabled = hasNarratedIntro(draft.packaging) && (!!draft.narration_text.trim() || (voiceEntry?.key === draftKey && voiceEntry.enabled));
  function chooseVoice(enabled: boolean) {
    if (busy || (enabled && !hasNarratedIntro(draft.packaging))) return;
    if (!enabled && draft.narration_text.trim()) voiceBackup.current = { key: draftKey, text: draft.narration_text };
    setVoiceEntry({ key: draftKey, enabled });
    onChange({ ...draft, narration_text: enabled ? draft.narration_text || (voiceBackup.current?.key === draftKey ? voiceBackup.current.text : "") : "" });
  }
  function changePackaging(packaging: Packaging) {
    if (!hasNarratedIntro(packaging) && draft.narration_text.trim()) voiceBackup.current = { key: draftKey, text: draft.narration_text };
    if (!hasNarratedIntro(packaging)) setVoiceEntry({ key: draftKey, enabled: false });
    onChange({ ...draft, packaging, narration_text: hasNarratedIntro(packaging) ? draft.narration_text : "" });
  }
  const total = draft.segments.reduce((sum, item) => sum + Math.max(0, item.end - item.start), 0);
  const limit = hasNarratedIntro(draft.packaging) && draft.narration_text.trim() ? 60 : 90;
  const base = episode.versions.find(version => version.id === draft.base_version_id);
  function move(index: number, offset: number) { const segments = [...draft.segments]; [segments[index], segments[index + offset]] = [segments[index + offset], segments[index]]; onChange({ ...draft, segments }); }
  const editor = <div className="studio-grid"><div className="stack">
    <section className="panel"><div className="section-title"><span className="eyebrow">03 / 片段出场顺序</span><h2>把值得留下的，排成这一期</h2></div><p className="muted">{base ? originName(base.origin) : "人工片段选择 · 本地剪接验证，非AI结果"}。保存会建立新版本，已确认的版本保留。</p>
      {!draft.segments.length && <div className="empty-state"><p>还没有安排片段。</p><Button onClick={onMaterials} disabled={busy}>去素材中加入选段</Button></div>}
      <ol className="segment-list">{draft.segments.map((segment, index) => { const asset = episode.assets.find(item => item.id === segment.asset_id); const fixed = !!asset?.fixed_segment; return <li className="segment-card" key={`${index}-${segment.asset_id}`}>
        <div className="section-title"><span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{asset?.filename || "来源已不可用"}</h3>{fixed && <span className="chip">本人确认的固定片段</span>}</div>
        {!asset && <Notice tone="error">此来源已不可用。请移除本段，再核对新版本。</Notice>}
        {segment.purpose && <p className="muted"><strong>导演安排：</strong>{segment.purpose}</p>}
        {segment.transition && segment.transition !== "cut" && <div className="toolbar"><p className="muted">从上一镜头{segment.transition === "dissolve" ? "叠化进入" : "淡黑过渡"} · {segment.transition_duration} 秒</p><Button variant="ghost" disabled={busy} onClick={() => onChange({ ...draft, segments: draft.segments.map((item, i) => i === index ? { ...item, transition: "cut", transition_duration: 0 } : item) })}>改为直接切换</Button></div>}
        <div className="toolbar"><Button aria-label={`第${index + 1}段上移`} disabled={busy || index === 0} onClick={() => move(index, -1)}><ArrowUp size={16} />上移</Button><Button aria-label={`第${index + 1}段下移`} disabled={busy || index === draft.segments.length - 1} onClick={() => move(index, 1)}><ArrowDown size={16} />下移</Button><Button disabled={busy || !asset} aria-haspopup="dialog" onClick={() => { onSource(segment.asset_id, segment.start, segment.end); setPreview({ id: segment.asset_id, start: segment.start, end: segment.end, sequence: index }); }}>回看选段</Button><Button variant="ghost" disabled={busy} onClick={() => onChange({ ...draft, segments: draft.segments.filter((_, i) => i !== index) })}>移除本段</Button></div>
        <div className="field-row">{(["start", "end"] as const).map(key => <label className="field" key={key}>{key === "start" ? "开始" : "结束"}（秒）<input type="number" min="0" step="1" max={asset ? Math.ceil(asset.duration) : undefined} disabled={busy || fixed} value={wholeSecond(segment[key], key)} onChange={event => onChange({ ...draft, segments: draft.segments.map((item, i) => i === index ? { ...item, [key]: editedSecond(event.target.value, key, asset?.duration) } : item) })} /></label>)}</div>
        <label className="field">花字（最多100字）<input value={segment.caption} maxLength={100} disabled={busy || fixed || !hasNarratedIntro(draft.packaging)} onChange={event => onChange({ ...draft, segments: draft.segments.map((item, i) => i === index ? { ...item, caption: event.target.value } : item) })} /></label>{draft.packaging === "light" && <p className="muted">轻包装只显示标题；选择标准或综艺包装可添加片段花字。</p>}
        {fixed && <p className="muted">可以移动或整体移除；截短和改写需要本人重新确认修订，不能直接修改。</p>}
      </li>; })}</ol><Button disabled={busy} onClick={onMaterials}><Plus size={16} />继续加入素材</Button>
    </section></div>
    <section className="panel editor-settings"><div className="section-title"><span className="eyebrow">这一版的包装</span><h2>让真人和原声做主角</h2></div>
      <label className="field">新版本名称<input maxLength={120} value={draft.title} disabled={busy} placeholder="例如：第一版 · 先看看" onChange={event => onChange({ ...draft, title: event.target.value })} /></label>
      <PackagingField id="edit-packaging" value={draft.packaging} disabled={busy} onChange={packaging => onChange({ ...draft, packaging, narration_text: hasNarratedIntro(packaging) ? draft.narration_text : "" })} />
      {["variety", "variety_v2"].includes(draft.packaging) && <div className="packaging-sample" aria-label="综艺包装样式说明"><span aria-hidden="true" className="packaging-sample-sticker">在场 · 开拍！</span><p>{draft.packaging === "variety_v2" ? "重点字分拍入场，搭配贴纸与短促音效；镜头之间留出原画，原声和人物画幅保持完整。" : "整句花字弹跳入场，搭配节目贴纸与短促音效；原声和人物画幅保持完整。"}</p></div>}
      {draft.packaging === "original" ? <p className="muted">不添加标题、花字和旁白；事后采访的身份标记保留。视频仍会720p转码，不是无损导出。</p> : <p className="muted">花字围绕真实内容。包装修改另存新版本，已有成片不被覆盖。</p>}
      {hasNarratedIntro(draft.packaging) && <><label className="field">{demo ? "演示片头文字（不会朗读）" : "旁白文案（可选，最多120字）"}<textarea maxLength={120} value={draft.narration_text} disabled={busy} onChange={event => onChange({ ...draft, narration_text: event.target.value })} /></label><Notice>{demo ? "演示只生成约1秒合成提示音，非人声、非旁白；文案不会被朗读。" : "已确认且文案、选段均未变的旁白可直接复用；改写后需重新确认生成。旁白放在独立片头。留空可直接本机合成。"}</Notice></>}
      <p className={total > limit ? "error-text" : "duration-summary"}>{draft.segments.length}段 · 合计 {Number.isFinite(total) ? Math.ceil(total) : "—"} 秒／{limit}秒{limit === 60 && "，为片头预留空间"}</p>
      {draft.segments.some(item => item.transition && item.transition !== "cut") && <p className="muted">转场衔接后正文约 {Math.ceil(editedDuration(draft.segments))} 秒，独立片头另计。</p>}
      <div className="sticky-action"><Button variant="primary" disabled={busy || !draft.segments.length || (guided && !!draftProblem(draft, episode))} onClick={onSave}>保存为新版本</Button><p className="muted">先保存，再合成与预览。</p></div>
    </section></div>;
  const problem = guided ? draftProblem(draft, episode) : null;
  const duration = editedDuration(draft.segments);
  return <>{guided ? <>
    <section className={`panel ${editorStyles.plan}`} aria-labelledby="director-plan-heading">
      <header className={editorStyles.heading}>
        <div><span className={editorStyles.kicker}>03 / 这版这样拍</span><h2 id="director-plan-heading">导演的镜头安排</h2>
          <p>{draft.segments.length} 个片段 · 正文约 {Number.isFinite(duration) ? Math.max(0, Math.ceil(duration)) : "—"} 秒</p></div>
        <Button className={editorStyles.adjust} disabled={busy} aria-haspopup="dialog" onClick={() => setManualOpen(true)}>微调片段</Button>
      </header>
      {!!draft.segments.length ? <ol className={editorStyles.shotRail} aria-label="镜头安排">{draft.segments.map((segment, index) => {
        const asset = episode.assets.find(item => item.id === segment.asset_id);
        return <li className={editorStyles.shot} key={`${index}-${segment.asset_id}`}>
          <div className={editorStyles.shotTop}><span>{String(index + 1).padStart(2, "0")}</span><span>{Math.max(0, Math.ceil(segment.end - segment.start))} 秒</span></div>
          <strong title={segment.caption || segment.purpose || "已保存选段"}>{segment.caption?.trim() || segment.purpose?.trim() || "已保存选段"}</strong>
          {segment.caption?.trim() && segment.purpose?.trim() && <p title={segment.purpose}>{segment.purpose}</p>}
          <Button variant="ghost" disabled={busy || !asset} aria-haspopup="dialog" aria-label={`回看第${index + 1}段`} onClick={() => { onSource(segment.asset_id, segment.start, segment.end); setPreview({ id: segment.asset_id, start: segment.start, end: segment.end, sequence: index }); }}>回看这一段 ↗</Button>
        </li>;
      })}</ol> : <div className={editorStyles.empty}><p>先放进想留下的片段。</p><Button disabled={busy} onClick={onMaterials}>去素材中加入选段</Button></div>}
      <div className={editorStyles.choices}>
        <div className={editorStyles.sound}>
          <h3>声音怎么留</h3>
          <div className={editorStyles.soundSwitch} role="group" aria-label="旁白选择">
            <button type="button" aria-pressed={!voiceEnabled} disabled={busy} onClick={() => chooseVoice(false)}>保留现场原声</button>
            <button type="button" aria-pressed={voiceEnabled} disabled={busy || !hasNarratedIntro(draft.packaging)} onClick={() => chooseVoice(true)}>{demo ? "加演示片头" : "加开场旁白"}</button>
          </div>
          {!hasNarratedIntro(draft.packaging) && <p className={editorStyles.hint}>选择综艺或标准包装可加旁白。</p>}
          {voiceEnabled && <label className={`field ${editorStyles.voiceField}`}>{demo ? "演示片头文字（不会朗读）" : "开场旁白文案"}<textarea aria-label={demo ? "演示片头文字（不会朗读）" : "开场旁白文案"} maxLength={120} rows={2} value={draft.narration_text} disabled={busy} placeholder="想让导演在开场说什么？" onChange={event => onChange({ ...draft, narration_text: event.target.value })} /><span>{demo ? "演示只生成提示音，不朗读文字。" : "只放在开场，保留现场原声。"}</span></label>}
        </div>
        <div className={editorStyles.packaging}><PackagingField id="plan-packaging" value={draft.packaging} disabled={busy} onChange={changePackaging} /></div>
      </div>
      {problem && <Notice tone="error">{problem}{draft.segments.length > 0 && " 点“微调片段”可修改。"}</Notice>}
      <footer className={editorStyles.actions}>
        <p>{draft.narration_text.trim() && hasNarratedIntro(draft.packaging) ? "下一步核对文案，再生成旁白。" : "保存并生成预览，原版本保留。"}</p>
        <Button variant="primary" disabled={busy || !!problem || !onContinue} onClick={onContinue}>{draft.narration_text.trim() && hasNarratedIntro(draft.packaging) ? "下一步：确认旁白" : "生成预览"}</Button>
      </footer>
    </section>
    <Dialog open={manualOpen} onClose={() => setManualOpen(false)} title="微调片段与包装" className={editorStyles.manualDialog}>
      {manualOpen && <div className={editorStyles.manualBody}>{(problem || feedbackError) && <Notice tone="error">{problem || feedbackError}</Notice>}{editor}<div className={editorStyles.manualDone}><Button onClick={() => setManualOpen(false)}>完成微调</Button></div></div>}
    </Dialog>
  </> : editor}<SourcePreviewDialog episode={episode} source={preview} onClose={() => setPreview(null)} /></>;
}

export function PreviewPanel({ episode, version, busy, demo, workflow, mediaReady, watched, acknowledged, onWatched, onAcknowledged, onVersion, onEdit, onRender, onNarration, onConfirm, onDownload, onSubtitles, onCheck, onVisualCheck, onAudioCheck, onRunControl, onFinished }: {
  onFinished?: (version: Version) => Promise<void>;
  episode: Episode; version?: Version; busy: boolean; demo: boolean; workflow: boolean; mediaReady: boolean; watched: boolean; acknowledged: boolean;
  onWatched: () => void; onAcknowledged: (value: boolean) => void; onVersion: (id: string) => void; onEdit: (version: Version, without?: boolean) => void;
  onRender: () => void; onNarration: () => void; onConfirm: () => void; onDownload: () => void; onSubtitles?: (version: Version, segments: Segment[]) => Promise<boolean>; onCheck?: () => void; onVisualCheck?: () => void; onAudioCheck?: () => void; onRunControl?: (action: RunAction) => void;
}) {
  const [finishingOpen, setFinishingOpen] = useState(false);
  const [subtitlesOpen, setSubtitlesOpen] = useState(false);
  const [subtitleTarget, setSubtitleTarget] = useState<SubtitleReviewTarget | undefined>();
  function openSubtitles(target?: SubtitleReviewTarget) { setSubtitleTarget(target); setSubtitlesOpen(true); }
  const [versionsOpen, setVersionsOpen] = useState(false);
  const run = [...episode.jobs].reverse().find(job => job.kind === "director_run" && job.version_id === version?.id);
  const delegated = runIsLive(run);
  const playable = version && ["ready", "confirmed"].includes(version.state);
  const supported = !!version && ["manual", "ai", "demo"].includes(version.origin) && ["original", "light", "standard", "variety", "variety_v2"].includes(version.packaging) && ["draft", "failed", "ready", "confirmed", "restricted", "rendering", "voicing"].includes(version.state);
  const needsVoice = !!version?.narration_text.trim() && !version.narration_ready;
  const hasRehearsal = episode.jobs.some(job => job.kind === "finishing_rehearsal" && Array.isArray(job.result?.checkpoints) && job.result.checkpoints.some(point => point?.version_id === version?.id));
  return <section className={`panel preview-panel ${previewStyles.preview}`}>
    <header className={previewStyles.heading}><div><span className="eyebrow">04 / 看看这一版</span><h2>{version?.title || "这一期，还没有保存版本"}</h2></div><Button aria-haspopup="dialog" onClick={() => setVersionsOpen(true)} disabled={busy}>切换版本（{episode.versions.length}）</Button></header>
    {version ? <>
      <div className={previewStyles.meta}><span className="chip">{run?.state === "paused" ? "已暂停" : run?.state === "pausing" ? "正在收尾后暂停" : run?.state === "blocked" ? "委托已停止" : versionState(version.state)}</span>{demo && <span className="chip">固定演示 · 非真实AI</span>}{["variety", "variety_v2"].includes(version.packaging) && <span className="chip variety-chip">{version.packaging === "variety_v2" ? "综艺节奏" : "基础综艺"}</span>}</div>
      <DirectorRun run={run} busy={busy} onControl={onRunControl} />
      {hasRehearsal && <details><summary>查看此前的三步试跑</summary><FinishingRehearsal episode={episode} versionId={version.id} busy={busy} onVersion={onVersion} /></details>}
      <div className={previewStyles.stage}>
        <div className={previewStyles.screen}>
          {playable ? <MediaPlayer key={`${version.id}-${version.media_url}-${version.state}`} src={version.media_url} title={version.title} onEnded={onWatched} /> : <div className={previewStyles.waiting}><Film size={34} aria-hidden="true" /><h3>{delegated ? run?.stage : version.state === "rendering" ? "这一期，正在成片" : needsVoice ? "先决定开场的声音" : "片段已就位"}</h3><p>{run?.state === "paused" ? "点上方“继续制作”，导演就会接着做。" : run?.state === "blocked" ? "查看上方原因，已有输入继续保留。" : version.state === "rendering" ? "完成后，预览会出现在这里。" : needsVoice ? "核对旁白，或直接用现场原声。" : "生成后即可播放。"}</p></div>}
        </div>
        <aside className={previewStyles.controls} aria-label="本版操作">
          {playable && version.packaging === "variety_v2" && onFinished && <Button variant="primary" disabled={busy || delegated} onClick={() => setFinishingOpen(true)}>三步调整 · 文字 / 声音 / 包装</Button>}
          {playable && <OutputCheck key={version.id} version={version} jobs={episode.jobs} busy={busy} onCheck={onCheck} onVisualCheck={!demo && workflow ? onVisualCheck : undefined} onAudioCheck={!demo && workflow ? onAudioCheck : undefined} onSubtitles={onSubtitles && version.packaging === "variety_v2" ? openSubtitles : undefined} />}
          {version.error && <Notice tone="error">{version.error.message}</Notice>}
          {version.state === "restricted" && <Notice tone="error">来源或授权已变化，请核对来源后建立新版本。</Notice>}
          {!supported && <Notice>当前页面尚不支持此版本的状态或包装，修改、确认和导出保持关闭。</Notice>}
          {version.narration_text && <details className={previewStyles.voice} open={needsVoice ? true : undefined}><summary>{demo ? "演示片头 · 非人声" : "本版旁白文案"}{version.narration_ready && " · 音频已就绪"}</summary><p className="narration-copy">{version.narration_text}</p>
            {demo && <p>只合成提示音，不朗读文案、不调用模型。</p>}
            {needsVoice && <div className={previewStyles.voiceActions}><Button variant="primary" disabled={busy || !supported || !workflow || !["draft", "failed"].includes(version.state)} onClick={onNarration}>{demo ? "确认并生成提示音" : "确认文案并生成旁白"}</Button><Button disabled={busy || !supported} onClick={() => onEdit(version, true)}>另存无旁白草案</Button>{!workflow && <p>旁白生成尚未开放，可先用现场原声。</p>}</div>}
          </details>}
          {!delegated && ["draft", "failed"].includes(version.state) && <Button variant="primary" disabled={busy || !supported || !mediaReady || needsVoice} onClick={onRender}>{version.state === "failed" ? "重试本机合成" : "开始本机合成"}</Button>}
          {!delegated && ["rendering", "voicing"].includes(version.state) && <p role="status">{version.state === "rendering" ? "正在合成，完成后自动更新。" : demo ? "正在生成非人声提示音。" : "正在生成旁白，完成后可合成视频。"}</p>}
          {version.state === "ready" && <div className={previewStyles.confirmation}><p>{watched ? "已经播完，可以确认这一版了。" : "看完后勾选，就能确认这一版。"}</p><label className="check-field"><input type="checkbox" checked={acknowledged} disabled={busy || !supported || !version.media_url} onChange={event => onAcknowledged(event.target.checked)} />我已看完，内容符合表达，素材有权使用。{demo && "（这是固定演示）"}</label><Button variant="primary" disabled={busy || !supported || !version.media_url || !acknowledged} onClick={onConfirm}>确认当前版本</Button></div>}
          {version.state === "confirmed" && supported && <><span className="chip success">已确认此版本</span><Button variant="primary" disabled={busy} onClick={onDownload}>导出这一版MP4</Button></>}
          {onSubtitles && version.packaging === "variety_v2" && playable && <Button disabled={busy} onClick={() => openSubtitles()}>校对字幕</Button>}
          <Button disabled={busy || !supported} onClick={() => onEdit(version)}>以这版为基础继续修改</Button>
        </aside>
      </div>
    </> : <div className="empty-state"><Film size={36} /><p>选好拍法，就可以生成第一版。</p></div>}
    {finishingOpen && version && onFinished && <FinishingEditor key={version.id} version={version} onClose={() => setFinishingOpen(false)} onCreated={onFinished} />}
    {subtitlesOpen && version && onSubtitles && <SubtitleEditor key={`${version.id}-${subtitleTarget?.cue_id ?? "all"}`} target={subtitleTarget} version={version} assets={episode.assets} busy={busy} onClose={() => setSubtitlesOpen(false)} onSave={segments => onSubtitles(version, segments)} />}
    <Dialog open={versionsOpen} onClose={() => setVersionsOpen(false)} title="本期版本">
      <div className="version-list">{[...episode.versions].reverse().map(item => <button key={item.id} className={`version-card${item.id === version?.id ? " selected" : ""}`} aria-pressed={item.id === version?.id} disabled={busy} onClick={() => { onVersion(item.id); setVersionsOpen(false); }}><strong>{item.title}</strong><span className="chip">{versionState(item.state)}</span><span className="muted">{originName(item.origin)}</span></button>)}</div>
    </Dialog>
  </section>;
}
