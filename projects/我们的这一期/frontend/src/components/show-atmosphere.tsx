'use client';

import { useEffect, useRef, useState } from 'react';
import styles from './show-atmosphere.module.css';

const OPENING_SEEN_KEY = 'our-episode:opening:v1';
const OPENING_DURATION = 2700;
let playedWithoutStorage = false;

function hasSeenOpening() {
  try { return window.sessionStorage.getItem(OPENING_SEEN_KEY) === '1'; }
  catch { return playedWithoutStorage; }
}

function rememberOpening() {
  playedWithoutStorage = true;
  try { window.sessionStorage.setItem(OPENING_SEEN_KEY, '1'); }
  catch { /* A blocked storage area must not block entry or replay on every route. */ }
}

function Star({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 100 100" fill="currentColor" aria-hidden="true" focusable="false"><path d="m50 0 9 32 27-18-18 27 32 9-32 9 18 27-27-18-9 32-9-32-27 18 18-27L0 50l32-9-18-27 27 18Z" /></svg>;
}

const stickers = [
  { id: 'scene', text: '名场面!', alternate: '就是这段!', label: '名场面字贴', className: 'scene' },
  { id: 'keep', text: '这段保留', alternate: '舍不得剪', label: '这段保留字贴', className: 'keep' },
  { id: 'laugh', text: '笑出声', alternate: '哈哈哈哈', label: '笑出声字贴', className: 'laugh' },
  { id: 'count', text: '3 2 1', alternate: '开拍!', label: '倒计时字贴', className: 'count' },
  { id: 'coming', text: '好戏来了', alternate: '主角是你', label: '好戏来了字贴', className: 'coming' },
] as const;

/** Place first inside a positioned, isolated hero; its central content sits at z-index: 2. */
export function ShowBackdrop() {
  const root = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState<Record<string, boolean>>({});
  useEffect(() => {
    const element = root.current;
    const hero = element?.parentElement;
    if (!element || !hero || !window.matchMedia) return;
    const fine = window.matchMedia('(hover: hover) and (pointer: fine)');
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0;
    let x = 0;
    let y = 0;
    const reset = () => {
      cancelAnimationFrame(frame); frame = 0;
      element.style.setProperty('--drift-x', '0px');
      element.style.setProperty('--drift-y', '0px');
    };
    const move = (event: PointerEvent) => {
      if (!fine.matches || reduced.matches || (event.pointerType && event.pointerType !== 'mouse')) return;
      x = event.clientX; y = event.clientY;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const bounds = hero.getBoundingClientRect();
        if (!bounds.width || !bounds.height) return;
        const dx = Math.max(-1, Math.min(1, ((x - bounds.left) / bounds.width - 0.5) * 2));
        const dy = Math.max(-1, Math.min(1, ((y - bounds.top) / bounds.height - 0.5) * 2));
        element.style.setProperty('--drift-x', `${dx * 7}px`);
        element.style.setProperty('--drift-y', `${dy * 5}px`);
      });
    };
    hero.addEventListener('pointermove', move, { passive: true });
    hero.addEventListener('pointerleave', reset);
    fine.addEventListener('change', reset);
    reduced.addEventListener('change', reset);
    return () => {
      reset();
      hero.removeEventListener('pointermove', move);
      hero.removeEventListener('pointerleave', reset);
      fine.removeEventListener('change', reset);
      reduced.removeEventListener('change', reset);
    };
  }, []);
  return <div ref={root} className={styles.backdrop} aria-label="综艺字贴，可点按换一句">
    <span className={styles.faintOrbit} aria-hidden="true" />
    {stickers.map(sticker => <button
      key={sticker.id}
      type="button"
      className={`${styles.sticker} ${styles[sticker.className]}`}
      aria-label={`${sticker.label}，点按换一句`}
      aria-pressed={!!active[sticker.id]}
      onClick={() => setActive(current => ({ ...current, [sticker.id]: !current[sticker.id] }))}
    >
      <span className={styles.stickerBody}>
        {sticker.id === 'count' && <Star className={styles.countStar} />}
        {sticker.id === 'keep' && <svg className={styles.arrow} viewBox="0 0 70 34" aria-hidden="true" focusable="false"><path d="M4 9c20 0 30 14 49 14M44 9l15 14-17 7" fill="none" stroke="currentColor" strokeWidth="7" strokeLinecap="square" strokeLinejoin="miter" /></svg>}
        <span className={styles.stickerText} aria-live="polite">{active[sticker.id] ? sticker.alternate : sticker.text}</span>
        <Star className={styles.spark} />
      </span>
    </button>)}
  </div>;
}

