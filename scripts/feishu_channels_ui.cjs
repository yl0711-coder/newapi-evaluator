const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'feishu-sync-ui-'));
let app, appExit, browser, failure, mockRequests = 0;
const errors = [];
(async () => {
  const probe = net.createServer(); await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => ['PATH','HOME','LANG','TMPDIR','PYTHONDONTWRITEBYTECODE'].includes(key)));
  app = spawn(process.env.PYTHON_EXECUTABLE || 'python3', ['-B','tests/fixtures/feishu_app.py',String(port)],
    {cwd:root, env:{...env,PLATFORM_DATA_DIR:data,RELAY_LAB_DATA_DIR:data,PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''}, stdio:'ignore'});
  appExit = new Promise(resolve => { app.once('exit', resolve); app.once('error', resolve); });
  let ready = false;
  for (let i=0; i<100; i++) {
    try { if ((await fetch(base+'/api/health')).ok) { ready=true; break; } } catch {}
    if (app.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve,100));
  }
  assert.ok(ready, 'Owned fixture server starts');
  browser = await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL}: {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror', error => errors.push(error.name));
  await page.goto(base+'/channels/');
  await page.getByText('请先填写飞书连接设置。',{exact:true}).waitFor();
  assert.equal(await page.locator('#feishu-sync').isDisabled(),true);
  assert.equal((await (await fetch(base+'/fixture/state')).json()).mockRequests,0);
  await page.locator('#feishu-url').fill('https://example.feishu.cn/base/SyntheticBase?table=tblSynthetic');
  await page.locator('#feishu-app-id').fill('cli_synthetic');
  await page.locator('#feishu-secret').fill('synthetic-application-secret');
  await page.locator('#feishu-save').click();
  await page.getByText('连接设置已保存，点击“手动同步”读取表格。',{exact:true}).waitFor();
  assert.equal(await page.locator('#feishu-secret').inputValue(),'');
  assert.equal((await (await fetch(base+'/fixture/state')).json()).mockRequests,0);
  async function sync() {
    const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/registry/feishu/sync');
    await page.locator('#feishu-sync').click();
    const result = await response;
    await page.waitForFunction(() => !document.querySelector('#feishu-refresh').disabled);
    return result;
  }
  assert.equal((await sync()).status(),200);
  await page.locator('#list h3').getByText('Synthetic',{exact:true}).waitFor();
  assert.match(await page.locator('#feishu-status').textContent(),/新增 1/);
  assert.match(await page.locator('#feishu-issues').textContent(),/recMissing/);
  const channels = async () => (await (await fetch(base+'/api/registry/channels')).json()).channels;
  const original = (await channels())[0];
  assert.equal((await sync()).status(),200);
  assert.equal((await channels()).length,1);
  assert.match(await page.locator('#feishu-status').textContent(),/未变化 1/);
  await fetch(base+'/fixture/rotate',{method:'POST'}); await sync();
  await page.locator('#list h3').getByText('Renamed',{exact:true}).waitFor();
  assert.equal((await channels())[0].id,original.id);
  await fetch(base+'/fixture/offline',{method:'POST'}); await sync();
  assert.equal((await channels())[0].enabled,false);
  assert.match(await page.locator('#feishu-status').textContent(),/停用 1/);
  await fetch(base+'/fixture/failure',{method:'POST'});
  assert.equal((await sync()).status(),502);
  await page.locator('#feishu-refresh').click();
  await page.waitForFunction(() => document.querySelector('#feishu-status').textContent.includes('保留上次结果'));
  assert.equal((await channels())[0].enabled,false);
  for (const width of [1440,390]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth+2),false);
    await page.screenshot({path:path.join(data,`feishu-sync-${width}.png`),fullPage:false});
  }
  const visible = await page.locator('body').innerText();
  const responses = JSON.stringify([await channels(),await (await fetch(base+'/api/registry/feishu/settings')).json()]);
  for (const secret of ['synthetic-application-secret','synthetic-upstream-key','synthetic-rotated-key']) {
    assert.ok(!visible.includes(secret)); assert.ok(!responses.includes(secret));
  }
  assert.deepEqual(errors,[]);
  mockRequests = (await (await fetch(base+'/fixture/state')).json()).mockRequests;
})().catch(error => { failure = error; }).finally(async () => {
  try { if (browser) await browser.close(); } catch { failure = failure || new Error('Browser cleanup failed'); }
  if (app && app.exitCode === null) {
    app.kill('SIGTERM');
    const finished = await Promise.race([appExit.then(()=>true),new Promise(resolve=>setTimeout(()=>resolve(false),5000))]);
    if (!finished) { app.kill('SIGKILL'); await appExit; failure = failure || new Error('Fixture cleanup timed out'); }
  }
  if (failure) { console.error(failure); process.exitCode=1; }
  console.log(JSON.stringify({status:failure ? 'failed':'passed',mockRequests,evidence:data,errors}));
});
