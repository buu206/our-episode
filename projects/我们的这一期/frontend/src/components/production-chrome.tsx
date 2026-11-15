import { LockKeyhole, MoreHorizontal } from 'lucide-react';

/** A decorative browser frame, shared by the screens after the opening page. */
export function ProductionChrome({ label }: { label: string }) {
  return <div className="premiere-chrome production-chrome">
    <div className="chrome-dots" aria-hidden="true"><i /><i /><i /></div>
    <div className="chrome-address" aria-label="装饰浏览器地址栏，非实际访问地址"><LockKeyhole size={12} aria-hidden="true" /><span>在场 / {label}</span></div>
    <MoreHorizontal size={22} className="chrome-more" aria-hidden="true" />
  </div>;
}
