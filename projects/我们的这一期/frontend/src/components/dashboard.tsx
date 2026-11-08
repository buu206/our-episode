'use client';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowRight, ChevronDown, Film, Paperclip, RefreshCw, RotateCcw, X } from 'lucide-react';
import { api, AppError, uploadAsset } from '@/lib/api';
import type { Episode, Episodes } from '@/lib/types';
import { Button, Notice, useMediaAccess } from './ui';

type UploadState = 'selected' | 'uploading' | 'processing' | 'done' | 'failed' | 'unknown';
type SelectedVideo = { id: string; name: string; size: number; fingerprint: string; file?: File; state: UploadState; sent: number; total: number; error?: string };
const maxBytes = 1024 ** 3;
const examples = [
  ['朋友聚会', '这次是朋友聚会。我想保留大家自然的互动和原声，剪成一段轻松的回顾。'],
  ['周末出游', '记录一次周末出游。希望把路上的小插曲和有意思的瞬间连起来，留住当时的心情。'],
  ['平凡的一天', '这是平凡的一天。我想从真实片段里找到值得记住的小事，做成温暖的一期。'],
];
const sizeLabel = (bytes: number) => bytes >= maxBytes ? `${(bytes / maxBytes).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
const stateLabel: Record<UploadState, string> = { selected: '等待提交', uploading: '正在传输', processing: '传输完成，正在核对视频', done: '已接收', failed: '未接收', unknown: '接收结果待核对' };

function episodeLabel(ep: Episode) {
  if (ep.jobs.some(job => job.state === 'running' || job.state === 'queued')) return '正在制作';
  if (ep.versions.some(version => version.state === 'confirmed')) return '已有确认成片';
  if (ep.versions.some(version => version.state === 'ready')) return '等你预览';
  return ep.assets.length ? '继续制作' : '等待素材';
}

export function Dashboard() {
  const [episodes, setEpisodes] = useState<Episode[] | null>(null);
  const [error, setError] = useState('');
  const [prompt, setPrompt] = useState('');
  const [videos, setVideos] = useState<SelectedVideo[]>([]);
  const [formError, setFormError] = useState('');
  const [busy, setBusy] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [created, setCreated] = useState<Episode | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [mediaFresh, setMediaFresh] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(false);
  const operation = useRef(0);
  const working = useRef(false);
  const selectionId = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const writeController = useRef<AbortController | null>(null);
  const listController = useRef<AbortController | null>(null);
  const access = useMediaAccess();
  const permission = useRef(access.allowed);
  useEffect(() => { permission.current = access.allowed; }, [access.allowed]);
  const router = useRouter();
  const incomplete = videos.some(video => video.state !== 'done');
  const dirty = !!busy || incomplete || (!created && !!prompt.trim());
  const blocked = !!busy || uncertain || !access.allowed;

  const reload = useCallback(async () => {
    const current = ++generation.current;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    try {
      const data = await api<Episodes>('/episodes', { signal: controller.signal });
      if (mounted.current && current === generation.current && !controller.signal.aborted) { setEpisodes([...data.items].reverse()); setError(''); setMediaFresh(!document.hidden); }
    } catch (cause) {
      if (mounted.current && current === generation.current && !controller.signal.aborted) { setMediaFresh(false); setError(cause instanceof Error ? cause.message : '节目暂时没有取回。'); }
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController(); listController.current = controller;
    const current = ++generation.current;
    api<Episodes>('/episodes', { signal: controller.signal })
      .then(data => { if (mounted.current && current === generation.current && !controller.signal.aborted) { setEpisodes([...data.items].reverse()); setError(''); setMediaFresh(!document.hidden); } })
      .catch(cause => { if (mounted.current && current === generation.current && !controller.signal.aborted) { setMediaFresh(false); setError(cause instanceof Error ? cause.message : '节目暂时没有取回。'); } });
    const visibility = () => { setMediaFresh(false); listController.current?.abort(); if (!document.hidden) void reload(); };
    const expired = () => { mounted.current = false; writeController.current?.abort(); listController.current?.abort(); };
    document.addEventListener('visibilitychange', visibility);
    window.addEventListener('episode:auth-expired', expired);
    return () => {
      mounted.current = false; writeController.current?.abort(); listController.current?.abort();
      document.removeEventListener('visibilitychange', visibility); window.removeEventListener('episode:auth-expired', expired);
      window.dispatchEvent(new Event('episode:dirty-release'));
    };
  }, [reload]);
  useEffect(() => { window.dispatchEvent(new CustomEvent('episode:dirty', { detail: dirty })); }, [dirty]);

  function addFiles(files: File[]) {
    if (blocked || created) return;
    const next = [...videos];
    for (const file of files) {
      if (!/\.(mp4|mov)$/i.test(file.name) || file.size === 0) { setFormError('请选择非空的 MP4 或 MOV 视频。视频格式与时长会在提交后核对。'); return; }
      const fingerprint = `${file.name}:${file.size}:${file.lastModified}`;
      if (!next.some(video => video.fingerprint === fingerprint)) next.push({ id: `video-${++selectionId.current}`, name: file.name, size: file.size, fingerprint, file, state: 'selected', sent: 0, total: file.size });
    }
    if (next.length > 8 || next.reduce((sum, video) => sum + video.size, 0) > maxBytes) { setFormError('每期最多选择 8 段视频，文件合计不超过 1 GiB。请减少一些再试。'); return; }
    setVideos(next); setFormError('');
  }
  function allowLeave() {
    if (!dirty) return true;
    return window.confirm(created ? '已接收的素材已经保存。离开后，未完成的视频需要重新选择；接收结果待核对的视频请先检查。继续离开？' : '想法和所选视频还没有全部保存。离开后需要重新填写或选择，继续离开？');
  }
  function enterEpisode(ep: Episode) {
    setPrompt(''); setVideos([]);
    window.dispatchEvent(new CustomEvent('episode:dirty', { detail: false }));
    router.push(`/episodes/${ep.id}?step=materials`);
  }
  async function sendFiles(ep: Episode, rows: SelectedVideo[], current: number, controller: AbortController) {
    const active = () => mounted.current && current === operation.current && !controller.signal.aborted;
    const update = () => { if (active()) setVideos(rows.map(row => ({ ...row }))); };
    for (const row of rows) {
      if (!active()) return;
      // Finish an already-sent request, but never start another while permission is being rechecked.
      if (document.hidden || !permission.current) break;
      if (!row.file || !['selected', 'failed'].includes(row.state)) continue;
      row.state = 'uploading'; row.sent = 0; row.error = undefined; update();
      try {
        await uploadAsset(ep.id, row.file, (sent, total) => {
          if (!active()) return;
          row.sent = sent; row.total = total || row.size; row.state = total > 0 && sent >= total ? 'processing' : 'uploading'; update();
        }, controller.signal);
        if (!active()) return;
        row.state = 'done'; row.file = undefined; update();
      } catch (cause) {
        if (!active()) return;
        row.state = cause instanceof AppError && cause.uncertain ? 'unknown' : 'failed';
        row.error = cause instanceof Error ? cause.message : '视频没有接收完成。'; update();
        if (cause instanceof AppError && cause.status === 401) return;
      }
    }
    if (!active()) return;
    if (rows.every(row => row.state === 'done') && !document.hidden && permission.current) enterEpisode(ep);
    else { setFormError('这一期已经建立，已接收的视频也已保存。可以继续接收未完成的视频，或进入这一期核对素材。'); void reload(); }
  }
  async function create(event: FormEvent) {
    event.preventDefault();
    if (working.current || blocked || created || (!prompt.trim() && !videos.length)) return;
    working.current = true;
    const current = ++operation.current;
    const controller = new AbortController(); writeController.current = controller;
    const rows = videos.map(row => ({ ...row }));
    const background = prompt.trim();
    setBusy('正在建立这一期'); setFormError('');
    try {
      const ep = await api<Episode>('/episodes', { method: 'POST', body: { title: Array.from(background.split(/\r?\n/)[0]?.trim() || '新的一期').slice(0, 120).join(''), background }, signal: controller.signal });
      if (!mounted.current || current !== operation.current || controller.signal.aborted) return;
      setCreated(ep);
      if (rows.length) { setBusy('正在接收视频'); await sendFiles(ep, rows, current, controller); }
      else if (!document.hidden && permission.current) enterEpisode(ep);
    } catch (cause) {
      if (!mounted.current || current !== operation.current || controller.signal.aborted) return;
      setFormError(cause instanceof Error ? cause.message : '创建没有完成。');
      if (cause instanceof AppError && cause.uncertain) { setUncertain(true); void reload(); }
    } finally { working.current = false; if (mounted.current && current === operation.current) setBusy(''); }
  }
  async function retryFailed() {
    if (!created || working.current || blocked) return;
    working.current = true; const current = ++operation.current;
    const controller = new AbortController(); writeController.current = controller;
    setBusy('正在接收视频'); setFormError('');
    try { await sendFiles(created, videos.map(row => ({ ...row })), current, controller); }
    finally { working.current = false; if (mounted.current && current === operation.current) setBusy(''); }
  }

  return <div className="dashboard premiere-home">
    <section className="premiere-hero" aria-labelledby="premiere-title">
      <p className="premiere-kicker" title="示例节目期数与场景">第 07 期 · 周五夜场</p>
      <h1 className="premiere-title" id="premiere-title">我们的<span>这一期</span></h1>
      <p className="premiere-theme">说好今晚不点外卖</p>
      <p className="premiere-description">熟人聚会一键变综艺。选主题、喊朋友、开拍——片头花字、反应镜头、名场面切片，AI 帮你包圆。</p>
      <div className="premiere-cast" aria-label="示例节目角色">{['阿哲', '小满', '老周', 'Nana', '豆豆'].map((name, index) => <div className="cast-person" key={name} title={`${name} · 原创生成的示例角色，并非你的节目参与者`}>
        <span className="cast-photo" style={{ backgroundPosition: `${index * 25}% 30%` }} aria-hidden="true" /><span className="cast-name">{name}</span>
      </div>)}</div>
      <div className="premiere-composer" aria-busy={!!busy}>
        <form className="capsule-bar" onSubmit={create}>
          <span className="ai-label" aria-hidden="true">✦ AI</span>
          <input className="capsule-input" type="text" aria-label="这一期的想法" value={prompt} onChange={event => setPrompt(event.target.value)} maxLength={4000} disabled={blocked || !!created} placeholder="今晚想拍什么？比如：说好不点外卖 / 谁先破功…" />
          {created ? <Link className="button primary capsule-shoot" href={`/episodes/${created.id}?step=materials`} onClick={event => { if (!allowLeave()) event.preventDefault(); }}>进入已创建的这一期<ArrowRight size={18} /></Link> : <Button variant="primary" className="capsule-shoot" type="submit" busy={!!busy} disabled={blocked || (!prompt.trim() && !videos.length)}><span className="record-dot" aria-hidden="true" />开拍</Button>}
        </form>
        {!!videos.length && <ul className="selected-files" aria-label="所选视频" aria-live="polite">{videos.map(video => <li key={video.id} className={`selected-file ${video.state}`}>
          <span className="file-icon"><Film size={18} /></span><div className="file-detail"><strong>{video.name}</strong><span>{sizeLabel(video.size)} · {stateLabel[video.state]}</span>
            {['uploading', 'processing'].includes(video.state) && <progress aria-label={`${video.name}传输进度`} value={video.sent} max={video.total || 1} />}
            {video.error && <span className="error-text">{video.error}</span>}{video.state === 'unknown' && <span className="muted">请进入已创建的这一期核对，这段视频不会自动重传。</span>}
          </div>{!created && <Button variant="ghost" aria-label={`移除${video.name}`} disabled={blocked} onClick={() => setVideos(previous => previous.filter(row => row.id !== video.id))}><X size={16} /></Button>}
        </li>)}</ul>}
        {busy && <p className="composer-status" role="status">{busy}。{videos.length > 0 && '传输结束后还需等待视频核对。'}</p>}
        {formError && <Notice tone="error">{formError}</Notice>}
        {uncertain && <Notice>创建结果暂未确认，想法和所选视频仍保留在本页。请先核对最近节目，避免重复创建；不会自动提交或认领任何一期。<a className="text-link" href="#recent">查看最近节目</a><Button variant="ghost" onClick={() => {
          if (window.confirm('请先刷新并检查最近节目。确认没有创建成功的这一期，才解除锁定；之后还需你再次点击开拍。如果原请求稍后成功，可能出现重复节目。现在解除锁定？')) { setUncertain(false); setFormError(''); }
        }}>已核对，允许重新提交</Button></Notice>}
        {created && videos.some(video => ['failed', 'selected'].includes(video.state)) && <Button disabled={blocked} onClick={() => void retryFailed()}>仅接收未完成的视频</Button>}
      </div>
      <p className="premiere-hint">丢一句主题就行，素材可稍后补</p>
      <a className="premiere-secondary" href="#recent">逛逛往期 <ArrowRight size={15} /></a>
    </section>
    <details className="premiere-extras">
      <summary>添加素材与更多灵感<ChevronDown size={17} /></summary>
      <div className="premiere-extras-content">
        <div className="composer-toolbar"><input ref={input} type="file" className="composer-file-input" aria-label="选择本期视频" hidden multiple accept=".mp4,.mov,video/mp4,video/quicktime" disabled={blocked || !!created} onChange={event => { addFiles(Array.from(event.currentTarget.files || [])); event.currentTarget.value = ''; }} />
          <Button variant="ghost" className="attach-video" disabled={blocked || !!created} onClick={() => input.current?.click()}><Paperclip size={19} /><span>添加视频{videos.length ? ` · ${videos.length}/8` : ''}</span></Button>
          <p className="composer-hint">MP4 / MOV · 最多 8 段、合计 10 分钟和 1 GiB。选择后，点击开拍才会提交。</p>
        </div>
        <div className="prompt-suggestions"><span>试试这些想法</span><div className="prompt-chips">{examples.map(([label, text]) => <button className="idea-chip" key={label} type="button" disabled={blocked || !!created} onClick={() => { if (!prompt.trim() || window.confirm('用这个示例替换当前想法？你仍然可以继续修改。')) setPrompt(text); }}>{label}</button>)}</div></div>
      </div>
    </details>
    <section className="recent-section" id="recent" aria-labelledby="recent-title">
      <div className="section-title"><div><span className="eyebrow">继续你们的故事</span><h2 id="recent-title">最近制作</h2></div><Button variant="ghost" onClick={() => void reload()} aria-label="刷新节目列表"><RefreshCw size={17} /><span>刷新</span></Button></div>
      {error && <Notice tone="error">{error}</Notice>}
      {episodes === null && !error && <div className="episode-grid recent-grid" aria-label="正在加载节目" role="status">{[1, 2, 3].map(i => <div key={i} className="skeleton-card" />)}</div>}
      {episodes?.length === 0 && <div className="recent-empty"><Film size={25} /><p>第一期，从你的素材开始</p><span>写下一点想法，或者先选几段视频。</span></div>}
      <div className="episode-grid recent-grid">{(showAll ? episodes : episodes?.slice(0, 3))?.map((ep, index) => {
        const version = [...ep.versions].reverse().find(v => ['ready', 'confirmed'].includes(v.state) && v.media_url);
        const time = new Date(ep.created_at);
        return <article key={ep.id} className="episode-card"><Link href={`/episodes/${ep.id}?step=${version ? 'preview&version=' + version.id : 'materials'}`} aria-label={`继续制作：${ep.title}`} onClick={event => { if (!allowLeave()) event.preventDefault(); }}>
          <div className={`episode-cover cover-${index % 3}`}>
            {version?.media_url && access.allowed && mediaFresh ? <video key={access.revision} src={version.media_url} muted playsInline preload="metadata" tabIndex={-1} aria-hidden="true" /> : <div className="cover-art" aria-hidden="true"><Film size={58} strokeWidth={1.2} /><span>我们的<br />这一期</span></div>}
            <span className="episode-number">EP. {String((episodes?.length || 0) - index).padStart(2, '0')}</span><span className="cover-label">{episodeLabel(ep)}</span>
          </div><div className="episode-card-body"><h3>{ep.title}</h3><p>{ep.assets.length} 段可用素材 · {ep.versions.length} 个版本</p><div className="card-bottom"><span>{Number.isNaN(time.getTime()) ? '最近制作' : time.toLocaleDateString('zh-CN')}</span><span>继续制作<ArrowRight size={16} /></span></div></div>
        </Link></article>;
      })}</div>
      {!!episodes && episodes.length > 3 && <Button className="show-all-episodes" variant="ghost" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>{showAll ? '收起，查看最近 3 期' : `展开全部 ${episodes.length} 期`}<ChevronDown size={17} /></Button>}
    </section>
    <section className="premiere-how" id="how-it-works" aria-labelledby="how-title">
      <div className="section-title"><h2 id="how-title">怎么玩</h2><button className="replay-opening" type="button" aria-label="重播开场" onClick={() => window.dispatchEvent(new Event('episode:replay-opening'))}><RotateCcw size={15} />重播开场</button></div>
      <ol><li><span>01</span><h3>丢一句主题</h3><p>写下这一期想留下什么，先给故事一个开头。</p></li><li><span>02</span><h3>添几段真实素材</h3><p>选好视频，补充背景，再安排你们的片段。</p></li><li><span>03</span><h3>看过，再确认</h3><p>预览完整成片，确认满意后再导出、分享。</p></li></ol>
    </section>
  </div>;
}
