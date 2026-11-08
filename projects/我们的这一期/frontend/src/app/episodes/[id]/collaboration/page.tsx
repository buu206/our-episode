import { notFound } from 'next/navigation';
import CollaborationManager from '@/components/collaboration-manager';
import { SessionBoundary } from '@/components/session-boundary';

export default async function CollaborationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!/^[a-f0-9]{32}$/.test(id)) notFound();
  return <SessionBoundary><CollaborationManager key={id} episodeId={id} /></SessionBoundary>;
}
