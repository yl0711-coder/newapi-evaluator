const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const evidenceRoot = fs.realpathSync(process.env.UI_EVIDENCE_ROOT || os.tmpdir());
assert.ok(evidenceRoot !== root && !evidenceRoot.startsWith(root + path.sep), 'Browser evidence belongs outside source');
const data = fs.mkdtempSync(path.join(evidenceRoot, 'admission-queue-ui-'));
const python = process.env.PYTHON_EXECUTABLE || path.join(root, '.venv', 'bin', 'python');
const requests = [], active = new Map(), held = new Map(), errors = [];
const gated = new Set(['fifo-one', 'fifo-two', 'restart-head', 'stop-visible', 'stop-pending', 'variable-height']);
const candidateKeys = new Set();
const splitCredentialKeys = new Set(), splitHeld = new Map();
const splitCases = [
  {owner:'split-chat', protocol:'openai'}, {owner:'split-responses', protocol:'responses'}, {owner:'split-messages', protocol:'anthropic'}
];
const expectedMockRequests = 126 + splitCases.length * 12;
const expectedChecks = 20 + splitCases.length;
const browserBudgetMs = 240000;
const runStartedAt = performance.now();
const rawCases = [
  {owner:'raw-http', protocol:'openai'}, {owner:'raw-chat', protocol:'openai'},
  {owner:'raw-responses', protocol:'responses'}, {owner:'raw-messages', protocol:'anthropic'},
  {owner:'raw-empty', protocol:'openai'}, {owner:'raw-malformed', protocol:'openai'}
];
let app, appExit, appError, browser, base, upstreamUrl, port, maxActiveJobs = 0, fixtureError;
let stderrBytes = 0, phase = 'startup';
const checks = [];
const screenshotAudits = [];
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

async function until(predicate, message, timeout = 30000) {
  const deadline = performance.now() + timeout;
  while (performance.now() < deadline) {
    if (fixtureError) throw fixtureError;
    const value = await predicate();
    if (value) return value;
    await pause(Math.min(75, Math.max(0, deadline - performance.now())));
  }
  assert.fail(message);
}

function finishResponse(res, body, protocol = 'openai') {
  const usage = {prompt_tokens:10, completion_tokens:3};
  if (body.stream) {
    res.writeHead(200, {'Content-Type':'text/event-stream'});
    if (protocol === 'responses') {
      res.end('data: ' + JSON.stringify({type:'response.output_text.delta', delta:'Synthetic queue answer'}) + '\n\ndata: ' +
        JSON.stringify({type:'response.completed', response:{status:'completed', model:body.model,
          output:[{type:'message', content:[{type:'output_text', text:'Synthetic queue answer'}]}], usage:{input_tokens:10, output_tokens:3}}}) + '\n\n');
      return;
    }
    if (protocol === 'anthropic') {
      res.end('data: ' + JSON.stringify({type:'content_block_delta', delta:{type:'text_delta', text:'Synthetic queue answer'}}) + '\n\ndata: ' +
        JSON.stringify({type:'message_stop'}) + '\n\n');
      return;
    }
    res.end('data: ' + JSON.stringify({model:body.model, choices:[{delta:{content:'Synthetic queue answer'}, finish_reason:'stop'}], usage}) + '\n\ndata: [DONE]\n\n');
  } else {
    res.writeHead(200, {'Content-Type':'application/json'});
    res.end(JSON.stringify({model:body.model, choices:[{message:{content:'OK'}, finish_reason:'stop'}], usage}));
  }
}

function rawFixtureResponse(res, body, owner, side, protocol) {
  if (side === 'reference') { finishResponse(res, body, protocol); return; }
  if (owner === 'raw-http') {
    const index = requests.filter(item => item.owner === owner && item.side === side).length - 1;
    const status = [401, 429, 503][index % 3];
    res.writeHead(status, {'Content-Type':'application/json'});
    res.end(JSON.stringify({error:{message:'Synthetic HTTP error <script>globalThis.__rawInjection=true</script> ' + 'x'.repeat(18000),
      code:`synthetic_http_${status}`}}));
    return;
  }
  res.writeHead(200, {'Content-Type':owner === 'raw-malformed' ? 'text/plain' : 'text/event-stream'});
  if (owner === 'raw-empty') { res.end(); return; }
  if (owner === 'raw-malformed') { res.end('\n\r\n<html><script>globalThis.__rawInjection=true</script>Synthetic malformed body</html>'); return; }
  const message = 'Synthetic upstream failure <script>globalThis.__rawInjection=true</script> Bearer synthetic-candidate-' + owner;
  if (protocol === 'responses') {
    res.end('data: ' + JSON.stringify({type:'response.output_text.delta', delta:'Synthetic partial answer'}) + '\n\ndata: ' +
      JSON.stringify({type:'response.failed', response:{status:'failed', error:{code:'synthetic_responses_error', message}}}) + '\n\n');
  } else if (protocol === 'anthropic') {
    res.end('data: ' + JSON.stringify({type:'content_block_delta', delta:{type:'text_delta', text:'Synthetic partial answer'}}) + '\n\ndata: ' +
      JSON.stringify({type:'error', error:{type:'synthetic_messages_error', message}}) + '\n\n');
  } else {
    res.end('data: ' + JSON.stringify({model:body.model, choices:[{delta:{content:'Synthetic partial answer'}}]}) + '\n\ndata: ' +
      JSON.stringify({error:{code:'synthetic_chat_error', message}}) + '\n\n');
  }
}

function unicodeJson(payload) {
  return JSON.stringify(payload).replace(/"(?:\\.|[^"\\])*"/g, token =>
    '"' + JSON.parse(token).split('').map(character => '\\u' + character.charCodeAt(0).toString(16).padStart(4,'0')).join('') + '"');
}

function splitDelta(protocol, field, text) {
  if (protocol === 'responses') return {type:field === 'content' ? 'response.output_text.delta' : 'response.reasoning_text.delta', delta:text};
  if (protocol === 'anthropic') return {type:'content_block_delta', delta:field === 'content' ? {type:'text_delta',text} : {type:'thinking_delta',thinking:text}};
  return {choices:[{delta:{[field === 'content' ? 'content' : 'reasoning_content']:text}}]};
}

async function writeSplitWire(res, wire) {
  const bytes = Buffer.from(wire);
  for (let offset = 0; offset < bytes.length && !res.destroyed; offset += 11) {
    res.write(bytes.subarray(offset, offset + 11));
    await new Promise(resolve => setImmediate(resolve));
  }
}

