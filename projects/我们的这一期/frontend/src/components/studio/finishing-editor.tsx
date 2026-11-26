"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { FinishingState, Segment, SubtitleTrack, Version } from "@/lib/types";
import { Button, Dialog, MediaPlayer, Notice } from "@/components/ui";
import styles from "./finishing-editor.module.css";

const names = ["文字", "音效", "旁白与包装"];
const palettes = { coral: "珊瑚红", amber: "暖黄", mint: "蓝绿", candy: "粉紫" };
const sounds = { none: "不加音效", wood: "轻敲提示", resolve: "落点提示", legacy: "沿用原效果" };
type Draft = FinishingState["draft"];

export function FinishingEditor({ version, onClose, onCreated }: {
  version: Version; onClose: () => void; onCreated: (version: Version) => Promise<void>;
}) {
  const [saved, setSaved] = useState<FinishingState | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [track, setTrack] = useState<SubtitleTrack | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const [listen, setListen] = useState<SubtitleTrack["rows"][number] | null>(null);
  const path = `/versions/${version.id}/finishing`;
  const dirty = saved && draft && JSON.stringify(draft) !== JSON.stringify(saved.draft);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all([api<FinishingState>(path, { signal: controller.signal }), api<SubtitleTrack>(`/versions/${version.id}/subtitles`, { signal: controller.signal })])
      .then(([state, captions]) => { setSaved(state); setDraft(state.draft); setTrack(captions); })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "读取失败，请重试。"); });
    return () => controller.abort();
  }, [path, version.id]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  function segment(index: number, change: Partial<Segment>) {
    if (draft) setDraft({ ...draft, segments: draft.segments.map((s, i) => i === index ? { ...s, ...change } : s) });
  }
  async function act(action: "save" | "next" | "back", close = false) {
    if (!saved || !draft || lock.current) return;
    lock.current = true; setBusy(true); setError(""); setListen(null);
    try {
      const state = await api<FinishingState>(path, { method: "PUT", body: { revision: saved.revision, action, draft } });
      setSaved(state); setDraft(state.draft);
      if (action === "next" && saved.step === 3) {
        const created = await api<Version>(`${path}/commit`, { method: "POST", body: { revision: state.revision } });
        // Only deterministic local composition. New narration stays pending for explicit approval.
        if ((!created.narration_text.trim() || created.narration_ready) && ["draft", "failed"].includes(created.state))
          await api(`/versions/${created.id}/render`, { method: "POST" });
        await onCreated(created); onClose();
      } else if (close) onClose();
    } catch (e) { setError(e instanceof Error ? e.message : "尚未完成，选择仍保留。"); }
    finally { lock.current = false; setBusy(false); }
  }
  async function reload() {
    if (lock.current) return;
    lock.current = true; setBusy(true);
    try { const [state, captions] = await Promise.all([api<FinishingState>(path), api<SubtitleTrack>(`/versions/${version.id}/subtitles`)]); setSaved(state); setDraft(state.draft); setTrack(captions); setError(""); }
    catch (e) { setError(e instanceof Error ? e.message : "读取失败。"); }
    finally { lock.current = false; setBusy(false); }
  }
  function close() { if (!busy) { if (dirty) void act("save", true); else onClose(); } }
  const changedVoice = !!draft?.narration_text.trim() && (draft.narration_text !== version.narration_text || !version.narration_ready);
  return <Dialog open title="让这一版更有节目感" onClose={close} className={styles.dialog}>
    {error && <Notice tone="error">{error} <Button disabled={busy} onClick={() => void reload()}>重新读取已保存选择</Button></Notice>}
    {!draft || !saved ? <p role="status">正在读取已保存选择…</p> : <>
      <ol className={styles.steps} aria-label="包装步骤">{names.map((name, i) => <li key={name} aria-current={saved.step === i + 1 ? "step" : undefined}><span>{i + 1}</span>{name}{saved.completed > i && " ✓"}</li>)}</ol>
      <div className={styles.layout}>
        <div className={styles.reference}>
          <MediaPlayer key={listen ? `${listen.cue_id}-${listen.segment_index}` : version.id} src={listen?.media_url || version.media_url} title={listen ? "回听这句原声" : "调整前的版本"} start={listen ? Math.max(0, listen.start - .3) : undefined} end={listen ? listen.end + .3 : undefined} />
          <p>{listen ? "回听原片，只修改字幕文字。" : "调整前的版本 · 保存三步后合成新预览"}</p>
          {listen && <Button onClick={() => setListen(null)}>回到整片</Button>}
        </div>
        <div className={styles.editor}>
          <h3>{saved.step === 1 ? "先把画面上的字定下来" : saved.step === 2 ? "把声音留给关键动作" : "最后，加一点开场和强调"}</h3>
          {saved.step === 1 && <>
            <div className={styles.cards}>{draft.segments.map((s, i) => <div key={i}>
              {s.visual_beats?.map((beat, j) => <div className={styles.card} key={j}>
                <label>花字 {i + 1}.{j + 1}<input aria-label={`花字 ${i + 1}.${j + 1}`} maxLength={8} disabled={busy} value={beat.text || ""} onChange={e => segment(i, { visual_beats: s.visual_beats?.map((b, k) => k === j ? { ...b, text: e.target.value } : b) })} /></label>
                <div className={styles.row}>{["headline", "cutout"].includes(beat.kind) && <label>配色<select disabled={busy} value={beat.palette || "candy"} onChange={e => segment(i, { visual_beats: s.visual_beats?.map((b, k) => k === j ? { ...b, palette: e.target.value as typeof b.palette } : b) })}>{Object.entries(palettes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}
                  {["focus", "cutout"].includes(beat.kind) && <label>位置<select disabled={busy} value={beat.position || "bottom_right"} onChange={e => segment(i, { visual_beats: s.visual_beats?.map((b, k) => k === j ? { ...b, position: e.target.value as typeof b.position } : b) })}>{Object.entries({top_left:"左上",top_right:"右上",bottom_left:"左下",bottom_right:"右下"}).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>}</div>
              </div>)}
              {s.editorial && s.editorial.treatment !== "none" && <div className={styles.card}><label>第 {i + 1} 段花字<input disabled={busy} maxLength={10} value={s.editorial.text || ""} onChange={e => segment(i, { editorial: { ...s.editorial!, text: e.target.value } })} /></label><label>补充文字<input disabled={busy} maxLength={40} value={s.editorial.detail || ""} onChange={e => segment(i, { editorial: { ...s.editorial!, detail: e.target.value } })} /></label></div>}
              {!s.visual_beats?.length && !s.editorial && <label className={styles.card}>第 {i + 1} 段花字<input disabled={busy} value={s.caption} maxLength={100} onChange={e => segment(i, { caption: e.target.value })} /></label>}
            </div>)}</div>
            <details className={styles.captions}><summary>对白字幕 · {track?.rows.length || 0} 句</summary>
              {!track?.rows.length && <p>没有可校对的对白，暂不显示字幕，现场原声保留。</p>}
              {track?.rows.map(row => {
                const s = draft.segments[row.segment_index];
                const edit = s.subtitle_edits?.find(e => e.cue_id === row.cue_id) || row;
                const update = (value: { text?: string; hidden?: boolean }) => segment(row.segment_index, { subtitle_edits: [...(s.subtitle_edits || []).filter(e => e.cue_id !== row.cue_id), { cue_id: row.cue_id, text: edit.text, hidden: edit.hidden, reviewed: true, ...value }] });
                return <div className={styles.card} key={`${row.segment_index}-${row.cue_id}`}><Button disabled={busy} onClick={() => setListen(row)}>回听这一句</Button><label>字幕<input disabled={busy || row.locked} value={edit.text} maxLength={500} onChange={e => update({ text: e.target.value })} /></label><label className={styles.check}><input type="checkbox" disabled={busy || row.locked} checked={edit.hidden} onChange={e => update({ hidden: e.target.checked })} />暂不显示这一句</label></div>;
              })}
            </details>
          </>}
          {saved.step === 2 && <>
            <p>保留现场原声；不用每次出字都配音。</p>
            {draft.segments.map((s, i) => <div key={i}>
              {s.visual_beats?.map((beat,j) => <label className={styles.card} key={j}>{beat.text || `第 ${i+1} 段动作 ${j+1}`}<select aria-label={`音效 ${i+1}.${j+1}`} disabled={busy} value={beat.sound || "legacy"} onChange={e => segment(i,{ visual_beats:s.visual_beats?.map((b,k)=>k===j?{...b,sound:e.target.value as typeof b.sound,sound_delay:["none","legacy"].includes(e.target.value)?0:b.sound_delay}:b) })}>{Object.entries(sounds).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>)}
              {s.editorial && s.editorial.treatment !== "none" && <label className={styles.card}>{s.editorial.text || `第 ${i+1} 段`}<select disabled={busy} value={s.editorial.sound || "legacy"} onChange={e => segment(i,{editorial:{...s.editorial!,sound:e.target.value as NonNullable<Segment["editorial"]>["sound"],sound_delay:0}})}>{Object.entries(sounds).map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>}
            </div>)}
            {!draft.segments.some(s=>s.visual_beats?.length || s.editorial?.treatment && s.editorial.treatment!=="none") && <p>这版没有已安排的动作音效节点，保留现场原声即可。</p>}
          </>}
          {saved.step === 3 && <>
            <label className={styles.card}>开场旁白<textarea aria-label="开场旁白" rows={3} maxLength={120} disabled={busy} value={draft.narration_text} placeholder="留空就只保留现场原声" onChange={e=>setDraft({...draft,narration_text:e.target.value})}/></label>
            <Button disabled={busy} onClick={()=>setDraft({...draft,narration_text:""})}>只用现场原声</Button>
            <p>{changedVoice ? "新文案先保存，下一步确认配音后再合成。" : draft.narration_text ? "沿用已生成旁白，无需再次配音。" : "不增加旁白，直接本机合成。"}</p>
            <h4>节点边框</h4>
            {draft.segments.map((s,i)=><div className={styles.card} key={i}><strong>第 {i+1} 段</strong>
              {(s.frame_accents || []).map((frame,j)=><div className={styles.row} key={j}><span>原片 {Math.round(frame.source_time)} 秒处</span><select aria-label={`边框配色 ${i+1}.${j+1}`} disabled={busy} value={frame.palette} onChange={e=>segment(i,{frame_accents:s.frame_accents?.map((f,k)=>k===j?{...f,palette:e.target.value as typeof f.palette}:f)})}>{Object.entries(palettes).filter(([v])=>v!=="candy").map(([value,label])=><option key={value} value={value}>{label}</option>)}</select><Button disabled={busy} onClick={()=>segment(i,{frame_accents:s.frame_accents?.filter((_,k)=>k!==j)})}>移除</Button></div>)}
              {!s.frame_accents?.length && <p>当前没有边框</p>}
              {s.visual_beats?.filter(b=>b.duration>=.5).map((b,j)=> <Button key={j} disabled={busy || (s.frame_accents?.length || 0)>=2 || draft.segments.reduce((n,s)=>n+(s.frame_accents?.length||0),0)>=6 || !!s.frame_accents?.some(f=>b.source_time<f.source_time+f.duration && b.source_time+Math.min(1.5,b.duration)>f.source_time)} onClick={()=>segment(i,{frame_accents:[...(s.frame_accents||[]),{source_time:b.source_time,duration:Math.min(1.5,b.duration),palette:"amber"}]})}>在「{b.text || "这个动作"}」加边框</Button>)}
            </div>)}
          </>}
        </div>
      </div>
      <footer className={styles.footer}><span role="status">{busy ? "正在保存…" : dirty ? "有新修改" : `已保存到第 ${saved.step} 步`}</span><div><Button disabled={busy || saved.step===1} onClick={()=>void act("back")}>上一步</Button><Button disabled={busy} onClick={()=>void act("save",true)}>保存，稍后继续</Button><Button variant="primary" disabled={busy} onClick={()=>void act("next")}>{saved.step<3?`保存，下一步：${names[saved.step]}`:changedVoice?"保存新版，去确认配音":"保存并生成新版"}</Button></div></footer>
    </>}
  </Dialog>;
}
