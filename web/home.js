(async () => {
  const data = await Workbench.ready;
  const {channels} = await Workbench.api('/api/registry/channels');
  document.querySelector('#channel-count').textContent = `${channels.length} 条连接记录 · ${channels.filter(x => x.status === 'online').length} 条已标记上线`;
  const descriptions = {'image-quality':'手动填写连接与提示词，固定请求 gpt-image-2，并排查看画面、人工评分和脱敏响应证据。', diagnosis:'根据 Token、耗时、HTTP 状态与流式标记生成合成请求，做小样本单因素对照并导出排查报告。',admission:'临时填写候选端，选择库中的参照端。首轮预热、同题成对评测，查看能力证据、相对速度与异常归因。', stability:'按指定时刻检查选中的模型端点，保存每轮结果，按需发送飞书通知。', reasoning:'单渠道 5 题非流式测试，查看最终答案、可见推理、截断信号和原始响应。', capacity:'单号、号池、网关、长任务与故障注入五种模式；默认使用本地 Mock，支持三路实时负载推子。'};
  for (const [index, feature] of data.features.entries()) {
    const card = Workbench.node('a', '', 'panel'); card.href = feature.url;
    card.append(Workbench.node('p', `0${index + 1}`, 'eyebrow'), Workbench.node('h2', feature.name), Workbench.node('p', descriptions[feature.id], 'muted'), Workbench.node('span', '打开测试工具'));
    document.querySelector('#features').append(card);
  }
  if (!data.features.length) document.querySelector('#status').textContent = '当前仅启动公共渠道管理。';
})().catch(error => { document.querySelector('#status').textContent = error.message; });