async function splitFixtureResponse(res, owner, side, protocol) {
  const candidateKey = `synthetic-candidate-${owner}`, referenceKey = `synthetic-reference-${owner}`;
  splitCredentialKeys.add(candidateKey); splitCredentialKeys.add(referenceKey);
  const safeContent = 'Synthetic safe content 合成🌊 '.repeat(3), safeReasoning = 'Synthetic safe reasoning 合成🌊 '.repeat(3);
  const initial = [splitDelta(protocol,'content',safeContent), splitDelta(protocol,'content',candidateKey.slice(0,12)),
    splitDelta(protocol,'reasoning',safeReasoning), splitDelta(protocol,'reasoning',referenceKey.slice(0,12))];
  const rest = [
    ...[candidateKey.slice(12),'|',referenceKey.slice(0,12),referenceKey.slice(12)].map(text=>splitDelta(protocol,'content',text)),
    ...[referenceKey.slice(12),'|',candidateKey.slice(0,12),candidateKey.slice(12)].map(text=>splitDelta(protocol,'reasoning',text)),
    protocol === 'responses' ? {type:'response.failed',response:{status:'failed',error:{code:'synthetic_split_error',message:'Synthetic split error 合成🌊'}}} :
      {type:'error',error:{code:'synthetic_split_error',type:'synthetic_split_error',message:'Synthetic split error 合成🌊'}}
  ];
  const encode = frames => frames.map(payload=>'data: ' + unicodeJson(payload) + '\n\n').join('');
  res.writeHead(200, {'Content-Type':'text/event-stream'});
  if (requests.filter(item=>item.owner === owner && item.side === side).length === 1) {
    await writeSplitWire(res,encode(initial));
    if (!splitHeld.has(owner)) splitHeld.set(owner,new Map());
    splitHeld.get(owner).set(res,encode(rest));
  } else {
    await writeSplitWire(res,encode([...initial,...rest])); res.end();
  }
}

async function releaseSplit(owner) {
  for (const [res,wire] of [...(splitHeld.get(owner) || [])]) { await writeSplitWire(res,wire); res.end(); }
  splitHeld.delete(owner);
}

const upstream = http.createServer(async (req, res) => {
  try {
    const parts = []; for await (const chunk of req) parts.push(chunk);
    const body = JSON.parse(Buffer.concat(parts));
    const match = req.url.match(/^\/([a-z-]+)\/(candidate|reference)\/v1\/(chat\/completions|responses|messages)$/);
    assert.ok(match, 'Only the owned synthetic upstream path is requested');
    const [, owner, side, endpoint] = match;
    const protocol = {responses:'responses', messages:'anthropic'}[endpoint] || 'openai';
    assert.equal(body.model, 'synthetic-queue-model');
    const input = body.messages?.at(-1)?.content || '';
    requests.push({owner, side, stream:Boolean(body.stream), specialized:input.startsWith('Read the synthetic inventory below. Reply only OK.\nrecord-0000:'),
      signature:crypto.createHash('sha256').update(req.url + JSON.stringify(body)).digest('hex')});
    active.set(owner, (active.get(owner) || 0) + 1);
    maxActiveJobs = Math.max(maxActiveJobs, active.size);
    assert.equal(active.size, 1, 'Upstream requests from different jobs never overlap');
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      const count = active.get(owner) - 1;
      if (count) active.set(owner, count); else active.delete(owner);
      held.get(owner)?.delete(res);
      splitHeld.get(owner)?.delete(res);
    };
    res.once('finish', close); res.once('close', close);
    if (gated.has(owner)) {
      if (!held.has(owner)) held.set(owner, new Map());
      held.get(owner).set(res, body);
    } else if (splitCases.some(item => item.owner === owner)) await splitFixtureResponse(res, owner, side, protocol);
    else if (rawCases.some(item => item.owner === owner)) rawFixtureResponse(res, body, owner, side, protocol);
    else finishResponse(res, body, protocol);
  } catch (error) {
    fixtureError = error;
    res.destroy();
  }
});

function release(owner) {
  gated.delete(owner);
  for (const [res, body] of held.get(owner) || []) finishResponse(res, body);
}

async function request(url, {body, method = body === undefined ? 'GET' : 'POST', expected = 200, headers = {}} = {}) {
  const response = await fetch(base + url, {method, headers:{...headers, ...(body === undefined ? {} : {'Content-Type':'application/json'})},
    ...(body === undefined ? {} : {body:JSON.stringify(body)}), signal:AbortSignal.timeout(5000)});
  assert.equal(response.status, expected, `HTTP ${method} ${url}`);
  return response.json();
}

const job = id => request(`/admission/api/jobs/${id}`);
const jobs = async () => (await request('/admission/api/jobs')).jobs;
async function completed(id) {
  return until(async () => {
    const snapshot = await job(id);
    assert.ok(!['failed', 'cancelled'].includes(snapshot.state), 'Expected job finishes successfully');
    return snapshot.state === 'succeeded' && snapshot;
  }, `Task #${id} completion is bounded`);
}

async function startApp() {
  const env = Object.fromEntries(['PATH', 'HOME', 'LANG', 'LC_ALL', 'SYSTEMROOT'].filter(name => process.env[name]).map(name => [name, process.env[name]]));
  Object.assign(env, {TMPDIR:data, PYTHONDONTWRITEBYTECODE:'1', PLATFORM_DATA_DIR:data, RELAY_LAB_DATA_DIR:path.join(data, 'relay-lab'),
    PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1', PLATFORM_USERNAME:'', PLATFORM_PASSWORD:''});
  appError = null;
  app = spawn(python, ['-B', 'run.py', '--app', 'admission', '--host', '127.0.0.1', '--port', String(port)], {cwd:root, env, stdio:['ignore', 'pipe', 'pipe']});
  app.stdout.resume(); app.stderr.on('data', chunk => {stderrBytes += chunk.length;});
  appExit = new Promise(resolve => {app.once('exit', resolve); app.once('error', error => {appError = error; resolve();});});
  await until(async () => {
    assert.ok(!appError && app.exitCode === null && app.signalCode === null, 'Owned admission server stays alive');
    try { return (await fetch(base + '/api/health', {signal:AbortSignal.timeout(500)})).ok; } catch { return false; }
  }, 'Owned admission server becomes ready', 12000);
}

async function stopApp(signal = 'SIGTERM') {
  if (!app) return;
  if (!appError && app.exitCode === null && app.signalCode === null) app.kill(signal);
  let cleanupTimer;
  const ended = await Promise.race([appExit.then(() => true), new Promise(resolve => {cleanupTimer = setTimeout(() => resolve(false), 5000);})]);
  clearTimeout(cleanupTimer);
  if (!ended) {
    app.kill('SIGKILL'); await appExit;
    assert.fail('Owned admission server did not stop within five seconds');
  }
  app = null;
}

async function openBrowser() {
  browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport:{width:1440, height:1000}});
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => errors.push(error.name));
  await page.goto(base + '/admission/');
  await page.locator('#preset option').first().waitFor({state:'attached'});
  await page.locator('#reference-id option').nth(1).waitFor({state:'attached'});
  assert.equal(await page.locator('#admission-queue-panel').evaluate(element => element.open), false, 'Queue starts collapsed');
  await page.locator('#admission-queue-panel > summary').press('Enter');
  await page.waitForFunction(() => document.getElementById('admission-queue-panel').open);
  return page;
}

function card(page, id) {
  return page.locator('#admission-queue article').filter({has:page.getByRole('heading', {name:new RegExp(`^任务 #${id} ·`)})});
}

