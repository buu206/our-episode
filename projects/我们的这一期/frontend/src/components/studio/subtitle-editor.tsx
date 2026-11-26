"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Asset, Segment, SubtitleTrack, Version } from "@/lib/types";
import { Button, Dialog, MediaPlayer, Notice } from "../ui";
import styles from "./subtitle-editor.module.css";

export type SubtitleReviewTarget = { version_id: string; cue_id: string; caption: string; recognized: string };

export function SubtitleEditor({ version, assets, busy, onClose, onSave, target }: {
  version: Version; assets: Asset[]; busy: boolean; onClose: () => void;
  onSave: (segments: Segment[]) => Promise<boolean>;
  target?: SubtitleReviewTarget;
}) {
  const [rows, setRows] = useState<SubtitleTrack["rows"]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [index, setIndex] = useState(0);
  const [targetIndex, setTargetIndex] = useState<number | null>(null);
  const [targetWarning, setTargetWarning] = useState("");
  const videoBox = useRef<HTMLDivElement>(null);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void api<SubtitleTrack>(`/versions/${version.id}/subtitles`, { signal: controller.signal }).then(track => {
      if (controller.signal.aborted) return;
      if (track.version_id !== version.id) throw new Error("版本已变化，请重新打开。");
      setRows(track.rows);
      if (target) {
        const matches = track.rows.map((row, at) => ({ row, at })).filter(({ row }) => row.cue_id === target.cue_id);
        if (target.version_id === version.id && matches.length === 1 && !matches[0].row.hidden && matches[0].row.text === target.caption) {
          setIndex(matches[0].at); setTargetIndex(matches[0].at);
        } else {
          setTargetWarning("这条差异无法唯一对应当前字幕，请手动选句；未套用识别文字。");
        }
      }
    }).catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : "字幕读取失败。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [version.id, target]);
  useEffect(() => {
    const prevent = (event: BeforeUnloadEvent) => { if (dirty) { event.preventDefault(); event.returnValue = ""; } };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);
  const row = rows[index];
  const asset = assets.find(value => value.id === row?.asset_id);
  const remaining = rows.filter(value => value.flags.length && !value.reviewed && !value.locked).length;
  function change(patch: Partial<SubtitleTrack["rows"][number]>) {
    setRows(previous => previous.map((value, at) => at === index ? { ...value, ...patch, reviewed: true } : value));
    setDirty(true);
  }
  function close() { if (!busy && (!dirty || window.confirm("字幕尚未保存，确定关闭吗？"))) onClose(); }
  async function save() {
    setError("");
    const segments = version.segments.map((segment, at) => ({ ...segment, subtitle_edits: rows.filter(value => value.segment_index === at && value.reviewed && !value.locked).map(value => ({ cue_id: value.cue_id, text: value.text, hidden: value.hidden, reviewed: true })) }));
    try { if (await onSave(segments)) { setDirty(false); onClose(); } else setError("尚未完成，请查看页面提示；修改仍保留。若已有新版本，请先核对它。"); }
    catch (e) { setError(e instanceof Error ? e.message : "保存失败，修改仍保留。"); }
  }
  return <Dialog open onClose={close} title="校对字幕" className={styles.dialog}>
    <p className={styles.hint}>听一句，改一句。只改字幕，现场原声与旧版本保留。</p>
    {loading && <p role="status">正在读取字幕…</p>}
    {error && <Notice tone="error">{error}</Notice>}
    {targetWarning && <Notice>{targetWarning}</Notice>}
    {!loading && !error && !rows.length && <p>本版没有可校对的对白字幕。</p>}
    {row && <>
      <nav className={styles.navigation} aria-label="字幕导航">
        <Button disabled={busy || index === 0} onClick={() => setIndex(index - 1)}>上一句</Button>
        <label>第 <select aria-label="选择字幕" value={index} disabled={busy} onChange={event => setIndex(Number(event.target.value))}>{rows.map((value, at) => <option key={`${value.segment_index}-${value.cue_id}`} value={at}>{at + 1} · {value.text.slice(0, 18)}{value.reviewed ? " ✓" : ""}</option>)}</select> / {rows.length} 句</label>
        <Button disabled={busy || index === rows.length - 1} onClick={() => setIndex(index + 1)}>下一句</Button>
      </nav>
      <div className={styles.content}>
        <div className={styles.video} ref={videoBox}>{asset && <MediaPlayer key={index} src={row.media_url} title="回听这一句" start={Math.max(0, row.start - .3)} end={Math.min(asset.duration, row.end + .3)} />}</div>
        <div className={styles.edit}>
          <div className={styles.tools}><Button disabled={busy || !asset} onClick={() => { const video = videoBox.current?.querySelector("video"); if (video) { video.currentTime = Math.max(0, row.start - .3); void video.play().catch(() => setError("未能开始播放，请点视频中的播放按钮。")); } }}>回听这一句</Button>{row.reviewed && <span>已校对</span>}</div>
          {row.flags.length > 0 && <p className={styles.flag}>{row.flags.join(" · ")}</p>}
          {row.locked && <Notice>这是本人确认的采访表达，请通过采访修订流程修改。</Notice>}
          {target && index === targetIndex && <div className={styles.suggestion}>
            <p>重新识别：{target.recognized || "没有识别到对应内容"}</p>
            <span>也可能听错，回听后再决定。保存前不会改动成片。</span>
            {target.recognized.trim() && <Button disabled={busy || row.locked || row.text === target.recognized} onClick={() => change({ text: target.recognized })}>用作修改草稿</Button>}
          </div>}
          <label className="field">屏幕上显示的字幕<textarea aria-label="屏幕上显示的字幕" rows={3} maxLength={500} disabled={busy || row.locked} value={row.text} onChange={event => change({ text: event.target.value })} /></label>
          <label className="check-field"><input type="checkbox" checked={row.hidden} disabled={busy || row.locked} onChange={event => change({ hidden: event.target.checked })} />不显示这一句（原声保留）</label>
          <div className={styles.tools}><Button disabled={busy || row.locked} onClick={() => change({})}>这句没问题</Button><Button disabled={busy || row.locked} onClick={() => change({ text: row.original_text, hidden: false })}>恢复识别文字</Button></div>
          <details><summary>查看原始识别</summary><p>{row.original_text}</p></details>
        </div>
      </div>
      <footer className={styles.footer}><span>{remaining ? `${remaining} 句建议回听，不代表一定识别错误` : "名字、否定词和句尾语气，建议听清再留"}</span><Button variant="primary" disabled={busy || !dirty || rows.some(value => value.reviewed && !value.hidden && !value.text.trim())} onClick={() => void save()}>{busy ? "正在处理…" : "保存并生成新版"}</Button></footer>
    </>}
  </Dialog>;
}
