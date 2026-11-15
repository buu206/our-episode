"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { ArrowRight, Check, Clapperboard, Send } from "lucide-react";
import { Button } from "@/components/ui";
import type { Direction, DirectorState, Episode, Version } from "@/lib/types";
import type { Step } from "./model";

export type DirectorMessageInput = { text?: string; choice?: NonNullable<DirectorState["focus"]> };
export type DirectorGuideProps = {
  episode: Episode; step: Step; version?: Version; busy: boolean; canGenerate: boolean; pending: boolean; demo?: boolean;
  onMessage: (message: DirectorMessageInput) => Promise<boolean>;
  onNavigate: (step: Step) => void;
  onGenerate: (kind: "analysis" | "directions") => void | Promise<unknown>;
  onSelect: (direction: Direction) => void | Promise<unknown>;
  onDirtyChange?: (dirty: boolean) => void;
  defaultOpen?: boolean;
};

const choices = [
  { value: "conversation", label: "保留聊天", detail: "让对话说完整" },
  { value: "highlights", label: "热闹瞬间", detail: "看看现场有什么亮点" },
  { value: "auto", label: "你先看看", detail: "看过素材，再决定怎么讲" },
] as const;
const focusName: Record<NonNullable<DirectorState["focus"]>, string> = {
  conversation: "保留聊天", highlights: "热闹瞬间", auto: "你先看看", warm: "温暖回忆",
};

function ShortText({ text, label, limit = 110 }: { text: string; label: string; limit?: number }) {
  const [expanded, setExpanded] = useState(false);
  const id = useId();
  const chars = Array.from(text), long = chars.length > limit;
  return <div className="director-copy"><p id={id}>{long && !expanded ? `${chars.slice(0, limit).join("")}…` : text}</p>
    {long && <button type="button" className="director-text-button" aria-expanded={expanded} aria-controls={id} aria-label={`${expanded ? "收起" : "展开"}${label}`} onClick={() => setExpanded(value => !value)}>{expanded ? "收起" : "展开完整内容"}</button>}
  </div>;
}

export function hasCurrentDirections(episode: Episode): boolean {
  return !!episode.director?.directions_current && episode.analysis_state === "confirmed"
    && episode.evidence.length > 0 && episode.evidence_confirmed_revision === episode.evidence_revision
    && episode.directions_revision === episode.evidence_revision && episode.directions.length > 0;
}

export function DirectorDirections({ episode, busy, demo = false, onSelect, onAutoSelect }: Pick<DirectorGuideProps, "episode" | "busy" | "onSelect"> & { demo?: boolean; onAutoSelect?: (direction: Direction) => void }) {
  if (!hasCurrentDirections(episode)) return null;
  return <section className="director-candidates" aria-label="导演给出的方向">
    <div className="director-candidates-heading"><div><span className="eyebrow">02 / 选个拍法</span><h2>这一期，想怎么讲？</h2></div>{demo && <span className="director-demo-note">固定演示 · 非真实 AI</span>}</div>
    <div className="director-directions">{episode.directions.slice(0, 3).map((direction, index) => <article className="director-direction" key={direction.id}>
      <span className="director-option-number">方案 {String(index + 1).padStart(2, "0")}</span><h3>{direction.title}</h3>
      <ShortText text={direction.summary} label={`方案${index + 1}完整说明`} limit={72} />
      {onAutoSelect && <Button variant="primary" disabled={busy} onClick={() => onAutoSelect(direction)}>导演直接出原声初稿<ArrowRight size={15} aria-hidden="true" /></Button>}
      <Button variant={onAutoSelect ? "ghost" : "primary"} disabled={busy} onClick={() => void onSelect(direction)}>{onAutoSelect ? "先微调，再开拍" : "按这个拍"}{!onAutoSelect && <ArrowRight size={15} aria-hidden="true" />}</Button>
    </article>)}</div>
  </section>;
}

export function DirectorGuide(props: DirectorGuideProps) {
  // Old cached episodes have no director record. Do not invent an empty history.
  if (!props.episode.director) return null;
  return <DirectorGuideContent key={props.episode.id} {...props} director={props.episode.director} />;
}

