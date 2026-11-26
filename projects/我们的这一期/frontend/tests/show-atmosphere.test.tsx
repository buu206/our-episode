import { StrictMode } from 'react';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ShowBackdrop, ShowOpening } from '@/components/show-atmosphere';

let reduced = false;
let finePointer = true;
let preferenceListeners: Set<() => void>;

beforeEach(() => {
  window.history.replaceState({}, '', '/');
  vi.useFakeTimers();
  sessionStorage.clear();
  reduced = false;
  finePointer = true;
  preferenceListeners = new Set();
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({
    get matches() { return query.includes('reduced-motion') ? reduced : finePointer; },
    media: query,
    addEventListener: (_event: string, callback: () => void) => { preferenceListeners.add(callback); },
    removeEventListener: (_event: string, callback: () => void) => { preferenceListeners.delete(callback); },
  })));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const begin = () => act(() => { vi.advanceTimersByTime(0); });
const replay = () => act(() => { window.dispatchEvent(new Event('episode:replay-opening')); });

describe('opening interaction boundary', () => {
  it.each(['/participate', '/watch/' + 'a'.repeat(32)])('does not cover a guest access screen: %s', path => {
    window.history.replaceState({}, '', path);
    render(<ShowOpening />); begin(); replay();
    expect(document.querySelector('dialog')?.open).toBe(false);
    expect(sessionStorage.getItem('our-episode:opening:v1')).toBeNull();
  });
  it('opens once in StrictMode, traps focus, and skipping restores focus and previous inert state', () => {
    const view = render(<StrictMode><button>继续编辑</button><div data-testid="already-inert" inert /><ShowOpening /></StrictMode>);
    const background = screen.getByRole('button', { name: '继续编辑' });
    const alreadyInert = screen.getByTestId('already-inert');
    // jsdom does not implement inert; preserve and verify the property boundary.
    alreadyInert.inert = true;
    background.focus();
    begin();
    const dialog = view.container.querySelector('dialog')!;
    const skip = screen.getByRole('button', { name: '跳过开场' });
    expect(dialog).toHaveAttribute('open');
    expect(background.inert).toBe(true);
    expect(skip).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(skip).toHaveFocus();
    fireEvent.click(skip);
    expect(dialog).not.toHaveAttribute('open');
    expect(background.inert).not.toBe(true);
    expect(alreadyInert.inert).toBe(true);
    expect(background).toHaveFocus();
    expect(document.body.style.overflow).not.toBe('hidden');
    expect(sessionStorage.getItem('our-episode:opening:v1')).toBeNull();
  });

  it('finishes automatically and replays on a fresh document mount even with an old session flag', () => {
    const first = render(<ShowOpening />);
    begin();
    expect(first.container.querySelector('dialog')).toHaveAttribute('open');
    act(() => { vi.advanceTimersByTime(2700); });
    expect(first.container.querySelector('dialog')).not.toHaveAttribute('open');
    first.unmount();
    sessionStorage.setItem('our-episode:opening:v1', '1');
    const next = render(<ShowOpening />);
    begin();
    expect(next.container.querySelector('dialog')).toHaveAttribute('open');
    fireEvent.keyDown(document, { key: 'Escape' });
    replay();
    expect(next.container.querySelector('dialog')).toHaveAttribute('open');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(next.container.querySelector('dialog')).not.toHaveAttribute('open');
  });

  it('never opens for reduced motion, including replay, and closes when the preference changes', () => {
    reduced = true;
    const view = render(<ShowOpening />);
    begin(); replay();
    expect(view.container.querySelector('dialog')).not.toHaveAttribute('open');
    reduced = false;
    replay();
    expect(view.container.querySelector('dialog')).toHaveAttribute('open');
    act(() => { reduced = true; preferenceListeners.forEach(callback => callback()); });
    expect(view.container.querySelector('dialog')).not.toHaveAttribute('open');
  });

  it('replays on a new mount without relying on session storage', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('blocked', 'SecurityError'); });
    const first = render(<ShowOpening />);
    begin(); replay();
    expect(first.container.querySelector('dialog')).toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: '跳过开场' }));
    first.unmount();
    const next = render(<ShowOpening />);
    begin();
    expect(next.container.querySelector('dialog')).toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: '跳过开场' }));
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('restores the application if unmounted mid-opening and removes its replay listener', () => {
    const background = document.createElement('button');
    document.body.append(background);
    background.focus();
    const view = render(<ShowOpening />);
    begin();
    expect(background.inert).toBe(true);
    view.unmount();
    expect(background.inert).not.toBe(true);
    expect(background).toHaveFocus();
    expect(document.body.style.overflow).not.toBe('hidden');
    replay();
    expect(document.querySelector('dialog[open]')).toBeNull();
    background.remove();
  });
});

