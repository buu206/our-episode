"use client";

import { useState } from "react";
import { z } from "zod";
import { Button, Dialog, MediaPlayer } from "@/components/ui";
import type { Job, Version } from "@/lib/types";
import styles from "./preview.module.css";
import type { SubtitleReviewTarget } from "./subtitle-editor";

const Finding = z.object({
  code: z.string(), cue_id: z.string(), message: z.string().min(1).max(200),
  start: z.number().finite().nonnegative(), end: z.number().finite().positive(),
  caption: z.string().max(160), recognized: z.string().max(160),
}).refine(row => row.end > row.start);
const Report = z.object({
  version_id: z.string(), scope: z.literal("output_audio_asr"), verification: z.literal("unverified"),
  duration: z.number().finite().positive(), captions_checked: z.number().int().positive(),
  issue_count: z.number().int().nonnegative(), findings_omitted: z.number().int().nonnegative(),
  findings: z.array(Finding).max(8),
}).refine(r => r.issue_count === r.findings.length + r.findings_omitted && r.findings.every(f => f.end <= r.duration));

export function AudioCheck({ version, jobs, busy, onCheck, onSubtitles }: {
  version: Version; jobs: Job[]; busy: boolean; onCheck?: () => void; onSubtitles?: (target?: SubtitleReviewTarget) => void;
}) {
  const [replay, setReplay] = useState<z.infer<typeof Finding> | null>(null);
  const job = [...jobs].reverse().find(j => j.kind === "audio_review" && j.version_id === version.id);
  const parsed = Report.safeParse(job?.result);
  const report = job?.state === "succeeded" && parsed.success && parsed.data.version_id === version.id ? parsed.data : null;
  const item = (finding: z.infer<typeof Finding>, index: number) => <div className={styles.checkIssue} key={`${finding.cue_id}-${index}`}>
    <div>{finding.code !== "CAPTION_AUDIO_DIFFERENCE" && <p>{finding.message}</p>}<p>字幕：{finding.caption}</p><p>重新识别：{finding.recognized || "没识别到对应内容"}</p></div>
    <Button disabled={busy || !version.media_url} onClick={() => setReplay(finding)}>回听 {Math.floor(finding.start)}—{Math.ceil(finding.end)}秒</Button>
    <Button disabled={busy} onClick={() => window.dispatchEvent(new CustomEvent("director:review-cue", {detail: {
      versionId: version.id, cueId: finding.cue_id, label: `这句字幕 · ${Math.floor(finding.start)}—${Math.ceil(finding.end)}秒`,
    }}))}>让导演看这句</Button>
    {onSubtitles && <Button disabled={busy} onClick={() => onSubtitles({version_id: version.id, cue_id: finding.cue_id, caption: finding.caption, recognized: finding.recognized})}>校对这句</Button>}
  </div>;
  if (!onCheck && !job) return null;
  return <div aria-label="对白核对">
    {job && ["queued", "running"].includes(job.state) ? <p role="status">正在听成片对白、核对字幕…</p>
      : report ? <details><summary>{report.issue_count ? `对白与字幕 · ${report.issue_count}处待听` : `已对照${report.captions_checked}句字幕`}</summary>
        <p>重新识别也可能听错，先听原声再改；字幕不会自动替换。</p>
        {report.findings.slice(0, 3).map(item)}
        {report.findings.length > 3 && <details><summary>其余{report.findings.length - 3}处</summary>{report.findings.slice(3).map(item)}</details>}
        {report.findings_omitted > 0 && <p>先列出8处差异；其余{report.findings_omitted}处未展开。</p>}
        {report.issue_count > 0 && onSubtitles && <Button disabled={busy} onClick={() => onSubtitles()}>全部字幕</Button>}
      </details>
      : job?.state === "failed" ? <p>对白核对未完成，候选和字幕仍保留。{job.error?.message}</p>
      : onCheck && <details><summary>听一遍，核对字幕</summary>
        <p>会将这一版的声音发送给转写模型，使用本期预算；识别差异只提示，不改字幕。</p>
        <Button disabled={busy} onClick={onCheck}>核对成片对白</Button>
      </details>}
    <Dialog open={!!replay} onClose={() => setReplay(null)} title="听听这句原话">
      {replay && <><p>字幕：{replay.caption}</p><p>重新识别：{replay.recognized || "没识别到对应内容"}</p>
        <MediaPlayer key={`${version.id}-${replay.cue_id}-${replay.start}`} src={version.media_url} title={version.title} start={replay.start} end={replay.end}/></>}
    </Dialog>
  </div>;
}