async function submit(page, owner, channelId, protocol = 'openai') {
  const key = `synthetic-candidate-${owner}`; candidateKeys.add(key);
  await page.locator('#candidate-name').fill(`候选 ${owner} <script>/..`);
  await page.locator('#candidate-url').fill(`${upstreamUrl}/${owner}/candidate/v1`);
  await page.locator('#candidate-key').fill(key);
  await page.locator('#candidate-model').fill('synthetic-queue-model');
  await page.locator('#reference-model').fill('synthetic-queue-model');
  await page.locator('#candidate-protocol').selectOption(protocol);
  await page.locator('#reference-protocol').selectOption(protocol);
  await page.locator('#reference-id').selectOption(String(channelId));
  await page.locator('#rounds').selectOption('1');
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === '/admission/api/jobs' && response.request().method() === 'POST');
  await page.locator('#start').click();
  const response = await accepted;
  assert.equal(response.status(), 202, 'Form submissions create server-owned jobs');
  const snapshot = await response.json();
  assert.ok(Number.isInteger(snapshot.id) && snapshot.id > 0);
  assert.equal(snapshot.candidate.name, `候选 ${owner} <script>/..`, 'Channel name is frozen on creation');
  assert.ok(response.request().headers()['idempotency-key'], 'Form submits an idempotency key');
  if (owner === 'fifo-one') {
    const replay = await request('/admission/api/jobs', {expected:202, headers:{'Idempotency-Key':response.request().headers()['idempotency-key']}, body:response.request().postDataJSON()});
    assert.equal(replay.id, snapshot.id, 'The same submitted intent keeps one persistent job');
    assert.equal((await jobs()).length, 1);
  }
  assert.equal(await page.locator('#candidate-key').inputValue(), '', 'Temporary candidate key is cleared after submission');
  return snapshot.id;
}

async function supply(page, id, owner) {
  const item = card(page, id);
  await item.getByLabel('补充临时 Key').fill(`synthetic-candidate-${owner}`);
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === `/admission/api/jobs/${id}/credentials` && response.request().method() === 'POST');
  await item.getByRole('button', {name:'补充并继续', exact:true}).click();
  assert.equal((await accepted).status(), 200);
  assert.equal(await item.getByLabel('补充临时 Key').inputValue(), '');
}

async function screenshot(page, label) {
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, `Admission queue fits ${width}px`);
    const audit = await page.evaluate(() => {
      const body = document.body.innerText.replace(/\s+/g, ''), heading = document.querySelector('h1').textContent.replace(/\s+/g, '');
      const ids = [...document.querySelectorAll('[id]')].map(element => element.id);
      const titles = [...document.querySelectorAll('#admission-queue article h3')].map(element => element.textContent.match(/^任务 #(\d+) ·/)?.[1]);
      return {nav_count:document.querySelectorAll('.platform-nav').length, h1_count:document.querySelectorAll('h1').length,
        form_count:document.querySelectorAll('#compare-form').length, queue_count:document.querySelectorAll('#admission-queue').length,
        queue_heading_count:document.querySelectorAll('#queue-title').length, body_heading_occurrences:body.split(heading).length - 1,
        duplicate_ids:ids.filter((id, index) => ids.indexOf(id) !== index), queue_card_count:titles.length,
        unique_job_ids:[...new Set(titles)], body_chars:body.length, document_height:document.documentElement.scrollHeight,
        viewport_width:innerWidth, viewport_height:innerHeight};
    });
    for (const key of ['nav_count', 'h1_count', 'form_count', 'queue_count', 'queue_heading_count', 'body_heading_occurrences']) {
      assert.equal(audit[key], 1, `Admission DOM has one ${key} at ${width}px`);
    }
    assert.deepEqual(audit.duplicate_ids, [], 'Admission page element IDs are unique');
    assert.equal(audit.unique_job_ids.length, audit.queue_card_count, 'Each queued job has exactly one DOM card');
    assert.ok(audit.unique_job_ids.every(id => id !== undefined), 'Queue cards identify their jobs');
    await page.evaluate(() => window.scrollTo({top:0, behavior:'instant'}));
    await page.screenshot({path:path.join(data, `${label}-${width}.png`), fullPage:true, animations:'disabled'});
    await page.evaluate(() => {
      const section = document.getElementById('admission-queue-panel');
      window.scrollTo({top:scrollY + section.getBoundingClientRect().top - 16, behavior:'instant'});
    });
    await page.waitForFunction(() => {
      const bounds = document.getElementById('queue-title').getBoundingClientRect();
      return bounds.top >= 0 && bounds.bottom < innerHeight;
    });
    await page.screenshot({path:path.join(data, `${label}-viewport-${width}.png`), fullPage:false, animations:'disabled'});
    const viewport = await page.evaluate(() => ({scroll_y:scrollY, queue_heading_top:document.getElementById('queue-title').getBoundingClientRect().top,
      visible_queue_cards:[...document.querySelectorAll('#admission-queue article')].filter(element => {
        const bounds = element.getBoundingClientRect(); return bounds.top < innerHeight && bounds.bottom > 0;
      }).length}));
    assert.ok(viewport.visible_queue_cards > 0, 'Queue viewport screenshot shows job cards');
    const evidence = {label, width, ...audit, ...viewport};
    screenshotAudits.push(evidence);
    fs.writeFileSync(path.join(data, `${label}-${width}-dom.json`), JSON.stringify(evidence, null, 2));
  }
  await page.setViewportSize({width:1440, height:1000});
}

function assertNoCandidateKeys(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value);
  for (const key of new Set([...candidateKeys,...splitCredentialKeys])) assert.ok(!text.includes(key), 'Temporary credentials never appear in public output');
}

function logicalRawText(raw, allowIncomplete = false) {
  const text = {content:'',reasoning:''};
  const lines = raw.split(/\r\n|\r|\n/);
  if (allowIncomplete && !/[\r\n]$/.test(raw)) lines.pop();
  for (const line of lines) {
    if (!line.startsWith('data:')) continue;
    const value = line.slice(5).trim();
    if (!value || value === '[DONE]') continue;
    const payload = JSON.parse(value), delta = payload.choices?.[0]?.delta;
    if (delta) { text.content += delta.content || ''; text.reasoning += delta.reasoning_content || ''; }
    else if (payload.type === 'response.output_text.delta') text.content += payload.delta;
    else if (payload.type === 'response.reasoning_text.delta') text.reasoning += payload.delta;
    else if (payload.type === 'content_block_delta') {
      text.content += payload.delta.text || ''; text.reasoning += payload.delta.thinking || '';
    }
  }
  return text;
}

function assertSplitResponseSafe(response, {pending = false} = {}) {
  assertNoCandidateKeys(response);
  const decoded = logicalRawText(response.raw_response || '',pending);
  assertNoCandidateKeys(decoded);
  if (pending) {
    for (const field of ['content','reasoning','raw_response']) {
      const text = response[field] || '';
      assert.ok(!text.includes('synthetic-'), 'Unresolved credential prefixes are withheld in live evidence');
      assert.ok(!text.includes('\\u0073\\u0079\\u006e'), 'Unicode-escaped credential prefixes are withheld in live raw evidence');
    }
  } else {
    assert.equal(response.raw_response_truncated,false,'Split-key fixture retains complete parseable wire evidence');
    assert.ok(response.raw_response.includes('\\u'),'Legal Unicode JSON escapes remain in upstream wire text');
    for (const field of ['content','reasoning']) {
      assert.ok(decoded[field].includes('合成🌊'),'Unicode escapes decode to the original safe Unicode text');
      assert.ok(decoded[field].includes('[REDACTED]'),'Credential spans are masked before logical stream reconstruction');
      assert.ok((response[field] || '').includes('[REDACTED]'),'Parsed answer and reasoning are independently credential safe');
    }
  }
}

