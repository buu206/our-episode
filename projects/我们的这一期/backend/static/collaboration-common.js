'use strict';
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
const ident=value=>encodeURIComponent(String(value));
let csrf='', busy=false;
const labels={draft:'待本人预览确认',submitted:'本人已确认',withdrawn:'已撤回使用授权',confirmed:'本人已确认',proposed:'待本人核对',pending:'待处理',ready:'已就绪',adopted:'已采用',revoked:'使用受限',rejected:'本人未接受',open:'待提出者解决',resolved:'提出者已确认解决',queued:'排队中',running:'处理中',succeeded:'已完成',failed:'失败',closed:'已关闭',expired:'已到期'};
const stateName=value=>labels[value]||value;
function notice(message,error=false){$('message').textContent=message;$('message').className='status'+(error?' error':'');}
function safeURL(value){if(!value)return '';try{const u=new URL(value,location.href);return u.origin===location.origin&&['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}}
async function api(path,options={}){const method=options.method||'GET',headers=new Headers(options.headers||{});let body=options.body;if(body&&!(body instanceof FormData)){headers.set('Content-Type','application/json');body=JSON.stringify(body);}if(!['GET','HEAD'].includes(method)&&csrf)headers.set('X-CSRF-Token',csrf);let response;try{response=await fetch('/api/v1'+path,{...options,method,headers,body,credentials:'same-origin',cache:'no-store'});}catch{throw new Error('无法连接本机服务，请检查服务是否正在运行。');}let data;try{data=await response.json();}catch{throw new Error('服务返回格式不正确，请刷新后重试。');}if(!response.ok){const error=new Error(data?.error?.message||'操作未完成，请重试。');error.code=data?.error?.code;error.status=response.status;throw error;}return data;}
async function action(message,fn){if(busy)return;busy=true;notice(message);try{await fn();}catch(error){notice(error.message,true);}finally{busy=false;}}
function button(text,handler,cls=''){const b=el('button',text,cls);b.type='button';b.addEventListener('click',()=>action('正在处理…',handler));return b;}
function field(labelText,node){const box=el('div',undefined,'field');const label=el('label',labelText);node.id=node.id||'field-'+crypto.randomUUID();label.htmlFor=node.id;box.append(label,node);return box;}
function input(value='',type='text',max){const n=el('input');n.type=type;n.value=value;if(max)n.maxLength=max;return n;}
function paragraph(text,cls='muted'){return el('p',text,cls);}
function mediaPlayer(url){const p=el('video');p.controls=true;p.preload='metadata';p.src=safeURL(url);p.addEventListener('error',()=>notice('这份媒体暂不可播放，可能仍在处理或使用权限已变化。请刷新状态查看原因。',true));return p;}
function empty(container,message){if(!container.children.length)container.append(el('div',message,'empty'));}
function confirmAction(title,text,yes='确认'){return new Promise(resolve=>{const dialog=$('confirm-dialog');$('confirm-title').textContent=title;$('confirm-text').textContent=text;$('confirm-yes').textContent=yes;let accepted=false;const onClose=()=>{dialog.removeEventListener('close',onClose);$('confirm-yes').onclick=null;$('confirm-no').onclick=null;resolve(accepted);};dialog.addEventListener('close',onClose);$('confirm-yes').onclick=()=>{accepted=true;dialog.close();};$('confirm-no').onclick=()=>dialog.close();dialog.showModal();});}
function details(title){const node=el('details');node.append(el('summary',title));return node;}
function listFields(node,entries){const dl=el('dl');for(const [key,value] of entries){dl.append(el('dt',key),el('dd',String(value??'')));}node.append(dl);}
