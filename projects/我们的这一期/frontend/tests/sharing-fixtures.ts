import type { Episode, Version } from '@/lib/types';
import type { Share, ViewerAuth, ViewerShare } from '@/lib/collaboration-types';

export const eid = 'e'.repeat(32), sid = 'a'.repeat(32), vid = 'b'.repeat(32), nextVid = 'c'.repeat(32), otherSid = 'd'.repeat(32);
export const version = (overrides: Partial<Version> = {}): Version => ({
  id: vid, title: '已经确认的一版', state: 'confirmed', origin: 'manual',
  segments: [], media_url: `/api/v1/versions/${vid}/media`, job_id: null, error: null,
  base_version_id: null, created_at: '2026-11-08T10:00:00+00:00', narration_text: '',
  narration_approved: false, narration_ready: false, packaging: 'original', evidence_revision: null, ...overrides,
});
export const episode = (overrides: Partial<Episode> = {}): Episode => ({
  id: eid, title: '朋友的一期', background: '', created_at: '2026-11-08T10:00:00+00:00', assets: [],
  versions: [version(), version({ id: nextVid, title: '尚未确认的草案', state: 'draft', media_url: null })],
  jobs: [], directions: [], evidence: [], evidence_revision: 1, evidence_confirmed_revision: null,
  analysis_state: 'empty', analysis_notes: [], analysis_coverage: [], directions_revision: null, selected_direction: null,
  budget: { spent: 0, reserved: 0, limit: 3, total_spent: 0, total_reserved: 0, total_limit: 30 }, ...overrides,
});
export const share = (overrides: Partial<Share> = {}): Share => ({
  id: sid, episode_id: eid, version_id: vid, label: '小林', title: '已经确认的一版', state: 'active', revision: 1,
  created_at: '2026-11-08T10:00:00+00:00', updated_at: '2026-11-08T10:00:00+00:00',
  expires_at: Date.now() / 1000 + 86400, closed_at: null, restricted_reason: null,
  confirmed_at: '2026-11-08T10:00:00+00:00', watch_path: `/watch/${sid}`, ...overrides,
});
export const viewerAuth = (overrides: Partial<ViewerAuth> = {}): ViewerAuth => ({ share_id: sid, label: '小林', csrf_token: 'fixture-viewer-csrf', ...overrides });
export const viewerShare = (overrides: Partial<ViewerShare> = {}): ViewerShare => ({
  id: sid, label: '小林', title: '已经确认的一版', version_id: vid, revision: 1, expires_at: Date.now() / 1000 + 86400,
  confirmed_at: '2026-11-08T10:00:00+00:00', media_url: `/api/v1/viewer/shares/${sid}/media?revision=1`, ...overrides,
});
