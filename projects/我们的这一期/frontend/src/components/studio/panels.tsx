"use client";

import { useState } from "react";
import { ArrowDown, ArrowUp, Film, Plus, Trash2, Upload } from "lucide-react";
import { Button, MediaPlayer, Notice } from "@/components/ui";
import type { Asset, Direction, Episode, Evidence, Packaging, Version } from "@/lib/types";
import { bytes, clockTime, originName, versionState, type Draft, type Source, type UploadRow } from "./model";

type SourceProps = { episode: Episode; source: Source | null };
export function SourcePreview({ episode, source }: SourceProps) {
  const asset = episode.assets.find(item => item.id === source?.id);
  return <section className="panel source-panel" aria-label="来源预览">
    <div className="section-title"><span className="chip">回到原片</span><h2>{asset?.filename || "先看见真实发生的事"}</h2></div>
    <MediaPlayer key={`${asset?.id}-${source?.sequence}`} src={asset?.media_url} title={asset ? `${asset.filename} · 来源预览` : "选择一份素材进行预览"} start={source?.start} end={source?.end} />
    <p className="muted">{asset?.source_kind === "interview" ? "事后采访 · 本人原声与已确认表达" : "原片预览 · 保留现场语境"}{source?.end != null ? ` · ${clockTime(source.start)}—${clockTime(source.end)}` : ""}</p>
  </section>;
}

