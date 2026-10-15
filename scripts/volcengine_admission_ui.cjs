'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {randomUUID} = require('node:crypto');
const {spawn} = require('node:child_process');

const root = fs.realpathSync(path.resolve(__dirname, '..'));
const artifactRoot = process.env.EVAL_TEST_ARTIFACT_ROOT;
if (!artifactRoot || !path.isAbsolute(artifactRoot)) throw new Error('External artifact root required');
const safeRoot = fs.realpathSync(artifactRoot);
if (safeRoot === root || safeRoot.startsWith(root + path.sep) || root.startsWith(safeRoot + path.sep)) throw new Error('Artifact root overlaps source');
if (process.platform === 'darwin') {
  const assignedRoot = fs.realpathSync('/Users/lmurder/Desktop/api中转站/中转站极限测试数据');
  if (safeRoot !== assignedRoot && !safeRoot.startsWith(assignedRoot + path.sep)) throw new Error('Artifact root must stay within the assigned test data root');
}
if (process.argv.includes('--check-artifact-root')) { console.log(JSON.stringify({status: 'passed', artifactRoot: safeRoot})); process.exit(0); }
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const data = fs.mkdtempSync(path.join(safeRoot, 'volcengine-admission-ui-'));
let app, appExit, browser, failure, phase = 'startup', mockRequests = 0;
const errors = [], checkpoints = [], apiWrites = [], detailReads = [];
const watchdog = setTimeout(() => {
  failure = failure || new Error('Volcengine browser budget exceeded');
  if (app) app.kill('SIGTERM');
  if (browser) browser.close().catch(() => {});
}, 260000);

