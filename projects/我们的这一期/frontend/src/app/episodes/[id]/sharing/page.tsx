import { notFound } from 'next/navigation';
import { SessionBoundary } from '@/components/session-boundary';
import { ShareManager } from '@/components/share-manager';

export default async function SharingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[a-f0-9]{32}$/.test(id)) notFound();
  return <SessionBoundary><ShareManager key={id} episodeId={id} /></SessionBoundary>;
}
