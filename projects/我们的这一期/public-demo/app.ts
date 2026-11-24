function el<T extends HTMLElement>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) throw new Error(`Missing interface element: ${selector}`);
  return node;
}
const state = { step: 0, theme: '朋友猜词，越认真越好笑。', direction: 'story', color: 'coral' };
const caption = el<HTMLInputElement>('#caption');
const accent = el<HTMLInputElement>('#accent');
const narration = el<HTMLInputElement>('#narration');
const sound = el<HTMLInputElement>('#sound');
const border = el<HTMLInputElement>('#border');
const next = el<HTMLButtonElement>('#next');
const previous = el<HTMLButtonElement>('#previous');
const directionLabel = () => state.direction === 'story' ? '保留完整故事' : '保留热闹互动';
const colorLabel = () => ({ coral: '珊瑚橙', teal: '蓝绿', yellow: '明黄' })[state.color] ?? '珊瑚橙';
function rows(): [string, string][] {
  return [['主题', state.theme], ['方向', directionLabel()], ['字幕', caption.value || '不显示'], ['花字', accent.value || '不显示'], ['配色', colorLabel()], ['音效', sound.checked ? '高光节点音效（未合成）' : '不添加'], ['旁白', narration.value || '不添加'], ['边框', border.checked ? '添加节目边框' : '保持画面干净']];
}
function update() {
  el('#flower').textContent = accent.value;
  el('#flower').hidden = !accent.value.trim();
  el('#flower').className = `flower ${state.color}`;
  el('#subtitle').textContent = caption.value ? `“${caption.value}”` : '';
  el('#subtitle').hidden = !caption.value.trim();
  el('.preview').classList.toggle('framed', border.checked);
  el('#brief').textContent = `本次主题：${state.theme}`;
  el('#summary').replaceChildren(...rows().flatMap(([label, value]) => {
    const dt = document.createElement('dt'); dt.textContent = label;
    const dd = document.createElement('dd'); dd.textContent = value;
    return [dt, dd];
  }));
}
function showStep(step: number) {
  state.step = Math.max(0, Math.min(3, step));
  document.querySelectorAll<HTMLElement>('[data-panel]').forEach(panel => panel.hidden = Number(panel.dataset.panel) !== state.step);
  document.querySelectorAll<HTMLButtonElement>('[data-step]').forEach(button => {
    if (Number(button.dataset.step) === state.step) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
  previous.disabled = state.step === 0;
  next.textContent = ['采用方向，继续 →', '文字选好了，继续 →', '回看方案 →', '再体验一次 ↺'][state.step];
  update();
}
el<HTMLFormElement>('#idea-form').addEventListener('submit', event => {
  event.preventDefault();
  state.theme = el<HTMLInputElement>('#idea').value.trim() || '朋友猜词，越认真越好笑。';
  showStep(0); location.hash = 'experience';
  el<HTMLButtonElement>('[data-step="0"]').focus({ preventScroll: true });
});
document.querySelectorAll<HTMLButtonElement>('[data-step]').forEach(button => button.addEventListener('click', () => showStep(Number(button.dataset.step))));
document.querySelectorAll<HTMLButtonElement>('[data-direction]').forEach(button => button.addEventListener('click', () => {
  state.direction = button.dataset.direction ?? 'story';
  document.querySelectorAll<HTMLButtonElement>('[data-direction]').forEach(option => {
    const chosen = option === button; option.classList.toggle('selected', chosen); option.setAttribute('aria-pressed', String(chosen));
  }); update();
}));
document.querySelectorAll<HTMLButtonElement>('[data-color]').forEach(button => button.addEventListener('click', () => {
  state.color = button.dataset.color ?? 'coral';
  document.querySelectorAll<HTMLButtonElement>('[data-color]').forEach(option => {
    const chosen = option === button; option.classList.toggle('selected', chosen); option.setAttribute('aria-pressed', String(chosen));
  }); update();
}));
[caption, accent, narration, sound, border].forEach(input => input.addEventListener('input', update));
next.addEventListener('click', () => showStep(state.step === 3 ? 0 : state.step + 1));
previous.addEventListener('click', () => showStep(state.step - 1));
el('#download').addEventListener('click', () => {
  const text = '在场 · 交互演示方案\n此文件仅记录本次示例选择，非视频或模型生成结果。\n\n' + rows().map(([k, v]) => `${k}：${v}`).join('\n');
  const url = URL.createObjectURL(new Blob(['\ufeff' + text], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a'); link.href = url; link.download = '在场-演示方案.txt'; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
const director = el('#director');
const toggle = el<HTMLButtonElement>('#director-toggle');
function closeDirector() { director.hidden = true; toggle.setAttribute('aria-expanded', 'false'); toggle.focus(); }
toggle.addEventListener('click', () => {
  director.hidden = !director.hidden; toggle.setAttribute('aria-expanded', String(!director.hidden));
  if (!director.hidden) el<HTMLButtonElement>('#director-close').focus();
});
el('#director-close').addEventListener('click', closeDirector);
document.addEventListener('keydown', event => { if (event.key === 'Escape' && !director.hidden) closeDirector(); });
const help: Record<string, string> = {
  brief: '先说你想留下什么：完整的故事，还是热闹的互动。完整版会记录偏好，并结合素材提出方向。你不必先学会剪辑术语。',
  edit: '在完整版里，可以告诉导演“太拖了”或“旁白短一点”。导演提供可执行的建议，确认后再另存新版本；当前页面只演示交互。',
  real: '当前链接是公开演示页，尚未提供在线上传和 AI 出片。GitHub 已公开完整项目与本机运行说明，可由开发者部署后使用。',
};
document.querySelectorAll<HTMLButtonElement>('[data-help]').forEach(button => button.addEventListener('click', () => el('#director-message').textContent = help[button.dataset.help ?? 'brief']));
const opening = el<HTMLDialogElement>('#opening');
const skip = el<HTMLButtonElement>('#skip-opening');
let openingTimer = 0;
skip.addEventListener('click', () => { clearTimeout(openingTimer); opening.close(); });
opening.addEventListener('cancel', () => clearTimeout(openingTimer));
if (!matchMedia('(prefers-reduced-motion: reduce)').matches && !location.hash) {
  opening.showModal(); skip.focus(); openingTimer = window.setTimeout(() => opening.close(), 2200);
}
update();