/** A native modal owns the focus/inert boundary; it does not wrap or remount the application. */
export function ShowOpening() {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const skipRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)');
    let timer: ReturnType<typeof setTimeout> | undefined;
    let previousFocus: HTMLElement | null = null;
    let previousOverflow = '';
    let inertElements: Array<{ element: HTMLElement; inert: boolean }> = [];

    const close = () => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      if (!dialog.open) return;
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
      for (const { element, inert } of inertElements) element.inert = inert;
      inertElements = [];
      if (document.body.style.overflow === 'hidden') document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
      previousFocus = null;
    };
    const start = (replay = false) => {
      // Invitation holders go straight to their own access/consent screen.
      if (window.location.pathname === '/participate' || window.location.pathname.startsWith('/watch/')) return;
      if (dialog.open || (!replay && hasSeenOpening())) return;
      rememberOpening();
      if (reduced?.matches) return;
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      previousOverflow = document.body.style.overflow;
      // Native showModal makes the rest of the document inert. Explicit sibling
      // inert state also keeps the small older-browser fallback keyboard safe.
      let child: Element = dialog;
      for (let parent = child.parentElement; parent && parent !== document.documentElement; parent = child.parentElement) {
        for (const sibling of parent.children) {
          if (sibling !== child && sibling instanceof HTMLElement && !['SCRIPT', 'STYLE', 'LINK'].includes(sibling.tagName)) {
            inertElements.push({ element: sibling, inert: sibling.inert });
            sibling.inert = true;
          }
        }
        child = parent;
      }
      try { dialog.showModal(); }
      catch { dialog.setAttribute('open', ''); }
      document.body.style.overflow = 'hidden';
      skipRef.current?.focus({ preventScroll: true });
      timer = setTimeout(close, OPENING_DURATION);
    };
    const replay = () => start(true);
    const cancel = (event: Event) => { event.preventDefault(); close(); };
    const keydown = (event: KeyboardEvent) => {
      if (!dialog.open) return;
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
      else if (event.key === 'Tab') { event.preventDefault(); skipRef.current?.focus(); }
    };
    const focus = (event: FocusEvent) => {
      if (dialog.open && event.target instanceof Node && !dialog.contains(event.target)) skipRef.current?.focus({ preventScroll: true });
    };
    const preference = () => { if (reduced?.matches) close(); };
    const skip = skipRef.current;
    // Deferral avoids consuming the once-per-tab flag in StrictMode's rehearsal.
    const initial = setTimeout(() => start(), 0);
    skip?.addEventListener('click', close);
    dialog.addEventListener('cancel', cancel);
    window.addEventListener('episode:replay-opening', replay);
    document.addEventListener('keydown', keydown, true);
    document.addEventListener('focusin', focus, true);
    reduced?.addEventListener('change', preference);
    return () => {
      clearTimeout(initial);
      skip?.removeEventListener('click', close);
      dialog.removeEventListener('cancel', cancel);
      window.removeEventListener('episode:replay-opening', replay);
      document.removeEventListener('keydown', keydown, true);
      document.removeEventListener('focusin', focus, true);
      reduced?.removeEventListener('change', preference);
      close();
    };
  }, []);

  return <dialog ref={dialogRef} className={styles.opening} aria-label="在场开场动画" aria-modal="true">
    <div className={styles.openingFrame} aria-hidden="true">
      <div className={`${styles.curtain} ${styles.curtainLeft}`} />
      <div className={`${styles.curtain} ${styles.curtainRight}`} />
      <div className={styles.cuts}>
        <span className={styles.cutOne}>生活，没有彩排。</span>
        <span className={styles.cutTwo}>这一期，你是主角。</span>
        <Star className={styles.cutStar} />
      </div>
      <div className={styles.openingWordmark}>
        <span className={styles.openingEyebrow}>生活综艺 · 现在开场</span>
        <span className={styles.wordmarkBottom}>在场<span className={styles.wordmarkDot}>!</span></span>
        <Star className={styles.wordmarkStar} />
        <span className={styles.openingRule} />
      </div>
      <span className={styles.openingCorner}>YOUR LIFE. YOUR SHOW.</span>
      <span className={styles.openingIndex}>EP. 001 — ∞</span>
    </div>
    <button ref={skipRef} type="button" className={styles.skip}>跳过开场 <span aria-hidden="true">↗</span></button>
  </dialog>;
}
