import type { Metadata, Viewport } from 'next';
import { NavigationGuard } from '@/components/session-boundary';
import { ShowOpening } from '@/components/show-atmosphere';
import './globals.css';
import './premiere.css';
import './community.css';

export const metadata: Metadata = {
  title: { default: '我们的这一期 · 把熟人聚会拍成综艺', template: '%s · 我们的这一期' },
  description: '从真实片段开始，一起制作属于你们的一期。',
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { width: 'device-width', initialScale: 1, themeColor: '#f8f5ef' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body><NavigationGuard /><ShowOpening /><a className="skip-link" href="#main">跳到主要内容</a>{children}</body></html>;
}