export function DirectorGuideDock(props: DirectorGuideProps) {
  if (!props.episode.director) return null;
  return <DirectorGuideDockContent key={props.episode.id} {...props} />;
}

function DirectorGuideDockContent(props: DirectorGuideProps) {
  const [expansion, setExpansion] = useState<{ key: string; open: boolean } | null>(null);
  const [dirty, setDirty] = useState(false);
  const key = `${props.step}:${!!props.defaultOpen}`;
  const expanded = expansion?.key === key ? expansion.open : !!props.defaultOpen;
  const parentDirtyChange = props.onDirtyChange;
  const dirtyChanged = useCallback((value: boolean) => {
    setDirty(value); parentDirtyChange?.(value);
  }, [parentDirtyChange]);
  return <details className="director-guide-dock compact" open={expanded || dirty}>
    <summary onClick={event => { event.preventDefault(); if (!dirty) setExpansion({ key, open: !expanded }); }}>
      <span className="director-guide-dock-label"><Clapperboard size={17} aria-hidden="true" />偏好与要求</span>
      <span className="director-guide-dock-hint">{dirty ? "有未发送的要求，先保存" : "按需补充"}</span>
    </summary>
    {/* Keep the same child mounted across all steps and disclosure states. */}
    <DirectorGuide {...props} onDirtyChange={dirtyChanged} />
  </details>;
}