function rawEventResponses(events) {
  const responses = new Map();
  for (const event of events) {
    if (!['raw_response','chunk'].includes(event.type)) continue;
    const key = `${event.round}:${event.question_id}:${event.side}`;
    if (!responses.has(key)) responses.set(key,{content:'',reasoning:'',raw_response:''});
    const response = responses.get(key);
    if (event.type === 'raw_response') response.raw_response = event.raw_response_replace ? event.raw_response : response.raw_response + (event.raw_response || '');
    else for (const field of ['content','reasoning']) response[field] += event[field] || '';
  }
  return [...responses.values()];
}

async function splitKeyChecks(page) {
  for (const {owner,protocol} of splitCases) {
    phase = owner;
    const referenceKey = `synthetic-reference-${owner}`;
    splitCredentialKeys.add(referenceKey);
    const channelId = (await request('/api/registry/channels',{body:{name:`Synthetic ${owner}`,base_url:`${upstreamUrl}/${owner}/reference/v1`,api_key:referenceKey,multiplier:1}})).id;
    await page.locator('#refresh-reference').click();
    await page.locator(`#reference-id option[value="${channelId}"]`).waitFor({state:'attached'});
    const id = await submit(page,owner,channelId,protocol);
    await until(()=>splitHeld.get(owner)?.size === 2,'Both first-question streams pause after incomplete credentials');
    const live = await until(async()=>{
      const snapshot = await job(id), responses = snapshot.report?.responses || [];
      return responses.length === 2 && responses.every(response=>response.content?.includes('Synthetic safe content') && response.raw_response?.startsWith('data:')) && snapshot;
    },'Live snapshots contain safe text before the split-key suffix is released');
    assert.equal(live.state,'running'); assert.equal(live.progress.sending,2);
    assertNoCandidateKeys(live);
    for (const response of live.report.responses) assertSplitResponseSafe(response,{pending:true});
    const liveEvents = await request(`/admission/api/jobs/${id}/events`);
    assertNoCandidateKeys(liveEvents);
    const eventResponses = rawEventResponses(liveEvents.events);
    assert.ok(eventResponses.length > 0,'Live event evidence is inspected before completion');
    for (const response of eventResponses) assertSplitResponseSafe(response,{pending:true});
    await page.locator('#refresh-queue').click();
    await page.waitForFunction(owner=>document.querySelector('#report-channels').textContent.includes(`候选 ${owner}`),owner);
    assertNoCandidateKeys(await page.locator('#report-panel').textContent());
    for (const raw of await page.locator('#results .raw-response-body').allTextContents()) assertSplitResponseSafe({raw_response:raw},{pending:true});
    await releaseSplit(owner);
    const snapshot = await completed(id), source = snapshot.report;
    assert.equal(source.responses.length,12);
    for (const response of source.responses) assertSplitResponseSafe(response);
    assert.ok(source.measurements.every(measurement=>measurement.upstream_error_code === 'synthetic_split_error'));
    await page.locator('#refresh-queue').click();
    await page.waitForFunction(()=>document.querySelector('#run-status').textContent.includes('已完成'));
    assertNoCandidateKeys(await page.locator('#report-panel').textContent());
    const checkVisibleRaw = async currentPage => {
      const raws = await currentPage.locator('.raw-response-body').allTextContents();
      assert.equal(raws.length,12);
      for (const raw of raws) {assertNoCandidateKeys(logicalRawText(raw));assert.ok(logicalRawText(raw).content.includes('[REDACTED]'));}
    };
    await checkVisibleRaw(page);
    await page.locator('#refresh-reports').click();
    await page.locator('#report-history article').filter({has:page.getByRole('heading',{name:new RegExp(`^候选 ${owner} `)})}).getByRole('button',{name:'查看报告',exact:true}).click();
    await page.waitForFunction(id=>document.querySelector('#run-status').textContent.includes(`历史报告 #${id} ·`),snapshot.report_id);
    await checkVisibleRaw(page);
    const jsonDownload = page.waitForEvent('download'); await page.locator('#export-json').click();
    const exported = JSON.parse(fs.readFileSync(await (await jsonDownload).path(),'utf8'));
    assertNoCandidateKeys(exported);
    for (const response of exported.responses) assertSplitResponseSafe(response);
    const htmlDownload = page.waitForEvent('download'); await page.locator('#export-technical-html').click();
    const html = fs.readFileSync(await (await htmlDownload).path(),'utf8'); assertNoCandidateKeys(html);
    const technicalPage = await browser.newPage();
    try {await technicalPage.setContent(html);await checkVisibleRaw(technicalPage);assertNoCandidateKeys(await technicalPage.locator('body').textContent());}
    finally {await technicalPage.close();}
    const ordinaryDownload = page.waitForEvent('download'); await page.locator('#export-html').click();
    const ordinary = fs.readFileSync(await (await ordinaryDownload).path(),'utf8');assertNoCandidateKeys(ordinary);
    for (const forbidden of ['Synthetic safe content','Synthetic safe reasoning','渠道原始响应',upstreamUrl]) assert.ok(!ordinary.includes(forbidden),'Split-key ordinary export remains a summary');
    checks.push(`${owner}_unicode_reassembly_live_events_history_exports_credentials_safe`);
  }
}

