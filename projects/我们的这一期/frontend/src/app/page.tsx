import { Dashboard } from '@/components/dashboard';
import { SessionBoundary } from '@/components/session-boundary';
export default function Home() { return <SessionBoundary><Dashboard /></SessionBoundary>; }