(async () => {
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const prefix = '/volcengine-admission';
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => ['PATH', 'HOME', 'LANG', 'TMPDIR', 'PYTHONDONTWRITEBYTECODE'].includes(key)));
  app = spawn(process.env.PYTHON_EXECUTABLE || 'python3', ['-B', 'tests/fixtures/volcengine_app.py', String(port)], {
    cwd: root, env: {...env, PLATFORM_DATA_DIR: data, RELAY_LAB_DATA_DIR: data, PLATFORM_USERNAME: '', PLATFORM_PASSWORD: ''}, stdio: 'ignore',
  });
  appExit = new Promise(resolve => { app.once('exit', resolve); app.once('error', resolve); });
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { if ((await fetch(base + '/api/health')).ok) { ready = true; break; } } catch {}
    if (app.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.ok(ready, 'Owned fixture starts');
  const read = async url => {
    const response = await fetch(base + url);
    assert.ok(response.ok, 'Owned read succeeds');
    return response.json();
  };
  const state = async () => {
    const current = await read('/fixture/state');
    mockRequests = current.mockRequests;
    return current;
  };
  const control = async command => {
    const response = await fetch(base + '/fixture/' + command, {method: 'POST'});
    assert.ok(response.ok, 'Owned fixture control succeeds');
    return response.json();
  };
  const until = async (predicate, label) => {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      if (await predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error(label);
  };
  const run = id => read(`${prefix}/api/runs/${id}`);
  browser = await chromium.launch({headless: true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel: process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}, acceptDownloads: true});
  page.on('pageerror', error => errors.push(error.name));
  page.on('request', request => {
    const pathname = new URL(request.url()).pathname;
    if (request.method() === 'POST' && pathname.startsWith(prefix + '/api/')) apiWrites.push(pathname);
    if (request.method() === 'GET' && new RegExp('^' + prefix + '/api/runs/[a-f0-9]+$').test(pathname)) detailReads.push(pathname);
  });
  const postResult = async (pathname, action) => {
    const waiting = page.waitForResponse(response => new URL(response.url()).pathname === pathname && response.request().method() === 'POST');
    await action();
    const response = await waiting;
    assert.ok(response.ok(), 'UI POST succeeds');
    return response.json();
  };
  const preview = () => postResult(prefix + '/api/preview', () => page.locator('#preview').click());
  const execute = () => postResult(prefix + '/api/runs', () => page.locator('#execute').click());
  const selectRun = async id => {
    await page.locator('#runs button').filter({hasText: id}).click();
    await page.waitForFunction(value => document.querySelector('#run-id').textContent.includes(value), id);
  };
  const delayedAction = async (pathname, action, whileHeld) => {
    await page.waitForFunction(() => !document.querySelector('#refresh-runs').disabled);
    let release, held = false;
    const gate = new Promise(resolve => { release = resolve; });
    const handler = async route => {
      const response = await route.fetch();
      held = true;
      await gate;
      await route.fulfill({response});
    };
    await page.route(base + pathname, handler);
    try {
      const waiting = page.waitForResponse(response => new URL(response.url()).pathname === pathname && response.request().method() === 'POST');
      await action();
      await until(() => held, 'Action response reaches an actual browser response gate');
      await whileHeld();
      await page.waitForFunction(() => !document.querySelector('#refresh-runs').disabled);
      const refreshed = page.waitForResponse(response => new URL(response.url()).pathname === prefix + '/api/runs' && response.request().method() === 'GET');
      release();
      const response = await waiting;
      assert.ok(response.ok(), 'Gated UI POST succeeds');
      await refreshed;
      await page.waitForFunction(() => !document.querySelector('#refresh-runs').disabled);
      return response.json();
    } finally {
      release();
      await page.unroute(base + pathname, handler);
    }
  };
  const selectModels = async selected => {
    if (!await page.locator('#legacy-models').evaluate(element => element.open)) await page.locator('#legacy-models > summary').click();
    for (let index = 0; index < 11; index++) await page.locator('#model-' + index).setChecked(selected.includes(index));
  };
  const downloadReport = async (name, forbidden) => {
    const waiting = page.waitForEvent('download');
    await page.locator('#download-report').click();
    const download = await waiting;
    const filename = path.join(data, name + '.json');
    await download.saveAs(filename);
    const text = fs.readFileSync(filename, 'utf8');
    for (const value of forbidden) assert.ok(!text.includes(value), 'Report excludes temporary and signed data');
    const report = JSON.parse(text);
    assert.equal(report.billing_status, 'unverified');
    assert.equal(report.actual_charged_cny, null);
    return report;
  };

  phase = 'navigation_and_defaults';
  await page.goto(base + '/');
  await page.getByRole('link', {name: '火山新 Key 验收', exact: true}).first().click();
  await page.getByRole('heading', {name: '火山新 Key 验收', exact: true}).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('.model-card').length === 11);
  assert.equal(await page.locator('.platform-nav a[aria-current="page"]').textContent(), '火山新 Key 验收');
  assert.equal(await page.locator('input[name="mode"][value="mock"]').isChecked(), true);
  assert.equal(await page.locator('.model-choice input:checked').count(), 6);
  assert.equal(await page.locator('#legacy-models').evaluate(element => element.open), false);
  assert.equal(await page.locator('#size-4').inputValue(), '1K');
  assert.equal(await page.locator('#size-5').inputValue(), '1K');
  assert.equal((await state()).mockRequests, 0, 'Loading page never calls upstream');
  const initial = await preview();
  assert.equal(initial.items.length, 6);
  assert.ok(Math.abs(initial.estimated_cny - 7.1784) < 1e-9);
  assert.equal((await state()).mockRequests, 0, 'Preview never calls upstream');
  await page.locator('#resolution-0').selectOption('720p');
  assert.equal(await page.locator('#preview-panel').isHidden(), true);
  assert.equal(await page.locator('#execute').isDisabled(), true);
  const higherResolution = await preview();
  assert.ok(higherResolution.estimated_cny > initial.estimated_cny);
  await page.locator('#duration-0').selectOption('5');
  assert.equal(await page.locator('#preview-panel').isHidden(), true);
  assert.ok((await preview()).estimated_cny > higherResolution.estimated_cny);
  await page.locator('#reset-models').click();
  assert.equal(await page.locator('#resolution-0').inputValue(), '480p');
  assert.equal(await page.locator('#duration-0').inputValue(), '4');
  await selectModels(Array.from({length: 11}, (_, index) => index));
  assert.equal(await page.locator('#duration-6').inputValue(), 'frames:29');
  assert.equal(await page.locator('#duration-7').inputValue(), 'frames:29');
  const all = await preview();
  assert.equal(all.items.length, 11);
  assert.ok(Math.abs(all.estimated_cny - 8.07112) < 1e-9);
  checkpoints.push('default_6_optional_5_specs_price_preview_invalidation');

  phase = 'mock_once_and_media';
  const syntheticKey = 'fixture-key-' + randomUUID();
  const syntheticPrompt = 'fixture-scene-' + randomUUID();
  await page.locator('#api-key').fill(syntheticKey);
  await page.locator('#prompt').fill(syntheticPrompt);
  assert.equal(await page.locator('#preview-panel').isHidden(), true);
  await preview();
  const first = await execute();
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#confirm-live').isChecked(), false);
  await until(async () => (await run(first.id)).status === 'completed', 'Mock run reaches terminal state');
  await page.locator('#refresh-runs').click();
  await page.waitForFunction(() => document.querySelector('#run-status').textContent === '本轮已结束');
  assert.equal(await page.locator('#run-items .result-card').count(), 11);
  assert.equal(await page.locator('#run-items img').count(), 5);
  assert.equal(await page.locator('#run-items video').count(), 6);
  assert.equal(await page.locator('#run-detail > details[open]').count(), 0);
  const sampleImage = page.locator('#run-items img').first();
  await sampleImage.scrollIntoViewIfNeeded();
  await sampleImage.evaluate(image => image.decode());
  assert.equal(await sampleImage.evaluate(image => image.naturalWidth), 128, 'Local PNG decodes');
  const sampleVideo = page.locator('#run-items video').first();
  await sampleVideo.scrollIntoViewIfNeeded();
  await sampleVideo.evaluate(async video => { video.muted = true; await video.play(); });
  await page.waitForFunction(() => document.querySelector('#run-items video').currentTime > 0);
  assert.equal(await sampleVideo.evaluate(video => video.videoWidth), 64, 'Local MP4 plays in browser');
  await sampleVideo.evaluate(video => video.pause());
  const afterMock = await state();
  assert.equal(afterMock.generations, 11);
  assert.equal(new Set(afterMock.generationModels).size, 11, 'Exactly one generation per selected model');
  const firstReport = await downloadReport('mock-report', [syntheticKey, syntheticPrompt, 'signature=', 'do-not-retain', 'video_url', 'b64_json']);
  assert.ok(firstReport.items.every(item => item.status === 'passed' && item.media_id));
  for (const [kind, extension] of [['img', 'png'], ['video', 'mp4']]) {
    const card = page.locator('.result-card').filter({has: page.locator(kind)}).first();
    const pending = page.waitForEvent('download');
    await card.getByRole('link', {name: '下载样片', exact: true}).click();
    const download = await pending;
    const filename = path.join(data, 'sample.' + extension);
    await download.saveAs(filename);
    const bytes = fs.readFileSync(filename);
    assert.ok(bytes.length > 32, 'Sample download contains bytes');
    if (extension === 'png') assert.equal(bytes.subarray(1, 4).toString(), 'PNG');
    else assert.equal(bytes.subarray(4, 8).toString(), 'ftyp');
  }
  await page.reload();
  await page.waitForFunction(() => document.querySelectorAll('#run-items .result-card').length === 11);
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#prompt').inputValue(), '');
  assert.equal((await state()).generations, 11, 'Reload reads persisted report without regenerating');
  checkpoints.push('mock_each_model_once_terminal_media_download_report_reload');

  phase = 'live_confirmation_and_auth';
  await page.locator('input[name="mode"][value="live"]').check();
  await selectModels([0, 4]);
  await page.locator('#api-key').fill(syntheticKey);
  await preview();
  assert.equal(await page.locator('#execute').isDisabled(), true);
  const writesBefore = apiWrites.filter(value => value === prefix + '/api/runs').length;
  await page.locator('#admission-form').evaluate(form => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true})));
  assert.equal(apiWrites.filter(value => value === prefix + '/api/runs').length, writesBefore);
  assert.equal((await state()).generations, 11, 'Unconfirmed live form submits nothing');
  assert.equal(await page.locator('#check-auth').isDisabled(), true);
  await page.locator('#confirm-read').check();
  const authentication = await postResult(prefix + '/api/auth', () => page.locator('#check-auth').click());
  assert.equal(authentication.status, 'passed');
  assert.equal((await state()).authReads, 1);
  assert.equal((await state()).generations, 11, 'Read-only authentication creates no generation');
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#confirm-read').isChecked(), false);
  assert.ok(!(await page.locator('body').innerText()).includes('do-not-retain'));
  checkpoints.push('live_unconfirmed_zero_submit_read_only_auth_scope');

  phase = 'stop_and_query_only_resume';
  await control('hold-submit');
  await page.locator('#api-key').fill(syntheticKey);
  await page.locator('#prompt').fill(syntheticPrompt);
  await preview();
  await page.locator('#confirm-live').check();
  const stopped = await execute();
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#confirm-live').isChecked(), false);
  await until(async () => (await state()).heldSubmission && (await run(stopped.id)).items[0].status === 'sending', 'POST is accepted behind an actual response gate');
  await page.locator('#refresh-runs').click();
  await page.locator('#stop-run').focus();
  assert.equal(await page.locator('#stop-run').evaluate(element => element === document.activeElement), true);
  await delayedAction(`${prefix}/api/runs/${stopped.id}/stop`, () => page.keyboard.press('Enter'), async () => {
    assert.equal((await run(stopped.id)).status, 'stopped', 'Stop targets the record shown when clicked');
    await selectRun(first.id);
  });
  assert.ok((await page.locator('#run-id').textContent()).includes(first.id), 'Late stop response preserves the newly selected history');
  assert.ok((await page.locator('#download-report').getAttribute('href')).includes(first.id));
  await selectRun(stopped.id);
  await page.waitForFunction(() => document.querySelector('#run-status').textContent === '本地已停止');
  const stillSettling = await run(stopped.id);
  assert.equal(stillSettling.execution_active, true);
  assert.equal(stillSettling.can_resume, false);
  assert.equal(stillSettling.items[0].task_id, null);
  assert.equal(await page.locator('#run-settling').isVisible(), true);
  assert.equal(await page.locator('#resume-controls').isHidden(), true);
  assert.equal(await page.locator('#cleanup-controls').isHidden(), true);
  await page.waitForFunction(() => !document.querySelector('#refresh-runs').disabled);
  const readsAtStop = detailReads.length;
  await control('release-submit');
  await page.locator('#resume-controls').waitFor({state: 'visible', timeout: 10000});
  assert.ok(detailReads.length > readsAtStop, 'Stopped in-flight request automatically refreshes without a manual click');
  assert.equal(await page.locator('#run-settling').isHidden(), true);
  const beforeResume = await run(stopped.id);
  assert.equal(beforeResume.execution_active, false);
  const knownTask = beforeResume.items[0].task_id;
  assert.ok(knownTask);
  assert.equal(beforeResume.items[1].status, 'not_sent');
  assert.equal(beforeResume.can_resume, true);
  const generationCount = (await state()).generations;
  assert.equal(generationCount, 12);
  await control('limit-capacity');
  const beforeBlockedResume = await state();
  await page.locator('#confirm-resume').check();
  assert.equal(await page.locator('#resume-run').isDisabled(), true, 'Original live key must be supplied again');
  await page.locator('#api-key').fill(syntheticKey);
  await postResult(`${prefix}/api/runs/${stopped.id}/resume`, () => page.locator('#resume-run').click());
  await until(async () => {
    const blocked = await run(stopped.id);
    return !blocked.execution_active && blocked.items[0].status === 'inconclusive';
  }, 'Known task pauses read-only recovery when local capacity is full');
  await page.locator('#refresh-runs').click();
  await page.waitForFunction(() => document.querySelector('#run-items').textContent.includes('上游任务已提交，可能继续执行并计费'));
  const blockedResume = await run(stopped.id);
  assert.equal(blockedResume.items[0].task_id, knownTask);
  assert.equal(blockedResume.items[0].error_category, 'media_capacity_unavailable');
  assert.equal(blockedResume.items[1].status, 'not_sent');
  assert.equal(blockedResume.can_resume, true);
  const blockedText = await page.locator('#run-items .result-card').first().innerText();
  assert.ok(blockedText.includes('原 Key 只读继续查询'));
  assert.ok(!blockedText.includes('未发送') && !blockedText.includes('未创建生成任务'), 'Known submitted task never claims it was not created');
  assert.equal((await state()).mockRequests, beforeBlockedResume.mockRequests, 'Capacity-blocked recovery makes no upstream call or generation');
  assert.equal((await state()).generations, generationCount);
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#confirm-resume').isChecked(), false);
  await page.locator('#resume-controls').waitFor({state: 'visible'});
  await page.locator('#run-detail').screenshot({path: path.join(data, 'volcengine-resume-capacity.png')});
  const blockedReport = await downloadReport('capacity-resume-report', [syntheticKey, syntheticPrompt, 'signature=', 'do-not-retain']);
  assert.equal(blockedReport.items[0].task_id, knownTask);
  assert.equal(blockedReport.items[0].status, 'inconclusive');
  checkpoints.push('known_task_capacity_blocked_resume_preserves_submission_zero_upstream_read_only_recovery');
  await control('release-capacity');
  await page.locator('#confirm-resume').check();
  await page.locator('#api-key').fill(syntheticKey);
  await delayedAction(`${prefix}/api/runs/${stopped.id}/resume`, () => page.locator('#resume-run').click(), async () => {
    await selectRun(first.id);
  });
  assert.ok((await page.locator('#run-id').textContent()).includes(first.id), 'Late resume response preserves the newly selected history');
  assert.ok((await page.locator('#download-report').getAttribute('href')).includes(first.id));
  assert.equal(await page.locator('#api-key').inputValue(), '');
  assert.equal(await page.locator('#confirm-resume').isChecked(), false);
  await until(async () => (await run(stopped.id)).status === 'completed', 'Query-only resume completes');
  await selectRun(stopped.id);
  await page.locator('#refresh-runs').click();
  await page.waitForFunction(() => document.querySelector('#run-status').textContent === '本轮已结束');
  const resumed = await run(stopped.id);
  assert.equal(resumed.items[0].status, 'passed');
  assert.equal(resumed.items[0].task_id, knownTask);
  assert.equal(resumed.items[1].status, 'not_sent');
  assert.equal((await state()).generations, generationCount, 'Resume performs no generation POST');
  const resumedReport = await downloadReport('resumed-report', [syntheticKey, syntheticPrompt, 'signature=', 'do-not-retain']);
  assert.equal(resumedReport.resume_only, true);
  checkpoints.push('gated_inflight_stop_auto_refresh_original_key_query_only_resume_no_unsent_post');
  checkpoints.push('late_stop_resume_responses_keep_selected_history_and_export_identity');

  phase = 'unknown_never_resubmitted';
  await page.locator('input[name="mode"][value="mock"]').check();
  await selectModels([0]);
  await control('unknown');
  await preview();
  const uncertain = await execute();
  await until(async () => (await run(uncertain.id)).status === 'completed', 'Unknown submission remains terminal locally');
  await page.locator('#refresh-runs').click();
  await page.waitForFunction(() => document.querySelector('#run-items').textContent.includes('提交结果未知'));
  const unknown = await run(uncertain.id);
  assert.equal(unknown.items[0].status, 'unknown');
  assert.equal(unknown.items[0].task_id, null);
  assert.equal(unknown.can_resume, false);
  const unknownCount = (await state()).generations;
  assert.equal(unknownCount, 13);
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#run-items').textContent.includes('提交结果未知'));
  await page.locator('#refresh-runs').click();
  assert.equal((await state()).generations, unknownCount, 'Unknown submissions are not retried by refresh');
  checkpoints.push('unknown_result_no_retry_or_resume');

  phase = 'responsive_accessible_and_redacted';
  await page.locator('#runs button').filter({hasText: first.id}).click();
  await page.waitForFunction(() => document.querySelectorAll('#run-items .result-card').length === 11);
  await page.locator('#run-items video').evaluateAll(videos => videos.forEach(video => { video.preload = 'auto'; video.load(); }));
  await page.waitForFunction(() => [...document.querySelectorAll('#run-items video')].every(video => video.readyState >= 2));
  await page.locator('#reset-models').click();
  for (const width of [390, 900, 1440]) {
    await page.setViewportSize({width, height: 1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2), false, 'No page horizontal overflow');
    assert.equal(await page.evaluate(() => {
      const ids = [...document.querySelectorAll('[id]')].map(element => element.id);
      return ids.length === new Set(ids).size;
    }), true);
    await page.screenshot({path: path.join(data, `volcengine-${width}.png`), fullPage: true});
    await page.locator('#run-detail').screenshot({path: path.join(data, `volcengine-result-${width}.png`)});
  }
  const radio = page.locator('input[name="mode"][value="mock"]');
  await radio.focus();
  await page.keyboard.press('Space');
  assert.equal(await radio.evaluate(element => element === document.activeElement), true);
  assert.notEqual(await radio.evaluate(element => getComputedStyle(element).outlineStyle), 'none');
  const visible = await page.locator('body').innerText();
  for (const value of [syntheticKey, syntheticPrompt, 'signature=', 'do-not-retain']) assert.ok(!visible.includes(value), 'Visible report is redacted');
  assert.ok(await page.locator('#run-items img,#run-items video').evaluateAll(elements => elements.every(element => new URL(element.src).pathname.startsWith('/volcengine-admission/api/runs/'))));
  assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0, 'No browser persistence');

  phase = 'capacity_prevents_generation';
  await control('limit-capacity');
  const fullPreview = await preview();
  assert.equal(fullPreview.capacity.available_files, 0);
  assert.ok(fullPreview.capacity_warnings.length > 0);
  assert.ok((await page.locator('#preview-notes').textContent()).includes('本地容量不足'));
  const callsBeforeCapacity = (await state()).mockRequests;
  const capacityRun = await execute();
  await until(async () => (await run(capacityRun.id)).status === 'completed', 'Capacity-full batch stops before upstream submission');
  await page.locator('#refresh-runs').click();
  await page.waitForFunction(() => document.querySelector('#run-items').textContent.includes('本地样片空间不足，本项未发送'));
  assert.ok((await run(capacityRun.id)).items.every(item => item.status === 'not_sent' && item.error_category === 'media_capacity_unavailable'));
  assert.equal((await state()).mockRequests, callsBeforeCapacity, 'Full media capacity causes no upstream generation');
  checkpoints.push('full_local_capacity_warns_and_skips_before_generation');

  phase = 'local_media_cleanup';
  await page.locator('#runs button').filter({hasText: first.id}).click();
  await page.waitForFunction(() => document.querySelectorAll('#run-items .result-card').length === 11);
  const capacityBefore = await read(prefix + '/api/capacity');
  assert.ok(capacityBefore.used_bytes > 0 && capacityBefore.used_files >= 11);
  await page.locator('#cleanup-controls').waitFor({state: 'visible'});
  assert.equal(await page.locator('#cleanup-media').isDisabled(), true);
  const cleanupsBefore = apiWrites.filter(value => value.endsWith('/cleanup')).length;
  const callsBeforeCleanup = (await state()).mockRequests;
  await page.locator('#cleanup-media').evaluate(button => button.click());
  assert.equal(apiWrites.filter(value => value.endsWith('/cleanup')).length, cleanupsBefore);
  assert.equal((await read(prefix + '/api/capacity')).used_files, capacityBefore.used_files);
  await page.locator('#confirm-cleanup').check();
  const cleaned = await delayedAction(`${prefix}/api/runs/${first.id}/cleanup`, () => page.locator('#cleanup-media').click(), async () => {
    await selectRun(capacityRun.id);
  });
  await page.getByText('本记录的本地样片已清理，报告和检测指标保留；未删除上游任务。', {exact: true}).waitFor();
  assert.ok((await page.locator('#run-id').textContent()).includes(capacityRun.id), 'Late cleanup response preserves the newly selected history');
  assert.ok((await page.locator('#download-report').getAttribute('href')).includes(capacityRun.id));
  await selectRun(first.id);
  assert.equal(await page.locator('#confirm-cleanup').isChecked(), false);
  assert.equal(await page.locator('#run-items img,#run-items video').count(), 0);
  assert.equal(await page.locator('#run-items a').count(), 0);
  assert.equal((await state()).mockRequests, callsBeforeCleanup, 'Local cleanup sends no upstream operation');
  assert.ok(cleaned.items.every(item => item.status === 'passed' && item.media_status === 'removed' && item.media_id === null));
  assert.deepEqual(cleaned.items.map(item => item.task_id), firstReport.items.map(item => item.task_id));
  const removedMedia = await fetch(`${base}${prefix}/api/runs/${first.id}/media/${firstReport.items[0].media_id}`);
  assert.equal(removedMedia.status, 404);
  const capacityAfter = await read(prefix + '/api/capacity');
  assert.equal(capacityAfter.used_files, capacityBefore.used_files - 11);
  assert.ok(capacityAfter.used_bytes < capacityBefore.used_bytes);
  const cleanedReport = await downloadReport('cleaned-report', [syntheticKey, syntheticPrompt, 'signature=', 'do-not-retain']);
  assert.equal(cleanedReport.items.length, 11);
  assert.ok(cleanedReport.items.every(item => item.status === 'passed' && item.media_status === 'removed'));
  const afterCleanupPreview = await preview();
  assert.equal(afterCleanupPreview.items.length, 6);
  assert.equal(afterCleanupPreview.capacity.used_files, capacityAfter.used_files);
  assert.ok(afterCleanupPreview.capacity.available_files >= 6);
  assert.equal(afterCleanupPreview.capacity_warnings.length, 0);
  assert.equal((await state()).mockRequests, callsBeforeCleanup, 'Preview after cleanup is read only');
  checkpoints.push('confirmed_local_cleanup_keeps_reports_no_upstream_releases_capacity');

  const final = await state();
  assert.equal(final.generations, 13);
  assert.equal(final.unexpected, 0);
  assert.ok(final.queryTaskIds.every(task => final.taskIds.includes(task)));
  assert.deepEqual(errors, []);
  mockRequests = final.mockRequests;
  checkpoints.push('390_900_1440_layout_screenshots_focus_no_pageerror_no_browser_storage');
  fs.writeFileSync(path.join(data, 'browser-evidence.json'), JSON.stringify({checkpoints, ...final, errors}, null, 2) + '\n');
})().catch(error => { failure = error; }).finally(async () => {
  clearTimeout(watchdog);
  try { if (browser) await browser.close(); } catch { failure = failure || new Error('Browser cleanup failed'); }
  if (app && app.exitCode === null) {
    app.kill('SIGTERM');
    const ended = await Promise.race([appExit.then(() => true), new Promise(resolve => setTimeout(() => resolve(false), 5000))]);
    if (!ended) { app.kill('SIGKILL'); await appExit; failure = failure || new Error('Owned fixture cleanup failed'); }
  }
  if (failure) { console.error(failure); process.exitCode = 1; }
  console.log(JSON.stringify({status: failure ? 'failed' : 'passed', phase, mockRequests, checkpoints: checkpoints.length, evidence: data, errors}));
});