async function rawResponseChecks(page) {
  let legacyReport;
  if (!(await page.locator('#report-history-panel').evaluate(element => element.open))) await page.locator('#report-history-panel > summary').click();
  for (const {owner, protocol} of rawCases) {
    phase = owner;
    const channelId = (await request('/api/registry/channels', {body:{name:`Synthetic ${owner}`,
      base_url:`${upstreamUrl}/${owner}/reference/v1`, api_key:`synthetic-reference-${owner}`, multiplier:1}})).id;
    await page.locator('#refresh-reference').click();
    await page.locator(`#reference-id option[value="${channelId}"]`).waitFor({state:'attached'});
    const id = await submit(page, owner, channelId, protocol);
    const snapshot = await completed(id), source = {...snapshot.report, id:snapshot.report_id};
    await page.locator('#refresh-queue').click();
    await page.waitForFunction(owner => document.querySelector('#report-channels').textContent.includes(`候选 ${owner}`) &&
      document.querySelector('#run-status').textContent.includes('已完成'), owner);
    const responses = source.responses.filter(item => item.side === 'candidate');
    const measurements = source.measurements.filter(item => item.side === 'candidate');
    assert.equal(responses.length, 6); assert.equal(measurements.length, 6);
    if (owner !== 'raw-http') assert.ok(measurements.every(item => item.http_status === 200), 'Only real HTTP status is recorded');
    assert.ok(measurements.every(item => !item.ok), 'Raw failure fixtures remain unsuccessful');
    const visibleRaw = page.locator('#results .endpoint-evidence[data-side="candidate"] .raw-response-body');
    for (let index = 0; index < responses.length; index++) {
      assert.equal(await visibleRaw.nth(index).textContent(), responses[index].raw_response, 'Visible raw text matches the saved upstream evidence exactly');
      assert.ok(await visibleRaw.nth(index).isVisible(), 'Error raw text is visible without opening technical details');
    }
    if (owner === 'raw-empty') {
      assert.ok(responses.every(item => item.raw_response === '' && item.content === ''), 'Empty upstream bodies stay empty');
      assert.equal(await visibleRaw.first().textContent(), '');
      assert.equal(await page.locator('.answer-response pre').first().textContent(), '', 'Empty answers have no synthetic text');
    } else {
      assert.ok(responses.every(item => item.raw_response), 'Every nonempty body is retained');
      assert.ok(responses[0].raw_response.includes(owner === 'raw-malformed' ? 'Synthetic malformed body' : owner === 'raw-http' ? 'Synthetic HTTP error' : 'Synthetic partial answer'));
    }
    if (owner === 'raw-http') {
      assert.deepEqual([...new Set(measurements.map(item => item.http_status))].sort(), [401, 429, 503]);
      assert.ok(responses.every(item => item.raw_response_truncated), 'Long upstream bodies disclose truncation');
      assert.ok(measurements.every(item => item.upstream_error_truncated), 'Long structured error fields disclose their own truncation');
      assert.equal(await page.locator('.upstream-error-truncated').count(), 6);
      const truncation = await page.locator('.raw-truncated').first().textContent();
      for (const fragment of ['超过保存预算', '中间已省略', '最多 16 KiB']) assert.ok(truncation.includes(fragment));
      assert.ok(measurements.every(item => item.upstream_error_code === `synthetic_http_${item.http_status}`));
    } else if (['raw-chat', 'raw-responses', 'raw-messages'].includes(owner)) {
      const expectedCode = { 'raw-chat':'synthetic_chat_error', 'raw-responses':'synthetic_responses_error', 'raw-messages':'synthetic_messages_error'}[owner];
      assert.ok(measurements.every(item => item.upstream_error_code === expectedCode), 'Real streaming error code survives partial answers');
      assert.ok(responses.every(item => item.content === 'Synthetic partial answer'), 'Errors never become answer text');
      assert.ok((await page.locator('.endpoint-status').first().textContent()).includes(expectedCode));
    }
    assert.equal(await page.evaluate(() => globalThis.__rawInjection), undefined, 'Upstream text cannot execute as page markup');
    assert.equal(await page.locator('#results script').count(), 0);
    assertNoCandidateKeys(await page.locator('#report-panel').textContent());
    await page.locator('#refresh-reports').click();
    const history = page.locator('#report-history article').filter({has:page.getByRole('heading', {name:new RegExp(`^候选 ${owner} `)})});
    await history.getByRole('button', {name:'查看报告', exact:true}).click();
    await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(`历史报告 #${id} ·`), snapshot.report_id);
    assert.equal(await visibleRaw.first().textContent(), responses[0].raw_response, 'History reload preserves raw body');
    const jsonDownload = page.waitForEvent('download'); await page.locator('#export-json').click();
    const exported = JSON.parse(fs.readFileSync(await (await jsonDownload).path(), 'utf8'));
    assert.equal(exported.responses[0].raw_response, source.responses[0].raw_response); assertNoCandidateKeys(exported);
    const htmlDownload = page.waitForEvent('download'); await page.locator('#export-technical-html').click();
    const html = fs.readFileSync(await (await htmlDownload).path(), 'utf8'); assertNoCandidateKeys(html);
    const technicalPage = await browser.newPage();
    try {
      await technicalPage.setContent(html);
      assert.equal(await technicalPage.locator('.raw-response-body').first().textContent(), responses[0].raw_response, 'Technical HTML preserves raw whitespace and escaping');
      assert.ok(await technicalPage.locator('.raw-response-body').first().isVisible());
      assert.equal(await technicalPage.evaluate(() => globalThis.__rawInjection), undefined);
      assert.equal(await technicalPage.locator('script').count(), 0);
      if (owner === 'raw-http') assert.equal(await technicalPage.locator('.upstream-error-truncated').count(), 6);
      if (measurements[0].upstream_error_code) assert.ok((await technicalPage.locator('.endpoint-status').first().textContent()).includes(measurements[0].upstream_error_code));
      for (const width of [390, 900, 1440]) {
        await page.setViewportSize({width, height:1000});
        await technicalPage.setViewportSize({width, height:1000});
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, `${owner} fits ${width}px`);
        assert.equal(await technicalPage.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, `Technical ${owner} fits ${width}px`);
        if (owner === 'raw-http') {
          await page.locator('#results .evidence-card').first().screenshot({path:path.join(data, `${owner}-${width}.png`), animations:'disabled'});
          screenshotAudits.push({scene:owner, width, raw_visible:true, horizontal_overflow:false});
        }
      }
    } finally { await technicalPage.close(); }
    const ordinaryDownload = page.waitForEvent('download'); await page.locator('#export-html').click();
    const ordinary = fs.readFileSync(await (await ordinaryDownload).path(), 'utf8');
    for (const forbidden of ['渠道原始响应', 'Synthetic partial answer', 'Synthetic HTTP error', 'Synthetic malformed body', upstreamUrl]) assert.ok(!ordinary.includes(forbidden), 'Ordinary HTML remains a summary');
    assertNoCandidateKeys(ordinary);
    checks.push(`${owner}_visible_history_technical_exports_empty_safe`);
    if (owner === 'raw-chat') legacyReport = source;
  }
  const legacyPattern = `**/admission/api/reports/${legacyReport.id}`;
  await page.route(legacyPattern, async route => {
    const saved = structuredClone(legacyReport);
    for (const response of saved.responses) { delete response.raw_response; delete response.raw_response_truncated; }
    for (const measurement of saved.measurements) {
      delete measurement.http_status; delete measurement.upstream_error_code; delete measurement.upstream_error_message; delete measurement.upstream_error_truncated;
      if (measurement.side === 'candidate') measurement.error = 'Synthetic legacy local diagnostic';
    }
    await route.fulfill({json:saved});
  });
  try {
    await page.locator('#report-history article').filter({has:page.getByRole('heading', {name:/^候选 raw-chat /})}).getByRole('button', {name:'查看报告', exact:true}).click();
    await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(`历史报告 #${id} ·`), legacyReport.id);
    assert.equal(await page.locator('.raw-response-body').first().textContent(), '', 'Legacy reports never invent missing raw protocol text');
    assert.ok((await page.locator('.local-diagnostic').first().textContent()).includes('Synthetic legacy local diagnostic'));
    assert.ok(!(await page.locator('.endpoint-status').first().textContent()).includes('HTTP 状态'), 'Legacy reports never invent an HTTP status');
    checks.push('legacy_missing_raw_stays_empty_diagnostic_preserved');
  } finally { await page.unroute(legacyPattern); }
  await page.setViewportSize({width:1440, height:1000});
}

