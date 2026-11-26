"use client";

import { z } from "zod";
import type { Episode } from "@/lib/types";
import styles from "./finishing-rehearsal.module.css";

const checkpoint = z.object({
  version_id: z.string(), label: z.string().max(30), choice: z.string().max(180),
});
const rehearsal = z.object({ simulation: z.literal(true), checkpoints: z.array(checkpoint).length(3) });

export function FinishingRehearsal({ episode, versionId, busy, onVersion }: {
  episode: Pick<Episode, "jobs"> & { versions: { id: string }[] }; versionId: string; busy: boolean; onVersion: (id: string) => void;
}) {
  const runs = episode.jobs.filter(job => job.kind === "finishing_rehearsal" && job.state === "succeeded");
  const result = runs.map(job => rehearsal.safeParse(job.result)).find(item => item.success
    && item.data.checkpoints.some(point => point.version_id === versionId));
  if (!result?.success) return null;
  const points = result.data.checkpoints;
  if (!points.every(point => episode.versions.some(version => version.id === point.version_id))) return null;
  const selected = points.findIndex(point => point.version_id === versionId);
  return <section className={styles.flow} aria-label="三步制作试跑">
    <div className={styles.heading}><strong>三步制作 · 流程试跑</strong><span>本轮由助手模拟选择，尚未确认成片</span></div>
    <nav className={styles.steps} aria-label="制作步骤">{points.map((point, index) =>
      <button type="button" key={point.version_id} disabled={busy} aria-current={index === selected ? "step" : undefined}
        onClick={() => onVersion(point.version_id)}><span>{String(index + 1).padStart(2, "0")}</span>{point.label}</button>
    )}</nav>
    <div className={styles.choice}><p>{points[selected].choice}</p>
      {selected < 2 ? <button type="button" disabled={busy} onClick={() => onVersion(points[selected + 1].version_id)}>看下一步 →</button>
        : <span>下方是合并后的完整候选</span>}</div>
  </section>;
}
