import Link from 'next/link';
export default function NotFound() { return <main id="main" className="error-page"><span className="eyebrow">没有找到这一页</span><h1>先回到导演台</h1><p>地址可能已变化，也可能不属于你当前的访问范围。</p><Link className="button primary" href="/">查看我的节目</Link></main>; }