async function run() {
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  upstreamUrl = `http://127.0.0.1:${upstream.address().port}`;
  const probe = net.createServer(); await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  base = `http://127.0.0.1:${port}`;
  await startApp();
  const owners = ['fifo-one', 'fifo-two', 'fifo-three', 'restart-head', 'restart-next', 'stop-visible', 'stop-pending'];
  const channels = {};
  for (const owner of owners) {
    channels[owner] = (await request('/api/registry/channels', {body:{name:`Synthetic ${owner}`, base_url:`${upstreamUrl}/${owner}/reference/v1`, api_key:`synthetic-reference-${owner}`, multiplier:1}})).id;
  }
  let page = await openBrowser();
  phase = 'fifo_submission';
  const first = await submit(page, 'fifo-one', channels['fifo-one']);
  await until(() => held.get('fifo-one')?.size === 2, 'First question pair reaches the local upstream gate');
  const second = await submit(page, 'fifo-two', channels['fifo-two']);
  const third = await submit(page, 'fifo-three', channels['fifo-three']);
  assert.ok(first < second && second < third);
  for (const [id, state, position] of [[first, 'running', 1], [second, 'queued', 2], [third, 'queued', 3]]) {
    const snapshot = await job(id);
    assert.equal(snapshot.state, state); assert.equal(snapshot.status, state); assert.equal(snapshot.queue_position, position);
    assertNoCandidateKeys(snapshot);
  }
  assert.equal((await jobs()).filter(item => item.state === 'running').length, 1);
  checks.push('three_submissions_fifo');
  await page.locator('#admission-queue-panel > summary').press('Enter');
  await page.waitForFunction(() => !document.getElementById('admission-queue-panel').open);
  assert.equal(await card(page, first).isVisible(), false, 'Collapsed queue hides task cards');
  await page.waitForResponse(response => new URL(response.url()).pathname === '/admission/api/jobs' && response.request().method() === 'GET');
  assert.equal((await job(first)).state, 'running', 'Collapsing never pauses the server task');
  assert.equal(requests.length, 2, 'Queue folding never resends or cancels the gated pair');
  assert.ok((await page.locator('#queue-count').textContent()).includes('3 个任务'));
  await page.locator('#admission-queue-panel > summary').press('Enter');
  await page.waitForFunction(() => document.getElementById('admission-queue-panel').open);
  checks.push('queue_keyboard_collapse_preserves_background');
  await screenshot(page, 'queue-running');
  assert.ok(!(await card(page, first).textContent()).includes('排队第 1 位'), 'Running job is never labeled queued first');
  assert.ok((await card(page, first).textContent()).includes('候选 fifo-one <script>/..'));
  assert.equal(await card(page, first).locator('script').count(), 0, 'Names render as text');
  assert.ok((await page.locator('#token-precheck').textContent()).includes('可信绑定'), 'Token prerequisite is visible before execution');
  checks.push('channel_identity_partial_coverage_precheck');
  await page.goto(base + '/channels/'); await page.reload();
  assert.equal((await job(first)).state, 'running');
  assert.equal(requests.length, 2, 'Navigation and refresh leave the gated job running');
  await browser.close(); browser = null;
  release('fifo-one');
  const firstResult = await completed(first);
  assert.equal(firstResult.report.measurements.length, 12);
  assert.equal(firstResult.report.token_usage?.conclusion, 'unable_to_determine', 'No trusted binding produces an unknown Token conclusion');
  assert.ok(requests.filter(item => item.owner === 'fifo-one').every(item => !item.specialized), 'No trusted binding sends zero specialized Token requests');
  await until(() => held.get('fifo-two')?.size === 2, 'Second job starts while the browser is closed');
  assert.equal((await job(third)).state, 'queued');
  page = await openBrowser();
  await card(page, first).getByText('已完成', {exact:false}).waitFor();
  await card(page, second).getByText('测试进行中', {exact:false}).waitFor();
  checks.push('navigation_refresh_browser_close_continue');
  const canceledResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/admission/api/jobs/${third}/cancel`);
  await card(page, third).getByRole('button', {name:`取消排队 #${third} · 候选 fifo-three <script>/..`, exact:true}).click();
  assert.equal((await canceledResponse).status(), 200);
  const cancelled = await until(async () => {const snapshot = await job(third); return snapshot.state === 'cancelled' && snapshot;}, 'Queued cancellation finishes');
  assert.ok(cancelled.report_id, 'Queued cancellation stores its partial report');
  assert.equal(requests.filter(item => item.owner === 'fifo-three').length, 0);
  release('fifo-two'); await completed(second);
  checks.push('queued_cancel_sends_zero_requests');
  phase = 'report_history_export';
  await page.locator('#report-history-panel > summary').click();
  await page.locator('#refresh-reports').click();
  await until(() => page.locator('#report-history article').count().then(count => count === 3), 'Completed and canceled reports appear in history');
  await card(page, first).getByRole('button', {name:'查看进度', exact:true}).click();
  await page.waitForFunction(() => document.querySelector('#run-status').textContent.includes('已完成'));
  assert.equal(await page.locator('#pair-summary tr').count(), 6);
  const download = page.waitForEvent('download'); await page.locator('#export-json').click();
  const exported = JSON.parse(fs.readFileSync(await (await download).path(), 'utf8'));
  assert.equal(exported.id, firstResult.report_id); assert.equal(exported.measurements.length, 12); assertNoCandidateKeys(exported);
  assert.equal(exported.candidate.name, '候选 fifo-one <script>/..');
  assert.equal(exported.reference.name, 'Synthetic fifo-one');
  for (const selector of ['#export-html', '#export-technical-html']) {
    const pending = page.waitForEvent('download'); await page.locator(selector).click();
    const artifact = await pending;
    const html = fs.readFileSync(await artifact.path(), 'utf8');
    assert.ok(html.includes('候选 fifo-one &lt;script&gt;/..'));
    assert.ok(html.includes('Synthetic fifo-one'));
    assert.ok(!html.includes('<script>'), 'Exported name cannot inject markup');
    assert.ok(!artifact.suggestedFilename().includes('/'), 'Export filename is safe');
    assertNoCandidateKeys(html);
  }
  checks.push('named_safe_html_json_exports');
  const historyCard = owner => page.locator('#report-history article').filter({has:page.getByRole('heading', {name:new RegExp(`^候选 ${owner} `)})});
  await historyCard('fifo-one').getByRole('button', {name:'查看报告', exact:true}).click();
  await page.waitForFunction(id=>document.querySelector('#run-status').textContent.includes(`历史报告 #${id} ·`), firstResult.report_id);
  assert.equal(await page.locator('#stop').isVisible(), false, 'History viewing has no stale task stop control');
  let releaseOrdinary;
  const ordinaryGate = new Promise(resolve=>{releaseOrdinary=resolve;});
  const ordinaryPattern = `**/admission/api/reports/${firstResult.report_id}/ordinary`;
  await page.route(ordinaryPattern, async route=>{await ordinaryGate;await route.continue();});
  const ordinaryRequest=page.waitForRequest(req=>new URL(req.url()).pathname===`/admission/api/reports/${firstResult.report_id}/ordinary`);
  const frozenDownload=page.waitForEvent('download');
  await page.locator('#export-html').click(); await ordinaryRequest;
  await historyCard('fifo-two').getByRole('button', {name:'查看报告', exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#report-channels').textContent.includes('候选 fifo-two'));
  releaseOrdinary();
  const frozenArtifact=await frozenDownload;
  assert.ok(frozenArtifact.suggestedFilename().includes('fifo-one'), 'Pending download filename stays tied to its original report');
  assert.ok(fs.readFileSync(await frozenArtifact.path(),'utf8').includes('候选 fifo-one &lt;script&gt;/..'));
  await page.unroute(ordinaryPattern);
  checks.push('history_controls_and_export_snapshot_identity');
  await screenshot(page, 'queue-completed');
  checks.push('report_history_export_desktop_mobile');
  phase = 'restart_waiting_credentials';
  const head = await submit(page, 'restart-head', channels['restart-head']);
  await until(() => held.get('restart-head')?.size === 2, 'Restart fixture sends exactly the first pair');
  const next = await submit(page, 'restart-next', channels['restart-next']);
  const interrupted = await job(head);
  assert.equal(interrupted.progress.sending, 2);
  const sentSignatures = requests.filter(item => item.owner === 'restart-head').map(item => item.signature);
  await stopApp('SIGKILL');
  await until(() => !active.size, 'Owned upstream sockets close after the application is killed');
  release('restart-head');
  await startApp(); await page.reload();
  await page.locator('#admission-queue-panel > summary').press('Enter');
  await page.waitForFunction(() => document.getElementById('admission-queue-panel').open);
  const waitingHead = await job(head), waitingNext = await job(next);
  assert.equal(waitingHead.state, 'waiting_credentials'); assert.equal(waitingHead.needs_credentials, true);
  assert.equal(waitingHead.progress.unknown, 2); assert.equal(waitingHead.progress.sending, 0);
  assert.equal(waitingNext.state, 'waiting_credentials');
  assert.equal(waitingHead.queue_position, 1); assert.equal(waitingNext.queue_position, 2);
  assert.equal((await job(first)).report_id, firstResult.report_id, 'Completed history survives server restart');
  assert.equal((await request(`/admission/api/reports/${firstResult.report_id}`)).measurements.length, 12);
  const countBeforeSupply = requests.length;
  await supply(page, next, 'restart-next');
  for (let index = 0; index < 4; index++) {
    const current = await jobs();
    assert.equal(current.find(item => item.id === head).state, 'waiting_credentials');
    assert.equal(current.find(item => item.id === next).state, 'queued');
    assert.equal(current.filter(item => item.state === 'running').length, 0);
    assert.equal(requests.length, countBeforeSupply, 'Supplying a later job never bypasses the waiting head');
    await pause(250);
  }
  await screenshot(page, 'queue-waiting-key');
  assert.ok((await card(page, head).textContent()).includes('队首暂停，后续 1 个任务等待'));
  assert.ok((await card(page, next).textContent()).includes(`等待队首 #${head}`));
  checks.push('restart_key_loss_fifo_head_blocks');
  phase = 'restart_resume_unknown';
  await supply(page, head, 'restart-head');
  const recovered = await completed(head); await completed(next);
  assert.equal(recovered.progress.unknown, 2);
  assert.equal(recovered.report.measurements.filter(item => item.status === 'unknown').length, 2);
  assert.equal(requests.filter(item => item.owner === 'restart-head').length, 12, 'Two unknown sends plus ten newly completed sends');
  for (const signature of sentSignatures) assert.equal(requests.filter(item => item.signature === signature).length, 1, 'An unknown request is never replayed');
  assert.equal(maxActiveJobs, 1);
  assert.equal(requests.filter(item => item.owner === 'restart-next').length, 12);
  checks.push('unknown_sends_not_replayed_single_running');
  phase = 'delayed_selection_stop_target';
  const visible = await submit(page, 'stop-visible', channels['stop-visible']);
  await until(() => held.get('stop-visible')?.size === 2, 'Visible stop target reaches its request gate');
  const pending = await submit(page, 'stop-pending', channels['stop-pending']);
  await card(page, visible).getByRole('button', {name:'查看进度', exact:true}).click();
  await page.waitForFunction(id => document.querySelector('#stop').textContent.includes(`#${id} ·`), visible);
  let releasePending;
  const pendingGate = new Promise(resolve => {releasePending = resolve;});
  const pendingPattern = `**/admission/api/jobs/${pending}`;
  await page.route(pendingPattern, async route => {await pendingGate; await route.continue();});
  const pendingRequest = page.waitForRequest(req => new URL(req.url()).pathname === `/admission/api/jobs/${pending}` && req.method() === 'GET');
  await card(page, pending).getByRole('button', {name:'查看进度', exact:true}).click();
  await pendingRequest;
  assert.ok((await page.locator('#run-status').textContent()).includes(`任务 #${visible} ·`), 'Delayed selection keeps the visible task details');
  assert.ok((await page.locator('#stop').textContent()).includes(`#${visible} ·`), 'Stop label stays tied to visible details');
  const stoppedResponse = page.waitForResponse(response => /\/admission\/api\/jobs\/\d+\/cancel$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST');
  await page.locator('#stop').click();
  const stopped = await stoppedResponse;
  assert.equal(new URL(stopped.url()).pathname, `/admission/api/jobs/${visible}/cancel`, 'Stop cancels the displayed task while another selection is loading');
  assert.equal(stopped.status(), 200);
  const pendingResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/admission/api/jobs/${pending}` && response.request().method() === 'GET');
  releasePending();
  await pendingResponse;
  await page.waitForFunction(id => document.querySelector('#run-status').textContent.includes(`任务 #${id} ·`), pending);
  await page.unroute(pendingPattern);
  await until(async () => (await job(visible)).state === 'cancelled', 'Displayed task is cancelled');
  await until(() => held.get('stop-pending')?.size === 2, 'Pending selection remains eligible and starts after its predecessor');
  assert.ok(!(await job(pending)).cancel_requested, 'Delayed selection is never cancelled by the prior stop control');
  await screenshot(page, 'queue-stop-target');
  await card(page, pending).getByRole('button', {name:`停止运行 #${pending} · 候选 stop-pending <script>/..`, exact:true}).click();
  await until(async () => (await job(pending)).state === 'cancelled', 'Remaining stop fixture closes within its bound');
  checks.push('delayed_selection_stop_targets_rendered_task');
  const snapshots = await jobs(); assert.equal(snapshots.length, 7); assertNoCandidateKeys(snapshots);
  await page.locator('#refresh-queue').click();
  await page.waitForFunction(() => document.querySelectorAll('#admission-queue article').length === 7);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:1000});
    await page.waitForFunction(() => {
      const root = document.getElementById('admission-queue');
      return root.style.maxHeight && root.scrollHeight > root.clientHeight;
    });
    const bounds = await page.locator('#admission-queue').evaluate(root => {
      root.scrollTop = 0;
      const end = root.getBoundingClientRect().bottom, cards = [...root.querySelectorAll('article')];
      const visible = cards.filter(card => {const bounds = card.getBoundingClientRect(); return bounds.top >= root.getBoundingClientRect().top - 1 && bounds.bottom <= end + 1;}).length;
      return {visible, sixth:cards[5].getBoundingClientRect().top, end};
    });
    assert.ok(bounds.visible > 0 && bounds.visible <= 5 && bounds.sixth > bounds.end, 'At most five complete cards fit in the queue');
    await page.locator('#admission-queue article').last().getByRole('button', {name:'查看进度', exact:true}).click();
    assert.ok(await page.locator('#admission-queue').evaluate(root => root.scrollTop > 0), 'Later task controls are reachable by scrolling');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    await page.locator('#admission-queue-panel').screenshot({path:path.join(data, `queue-five-scroll-${width}.png`), animations:'disabled'});
  }
  checks.push('queue_five_visible_remaining_scroll');
  for (const snapshot of snapshots) {
    const detail = await job(snapshot.id); assertNoCandidateKeys(detail);
    assert.ok(detail.report_id && detail.report, 'Every terminal job retains its report');
  }
  phase = 'variable_height_queue';
  const variableKey = 'synthetic-candidate-variable-height'; candidateKeys.add(variableKey);
  const variableReference = (await request('/api/registry/channels', {body:{name:'Synthetic variable queue',
    base_url:`${upstreamUrl}/variable-height/reference/v1`, api_key:'synthetic-reference-variable-height', multiplier:1}})).id;
  const variableIds = [];
  for (let index = 0; index < 20; index++) {
    const snapshot = await request('/admission/api/jobs', {expected:202, headers:{'Idempotency-Key':crypto.randomUUID()},
      body:{candidate:{name:index < 5 ? '合成渠道长名称'.repeat(18) : `合成渠道 ${index + 1}`,
        base_url:`${upstreamUrl}/variable-height/candidate/v1`, api_key:variableKey, model:'synthetic-queue-model', protocol:'openai'},
        reference:{channel_id:variableReference, model:'synthetic-queue-model', protocol:'openai'}, rounds:1}});
    variableIds.push(snapshot.id);
  }
  await until(() => held.get('variable-height')?.size === 2, 'Variable height queue keeps its head behind the owned request gate');
  await page.locator('#refresh-queue').click();
  await page.waitForFunction(() => document.querySelectorAll('#admission-queue article').length === 27);
  for (const width of [1440, 390]) {
    await page.setViewportSize({width, height:1000});
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const geometry = await page.locator('#admission-queue').evaluate(root => {
      const heights = [...root.querySelectorAll('article')].map(card => card.getBoundingClientRect().height);
      return {queueHeight:root.clientHeight, minimumCardHeight:Math.min(...heights), maximumCardHeight:Math.max(...heights)};
    });
    assert.ok(geometry.maximumCardHeight - geometry.minimumCardHeight > 24, 'Long names do not inflate the shorter task cards');
    assert.ok(geometry.queueHeight < geometry.maximumCardHeight * 5, 'A long task does not expand every queue row');
    screenshotAudits.push({scene:'variable_height_queue', width, ...geometry});
    for (const position of ['top', 'middle', 'bottom']) {
      const visible = await page.locator('#admission-queue').evaluate((root, position) => {
        root.scrollTop = position === 'top' ? 0 : position === 'middle' ? (root.scrollHeight - root.clientHeight) / 2 : root.scrollHeight;
        const bounds = root.getBoundingClientRect();
        return [...root.querySelectorAll('article')].filter(card => {
          const rect = card.getBoundingClientRect(); return rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1;
        }).length;
      }, position);
      assert.ok(visible <= 5 && visible > 0, `At most five complete tasks fit at ${position} with mixed name lengths at ${width}px`);
    }
    await page.locator('#refresh-queue').click();
    await page.waitForFunction(() => {
      const root = document.getElementById('admission-queue'); return root.scrollHeight - root.clientHeight - root.scrollTop <= 2;
    });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false);
    await page.locator('#admission-queue-panel').screenshot({path:path.join(data, `queue-variable-height-${width}.png`), animations:'disabled'});
  }
  for (const id of variableIds.slice(1)) await request(`/admission/api/jobs/${id}/cancel`, {method:'POST'});
  await request(`/admission/api/jobs/${variableIds[0]}/cancel`, {method:'POST'});
  await until(async () => (await job(variableIds[0])).state === 'cancelled', 'Variable height fixture head is cancelled');
  assert.equal(requests.filter(item => item.owner === 'variable-height').length, 2, 'The remaining nineteen tasks never send requests');
  checks.push('queue_mixed_heights_five_at_every_scroll_position');
  await rawResponseChecks(page);
  await splitKeyChecks(page);
  assert.deepEqual(errors, []);
  await browser.close(); browser = null; await stopApp();
  for (const name of fs.readdirSync(data, {recursive:true})) {
    const file = path.join(data, name);
    if (fs.statSync(file).isFile()) for (const key of new Set([...candidateKeys,...splitCredentialKeys])) assert.ok(!fs.readFileSync(file).includes(Buffer.from(key)), 'Temporary credentials never reach an evidence file or database');
  }
  assert.ok(!fixtureError);
  assert.equal(requests.length,expectedMockRequests,'Fixed Mock request count includes the three new split-key jobs');
  assert.equal(checks.length,expectedChecks,'Every fixed browser acceptance scenario ran');
  return {status:'passed', checks, mockRequests:requests.length, maxActiveJobs, unknownSteps:recovered.progress.unknown,
    screenshotAudits, duration_ms:Math.round(performance.now()-runStartedAt), budget_ms:browserBudgetMs, real_upstream_tested:false, artifacts:data};
}

