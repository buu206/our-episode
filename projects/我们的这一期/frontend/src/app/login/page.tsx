import { Suspense } from 'react';
import { Login } from '@/components/login';
export default function LoginPage() { return <Suspense fallback={<main id="main" className="loading-page">正在打开导演台…</main>}><Login /></Suspense>; }
