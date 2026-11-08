'use strict';
const episodeId=new URLSearchParams(location.search).get('episode');
let episode=null,shares=[],newCode='',newWatchPath='',requestId=crypto.randomUUID();
const shareNames={active:'可观看',restricted:'内容受限，需明确更新',closed:'已关闭',expired:'已过期'};
$('back').href='/?episode='+ident(episodeId);
function confirmedVersions(){return (episode?.versions||[]).filter(v=>v.state==='confirmed');}
function versionOptions(select,selected){
  select.replaceChildren();
  for(const version of confirmedVersions()){const option=el('option',version.title);option.value=version.id;select.append(option);}
  if([...select.options].some(o=>o.value===selected))select.value=selected;
}
function renderShares(){
  const list=$('shares');list.replaceChildren();
  for(const share of shares){
    const card=el('div',undefined,'item');
    card.append(el('h3',share.label+' · '+shareNames[share.state]));
    listFields(card,[['分享成片',share.title],['发布修订',share.revision],['到期时间',new Date(share.expires_at*1000).toLocaleString()],['观看范围','仅指定成片，不开放原片或采访稿件']]);
    if(share.restricted_reason)card.append(paragraph(share.restricted_reason.message||'内容或确认状态已变化，请核对后明确更新。'));
    if(['active','restricted'].includes(share.state)){
      const editor=details('明确更新这条分享的版本'),select=el('select');
      versionOptions(select,share.version_id);editor.append(field('新的已确认版本',select),paragraph('此操作只更新给“'+share.label+'”的这条分享。原媒体地址会失效，截止时间保持不变。'));
      let updateId=crypto.randomUUID();select.addEventListener('change',()=>{updateId=crypto.randomUUID();});
      const update=button('确认并更新此分享',async()=>{
        if(!select.value)throw new Error('请先在工作台预览并确认可分享的版本。');
        const target=confirmedVersions().find(v=>v.id===select.value);
        if(!await confirmAction('更新这条分享？','观看对象：'+share.label+'\n原版本：'+share.title+'\n新版本：'+target.title+'\n旧媒体地址将失效；有效期不延长。','确认更新'))return;
        await api('/shares/'+ident(share.id)+'/update',{method:'POST',body:{version_id:target.id,expected_revision:share.revision,confirmed:true,client_request_id:updateId}});
        updateId=crypto.randomUUID();await refresh();notice('分享已明确更新，旧媒体地址已失效。');
      },'primary');update.disabled=!select.options.length;editor.append(update);card.append(editor);
      card.append(button('关闭这条分享',async()=>{
        if(!await confirmAction('关闭这条分享？','给“'+share.label+'”的此观看邀请将停止后续访问，旧媒体地址也会失效。已接收的副本无法收回；再次分享需要新建邀请。','确认关闭'))return;
        await api('/shares/'+ident(share.id)+'/close',{method:'POST',body:{expected_revision:share.revision,confirmed:true}});
        await refresh();notice('分享已关闭。重新观看需要发起人新建邀请。');
      },'danger'));
    }else card.append(paragraph('这条邀请不能重新开放；需要时请创建新的观看邀请。'));
    list.append(card);
  }
  empty(list,'还没有观看邀请。确认好成片后，在上方创建。');
}
async function refresh(){
  const [ep,data]=await Promise.all([api('/episodes/'+ident(episodeId)),api('/episodes/'+ident(episodeId)+'/shares')]);
  episode=ep;shares=data.items;$('episode-name').textContent=episode.title;$('workspace').hidden=false;
  const selected=$('share-version').value;versionOptions($('share-version'),selected);
  $('create-share').disabled=!confirmedVersions().length;
  $('version-help').textContent=confirmedVersions().length?'只列出已确认版本；最新生成的草案不会自动加入分享。':'还没有已确认版本，请先返回工作台完整预览并确认。';
  renderShares();
}
for(const id of ['share-label','share-days','share-version'])$(id).addEventListener('change',()=>{requestId=crypto.randomUUID();});
$('share-form').addEventListener('submit',event=>{event.preventDefault();action('正在创建指定版本的观看邀请…',async()=>{
  const created=await api('/versions/'+ident($('share-version').value)+'/shares',{method:'POST',body:{label:$('share-label').value.trim(),expires_days:Number($('share-days').value),confirmed:true,client_request_id:requestId}});
  requestId=crypto.randomUUID();newCode=created.share_code||'';newWatchPath=created.watch_path||('/watch/'+ident(created.id));
  $('share-code').value=newCode;$('new-share').hidden=!newCode;$('scope-confirm').checked=false;
  await refresh();notice(newCode?'观看邀请已创建，固定到你确认的这一版。':'此前请求已经创建了这条分享，已复用记录。一次性代码不再显示；若未保存，请关闭该邀请后重新创建。');
});});
$('copy-share').addEventListener('click',()=>action('正在复制本机观看链接…',async()=>{
  if(!newCode)throw new Error('本次代码已收起，请查看已创建的分享。');
  await navigator.clipboard.writeText(location.origin+newWatchPath+'#code='+encodeURIComponent(newCode));notice('已复制本机观看链接，仅在这台电脑访问。');
}));
$('open-viewer').addEventListener('click',()=>{if(newCode)window.open(newWatchPath+'#code='+encodeURIComponent(newCode),'_blank','noopener');});
$('hide-code').addEventListener('click',()=>{newCode='';newWatchPath='';$('share-code').value='';$('new-share').hidden=true;});
$('refresh').addEventListener('click',()=>action('正在核对分享状态…',async()=>{await refresh();notice('分享状态已刷新。');}));
async function boot(){
  if(!episodeId||!/^[a-f0-9]{32}$/.test(episodeId))throw new Error('请从本期工作台进入分享页面。');
  try{const auth=await api('/auth/me');csrf=auth.csrf_token;await refresh();}
  catch(error){if(error.status===401)$('login-needed').hidden=false;throw error;}
}
boot().catch(error=>notice(error.message,true));
