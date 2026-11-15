import type { Metadata, Viewport } from 'next';
import { NavigationGuard } from '@/components/session-boundary';
import { ShowOpening } from '@/components/show-atmosphere';
import './globals.css';
import './premiere.css';
import './community.css';
import './production.css';
import './director.css';
import './director-chat.css';

export const metadata: Metadata = {
  title: { default: '在场 · 把熟人聚会拍成综艺', template: '%s · 在场' },
  description: '从真实片段开始，一起制作属于你们的一期。',
  icons: { icon: '/brand/zaichang-logo.png', apple: '/brand/zaichang-logo.png' },
  robots: { index: false, follow: false },
};
export const viewport: Viewport = { width: 'device-width', initialScale: 1, themeColor: '#f8f5ef' };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><body><NavigationGuard /><ShowOpening /><a className="skip-link" href="#main">跳到主要内容</a>{children}</body></html>;
}
