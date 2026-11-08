import type { Evidence } from '@/lib/types';
import type { Draft } from './model';

// Current-tab memory only. Never stores files, media bytes, credentials or confirmations.
export type WorkspaceDraft = { draft?: Draft; evidence?: { items: Evidence[]; revision: number } };
let owner: string | null = null;
const entries = new Map<string, WorkspaceDraft>();
export function setDraftOwner(value: string) { if (owner !== value) entries.clear(); owner = value; }
export function clearWorkspaceDrafts() { owner = null; entries.clear(); }
export function readWorkspaceDraft(id: string) { const value = owner && entries.get(id); return value ? structuredClone(value) : null; }
export function writeWorkspaceDraft(id: string, value: WorkspaceDraft) {
  if (!owner) return;
  if (!value.draft && !value.evidence) entries.delete(id);
  else entries.set(id, structuredClone(value));
}
export function discardWorkspaceDraft(pathname: string) {
  const id = /^\/episodes\/([a-f0-9]{32})$/.exec(pathname)?.[1];
  if (id) entries.delete(id);
}