const watchdog = setTimeout(() => {
  fixtureError = new Error('Admission queue browser exceeded its 240 second budget');
  if (app && app.exitCode === null && app.signalCode === null) app.kill('SIGKILL');
  if (browser) void browser.close();
  upstream.closeAllConnections();
}, browserBudgetMs);

run().then(result => {fs.writeFileSync(path.join(data, 'result.json'), JSON.stringify(result, null, 2)); console.log(JSON.stringify(result));})
  .catch(error => {
    const summary = error instanceof assert.AssertionError ? error.message.split('\n')[0] :
      error.name === 'TimeoutError' ? 'browser_operation_timeout' : 'owned_browser_queue_execution_failed';
    const result = {status:'failed', phase, error_name:error.name, error:summary, checks, mockRequests:requests.length, stderrBytes, artifacts:data};
    fs.writeFileSync(path.join(data, 'result.json'), JSON.stringify(result, null, 2));
    console.error(JSON.stringify(result)); process.exitCode = 1;
  }).finally(async () => {
    clearTimeout(watchdog);
    try {
      if (browser) await browser.close();
      await stopApp();
    } catch (error) {console.error(JSON.stringify({status:'failed', phase:'owned_resource_cleanup', error_name:error.name})); process.exitCode = 1;}
    for (const responses of held.values()) for (const res of responses.keys()) res.destroy();
    for (const responses of splitHeld.values()) for (const res of responses.keys()) res.destroy();
    upstream.closeAllConnections();
    if (upstream.listening) await new Promise(resolve => upstream.close(resolve));
  });
