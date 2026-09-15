'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const samples = [];
  let controller = null, timer = null;
  const errors = {
    timeout_result_unknown: '等待超时，上游处理状态未确认。请先核对渠道用量，再决定是否重试。',
    connection_failed: '连接失败：请检查地址、网络及工作台出站白名单。',
    authentication_failed: '接口拒绝鉴权，请检查 Key 和渠道权限。',
    rate_limited: '接口限流，本次没有自动重试。',
    redirect_rejected: '接口返回重定向，已停止请求，请填写最终接口地址。',
    upstream_http_error: '接口返回 HTTP 错误，请结合状态码向渠道方核对。',
    upstream_error: '接口返回了错误对象。',
    invalid_json: '接口返回内容不是有效的 JSON。',
    invalid_response: '接口返回结构不符合生图协议。',
    missing_image: '接口未返回恰好 1 张 Base64 图片。',
    image_url_only: '仅返回了图片 URL；本次要求 Base64 图片，未自动访问该 URL。',
    invalid_image: '图片数据无效或不完整。',
    unexpected_image_format: '返回图片不是请求的 PNG 格式。',
    unsupported_png_features: '图片的位深、色彩参数或动画格式暂不支持，无法保真处理。',
    image_size_limit: '返回图片超过允许的尺寸范围。',
    response_too_large: '响应超过 48 MiB 上限，已停止接收。',
  };
  function refreshCount() {
    $('#sample-count').textContent = samples.length + ' / 6';
    $('#empty-state').hidden = samples.length > 0;
    $('#clear').disabled = Boolean(controller) || samples.length === 0;
    $('#generate').disabled = Boolean(controller) || samples.length >= 6;
  }
  function appendMetric(grid, label, value) {
    const item = Workbench.node('div', '');
    item.append(Workbench.node('small', label), Workbench.node('span', value ?? '未返回'));
    grid.append(item);
  }
  function renderSample(report) {
    const image = report.image;
    const metadata = {...report, scores: {prompt_adherence:null, composition:null, detail:null}};
    const evidence = Workbench.node('pre', '');
    if (image) {
      const {b64_json, ...imageMetadata} = image;
      metadata.image = imageMetadata;
    }
    const card = Workbench.node('article', '', 'sample-card');
    const sample = {metadata, url:null};
    samples.push(sample);
    if (image) {
      const bytes = Uint8Array.from(atob(image.b64_json), char => char.charCodeAt(0));
      sample.url = URL.createObjectURL(new Blob([bytes], {type:'image/png'}));
      const preview = document.createElement('img');
      preview.className = 'sample-image'; preview.src = sample.url;
      preview.alt = '生图质量样本 ' + samples.length;
      card.append(preview);
    } else {
      card.append(Workbench.node('div', '本次未得到有效图片', 'failure-art'));
    }
    const body = Workbench.node('div', '', 'sample-body');
    body.append(Workbench.node('h3', '样本 ' + String(samples.length).padStart(2, '0')));
    if (report.error_code) body.append(Workbench.node('p', errors[report.error_code] || '响应处理失败。', 'error'));
    const grid = Workbench.node('div', '', 'metric-grid');
    appendMetric(grid, '请求模型', report.settings.model);
    appendMetric(grid, '返回模型（自报）', report.returned_model);
    appendMetric(grid, '服务端总耗时', report.total_seconds == null ? null : report.total_seconds + ' 秒');
    appendMetric(grid, 'HTTP 状态', report.http_status);
    if (image) appendMetric(grid, '实际尺寸', image.width + ' × ' + image.height);
    appendMetric(grid, '质量参数', report.settings.quality);
    body.append(grid);
    body.append(Workbench.node('p', '连接指纹 ' + report.settings.endpoint_fingerprint.slice(0,12) +
      ' · 请求指纹 ' + report.request_fingerprint.slice(0,12), 'hint'));
    if (report.size_matches === false) body.append(Workbench.node('p', '实际尺寸与请求尺寸不同。', 'error'));
    if (image) {
      const ratings = Workbench.node('div', '', 'ratings');
      for (const [key, name] of [['prompt_adherence','提示词遵循'],['composition','构图'],['detail','细节']]) {
        const label = Workbench.node('label', name);
        const select = document.createElement('select');
        select.setAttribute('aria-label', '样本 ' + samples.length + ' ' + name);
        select.append(new Option('待评', ''));
        for (let score=1; score<=5; score++) select.append(new Option(score + ' 分', String(score)));
        select.addEventListener('change', () => {
          metadata.scores[key] = select.value ? Number(select.value) : null;
          evidence.textContent = JSON.stringify(metadata, null, 2);
        });
        label.append(select); ratings.append(label);
      }
      body.append(ratings, Workbench.node('p', '人工评分：1 分低，5 分高；不代表模型身份。', 'hint'));
    }
    const details = document.createElement('details');
    evidence.textContent = JSON.stringify(metadata, null, 2);
    details.append(Workbench.node('summary', '响应证据'), evidence);
    body.append(details);
    const actions = Workbench.node('div', '', 'actions');
    if (sample.url) {
      const download = Workbench.node('a', '下载 PNG', 'button secondary');
      download.href = sample.url; download.download = 'image-quality-' + report.sample_id + '.png';
      actions.append(download);
    }
    const exportButton = Workbench.node('button', '导出指标', 'secondary');
    exportButton.type = 'button';
    exportButton.addEventListener('click', () => Workbench.download(metadata, 'image-quality-' + report.sample_id + '.json'));
    actions.append(exportButton); body.append(actions); card.append(body); $('#samples').append(card);
    refreshCount();
  }
  $('#image-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (controller || samples.length >= 6 || !$('#image-form').reportValidity()) return;
    let payload = {
      base_url:$('#base-url').value.trim(), api_key:$('#api-key').value, prompt:$('#prompt').value,
      model:'gpt-image-2', size:$('#size').value, quality:$('#quality').value,
      timeout_seconds:Number($('#timeout').value), confirm_live:$('#confirm-live').checked,
    };
    controller = new AbortController();
    const signal = controller.signal, started = performance.now();
    $('#api-key').value = ''; $('#confirm-live').checked = false;
    $('#form-error').textContent = ''; $('#cancel').hidden = false;
    const update = () => {$('#run-status').textContent = '正在生成 · 已等待 ' + Math.floor((performance.now()-started)/1000) + ' 秒';};
    update(); timer = setInterval(update, 1000); refreshCount();
    try {
      const pending = fetch('./api/generate', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload), signal,
      });
      payload = null;
      const response = await pending;
      const report = await response.json();
      if (!response.ok) {
        const detail = Array.isArray(report.detail) ? report.detail.map(item=>item.msg).join('；') : report.detail;
        throw new Error(detail || '工作台请求失败（' + response.status + '）');
      }
      renderSample(report);
      $('#run-status').textContent = report.status === 'success' ? '本次生图完成。' : '本次未成功，请查看样本中的诊断信息。';
    } catch (error) {
      $('#run-status').textContent = '';
      $('#form-error').textContent = error.name === 'AbortError'
        ? '已停止本地等待；上游可能继续处理，请核对用量后再重试。'
        : '未完成：' + error.message;
    } finally {
      payload = null; clearInterval(timer); controller = null;
      $('#cancel').hidden = true; refreshCount();
    }
  });
  $('#cancel').addEventListener('click', () => controller?.abort());
  $('#clear').addEventListener('click', () => {
    for (const sample of samples) if (sample.url) URL.revokeObjectURL(sample.url);
    samples.length = 0; $('#samples').replaceChildren(); refreshCount();
  });
  window.addEventListener('pagehide', () => {
    controller?.abort();
    for (const sample of samples) if (sample.url) URL.revokeObjectURL(sample.url);
  });
})();
