// 共用工具：请求、状态文案、DOM 小助手。各页面只写自己的逻辑。

let authState = null;
const authReady = fetch('/api/auth/me', { headers: { Accept: 'application/json' } })
  .then(async (response) => {
    if (response.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.replace(`/login.html?next=${next}`);
      throw new Error('请先登录');
    }
    if (!response.ok) throw new Error(`会话检查失败（HTTP ${response.status}）`);
    authState = await response.json();
    return authState;
  });

// SOURCE: primary-navigation-order
const NAVIGATION_ITEMS = [
  ['index.html', '首页'],
  ['workspace.html', '渠道配置'],
  ['compare.html', '双端准入'],
  ['insights.html', '生产洞察'],
  ['incidents.html', '异常归因'],
  ['launches.html', '上线与飞书'],
  ['scheduled.html', '定时测试'],
  ['history.html', '任务与报告'],
  ['admin.html', '运营设置'],
];

function initNavigation() {
  const nav = document.querySelector('.topbar nav');
  if (!nav) return;
  const linksByHref = new Map(
    [...nav.querySelectorAll('a[href]')].map((link) => [link.getAttribute('href'), link]),
  );
  const orderedLinks = NAVIGATION_ITEMS.map(([href, label]) => {
    const link = linksByHref.get(href) || document.createElement('a');
    link.href = href;
    link.textContent = label;
    return link;
  });
  nav.replaceChildren(...orderedLinks);
}

initNavigation();

function getTopbarActions() {
  const topbar = document.querySelector('.topbar .inner');
  if (!topbar) return null;
  let actions = topbar.querySelector('.topbar-actions');
  if (!actions) {
    actions = document.createElement('div');
    actions.className = 'topbar-actions';
    topbar.append(actions);
  }
  return actions;
}

async function api(path, opts = {}) {
  await authReady;
  const headers = {
    'Content-Type': 'application/json',
    'X-CSRF-Token': authState.csrf_token,
    ...(opts.headers || {}),
  };
  const res = await fetch(path, {
    ...opts,
    headers,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    throw new Error((data && data.detail) || `请求失败（HTTP ${res.status}）`);
  }
  return data;
}

async function initSessionControls() {
  const state = await authReady;
  const actions = getTopbarActions();
  if (!actions) return;
  const controls = document.createElement('div');
  controls.className = 'session-controls';
  controls.innerHTML = `<span>${esc(state.user.display_name || state.user.username)}</span>`
    + '<a href="account.html">修改密码</a>'
    + '<button type="button" class="session-logout">退出</button>';
  controls.querySelector('button').addEventListener('click', async () => {
    await post('/api/auth/logout');
    location.replace('/login.html');
  });
  actions.append(controls);
}

const get = (p) => api(p);
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });
const del = (p) => api(p, { method: 'DELETE' });

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const button = $('.theme-toggle');
  if (!button) return;
  const dark = theme === 'dark';
  button.textContent = dark ? '☀️ 切换浅色' : '🌙 切换深色';
  button.setAttribute('aria-label', button.textContent);
  button.setAttribute('aria-pressed', String(dark));
}

function initTheme() {
  let saved = 'dark';
  try { saved = localStorage.getItem('theme') || 'dark'; } catch {}
  setTheme(saved === 'light' ? 'light' : 'dark');
  const actions = getTopbarActions();
  if (!actions) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'theme-toggle';
  actions.append(button);
  setTheme(document.documentElement.dataset.theme);
  button.addEventListener('click', () => {
    const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    setTheme(theme);
    try { localStorage.setItem('theme', theme); } catch {}
  });
}

initTheme();

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

initSessionControls().catch((error) => {
  if (error.message !== '请先登录') showError(error.message);
});

// 任务状态 → 中文 + 徽标样式
const STATUS = {
  queued: ['排队中', 'run'], running: ['执行中', 'run'],
  success: ['成功', 'ok'], partial: ['部分失败', 'warn'],
  failed: ['失败', 'bad'], cancelled: ['已取消', ''],
  interrupted: ['已中断', 'warn'],
};

function statusTag(s) {
  const [text, cls] = STATUS[s] || [s, ''];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

const KIND_NAME = {
  admission: '接入检测', inspect: '定期巡检',
  degrade: '降智复核', capability: '能力评测', load: '压力测试',
  paired_admission: '双端准入', scheduled_measurement: '定时监测',
};

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function showError(msg, target = '#notice') {
  const el = $(target);
  if (!el) return alert(msg);
  el.className = 'notice err';
  el.textContent = msg;
  el.classList.remove('hide');
}

function clearError(target = '#notice') {
  const el = $(target);
  if (el) el.classList.add('hide');
}
