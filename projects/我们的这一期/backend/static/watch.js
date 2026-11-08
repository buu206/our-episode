'use strict';
const shareId=location.pathname.split('/').filter(Boolean).at(-1);
const fragment=new URLSearchParams(location.hash.slice(1));
if(fragment.has('code')){$('viewer-code').value=fragment.get('code');history.replaceState(null,'',location.pathname);}
let identity=null,shownRevision=null,mediaURL='',pollTimer,loadGeneration=0;
function clearFilm(){const video=$('film');video.pause();video.removeAttribute('src');video.load();video.hidden=true;mediaURL='';}
function unavailable(message){clearFilm();$('unavailable').textContent=message;$('unavailable').hidden=false;$('share-state').textContent='暂不可观看';}
function schedule(){clearTimeout(pollTimer);pollTimer=setTimeout(()=>load().catch(error=>notice(error.message,true)),5000);}
async function load({announce=false}={}){
  const generation=++loadGeneration;clearTimeout(pollTimer);
  try{
    const auth=await api('/viewer/auth/me');if(generation!==loadGeneration)return false;
    if(auth.share_id!==shareId){clearFilm();identity=null;csrf='';$('watch').hidden=true;$('login').hidden=false;$('logout').hidden=true;notice('当前观看身份属于另一条邀请。请使用本链接里的观看代码进入这条分享。',true);return false;}
    identity=auth;csrf=auth.csrf_token;$('logout').hidden=false;
    const share=await api('/viewer/shares/'+ident(shareId));if(generation!==loadGeneration)return false;
    $('login').hidden=true;$('watch').hidden=false;$('film-title').textContent=share.title;
    $('share-state').textContent='可观看';$('film-detail').textContent='给 '+share.label+' 的观看邀请 · 发布修订 '+share.revision+' · 到期 '+new Date(share.expires_at*1000).toLocaleString();
    $('scope').textContent='此邀请只开放这一份确认过的成片。你不会因此取得原片、采访稿件或编辑权限。';
    const recovering=!$('unavailable').hidden;$('unavailable').hidden=true;
    const url=safeURL(share.media_url);
    if(!url)throw new Error('没有可用的授权媒体地址。');
    if(shownRevision!==null&&shownRevision!==share.revision){clearFilm();notice('发起人已明确更新分享版本。旧播放已停止，请重新核对标题后播放新版。');}
    else if(announce||recovering)notice('分享状态已确认，可以观看此版本。');
    shownRevision=share.revision;
    if(mediaURL!==url){mediaURL=url;$('film').src=url;$('film').hidden=false;}
    schedule();return true;
  }catch(error){
    if(generation!==loadGeneration)return false;
    if(error.status===401){identity=null;csrf='';clearFilm();$('watch').hidden=true;$('login').hidden=false;$('logout').hidden=true;notice('请使用本次观看邀请进入。',true);return false;}
    $('watch').hidden=false;unavailable(error.message);notice(error.message,true);
    // Keep existing playback stopped. Retry read-only state so an explicit
    // owner update can be observed; never reactivate a share in the browser.
    if(error.status!==410)schedule();return false;
  }
}
$('login-form').addEventListener('submit',event=>{event.preventDefault();action('正在核对观看范围…',async()=>{
  ++loadGeneration;clearTimeout(pollTimer);clearFilm();
  const auth=await api('/viewer/auth/login',{method:'POST',body:{share_id:shareId,share_code:$('viewer-code').value}});
  identity=auth;csrf=auth.csrf_token;$('viewer-code').value='';shownRevision=null;clearFilm();if(await load())notice('已进入本次受邀观看。');
});});
$('logout').addEventListener('click',()=>action('正在退出观看…',async()=>{
  ++loadGeneration;clearTimeout(pollTimer);clearFilm();
  await api('/viewer/auth/logout',{method:'POST',body:{}});identity=null;csrf='';shownRevision=null;clearTimeout(pollTimer);clearFilm();$('watch').hidden=true;$('login').hidden=false;$('logout').hidden=true;notice('已退出观看身份。');
}));
$('refresh').addEventListener('click',()=>action('正在检查分享状态…',async()=>{await load({announce:true});}));
$('film').addEventListener('error',()=>{if(mediaURL)load().catch(error=>notice(error.message,true));});
async function boot(){
  if(!/^[a-f0-9]{32}$/.test(shareId)){notice('观看地址不完整，请使用发起人提供的邀请链接。',true);$('login-form').hidden=true;return;}
  if($('viewer-code').value){notice('已从本次链接读取观看代码，请确认观看范围后进入。');return;}
  await load();
}
boot().catch(error=>notice(error.message,true));
