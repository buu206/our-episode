import { StrictMode } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { MediaAccessContext, MediaPlayer } from '@/components/ui';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('restores the authorized source after StrictMode cleanup and still clears hidden, revoked and unmounted media', () => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  const load = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  const src = '/api/v1/assets/' + 'a'.repeat(32) + '/media';
  const player = (allowed: boolean) => <StrictMode><MediaAccessContext.Provider value={{ allowed, revision: 1 }}><MediaPlayer src={src} title="授权原片" /></MediaAccessContext.Provider></StrictMode>;
  const view = render(player(true));
  const initial = screen.getByLabelText('授权原片');
  // These calls prove the rehearsal cleanup ran on the reused video element.
  expect(pause).toHaveBeenCalled();
  expect(load).toHaveBeenCalled();
  expect(initial).toHaveAttribute('src', src);

  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  fireEvent(document, new Event('visibilitychange'));
  expect(initial).not.toHaveAttribute('src');
  expect(view.container.querySelector('video')).toBeNull();

  view.rerender(player(false));
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  fireEvent(document, new Event('visibilitychange'));
  expect(view.container.querySelector('video')).toBeNull();
  view.rerender(player(true));
  const restored = screen.getByLabelText('授权原片');
  expect(restored).toHaveAttribute('src', src);
  view.rerender(player(false));
  expect(restored).not.toHaveAttribute('src');
  expect(view.container.querySelector('video')).toBeNull();
  view.rerender(player(true));
  const final = screen.getByLabelText('授权原片');
  expect(final).toHaveAttribute('src', src);
  view.unmount();
  expect(final).not.toHaveAttribute('src');
});
