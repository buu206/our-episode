import { Suspense } from 'react';
import Studio from '@/components/studio';
import { SessionBoundary } from '@/components/session-boundary';
import { notFound } from 'next/navigation';

export default async function EpisodePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[a-f0-9]{32}$/.test(id)) notFound();
  return <SessionBoundary><Suspense fallback={<p className="loading-page">正在取回这一期…</p>}><Studio episodeId={id} /></Suspense></SessionBoundary>;
}
