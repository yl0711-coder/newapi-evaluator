window.Workbench = {
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
