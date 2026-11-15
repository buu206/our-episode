"use client";

import { Button } from "@/components/ui";
import type { Job } from "@/lib/types";
import styles from "./preview.module.css";

export type RunAction = "pause" | "resume" | "cancel";
export const runIsLive = (run?: Job) => !!run && ["active", "pausing", "paused", "blocked", "cancelling"].includes(run.state);
export function DirectorRun({ run, busy, onControl }: { run?: Job; busy: boolean; onControl?: (action: RunAction) => void }) {
  if (!run) return null;
  return <div className={styles.delegation} aria-label="导演制作委托">
    <p role="status">{run.stage}</p>
    {run.error && <p>{run.error.message}</p>}
    {onControl && runIsLive(run) && <div className="toolbar">
      {run.state === "active" && <Button disabled={busy} onClick={() => onControl("pause")}>暂停导演</Button>}
      {["paused", "blocked"].includes(run.state) && <Button disabled={busy} onClick={() => onControl("resume")}>继续制作</Button>}
      {run.state !== "cancelling" && <Button disabled={busy} onClick={() => onControl("cancel")}>结束委托</Button>}
    </div>}
    <details><summary>导演这次负责什么</summary><p>按已选拍法合成原声初稿，再检查画面与声音。仅本机处理，中断最多重试一次；完成后停在候选，不替你确认。</p><p>暂停或结束时，正在合成的任务会收尾保存，之后不再启动下一步。</p></details>
  </div>;
}
