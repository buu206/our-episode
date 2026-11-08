import { ViewerPortal } from '@/components/viewer-portal';

export const metadata = { title: '受邀观看' };

export default async function WatchPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ViewerPortal key={id} shareId={id} />;
}
