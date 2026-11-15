"use client";

import { useState } from "react";
import { z } from "zod";
import { Button, Dialog, MediaPlayer } from "@/components/ui";
import type { Job, Version } from "@/lib/types";
import styles from "./preview.module.css";

const Report = z.object({
  version_id: z.string(), scope: z.literal("local_technical"),
  decoded_full_output: z.literal(true),
  status: z.enum(["checked", "needs_attention"]),
  issues: z.array(z.object({ code: z.string(), message: z.string().max(200),
    start: z.number().finite().nonnegative(), end: z.number().finite().positive() })
    .refine(row => row.end > row.start)).max(10),
});
const SourceReport = z.object({
  scope: z.literal("source_metadata"), evidence_confirmed: z.boolean(),
  speech_checked: z.number().int().nonnegative(), captions_checked: z.number().int().nonnegative(),
  issue_count: z.number().int().nonnegative(),
  issues: z.array(z.object({ code: z.string(), segment_number: z.number().int().positive(),
    message: z.string().min(1).max(200), count: z.number().int().positive().default(1) })).max(8),
}).refine(value => value.issue_count >= value.issues.reduce((sum, issue) => sum + issue.count, 0));
const VisualReport = z.object({
  version_id: z.string(), scope: z.literal("sampled_visual"),
  verification: z.literal("unverified"), audio_analyzed: z.literal(false),
  summary: z.string().min(1).max(120), duration: z.number().finite().positive(),
  sample_times: z.array(z.number().finite().nonnegative()).min(1).max(12),
  findings: z.array(z.object({ time: z.number().finite().nonnegative(),
    observation: z.string().min(1).max(120), suggestion: z.string().min(1).max(120) })).max(3),
}).refine(report => report.sample_times.every(time => time < report.duration)
  && report.findings.every(finding => report.sample_times.includes(finding.time)));

export function OutputCheck({ version, jobs, busy, onCheck, onVisualCheck, onSubtitles }: {
  version: Version; jobs: Job[]; busy: boolean; onCheck?: () => void; onVisualCheck?: () => void; onSubtitles?: () => void;
}) {
  const [replay, setReplay] = useState<{ start: number; end: number } | null>(null);
  const [visualReplay, setVisualReplay] = useState<{ start: number; end: number } | null>(null);
  const job = [...jobs].reverse().find(row => row.kind === "technical_review" && row.version_id === version.id);
  const parsed = Report.safeParse(job?.result);
  const report = job?.state === "succeeded" && parsed.success && parsed.data.version_id === version.id ? parsed.data : null;
  const sourceParsed = SourceReport.safeParse(job?.result?.source_review);
  const source = report && sourceParsed.success && sourceParsed.data.evidence_confirmed ? sourceParsed.data : null;
  const working = job && ["queued", "running"].includes(job.state);
  const visualJob = [...jobs].reverse().find(row => row.kind === "visual_review" && row.version_id === version.id);
  const visualParsed = VisualReport.safeParse(visualJob?.result);
  const visual = visualJob?.state === "succeeded" && visualParsed.success && visualParsed.data.version_id === version.id ? visualParsed.data : null;
  return <div className={styles.check} aria-label="成片检查">
    {working ? <p role="status">导演助手正在检查画面与声音…</p> : report ? <>
      <details><summary>{report.issues.length ? `有${report.issues.length}处声音值得回听` : "画面与音轨检查完成"}</summary>
        <p>这是本机播放检查，故事、字幕准确性和综艺感仍需看片判断。</p>
        {report.issues.map((issue, index) => <div key={index} className={styles.checkIssue}>
          <span>{issue.message}</span><Button disabled={busy || !version.media_url}
            onClick={() => setReplay({ start: issue.start, end: issue.end })}>
            回听 {Math.floor(issue.start)}—{Math.ceil(issue.end)}秒
          </Button>
        </div>)}
      </details>
    </> : <><span>{job?.state === "failed" ? "成片检查未完成，视频仍保留" : "这版还没有播放检查"}</span>
      {onCheck && <Button disabled={busy} onClick={onCheck}>{job?.state === "failed" ? "重新检查" : "检查画面与声音"}</Button>}</>}
    {source && (source.speech_checked > 0 || source.captions_checked > 0) && <details>
      <summary>{source.issue_count ? `剪辑与字幕 · ${source.issue_count}处建议` : "原话边界与字幕时长已核对"}</summary>
      <p>对照已确认文字和时间检查，识别错字、实际听感仍需回听。</p>
      {source.issues.slice(0, 3).map((issue, index) => <p key={index}>第{issue.segment_number}个镜头{issue.count > 1 ? `（${issue.count}处）` : ""}：{issue.message}</p>)}
      {source.issues.length > 3 && <p>先看以上重点，其余建议可以问导演。</p>}
      {source.issues.some(issue => issue.code === "CAPTION_REVIEW") && onSubtitles &&
        <Button disabled={busy} onClick={onSubtitles}>去校对字幕</Button>}
    </details>}
    {visual ? <details><summary>导演看了{visual.sample_times.length}处画面{visual.findings.length ? ` · ${visual.findings.length}条建议` : ""}</summary>
      <p>{visual.summary}</p>
      <p>仅抽样截图，未听声音；以下是待核对建议。</p>
      {visual.findings.map((finding, index) => <div key={index} className={styles.checkIssue}>
        <div><p>{finding.observation}</p><p>{finding.suggestion}</p></div>
        <Button disabled={busy || !version.media_url} onClick={() => setVisualReplay({ start: Math.max(0, finding.time - 1), end: Math.min(visual.duration, finding.time + 2) })}>回看 {Math.round(finding.time)}秒附近</Button>
      </div>)}
      <details><summary>检查了哪些位置</summary><p>{visual.sample_times.map(time => `${Math.round(time)}秒`).join("、")}。其余画面及连续动作未检查。</p></details>
    </details> : visualJob && ["queued", "running"].includes(visualJob.state) ? <p role="status">导演正在看抽样画面…</p>
      : visualJob?.state === "failed" ? <p>画面审片未完成，候选仍保留。{visualJob.error?.message}</p>
      : onVisualCheck && <details><summary>让导演看看画面</summary><p>抽样检查花字、遮挡和构图，使用本期模型预算。</p><Button disabled={busy} onClick={onVisualCheck}>检查这一版画面</Button></details>}
    <Dialog open={!!replay} onClose={() => setReplay(null)} title="回听这一处">
      {replay && <MediaPlayer key={`${version.id}-${replay.start}`} src={version.media_url}
        title={version.title} start={replay.start} end={replay.end} />}
    </Dialog>
    <Dialog open={!!visualReplay} onClose={() => setVisualReplay(null)} title="回看这一处">
      {visualReplay && <MediaPlayer key={`${version.id}-${visualReplay.start}`} src={version.media_url}
        title={version.title} start={visualReplay.start} end={visualReplay.end} />}
    </Dialog>
  </div>;
}