export function MaterialsPanel({ episode, source, busy, uploads, onUpload, onSource, onAdd, onDelete, onCleanup, onNext }: SourceProps & {
  busy: boolean; uploads: UploadRow[]; onUpload: (files: File[]) => void; onSource: (asset: Asset) => void;
  onAdd: (asset: Asset) => void; onDelete: (asset: Asset) => void; onCleanup: (id: string, filename: string) => void; onNext: () => void;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const quota = episode.quota;
  return <div className="studio-grid">
    <div className="stack">
      <SourcePreview episode={episode} source={source} />
      <section className="panel"><div className="section-title"><span className="eyebrow">01 / 开场素材</span><h2>把这次的小事带过来</h2></div>
        <p className="muted">选择本次愿意提供的MP4／MOV，只上传本人或已获得同意的成年人素材。</p>
        <form className="upload-zone" onSubmit={event => { event.preventDefault(); if (files.length) { onUpload(files); setFiles([]); event.currentTarget.reset(); } }}>
          <label className="field"><span><Upload size={18} aria-hidden="true" /> 选择视频文件</span><input type="file" accept=".mp4,.mov,video/mp4,video/quicktime" multiple disabled={busy} onChange={event => setFiles(Array.from(event.target.files || []))} /></label>
          <Button type="submit" variant="primary" disabled={busy || !files.length}>上传{files.length ? `这${files.length}份素材` : "所选素材"}</Button>
        </form>
        {quota ? <p className="quota-summary">原始素材 {quota.original_count}／{quota.max_count}份 · {clockTime(quota.total_seconds)}／{clockTime(quota.max_seconds)} · {bytes(quota.total_bytes)}／{bytes(quota.max_bytes)}<span className="muted">（包含仍留存的协作草稿与撤回素材）</span></p> : <Notice>服务尚未提供共享配额数据；以上传接口的实际校验为准。</Notice>}
        {!!uploads.length && <ul className="upload-list" aria-live="polite">{uploads.map((upload, index) => <li key={`${index}-${upload.name}`}>
          <strong>{upload.name}</strong><span>{({ waiting: "等待上传", uploading: "正在传输", processing: "已传输，正在校验媒体", done: "已接收", failed: "未接收，请核对原因" })[upload.state]}</span>
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
          <p className="muted">{clockTime(asset.duration)} · {asset.width}×{asset.height} · {asset.has_audio ? "含原声" : "无音轨"}</p>
          {asset.source_kind !== "original" && <span className="chip">{asset.source_kind === "interview" ? "事后采访" : "朋友供材"} · {asset.contributor || "参与者"}</span>}
          <div className="toolbar"><Button disabled={busy} onClick={() => onSource(asset)}>预览原片</Button><Button disabled={busy} onClick={() => onAdd(asset)}><Plus size={16} aria-hidden="true" />加入选段</Button><Button variant="ghost" disabled={busy} onClick={() => onDelete(asset)} aria-label={`删除${asset.filename}`}><Trash2 size={16} aria-hidden="true" />删除</Button></div>
        </article>)}</div>
      </section>
      {episode.pending_cleanup == null ? <Notice>暂未取得待清理状态，可刷新后再核对。</Notice> : episode.pending_cleanup.length > 0 && <section className="panel"><h2>删除尚未清理完成</h2><Notice>这些素材已限制使用，应用副本尚未清理完成。可重试清理，只处理已确认的删除。</Notice>{episode.pending_cleanup.map(item => <div className="toolbar" key={item.asset_id}><span>{item.filename}</span><Button disabled={busy} onClick={() => onCleanup(item.asset_id, item.filename)}>重试清理</Button></div>)}</section>}
      <Button variant="primary" disabled={busy || !episode.assets.length} onClick={onNext}>去核对来源与方向 →</Button>
    </div>
  </div>;
}

export function PackagingField({ value, onChange, disabled, id }: { value: Packaging; onChange: (value: Packaging) => void; disabled?: boolean; id: string }) {
  return <label className="field" htmlFor={id}><span id={`${id}-label`}>节目包装</span><select id={id} aria-labelledby={`${id}-label`} value={value} disabled={disabled} onChange={event => onChange(event.target.value as Packaging)}>
    <option value="original">原声为主 · 不加标题花字和旁白</option><option value="light">轻包装 · 花字与原声</option><option value="standard">标准包装 · 可加独立片头</option>
  </select></label>;
}

export function DirectionPanel({ episode, source, evidence, revision, dirty, busy, demo, workflow, reason, packaging, onPackaging, onSource, onEvidence, onReload, onConfirm, onAnalyze, onDirections, onSelect, onManual }: SourceProps & {
  evidence: Evidence[]; revision: number; dirty: boolean; busy: boolean; demo: boolean; workflow: boolean; reason: string;
  packaging: Packaging; onPackaging: (value: Packaging) => void; onSource: (assetId: string, start: number, end: number) => void;
  onEvidence: (items: Evidence[]) => void; onReload: () => void; onConfirm: () => void; onAnalyze: () => void; onDirections: () => void; onSelect: (direction: Direction) => void; onManual: () => void;
}) {
  const working = episode.jobs.some(job => ["analysis", "directions"].includes(job.kind) && ["queued", "running"].includes(job.state));
  const stale = revision !== episode.evidence_revision;
  const confirmed = !dirty && !stale && !!evidence.length && episode.analysis_state === "confirmed" && episode.evidence_confirmed_revision === episode.evidence_revision;
  const validDirections = confirmed && episode.directions_revision === episode.evidence_revision;
  function change(index: number, patch: Partial<Evidence>) { onEvidence(evidence.map((item, i) => i === index ? { ...item, ...patch } : item)); }
  return <div className="studio-grid"><div className="stack"><SourcePreview episode={episode} source={source} />
    <section className="panel"><div className="section-title"><span className="eyebrow">02 / 先核对，再定方向</span><h2>{demo ? "试走一次核对流程" : "这一期，从真实的片段开始"}</h2></div>
      <Notice>{demo ? "以下观察和文字都是固定演示占位，没有理解画面或转写原声。确认只表示继续演示。" : "观察和转写需要你回看原片核对；静态采样不能代表连续理解。不确定的原话请修改或移除。"}</Notice>
      {!workflow && <Notice>{reason || "AI尚未启用。你可以使用人工选段与本机合成。"}</Notice>}
      <div className="toolbar"><Button variant="primary" disabled={busy || working || !workflow || !episode.assets.length} onClick={onAnalyze}>{demo ? "取得固定演示占位" : episode.evidence.length ? "重新分析当前素材" : "分析当前素材"}</Button><Button disabled={busy} onClick={onManual}>直接人工选段</Button></div>
      {episode.analysis_notes.map((note, index) => <p className="muted" key={index}>{note}</p>)}
      {episode.analysis_coverage.map(item => <details key={item.asset_id} className="coverage"><summary>{episode.assets.find(asset => asset.id === item.asset_id)?.filename || "来源素材"} · {demo ? "演示说明" : "采样覆盖"}</summary><p>{demo ? "演示没有理解画面，全部时间段均需人工核对。" : `抽帧时刻：${item.sample_times.map(clockTime).join("、") || "无"}。采样点之间未连续观察。`}</p>{!demo && <p>未连续覆盖：{item.unsampled_intervals.map(span => `${clockTime(span.start)}—${clockTime(span.end)}`).join("，") || "见采样说明"}</p>}</details>)}
      {stale && <Notice tone="error">来源核对已在服务端更新。当前未保存文字仍保留，请重新载入后核对。</Notice>}
      {dirty && <p className="dirty-note">有未保存的核对修改。</p>}
      <div className="evidence-list">{evidence.map((item, index) => <article className="evidence-card" key={item.id}>
        <div className="section-title"><h3>{index + 1}. {episode.assets.find(asset => asset.id === item.asset_id)?.filename || "来源已不可用"}</h3><span className="chip">{demo ? "演示占位" : item.kind === "speech" ? "原声转写 · 待核对" : "静态观察 · 待核对"}</span></div>
        <div className="toolbar"><Button disabled={busy || !episode.assets.some(asset => asset.id === item.asset_id)} onClick={() => onSource(item.asset_id, item.start, item.end)}>回看此处</Button><Button variant="ghost" disabled={busy} onClick={() => onEvidence(evidence.filter((_, i) => index !== i))}>移除不可靠内容</Button></div>
        <div className="field-row"><label className="field">开始（秒）<input type="number" step="0.01" min="0" value={Number.isFinite(item.start) ? item.start : ""} disabled={busy} onChange={event => change(index, { start: event.target.value === "" ? NaN : Number(event.target.value) })} /></label><label className="field">结束（秒）<input type="number" step="0.01" min="0" value={Number.isFinite(item.end) ? item.end : ""} disabled={busy} onChange={event => change(index, { end: event.target.value === "" ? NaN : Number(event.target.value) })} /></label></div>
        <label className="field">{demo ? "演示文字（非真实转写或观察）" : item.kind === "speech" ? "核对后的文字，不确定的原话请移除" : "只保留原片能支持的描述"}<textarea maxLength={2000} value={item.note} disabled={busy} onChange={event => change(index, { note: event.target.value })} /></label>
      </article>)}</div>
      {!evidence.length && <p className="empty-state">当前没有可核对的证据；缺少可靠依据时可保留为空。</p>}
      <div className="toolbar"><Button variant="primary" disabled={busy || working || stale || !["review", "confirmed"].includes(episode.analysis_state) || (!evidence.length && !dirty)} onClick={onConfirm}>{demo ? "保存并确认演示占位" : "保存并确认证据"}</Button><Button disabled={busy} onClick={onReload}>重新载入证据</Button></div>
    </section></div>
    <section className="panel direction-panel"><div className="section-title"><span className="eyebrow">本期怎么讲</span><h2>{demo ? "固定方向，试着选一版" : "挑一个有依据的方向"}</h2></div>
      <PackagingField id="direction-packaging" value={packaging} onChange={onPackaging} disabled={busy} />
      <Button variant="primary" disabled={busy || working || !workflow || !confirmed} onClick={onDirections}>{demo ? "取得固定演示方向" : "根据已确认证据提出方向"}</Button>
      {!episode.directions.length && <div className="empty-state"><h3>不为凑选项，编一个故事。</h3><p>{confirmed ? "现在还没有方向。可请求提案；素材不足时也可以人工剪一段短回顾。" : "先回看来源并确认至少一条证据，再提出方向。"}</p></div>}
      <div className="direction-list">{episode.directions.map((direction, index) => <article key={direction.id} className="direction-card"><span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{direction.title}</h3><p>{direction.summary}</p>{episode.selected_direction === direction.id && <span className="chip">已选过此方向</span>}<p className="muted">这个方向依据的片段</p><ul>{direction.evidence.map((span, i) => <li key={i}><Button variant="ghost" disabled={busy} onClick={() => onSource(span.asset_id, span.start, span.end)}>{episode.assets.find(asset => asset.id === span.asset_id)?.filename || "来源不可用"} · {clockTime(span.start)}—{clockTime(span.end)}</Button><p className="muted">{span.note}</p></li>)}</ul><Button variant="primary" disabled={busy || working || !validDirections} onClick={() => onSelect(direction)}>选这个方向，建立编辑草案</Button>{!validDirections && <p className="muted">依据已变化或尚未确认，请重新核对后取得方向。</p>}</article>)}</div>
    </section></div>;
}

export function EditorPanel({ episode, draft, busy, demo, onChange, onSave, onSource, source, onMaterials }: SourceProps & {
  draft: Draft; busy: boolean; demo: boolean; onChange: (draft: Draft) => void; onSave: () => void;
  onSource: (id: string, start: number, end: number) => void; onMaterials: () => void;
}) {
  const total = draft.segments.reduce((sum, item) => sum + Math.max(0, item.end - item.start), 0);
  const limit = draft.packaging === "standard" && draft.narration_text.trim() ? 60 : 90;
  const base = episode.versions.find(version => version.id === draft.base_version_id);
  function move(index: number, offset: number) { const segments = [...draft.segments]; [segments[index], segments[index + offset]] = [segments[index + offset], segments[index]]; onChange({ ...draft, segments }); }
  return <div className="studio-grid"><div className="stack"><SourcePreview episode={episode} source={source} />
    <section className="panel"><div className="section-title"><span className="eyebrow">03 / 片段出场顺序</span><h2>把值得留下的，排成这一期</h2></div><p className="muted">{base ? originName(base.origin) : "人工片段选择 · 本地剪接验证，非AI结果"}。保存会建立新版本，已确认的版本保留。</p>
      {!draft.segments.length && <div className="empty-state"><p>还没有安排片段。</p><Button onClick={onMaterials} disabled={busy}>去素材中加入选段</Button></div>}
      <ol className="segment-list">{draft.segments.map((segment, index) => { const asset = episode.assets.find(item => item.id === segment.asset_id); const fixed = !!asset?.fixed_segment; return <li className="segment-card" key={`${index}-${segment.asset_id}`}>
        <div className="section-title"><span className="segment-number">{String(index + 1).padStart(2, "0")}</span><h3>{asset?.filename || "来源已不可用"}</h3>{fixed && <span className="chip">本人确认的固定片段</span>}</div>
        {!asset && <Notice tone="error">此来源已不可用。请移除本段，再核对新版本。</Notice>}
        <div className="toolbar"><Button aria-label={`第${index + 1}段上移`} disabled={busy || index === 0} onClick={() => move(index, -1)}><ArrowUp size={16} />上移</Button><Button aria-label={`第${index + 1}段下移`} disabled={busy || index === draft.segments.length - 1} onClick={() => move(index, 1)}><ArrowDown size={16} />下移</Button><Button disabled={busy || !asset} onClick={() => onSource(segment.asset_id, segment.start, segment.end)}>回看选段</Button><Button variant="ghost" disabled={busy} onClick={() => onChange({ ...draft, segments: draft.segments.filter((_, i) => i !== index) })}>移除本段</Button></div>
        <div className="field-row">{(["start", "end"] as const).map(key => <label className="field" key={key}>{key === "start" ? "开始" : "结束"}（秒）<input type="number" min="0" step="0.01" max={asset?.duration} disabled={busy || fixed} value={Number.isFinite(segment[key]) ? segment[key] : ""} onChange={event => onChange({ ...draft, segments: draft.segments.map((item, i) => i === index ? { ...item, [key]: event.target.value === "" ? NaN : Number(event.target.value) } : item) })} /></label>)}</div>
        <label className="field">花字（最多100字）<input value={segment.caption} maxLength={100} disabled={busy || fixed || draft.packaging === "original"} onChange={event => onChange({ ...draft, segments: draft.segments.map((item, i) => i === index ? { ...item, caption: event.target.value } : item) })} /></label>
        {fixed && <p className="muted">可以移动或整体移除；截短和改写需要本人重新确认修订，不能直接修改。</p>}
      </li>; })}</ol><Button disabled={busy} onClick={onMaterials}><Plus size={16} />继续加入素材</Button>
    </section></div>
    <section className="panel editor-settings"><div className="section-title"><span className="eyebrow">这一版的包装</span><h2>让真人和原声做主角</h2></div>
      <label className="field">新版本名称<input maxLength={120} value={draft.title} disabled={busy} placeholder="例如：第一版 · 先看看" onChange={event => onChange({ ...draft, title: event.target.value })} /></label>
      <PackagingField id="edit-packaging" value={draft.packaging} disabled={busy} onChange={packaging => onChange({ ...draft, packaging, narration_text: packaging === "standard" ? draft.narration_text : "" })} />
      {draft.packaging === "original" ? <p className="muted">不添加标题、花字和旁白；事后采访的身份标记保留。视频仍会720p转码，不是无损导出。</p> : <p className="muted">花字围绕真实内容。包装修改另存新版本，已有成片不被覆盖。</p>}
      {draft.packaging === "standard" && <><label className="field">{demo ? "演示片头文字（不会朗读）" : "旁白文案（可选，最多120字）"}<textarea maxLength={120} value={draft.narration_text} disabled={busy} onChange={event => onChange({ ...draft, narration_text: event.target.value })} /></label><Notice>{demo ? "演示只生成约1秒合成提示音，非人声、非旁白；文案不会被朗读。" : "保存后还需确认文案再生成系统旁白，放在独立片头，不覆盖真人原声。留空可直接本机合成。"}</Notice></>}
      <p className={total > limit ? "error-text" : "duration-summary"}>{draft.segments.length}段 · 合计 {Number.isFinite(total) ? total.toFixed(2) : "—"} 秒／{limit}秒{limit === 60 && "，为片头预留空间"}</p>
      <div className="sticky-action"><Button variant="primary" disabled={busy || !draft.segments.length} onClick={onSave}>保存为新版本</Button><p className="muted">先保存，再合成与预览。</p></div>
    </section></div>;
}

export function PreviewPanel({ episode, version, busy, demo, workflow, mediaReady, watched, acknowledged, onWatched, onAcknowledged, onVersion, onEdit, onRender, onNarration, onConfirm, onDownload }: {
  episode: Episode; version?: Version; busy: boolean; demo: boolean; workflow: boolean; mediaReady: boolean; watched: boolean; acknowledged: boolean;
  onWatched: () => void; onAcknowledged: (value: boolean) => void; onVersion: (id: string) => void; onEdit: (version: Version, without?: boolean) => void;
  onRender: () => void; onNarration: () => void; onConfirm: () => void; onDownload: () => void;
}) {
  const playable = version && ["ready", "confirmed"].includes(version.state);
  const supported = !!version && ["manual", "ai", "demo"].includes(version.origin) && ["original", "light", "standard"].includes(version.packaging) && ["draft", "failed", "ready", "confirmed", "restricted", "rendering", "voicing"].includes(version.state);
  const needsVoice = !!version?.narration_text.trim() && !version.narration_ready;
  return <div className="studio-grid"><section className="panel preview-panel"><div className="section-title"><span className="eyebrow">04 / 先看过，再决定留下</span><h2>{version?.title || "这一期，还没有保存版本"}</h2></div>
    {version ? <><div className="toolbar"><span className="chip">{versionState(version.state)}</span><span className="chip">{originName(version.origin)}</span></div>
      <MediaPlayer key={`${version.id}-${version.media_url}-${version.state}`} src={playable ? version.media_url : null} title={version.title} onEnded={onWatched} />
      {version.error && <Notice tone="error">{version.error.message}</Notice>}
      {version.state === "restricted" && <Notice tone="error">此版依赖的来源或授权已变化。旧记录保留不等于可以继续使用；请核对来源，再建立安全的新版本。</Notice>}
      {!supported && <Notice>此版本包含当前页面尚不支持的状态或包装，请先核对服务版本。修改、确认和导出保持关闭。</Notice>}
      {["draft", "failed"].includes(version.state) && <div className="toolbar"><Button variant="primary" disabled={busy || !supported || !mediaReady || needsVoice} onClick={onRender}>{version.state === "failed" ? "重试本机合成" : "开始本机合成"}</Button>{needsVoice && <p className="muted">先确认并生成{demo ? "演示提示音" : "旁白音频"}，或另存无旁白版本。</p>}</div>}
      {["rendering", "voicing"].includes(version.state) && <Notice>{version.state === "rendering" ? "正在合成，任务状态会自动更新。已保存内容可在刷新后恢复。" : demo ? "正在生成非人声提示音，不朗读文案。" : "旁白任务处理中，完成后可提交视频合成。"}</Notice>}
      {version.state === "ready" && <div className="confirmation-area"><p>{watched ? "本版已播放到结尾，请再确认你的核对结论。" : "请先将此版本播放到结尾，核对画面、原声、花字与片头。"}</p><label className="check-field"><input type="checkbox" checked={acknowledged} disabled={busy || !supported || !watched} onChange={event => onAcknowledged(event.target.checked)} />我已完整预览，确认这一版符合表达且有权使用素材{demo ? "；我知道占位文字和提示音并非真实AI效果" : ""}。</label><Button variant="primary" disabled={busy || !supported || !watched || !acknowledged} onClick={onConfirm}>确认当前版本</Button></div>}
      {version.state === "confirmed" && supported && <div className="toolbar"><span className="chip success">已确认此版本</span><Button variant="primary" disabled={busy} onClick={onDownload}>导出这一版MP4</Button></div>}
      <Button disabled={busy || !supported} onClick={() => onEdit(version)}>以这版为基础继续修改</Button>
    </> : <div className="empty-state"><Film size={36} /><p>先保存剪接草案；渲染出的新候选不会覆盖旧版本。</p></div>}
  </section><div className="stack">
    <section className="panel"><h2>本期版本</h2><p className="muted">你看到和导出的，始终是明确选中的这一版。</p><div className="version-list">{[...episode.versions].reverse().map(item => <button key={item.id} className={`version-card${item.id === version?.id ? " selected" : ""}`} aria-pressed={item.id === version?.id} disabled={busy} onClick={() => onVersion(item.id)}><strong>{item.title}</strong><span className="chip">{versionState(item.state)}</span><span className="muted">{originName(item.origin)}</span></button>)}</div></section>
    {version?.narration_text && <section className="panel"><h2>{demo ? "演示片头 · 非人声" : "本版旁白文案"}</h2><p className="narration-copy">{version.narration_text}</p><Notice>{demo ? "这里只合成提示音，不朗读文字，不调用AI，不外发、不扣费。" : "使用系统音色，不仿冒真人声纹；独立片头不覆盖真人原声。"}</Notice><p>{version.narration_ready ? "音频已就绪" : version.narration_approved ? "文案已有确认记录，音频尚未就绪" : "文案等待你核对确认"}</p><div className="toolbar"><Button variant="primary" disabled={busy || !supported || !workflow || !["draft", "failed"].includes(version.state) || version.narration_ready} onClick={onNarration}>{demo ? "确认并生成提示音" : "确认文案并生成旁白"}</Button><Button disabled={busy || !supported} onClick={() => onEdit(version, true)}>另存无旁白草案</Button></div></section>}
  </div></div>;
}