function DirectorGuideContent({ episode, director, step, version, busy, canGenerate, pending, onMessage, onNavigate, onGenerate, onDirtyChange }: DirectorGuideProps & { director: DirectorState }) {
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const requestInFlight = useRef(false);
  const headingId = useId(), inputId = useId(), helpId = useId();
  const disabled = busy || pending || saving;
  const dirty = !!text.trim();
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  async function save(message: DirectorMessageInput) {
    if (disabled || requestInFlight.current) return;
    requestInFlight.current = true;
    setSaving(true); setSaveError("");
    const submitted = text;
    try {
      if (await onMessage(message)) {
        if (message.text) setText(current => current === submitted ? "" : current);
      } else setSaveError("还没有保存成功，你的输入已保留。请稍后重试。");
    } catch {
      setSaveError("暂时没能保存，你的输入已保留。请稍后重试。");
    } finally { requestInFlight.current = false; setSaving(false); }
  }

  const confirmed = episode.analysis_state === "confirmed" && episode.evidence.length > 0 && episode.evidence_confirmed_revision === episode.evidence_revision;
  const current = hasCurrentDirections(episode);
  const directions = current ? episode.directions.slice(0, 3) : [];
  const previewable = !!version && ["ready", "confirmed"].includes(version.state) && !!version.media_url;
  const recent = director.messages.slice(-2), previous = director.messages.slice(0, -2);
  const changed = !current && (episode.directions.length > 0 || (director.revision > 0 && !!version));

  let next: { label: string; detail: string; navigate?: Step; generate?: "analysis" | "directions" } | null = null;
  if (step === "preview" && version) {
    next = { label: previewable ? "当前候选就在下面" : "在下方继续制作", detail: previewable ? "先看完整一遍，再告诉我想保留什么、哪里需要调整。" : "候选已准备好，在下方确认旁白并合成；若处理失败，可查看具体提示。" };
  } else if (!director.focus && previewable) {
    next = { label: "去看当前候选", detail: "已有成片可以先看，不必重新开始。", navigate: "preview" };
  } else if (director.focus) {
    if (!episode.assets.length) next = { label: "带来这一期的素材", detail: "先上传原片，导演才能根据真实内容提方案。", navigate: "materials" };
    else if (["empty", "stale"].includes(episode.analysis_state)) next = { label: "让导演先看素材", detail: "分析画面与原声，找出可以讲的内容。", generate: "analysis" };
    else if (!confirmed) next = { label: "看看素材理解是否准确", detail: "只需确认概览，有明显不对的地方再修改。", navigate: "direction" };
    else if (!current || !directions.length) next = { label: changed ? "按新要求生成方向" : "请导演提出拍法", detail: "根据已确认的素材和你的要求，生成可选择的方向。", generate: "directions" };
    else if (version) next = { label: previewable ? "去看当前候选" : "继续安排这一期", detail: previewable ? "先看成片效果，再决定要怎么改。" : "方向已经选好，接下来安排片段与旁白。", navigate: previewable ? "preview" : "edit" };
  }

  const choiceButtons = <div className="director-choices" aria-label="这一期想留下什么">{choices.map(choice => <button type="button" key={choice.value} disabled={disabled} aria-pressed={director.focus === choice.value} onClick={() => void save({ choice: choice.value })}>
    <span>{choice.label}{director.focus === choice.value && <Check size={14} aria-hidden="true" />}</span><small>{choice.detail}</small>
  </button>)}</div>;
  return <section className="director-guide" aria-labelledby={headingId} aria-busy={saving || pending || undefined}>
    <div className="director-heading"><span className="director-avatar" aria-hidden="true"><Clapperboard size={23} /></span><div><h2 id={headingId}>{director.focus ? "本期偏好" : "这一期，最想留下什么？"}</h2></div>{director.focus && <span className="director-focus">{focusName[director.focus]}</span>}</div>
    {!director.focus ? choiceButtons : <details className="director-preferences"><summary>调整这期的侧重点</summary>{choiceButtons}</details>}

    {!!recent.length && <div className="director-conversation" aria-label="最近的导演沟通">{recent.map(message => <div className={`director-message ${message.role}`} key={message.id}><span>{message.role === "user" ? "你" : "记录确认"}</span><ShortText text={message.text} label={`${message.role === "user" ? "你的要求" : "导演回复"}完整内容`} limit={130} /></div>)}</div>}
    {!!previous.length && <details className="director-history"><summary>查看之前的 {previous.length} 条沟通</summary><div>{previous.map(message => <div className={`director-message ${message.role}`} key={message.id}><span>{message.role === "user" ? "你" : "记录确认"}</span><ShortText text={message.text} label="历史沟通完整内容" limit={130} /></div>)}</div></details>}

    <form className="director-message-form" onSubmit={event => { event.preventDefault(); if (text.trim() && Array.from(text).length <= 1000) void save({ text: text.trim() }); }}>
      <label htmlFor={inputId}>还有想保留或避开的内容？</label><div className="director-composer"><textarea id={inputId} rows={1} maxLength={1000} value={text} aria-describedby={helpId} placeholder="比如：保留大家聊天，别放尴尬的镜头" disabled={disabled} onChange={event => setText(event.target.value)} /><Button type="submit" disabled={disabled || !text.trim() || Array.from(text).length > 1000} busy={saving} aria-label="保存导演要求"><Send size={17} aria-hidden="true" /><span>保存要求</span></Button></div>
      <div className="director-form-help"><p id={helpId}>先保存要求，生成方案时交给 AI 导演。</p><span>{Array.from(text).length}/1000</span></div>
    </form>
    {saveError && <p className="director-error" role="alert">{saveError}</p>}
    {changed && <p className="director-change-note">当前要求或素材有更新，重新生成方向后才会用于新候选。已有成片仍可预览。</p>}

    {pending ? <p className="director-progress" role="status">正在处理这一期，完成后继续下一步。</p> : next && <div className="director-next"><div><span>接下来</span><p>{next.detail}</p></div>{next.generate || next.navigate ? <Button variant="primary" disabled={disabled || (!!next.generate && (!canGenerate || dirty))} onClick={() => { if (next.generate) void onGenerate(next.generate); else if (next.navigate) onNavigate(next.navigate); }}>{next.label}<ArrowRight size={17} aria-hidden="true" /></Button> : <strong>{next.label}</strong>}</div>}
    {next?.generate && !pending && (!canGenerate || dirty) && <p className="director-gate-note">{dirty ? "先保存上面这句话，再把要求交给导演。" : "AI 生成暂不可用，请先完成页面提示的准备。"}</p>}
    {previewable && changed && step !== "preview" && <button type="button" className="director-text-button director-existing" disabled={disabled} onClick={() => onNavigate("preview")}>先看已有成片</button>}
  </section>;
}
