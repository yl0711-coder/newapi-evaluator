window.ProtocolProfileForm = {
  options: null,
  async mount(container, prefix) {
    this.options ||= Workbench.api('/api/registry/protocol-options');
    const options = await this.options;
    if (container.dataset.mounted) return;
    const selects = [
      ['upstream_type','上游来源',options.upstream_types],
      ['proposed_type','拟配置的 NewAPI 类型',options.channel_types],
      ['credential_mode','供应商提供的凭据方式',{api_key:'API Key 接口',codex_oauth:'Codex OAuth 凭据（需网关验证）'}],
      ['confirmation_source','说明来源',options.source_types],
    ];
    const grid = Workbench.node('div','','two');
    for (const [field,title,values] of selects) {
      const label = Workbench.node('label',title), select = document.createElement('select');
      select.id = `${prefix}-${field}`;
      for (const [value,text] of Object.entries(values)) select.append(new Option(text,value));
      label.append(select); grid.append(label);
    }
    for (const [field,title,type,max] of [['supplier','供应商名称','text',160],['confirmed_on','确认日期','date',10],['description','程序或协议说明','text',2000],['note','协议备注','text',2000]]) {
      const label=Workbench.node('label',title), input=document.createElement(field==='description'||field==='note'?'textarea':'input');
      input.id=`${prefix}-${field}`; if(input.tagName==='INPUT')input.type=type;
      input.maxLength=max; label.append(input);grid.append(label);
    }
    container.append(grid,Workbench.node('p','填写对外提供的接入协议。不要在说明中填写密钥、账号资料或带敏感参数的地址。','hint'));
    container.dataset.mounted='true';
  },
  set(prefix, value={}) {
    for(const field of ['upstream_type','proposed_type','credential_mode','confirmation_source','supplier','confirmed_on','description','note']) {
      const input=document.getElementById(`${prefix}-${field}`);
      input.value=value[field] || ({upstream_type:'unknown',proposed_type:'unknown',credential_mode:'api_key',confirmation_source:'unknown'}[field] || '');
    }
  },
  get(prefix) {
    const value={};
    for(const field of ['upstream_type','proposed_type','credential_mode','confirmation_source','supplier','confirmed_on','description','note']) value[field]=document.getElementById(`${prefix}-${field}`).value.trim();
    value.confirmed_on ||= null;
    return value;
  },
};
