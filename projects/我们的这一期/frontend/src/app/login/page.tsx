import { Suspense } from 'react';
import { Login } from '@/components/login';
import { redirect } from 'next/navigation';
export const dynamic = 'force-dynamic';
export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const origin = process.env.APP_ORIGIN || '';
  if (process.env.LOCAL_DEMO_ENTRY === '1' && process.env.BACKEND_MODE === 'normal' && /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(origin)) {
    const next = (await searchParams).next;
    redirect(next && /^\/episodes\/[a-f0-9]{32}(?:\/(?:collaboration|sharing))?(?:\?[^#\\\r\n]*)?$/.test(next) ? next : '/');
  }
  return <Suspense fallback={<main id="main" className="loading-page">正在打开导演台…</main>}><Login /></Suspense>;
}
