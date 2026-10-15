window.Workbench = {
  requestId() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  },
  async api(path, options = {}) {
    const response = await fetch(path, {...options, headers: {'Content-Type':'application/json', ...options.headers}});
    if (!response.ok) {
      let message = `请求失败 (${response.status})`;
      try { const body = await response.json(); message = Array.isArray(body.detail) ? body.detail.map(x => x.msg).join('；') : body.detail || message; } catch {}
      throw new Error(message);
    }
    return response.json();
  },
  node(tag, value, className = '') {
    const element = document.createElement(tag);
    element.textContent = value ?? '';
    element.className = className;
    return element;
  },
  label(channel) {
    return `${channel.name} · ${channel.scope || '通用'} · ${channel.multiplier}x · #${channel.id}${channel.status === 'online' ? ' · 已上线' : ' · 已记录'}`;
  },
  async picker(select, selected, options = {}) {
    const {channels} = await this.api('/api/registry/channels');
    const available = channels.filter(channel => channel.enabled);
    select.replaceChildren(new Option('请选择渠道', ''));
    for (const channel of available) select.append(new Option(this.label(channel), channel.id));
    if (selected) select.value = String(selected);
    return available;
  },
  async modelInput(input) {
    const protocol = document.getElementById(input.dataset.modelProtocol);
    const list = document.createElement('datalist'); list.id = input.id + '-models';
    input.setAttribute('list', list.id);
    const controls = this.node('div', '', 'actions model-options');
    const manage = this.node('a', '管理共用模型'); manage.href = '/channels/#shared-models'; manage.target = '_blank'; manage.rel = 'noopener noreferrer';
    const refresh = this.node('button', '刷新模型选项', 'secondary'); refresh.type = 'button';
    const status = this.node('span', '', 'hint'); status.setAttribute('role', 'status');
    controls.append(manage, refresh, status); (input.closest('label') || input).after(list, controls);
    let models = [];
    async function load() {
      refresh.disabled = true;
      try {
        const data = await Workbench.api('/api/model-coverage/models');
        const supported = new Set(protocol ? [...protocol.options].map(option => option.value) : ['openai']);
        models = data.models.filter(model => supported.has(model.protocol));
        list.replaceChildren(...models.map(model => new Option(`${model.family} · ${model.label}`, model.model)));
        status.textContent = `共用模型 ${models.length} 个，也可手动填写。`;
      } catch (error) { status.textContent = '模型选项刷新失败：' + error.message; }
      finally { refresh.disabled = false; }
    }
    refresh.addEventListener('click', load);
    input.addEventListener('change', () => {
      const model = models.find(model => model.model === input.value.trim());
      if (model && protocol) { protocol.value = model.protocol; protocol.dispatchEvent(new Event('change', {bubbles:true})); }
    });
    await load();
  },
  download(value, name, type = 'application/json') {
    const data = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    const url = URL.createObjectURL(new Blob([data], {type}));
    const link = document.createElement('a'); link.href = url; link.download = name;
    document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};
Workbench.ready = (async () => {
  const data = await Workbench.api('/api/platform');
  const nav = document.createElement('nav'); nav.className = 'platform-nav'; nav.setAttribute('aria-label', '工作台导航');
  for (const [name, url] of [['模型测试工作台', '/'], ['公共渠道', '/channels/'], ...data.features.map(f => [f.name, f.url])]) {
    const link = Workbench.node('a', name); link.href = url;
    if (location.pathname === url) link.setAttribute('aria-current', 'page');
    nav.append(link);
  }
  document.body.prepend(nav);
  return data;
})();
Workbench.ready.catch(() => {});
for (const input of document.querySelectorAll('[data-model-catalog]')) Workbench.modelInput(input);
if (location.pathname.startsWith('/capacity/')) {
  const model = document.getElementById('model');
  if (model) Workbench.modelInput(model);
}
