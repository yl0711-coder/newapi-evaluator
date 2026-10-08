// Synthetic signed caller and owned loopback upstream; no production credentials or traffic.
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'monitor-control-ui-'));
const keyId = 'synthetic-control-caller';
const secret = 'synthetic-control-signing-secret-000000';
const upstreamKey = 'synthetic-control-upstream-credential';
const identity = 'synthetic-control-channel';
let app, appExit, browser, passedResult, calls = 0, checks = 0, stage = 'startup';
function phase(value) { stage = value; console.log(JSON.stringify({stage})); }
const upstream = http.createServer(async (req, res) => {
  assert.equal(req.url, '/v1/chat/completions');
  for await (const chunk of req) { /* consume without retaining the synthetic request body */ }
  calls++;
  res.writeHead(200, {'Content-Type':'text/event-stream'});
  res.end('data: ' + JSON.stringify({model:upstreamKey, choices:[{delta:{content:'OK'}, finish_reason:'stop'}],
    usage:{prompt_tokens:10, completion_tokens:0}}) + '\n\ndata: [DONE]\n\n');
});
const check = (condition, message) => { assert.ok(condition, message); checks++; };
async function port() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const value = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return value;
}
const stamp = seconds => new Date(seconds * 1000).toISOString();

(async () => {
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${await port()}`;
  const url = `http://127.0.0.1:${upstream.address().port}/v1`;
  app = spawn(process.env.PYTHON_EXECUTABLE || 'python3', ['-B', 'run.py', '--port', new URL(base).port], {
    cwd:root, env:{...process.env, PLATFORM_DATA_DIR:data, PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',
      PLATFORM_USERNAME:'', PLATFORM_PASSWORD:'', EVAL_MONITOR_KEY_ID:keyId, EVAL_MONITOR_SECRET:secret,
      EVAL_MONITOR_EXECUTOR:'live'}, stdio:['ignore','ignore','ignore']});
  appExit = new Promise(resolve => {
    app.once('exit', (code, signal) => resolve({code, signal}));
    app.once('error', () => resolve({code:null, signal:null, error:'spawn_failed'}));
  });
  let ready = false;
  for (let i=0; i<100; i++) {
    if (app.exitCode !== null) break;
    try { if ((await fetch(base + '/api/health')).ok) { ready=true; break; } } catch {}
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  check(ready, 'Owned control server starts');
  async function api(endpoint, body, method='POST') {
    const response = await fetch(base + endpoint, body === undefined ? {} : {
      method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    assert.ok(response.ok, `Local API status ${response.status}`);
    return response.json();
  }
  async function signed(method, endpoint, body, idempotency) {
    const raw = body === undefined ? '' : JSON.stringify(body);
    const ts = new Date().toISOString(), nonce = crypto.randomBytes(16).toString('hex');
    const canonical = [method, endpoint, ts, nonce, crypto.createHash('sha256').update(raw).digest('hex')].join('\n');
    const headers = {'Content-Type':'application/json', 'X-Nexus-Client':'monitor', 'X-Nexus-Key-Id':keyId,
      'X-Nexus-Timestamp':ts, 'X-Nexus-Nonce':nonce,
      'X-Nexus-Signature':'v1=' + crypto.createHmac('sha256', secret).update(canonical).digest('hex')};
    if (method !== 'GET') headers['Idempotency-Key'] = idempotency || body.idempotency_key;
    const response = await fetch(base + endpoint, {method, headers, ...(method==='GET' ? {} : {body:raw})});
    assert.ok(response.ok, `Signed local API status ${response.status}`);
    return response.json();
  }
  const channel = await api('/api/registry/channels', {name:'Synthetic control channel', base_url:url,
    api_key:upstreamKey, multiplier:1});
  browser = await chromium.launch({headless:true,
    ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror', error => errors.push(error.name));
  await page.goto(base + '/channels/');
  await page.locator('#list .record').first().waitFor();
  check(calls===0, 'Opening the control page sends no upstream request');
  await page.evaluate(() => {
    window.syntheticProductionRenders = 0;
    new MutationObserver(() => window.syntheticProductionRenders++).observe(document.getElementById('production-list'), {childList:true, subtree:true});
  });
  async function importSnapshot(value, expectedStatus=200) {
    const beforeRender = await page.evaluate(() => window.syntheticProductionRenders);
    let importResponded = false;
    const imported = page.waitForResponse(response => {
      if (response.url().endsWith('/api/model-coverage/production/import') && response.request().method()==='POST') {
        importResponded = true; return true;
      }
      return false;
    });
    const refreshed = expectedStatus===200 ? page.waitForResponse(response => importResponded && response.url().endsWith('/api/model-coverage') && response.request().method()==='GET') : null;
    page.once('dialog', dialog => dialog.accept(JSON.stringify(value)));
    await page.locator('#production-import').click();
    assert.equal((await imported).status(), expectedStatus, 'Production import response follows the snapshot contract');
    if (refreshed) {
      const response = await refreshed;
      assert.equal(response.status(), 200, 'Production view refresh succeeds after import');
      check((await response.json()).production_coverage.sources.find(source => source.source===value.source).version==='newer', 'Current production view remains on the latest version');
      await page.waitForFunction(count => window.syntheticProductionRenders>count, beforeRender);
    }
  }
  phase('production-snapshots');
  const snapshot = {source:'synthetic-ui', version:'newer', generated_at:Date.now()/1000-60, cursor:'synthetic-cursor',
    items:[{channel_identity:identity, model:'gpt-5.5', protocol:'openai', production_status:'online', eval_channel_id:channel.id}]};
  await importSnapshot(snapshot);
  await page.waitForFunction(() => document.getElementById('production-list').textContent.includes('版本 newer'));
  check((await page.locator('#production-status').innerText()).includes('已导入'), 'Production import has visible feedback');
  await importSnapshot({...snapshot, version:'older', generated_at:snapshot.generated_at-60,
    items:[{...snapshot.items[0], production_status:'offline'}]});
  await page.waitForFunction(() => document.getElementById('production-list').textContent.includes('版本 newer'));
  const coverage = await api('/api/model-coverage');
  check(coverage.production_coverage.sources[0].version==='newer', 'Late production snapshot does not replace current');
  await importSnapshot(snapshot);
  await page.waitForFunction(() => document.getElementById('production-status').textContent.includes('未重复写入'));
  check(true, 'Production replay is idempotent');
  await importSnapshot({...snapshot, items:[{...snapshot.items[0], production_status:'offline'}]}, 409);
  await page.waitForFunction(() => document.getElementById('production-status').textContent.includes('已拒绝覆盖'));
  check((await api('/api/model-coverage')).production_coverage.sources[0].version==='newer', 'Rejected conflict preserves current');
  phase('local-task-lifecycle');
  let prompts = ['production-coverage-reconcile', 'synthetic-control-local-task'];
  const promptHandler = dialog => dialog.accept(prompts.shift());
  const createdTask = page.waitForResponse(response => response.url().endsWith('/api/model-coverage/workflow/tasks') && response.request().method()==='POST');
  page.on('dialog', promptHandler);
  await page.locator('#workflow-create').click();
  const createdResponse = await createdTask;
  assert.equal(createdResponse.status(), 200, 'Local task creation succeeds');
  const createdBody = await createdResponse.json();
  await page.waitForFunction(() => document.getElementById('workflow-list').textContent.includes('queued'));
  page.off('dialog', promptHandler);
  const local = (await api('/api/model-coverage/workflow/tasks')).tasks[0];
  check(createdBody.id===local.id, 'Created task response identifies the visible task');
  prompts = ['production-coverage-reconcile', 'synthetic-control-local-task'];
  let replayResponded = false;
  const replayedTask = page.waitForResponse(response => {
    if (response.url().endsWith('/api/model-coverage/workflow/tasks') && response.request().method()==='POST') {
      replayResponded = true; return true;
    }
    return false;
  });
  const refreshedTasks = page.waitForResponse(response => replayResponded && response.url().endsWith('/api/model-coverage/workflow/tasks') && response.request().method()==='GET');
  page.on('dialog', promptHandler); await page.locator('#workflow-create').click();
  const replayResponse = await replayedTask;
  assert.equal(replayResponse.status(), 200, 'Local task replay succeeds');
  check((await replayResponse.json()).id===local.id, 'Local task replay returns the same identity');
  const refreshedResponse = await refreshedTasks;
  assert.equal(refreshedResponse.status(), 200, 'Task list refresh succeeds after replay');
  check((await refreshedResponse.json()).tasks.some(task => task.id===local.id), 'Refreshed list retains the replayed task');
  await page.waitForFunction(id => document.getElementById('workflow-list').textContent.includes(`#${id} ·`), local.id);
  page.off('dialog', promptHandler);
  check((await api('/api/model-coverage/workflow/tasks')).tasks.length===1, 'UI task replay preserves one task');
  await page.locator('#workflow-list').getByRole('button', {name:'取消',exact:true}).click();
  await page.waitForFunction(() => document.getElementById('workflow-list').textContent.includes('cancelled'));
  check((await api('/api/model-coverage/workflow/tasks')).tasks[0].id===local.id, 'Task cancellation preserves identity');
  phase('monitor-identity');
  await page.locator(`details.model-coverage[data-channel="${channel.id}"] > summary`).click();
  page.once('dialog', dialog => dialog.accept(identity));
  await page.getByRole('button', {name:'绑定 NewAPI 渠道身份',exact:true}).first().click();
  await page.getByRole('button', {name:`NewAPI 渠道身份：${identity}`,exact:true}).waitFor();
  check((await api('/api/model-coverage/monitor/identities')).identities[String(channel.id)]===identity, 'Visible identity binding persists');
  const now = Date.now()/1000;
  const job = (id, start) => ({schema_version:'1.0', idempotency_key:id, source_event_id:id, job_type:'incident', priority:'p1',
    channel_identity:identity, model:'gpt-5.5', protocol:'openai', probe_path:'direct', scenarios:['short_stream'], rounds:1,
    not_before:stamp(start), expires_at:stamp(now+600),
    budget:{max_requests:1,max_input_tokens:1000,max_output_tokens:5000}, reason:'synthetic control regression'});
  phase('signed-monitor-cancellation');
  const queuedBody = job('synthetic-control-queued', now+300);
  const queued = await signed('POST','/internal/v1/probe-jobs', queuedBody);
  const replay = await signed('POST','/internal/v1/probe-jobs', queuedBody);
  check(queued.job_id===replay.job_id, 'Signed caller retry with fresh nonce is idempotent');
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.getElementById('monitor-list').textContent.includes('排队中'));
  check(calls===0, 'Future Monitor job is visible without any upstream request');
  await signed('POST',`/internal/v1/probe-jobs/${queued.job_id}/cancel`, {schema_version:'1.0'}, 'synthetic-control-cancel');
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.getElementById('monitor-list').textContent.includes('已取消'));
  check(calls===0, 'Signed cancellation stops the queued job');
  phase('signed-monitor-execution');
  const immediate = await signed('POST','/internal/v1/probe-jobs', job('synthetic-control-immediate', now-1));
  let detail;
  for (let i=0; i<200; i++) {
    detail = await signed('GET',`/internal/v1/probe-jobs/${immediate.job_id}`);
    if (['completed','failed','rejected','partially_completed'].includes(detail.status)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  check(detail.status==='completed' && calls===1, 'Signed job traverses the owned HTTP upstream once');
  check(detail.progress.completed_requests===1 && detail.budget_consumed.requests===1, 'Result and attempt counts agree');
  check(detail.budget_consumed.output_tokens_reported===0, 'Explicit upstream zero survives bookkeeping');
  const results = await signed('GET','/internal/v1/probe-results?cursor=0&limit=1');
  check(results.items.length===1 && results.items[0].model_reported==='unrecognized', 'Echoed credential is excluded from published model');
  check(!JSON.stringify(results).includes(upstreamKey), 'Signed result envelope contains no upstream credential');
  const following = await signed('GET',`/internal/v1/probe-results?cursor=${encodeURIComponent(results.next_cursor)}&limit=1`);
  check(following.items.length===0, 'Result cursor prevents duplicate delivery');
  check(!fs.readFileSync(path.join(data,'channels.db')).includes(Buffer.from(upstreamKey)), 'Result database excludes plaintext upstream credential');
  check((await fetch(base+'/internal/v1/probe-results')).status===403, 'Unsigned callers cannot read results');
  phase('monitor-result-feedback');
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.getElementById('monitor-list').textContent.includes('已完成'));
  await page.route('**/api/model-coverage/monitor/jobs', route => route.fulfill({status:503,
    contentType:'application/json', body:JSON.stringify({detail:'Synthetic status unavailable'})}));
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.getElementById('monitor-status').textContent==='Synthetic status unavailable');
  check(true, 'Monitor refresh error is visible');
  await page.unroute('**/api/model-coverage/monitor/jobs');
  await page.locator('#monitor-refresh').click();
  await page.waitForFunction(() => document.getElementById('monitor-status').textContent==='已刷新');
  check(true, 'Monitor status recovers after a transient local API error');
  phase('responsive-layout');
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    check(await page.evaluate(() => document.documentElement.scrollWidth<=innerWidth+2), 'Control page fits viewport');
    await page.screenshot({path:path.join(data,`monitor-control-${width}.png`),fullPage:true});
  }
  check(errors.length===0, 'No browser script errors');
  passedResult = {status:'passed', checks, mockRequests:calls, real_upstream_tested:false, artifacts:data};
})().catch(error => {
  console.error(JSON.stringify({status:'failed',stage,checks,mockRequests:calls,error:error.name,message:String(error.message).replaceAll(upstreamKey,'[redacted]').replaceAll(secret,'[redacted]')}));
  process.exitCode=1;
}).finally(async () => {
  const cleanupErrors = [];
  try { if (browser) await browser.close(); } catch { cleanupErrors.push('browser_close_failed'); }
  if (appExit) {
    if (app.exitCode!==null || app.signalCode!==null) cleanupErrors.push('application_exited_before_shutdown');
    else app.kill('SIGTERM');
    let timer;
    const exit = await Promise.race([appExit, new Promise(resolve => { timer=setTimeout(() => resolve(null), 5000); })]);
    clearTimeout(timer);
    if (!exit) { app.kill('SIGKILL'); await appExit; cleanupErrors.push('application_shutdown_timeout'); }
    // Uvicorn re-raises the requested signal after its lifespan has closed.
    else if (exit.error || !(exit.code===0 && !exit.signal || exit.code===null && exit.signal==='SIGTERM')) {
      cleanupErrors.push('application_shutdown_failed');
    }
  }
  upstream.closeAllConnections();
  await new Promise(resolve => upstream.close(resolve));
  if (cleanupErrors.length) throw new Error(cleanupErrors.join(', '));
  if (!process.exitCode && passedResult) console.log(JSON.stringify(passedResult));
}).catch(error => {
  console.error(JSON.stringify({status:'failed',stage:'cleanup',checks,mockRequests:calls,error:error.name,message:String(error.message)}));
  process.exitCode=1;
});
