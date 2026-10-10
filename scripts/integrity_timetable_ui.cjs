// Owned loopback HTTP and real scoring/persistence; no real upstream or notifications.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const http = require('node:http'), net = require('node:net'), os = require('node:os');
const {spawn} = require('node:child_process');
const execFile = require('node:util').promisify(require('node:child_process').execFile);
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..'), data = fs.mkdtempSync(path.join(os.tmpdir(), 'integrity-ui-timetable-'));
const python = process.env.PYTHON_EXECUTABLE || 'python3';
let app, appExit, browser, logs = '', calls = 0, active = 0, peak = 0, checks = 0;
const check = (value, message) => {assert.ok(value, message); checks++;};
const canary = new Map(JSON.parse(fs.readFileSync(path.join(root, 'features/integrity/assets/canary/probes.json'))).map(p => [p.prompt, String(p.expected)]));
const upstream = http.createServer(async (req, res) => {
  active++; peak = Math.max(peak, active);
  try {
    const pieces = []; for await (const piece of req) pieces.push(piece);
    const body = JSON.parse(Buffer.concat(pieces)); calls++;
    assert.equal(req.url, '/v1/responses'); assert.equal(body.stream, false); assert.equal(body.store, false);
    let answer = 'OK';
    if (canary.has(body.input)) answer = canary.get(body.input);
    else if (body.max_output_tokens === 2048) answer = Array(331).fill('42').join(' ');
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({status: 'completed', model: body.model, output: [{type: 'message', content: [{type: 'output_text', text: answer}]}], usage: {input_tokens: 10, output_tokens: 1}}));
  } finally {active--;}
});
async function port() {const s = net.createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r)); const n = s.address().port; await new Promise(r => s.close(r)); return n;}
async function until(fn, message, timeout = 30000) {const end = performance.now() + timeout; while (performance.now() < end) {if (await fn()) return; await new Promise(r => setTimeout(r, 100));} assert.fail(message);}
(async () => {
  await new Promise(r => upstream.listen(0, '127.0.0.1', r)); const base = `http://127.0.0.1:${await port()}`;
  const env = {...process.env, PLATFORM_DATA_DIR: data, PLATFORM_EGRESS_ALLOWLIST: '127.0.0.1', PLATFORM_USERNAME: '', PLATFORM_PASSWORD: '', PYTHONDONTWRITEBYTECODE: '1', EVAL_INTEGRITY_EXECUTOR: 'off', EVAL_MONITOR_EXECUTOR: 'off'};
  app = spawn(python, ['-B', 'run.py', '--port', new URL(base).port], {cwd: root, env, stdio: ['ignore', 'pipe', 'pipe']});
  appExit = new Promise(r => {app.once('exit', r); app.once('error', r);}); app.stderr.on('data', c => logs += c.toString());
  await until(async () => {try {return (await fetch(base + '/api/health')).ok;} catch {return false;}}, 'owned server starts');
  async function request(url, body, method = 'POST') {
    const res = await fetch(base + url, body === undefined ? {} : {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    check(res.ok, `API ${url}: ${res.status}`); return res.json();
  }
  const channel = await request('/api/registry/channels', {name: 'Synthetic timetable browser', base_url: `http://127.0.0.1:${upstream.address().port}/v1`, api_key: 'synthetic-timetable-browser-credential', multiplier: 2.5, status: 'online'});
  browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || undefined});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}}), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto(base + '/stability/'); await page.locator('[data-view="schedules"]').click(); await page.locator('#new-schedule').click();
  await page.locator('#schedule-dialog').waitFor(); check(await page.locator('#schedule-pack').inputValue() === 'layered-integrity-v2', 'new plans default v2');
  check(await page.locator('#timetable-grid input:checked').count() === 168, 'default 24 Canary and 144 MT points');
  await page.locator('#schedule-name').fill('Synthetic irregular timetable');
  await page.getByRole('checkbox', {name: '选择公共渠道 Synthetic timetable browser', exact: true}).check(); await page.locator('#schedule-enabled').uncheck();
  await until(async () => (await page.locator('#timetable-preview').innerText()).includes('5184'), 'default 5184 requests per channel');
  const clear = async method => page.getByRole('group', {name: method + ' 时刻', exact: true}).getByRole('button', {name: '清空', exact: true}).click();
  await clear('Canary'); await clear('MT');
  const wanted = {Canary: ['09:20', '11:00', '16:40'], MT: ['09:10', '09:50', '14:30']};
  for (const [method, times] of Object.entries(wanted)) for (const t of times) await page.getByRole('checkbox', {name: method + ' ' + t, exact: true}).check();
  // Keyboard toggling is independent for both methods.
  const key = page.getByRole('checkbox', {name: 'Canary 09:20', exact: true}); await key.focus(); await key.press('Space'); check(!(await key.isChecked()), 'keyboard clears Canary point'); await key.press('Space');
  await until(async () => (await page.locator('#timetable-preview').innerText()).includes('591'), 'irregular request graph preview');
  async function save() {const pending = page.waitForResponse(r => r.url() === base + '/stability/api/schedules' && r.request().method() === 'POST'); await page.locator('#schedule-form button[type=submit]').click(); const res = await pending; check(res.ok(), `schedule saves ${res.status()}`); await page.locator('#schedule-dialog').waitFor({state: 'hidden'}); return (await res.json()).id;}
  const id = await save();
  async function plan() {return (await request('/stability/api/schedules')).schedules.find(p => p.id === id);}
  async function edit() {await page.locator('[data-view="schedules"]').click(); await page.locator('#schedule-list article').filter({has: page.getByRole('heading', {name: 'Synthetic irregular timetable', exact: true})}).getByRole('button', {name: '编辑', exact: true}).click(); await page.locator('#schedule-dialog').waitFor();}
  let saved = await plan(); check(JSON.stringify(saved.layered_config.canary_times) === JSON.stringify(wanted.Canary), 'irregular Canary set persisted'); check(JSON.stringify(saved.layered_config.modeltrace_times) === JSON.stringify(wanted.MT), 'irregular MT set persisted');
  await edit(); check(await page.locator('#timetable-grid input:checked').count() === 6, 'reopen retains exactly six independent selections'); await clear('Canary'); await save();
  saved = await plan(); check(saved.layered_config.canary_times.length === 0 && JSON.stringify(saved.layered_config.modeltrace_times) === JSON.stringify(wanted.MT), 'Canary cancellation preserves MT');
  await edit(); await clear('MT'); await save(); saved = await plan(); check(saved.layered_config.canary_times.length === 0 && saved.layered_config.modeltrace_times.length === 0, 'both empty saved without defaults');
  await edit(); check(await page.locator('#timetable-grid input:checked').count() === 0, 'both empty reopen unchanged');
  for (const [method, times] of Object.entries(wanted)) for (const t of times) await page.getByRole('checkbox', {name: method + ' ' + t, exact: true}).check(); await save();
  check(calls === 0, 'editing and preview send no upstream requests');
  const driven = JSON.parse((await execFile(python, ['-B', 'scripts/integrity_browser_fixture.py', 'timetable', String(id)], {cwd: root, env, timeout: 120000, encoding: 'utf8'})).stdout);
  check(driven.attempted === 591 && driven.status === 'completed', 'three independent 192 Canary batches and three MT batches complete');
  check(calls === 591 && peak === 1, 'exact graph requests and HTTP in-flight one');
  const current = (await request('/api/registry/channels')).channels.find(c => c.id === channel.id);
  const fields = Object.fromEntries(['name', 'base_url', 'scope', 'multiplier', 'note', 'enabled', 'version', 'protocol_profile', 'status'].map(k => [k, current[k]]));
  await request(`/api/registry/channels/${channel.id}`, {...fields, name: 'Renamed timetable browser', multiplier: 9, api_key: ''}, 'PUT');
  await page.reload();
  await until(async () => (await page.locator('#report-counts').innerText()).includes('共 6 行'), 'initial date report has six rows');
  const targetReportReady = () => page.evaluate(({date, runId}) => {
    const records = [...document.querySelectorAll('#timetable-report [data-record]')];
    const dates = [...document.querySelectorAll('#timetable-report .timetable-overview tbody th small:first-of-type')];
    return document.querySelector('#report-counts').textContent.includes('共 6 行') && records.length === 6
      && records.every(node => node.dataset.record.startsWith(`${runId}:`))
      && dates.length === 6 && dates.every(node => node.textContent === date);
  }, {date: driven.date, runId: driven.run_id});
  // Both dates have six rows: hold the target response to prove old DOM cannot
  // satisfy readiness, then wait for that response and its date/run rendering.
  let releaseDateQuery, dateQueryRequested = false;
  const dateQueryGate = new Promise(resolve => releaseDateQuery = resolve);
  const reportPattern = '**/stability/api/timetable/report?**';
  const holdDateQuery = async route => {
    if (new URL(route.request().url()).searchParams.get('date') === driven.date) {
      dateQueryRequested = true; await dateQueryGate;
    }
    await route.continue();
  };
  await page.route(reportPattern, holdDateQuery);
  const targetResponse = page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.pathname === '/stability/api/timetable/report' && url.searchParams.get('date') === driven.date;
  });
  try {
    await page.locator('#report-date').fill(driven.date); await page.locator('#report-date').dispatchEvent('change');
    await until(async () => dateQueryRequested, 'target date query reaches response gate');
    check((await page.locator('#report-counts').innerText()).includes('共 6 行') && !(await targetReportReady()), 'old six-row DOM cannot satisfy target date/run readiness');
    await page.screenshot({path: path.join(data, 'timetable-date-query-pending-1440.png'), fullPage: true});
  } finally {releaseDateQuery();}
  check((await targetResponse).ok(), 'target date report response succeeds');
  await until(targetReportReady, 'six target-date rows render the completed run');
  await page.unroute(reportPattern, holdDateQuery);
  const table = page.locator('#timetable-report table'); check((await table.innerText()).includes('2.5x') && !(await table.innerText()).includes('Renamed timetable browser'), 'report retains sampled name and multiplier');
  check(await table.evaluate(e => e.classList.contains('timetable-overview')), 'time overview is the default report');
  check((await table.innerText()).includes('192/192') && (await table.innerText()).includes('参照不足'), 'complete scores retain missing-baseline limit');
  check((await table.innerText()).includes('+08:00') && (await table.innerText()).includes('Asia/Shanghai'), 'planned and actual periods include timezone');
  check(await table.locator('details[open]').count() === 0, 'technical metrics collapsed');
  const recordIds = async () => page.locator('#timetable-report [data-record]').evaluateAll(nodes => nodes.map(e => e.dataset.record).sort());
  const realIds = await recordIds();
  await page.getByRole('button', {name: '明细表', exact: true}).click();
  check(JSON.stringify(await recordIds()) === JSON.stringify(realIds), 'detail and overview use every identical real record');
  check((await table.innerText()).includes('实际采样时段'), 'detail keeps actual sampling periods');
  await page.screenshot({path: path.join(data, 'timetable-detail-1440.png'), fullPage: true});
  await page.getByRole('button', {name: '时间总览', exact: true}).click();
  await page.locator('#report-channel').selectOption(String(channel.id)); await page.locator('#report-anomalies').check();
  await until(async () => (await page.locator('#report-counts').innerText()).includes('显示 6 行'), 'channel and anomaly filter preserve actual counts');
  const savedPlans = (await request('/stability/api/schedules')).schedules.length;
  let demoRequests = 0; const countDemoRequests = req => {if (new URL(req.url()).pathname.includes('/api/')) demoRequests++;};
  page.on('request', countDemoRequests);
  await page.getByRole('button', {name: '查看演示', exact: true}).click();
  await until(async () => (await page.locator('#report-counts').innerText()).includes('共 18 行'), 'fixed demo has three channels and six times');
  check(await page.locator('#report-demo-note').isVisible() && (await page.locator('#report-demo-note').innerText()).includes('合成演示数据，不代表真实检测'), 'demo is conspicuously synthetic');
  check(await table.locator('thead th').count() === 4 && await table.locator('tbody tr').count() === 6, 'demo matrix has one column per channel and row per time');
  const demoText = await table.innerText();
  for (const label of ['正常观测', '疑似能力下降', '指纹差异，需复核', '未完成 / 未测', '参照不足', '待采样', '未安排', 'Canary: 2x / MT: 2.5x']) check(demoText.includes(label), `demo covers ${label}`);
  check(await page.locator('#timetable-report button, #timetable-report a').count() === 0, 'demo has no baseline mutations or real run downloads');
  const demoIds = await recordIds();
  await page.getByRole('button', {name: '明细表', exact: true}).click(); check(JSON.stringify(await recordIds()) === JSON.stringify(demoIds), 'both demo views use identical records');
  await page.screenshot({path: path.join(data, 'timetable-demo-detail-1440.png'), fullPage: true});
  await page.getByRole('button', {name: '时间总览', exact: true}).click();
  check(!(await page.locator('#run-list').isVisible()), 'demo hides real run detail and download controls');
  for (const width of [390, 1440]) {
    await page.setViewportSize({width, height: 1000}); check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)), `demo fits ${width}`);
    check(await page.locator('#timetable-report .timetable-cell').evaluateAll(nodes => nodes.every(e => e.scrollWidth <= e.clientWidth + 2)), `each demo cell fits ${width} without crossing channels`);
    await page.screenshot({path: path.join(data, `timetable-demo-overview-${width}.png`), fullPage: true});
  }
  await page.locator('#report-channel').selectOption('902'); await page.locator('#report-anomalies').check();
  await until(async () => (await page.locator('#report-counts').innerText()).includes('共 6 行，显示 3 行'), 'demo channel/anomaly filters use real report semantics');
  const anomalyIds = await recordIds(); await page.getByRole('button', {name: '明细表', exact: true}).click(); check(JSON.stringify(await recordIds()) === JSON.stringify(anomalyIds), 'filtered demo records agree between views');
  await page.locator('#report-date').fill('2026-10-07'); await page.locator('#report-date').dispatchEvent('change');
  await until(async () => (await page.locator('#report-counts').innerText()).includes('共 0 行'), 'demo date filter does not borrow other dates');
  check(await page.evaluate(() => {
    const record = window.timetableDemoReport().rows[0], other = {...record, run_id: 'second-run'}, utc = {...record, run_id: 'utc-run', timezone: 'UTC', scheduled_at: new Date(record.scheduled_at_utc * 1000).toISOString()};
    const root = document.createElement('div'); renderTimetableReport(root, {rows: [record, other, utc], empty_plans: [], synthetic: true}, null, 'overview');
    return root.querySelectorAll('tbody tr').length === 2 && root.querySelectorAll('[data-record]').length === 3;
  }), 'same cell retains multiple runs and same UTC time in distinct zones stays distinct');
  check(demoRequests === 0, 'entering and interacting with demo make zero API requests'); page.off('request', countDemoRequests);
  await page.route('**/stability/api/timetable/report?**', route => route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({detail: 'synthetic report unavailable'})}), {times: 1});
  await page.getByRole('button', {name: '返回真实报告', exact: true}).click();
  await until(async () => (await page.locator('#report-counts').innerText()).includes('加载失败'), 'exit query failure is visible');
  check(await page.locator('#timetable-report [data-record]').count() === 0 && !(await page.locator('#timetable-report').innerText()).includes('演示渠道'), 'failed exit cannot relabel synthetic data as real');
  await page.getByRole('button', {name: '时间总览', exact: true}).click(); check(await page.locator('#timetable-report [data-record]').count() === 0, 'view switch after failed exit cannot restore stale synthetic data');
  await page.locator('#refresh-runs').click();
  await until(async () => !(await page.locator('#report-demo-note').isVisible()) && (await page.locator('#report-counts').innerText()).startsWith('共 6 行'), 'exit demo restores real date and filters');
  check(await page.locator('#report-channel').inputValue() === String(channel.id) && await page.locator('#report-anomalies').isChecked(), 'exit restores real filter selection');
  check((await request('/stability/api/schedules')).schedules.length === savedPlans && calls === 591, 'demo creates no plans and sends no upstream requests');
  await page.getByRole('button', {name: '时间总览', exact: true}).click();
  await page.screenshot({path: path.join(data, 'timetable-overview-1440.png'), fullPage: true});
  await page.goto(base + `/stability/?run=${driven.run_id}`); await page.locator('#run-dialog').waitFor();
  const download = page.waitForEvent('download'); await page.getByRole('link', {name: '导出 JSON', exact: true}).click(); await (await download).saveAs(path.join(data, 'synthetic-timetable-export.json'));
  const exported = JSON.parse(fs.readFileSync(path.join(data, 'synthetic-timetable-export.json')));
  check(exported.slots.filter(s => s.method === 'canary').every(s => s.summary.attempted === 192), 'export retains every complete denominator');
  check(!JSON.stringify(exported).includes('synthetic-timetable-browser-credential'), 'export excludes credential');
  for (const width of [390, 900, 1440]) {await page.setViewportSize({width, height: 1000}); check(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)), `page fits ${width}`); await page.screenshot({path: path.join(data, `timetable-report-${width}.png`), fullPage: true});}
  await page.locator('[data-close="run-dialog"]').click(); await edit();
  await page.setViewportSize({width: 390, height: 1000}); check(!(await page.locator('#schedule-dialog').evaluate(e => e.scrollWidth > e.clientWidth + 2)), 'grid scroll remains local in narrow dialog');
  await page.screenshot({path: path.join(data, 'timetable-grid-390.png'), fullPage: true}); check(errors.length === 0, 'no browser errors');
  console.log(JSON.stringify({status: 'passed', mockRequests: calls, checks, artifacts: data, canaryAttempts: 576, realUpstream: false, peakInflight: peak}));
})().catch(e => {console.error(e); console.error(logs); process.exitCode = 1;}).finally(async () => {if (browser) await browser.close(); if (app && app.exitCode === null && app.signalCode === null) app.kill('SIGTERM'); if (appExit) await appExit; upstream.closeAllConnections(); await new Promise(r => upstream.close(r));});
