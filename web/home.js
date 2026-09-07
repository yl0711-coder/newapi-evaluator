(async () => {
  const data = await Workbench.ready;
  const {channels} = await Workbench.api('/api/registry/channels');
  document.querySelector('#channel-count').textContent = `${channels.length} 条连接记录 · ${channels.filter(x => x.status === 'online').length} 条已标记上线`;
  const descriptions = {admission:'临时填写候选端，选择库中的参照端。5 题双端流式对比，查看首包、首答和速度。', stability:'按指定时刻检查选中的模型端点，保存每轮结果，按需发送飞书通知。', reasoning:'单渠道 5 题非流式测试，查看最终答案、可见推理、截断信号和原始响应。'};
  for (const [index, feature] of data.features.entries()) {
    const card = Workbench.node('a', '', 'panel'); card.href = feature.url;
    card.append(Workbench.node('p', `0${index + 1}`, 'eyebrow'), Workbench.node('h2', feature.name), Workbench.node('p', descriptions[feature.id], 'muted'), Workbench.node('span', '打开测试工具'));
    document.querySelector('#features').append(card);
  }
  if (!data.features.length) document.querySelector('#status').textContent = '当前仅启动公共渠道管理。';
})().catch(error => { document.querySelector('#status').textContent = error.message; });