describe('interactive backdrop', () => {
  it('supports native keyboard activation without a mouse', async () => {
    vi.useRealTimers();
    const user = userEvent.setup();
    render(<ShowBackdrop />);
    const sticker = screen.getByRole('button', { name: '这段保留字贴，点按换一句' });
    sticker.focus();
    await user.keyboard('{Enter}');
    expect(sticker).toHaveTextContent('舍不得剪');
    await user.keyboard(' ');
    expect(sticker).toHaveTextContent('这段保留');
  });

  it('changes a sticker only on its own button and keeps it a non-submitting native control', () => {
    const submit = vi.fn();
    render(<form onSubmit={submit}><ShowBackdrop /></form>);
    const scene = screen.getByRole('button', { name: '名场面字贴，点按换一句' });
    expect(scene).toHaveAttribute('type', 'button');
    expect(scene).toHaveAttribute('aria-pressed', 'false');
    scene.focus();
    expect(scene).toHaveFocus();
    fireEvent.click(scene);
    expect(scene).toHaveAttribute('aria-pressed', 'true');
    expect(scene).toHaveTextContent('就是这段!');
    expect(screen.getByRole('button', { name: '笑出声字贴，点按换一句' })).toHaveTextContent('笑出声');
    expect(submit).not.toHaveBeenCalled();
    fireEvent.click(scene);
    expect(scene).toHaveTextContent('名场面!');
  });

  it('batches mouse parallax into one animation frame and cancels it on unmount', () => {
    const queue = vi.spyOn(window, 'requestAnimationFrame');
    const cancel = vi.spyOn(window, 'cancelAnimationFrame');
    const view = render(<section><ShowBackdrop /></section>);
    const hero = view.container.querySelector('section')!;
    vi.spyOn(hero, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, top: 0, left: 0, right: 400, bottom: 400, width: 400, height: 400, toJSON: () => ({}) });
    const backdrop = hero.firstElementChild as HTMLElement;
    const move = (x: number) => {
      const event = new Event('pointermove');
      Object.defineProperties(event, { pointerType: { value: 'mouse' }, clientX: { value: x }, clientY: { value: 400 } });
      hero.dispatchEvent(event);
    };
    move(300); move(400); move(400);
    expect(queue).toHaveBeenCalledTimes(1);
    act(() => { vi.advanceTimersByTime(20); });
    expect(backdrop.style.getPropertyValue('--drift-x')).toBe('7px');
    expect(backdrop.style.getPropertyValue('--drift-y')).toBe('5px');
    move(0);
    const frame = queue.mock.results.at(-1)!.value;
    view.unmount();
    expect(cancel).toHaveBeenCalledWith(frame);
  });

  it('does not schedule parallax for reduced motion or a coarse pointer', () => {
    const queue = vi.spyOn(window, 'requestAnimationFrame');
    const view = render(<section><ShowBackdrop /></section>);
    const hero = view.container.querySelector('section')!;
    reduced = true;
    fireEvent.pointerMove(hero, { clientX: 100, clientY: 100 });
    reduced = false; finePointer = false;
    fireEvent.pointerMove(hero, { clientX: 100, clientY: 100 });
    expect(queue).not.toHaveBeenCalled();
  });
});
