const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'workbench-ui-'));
const requests = [];
let app, browser;
const upstream = http.createServer(async (req, res) => {
  const parts = []; for await (const chunk of req) parts.push(chunk);
  const body = JSON.parse(Buffer.concat(parts)); requests.push({key:req.headers.authorization, body});
  if (body.stream) {
    res.writeHead(200, {'Content-Type':'text/event-stream'});
    res.write('data: ' + JSON.stringify({model:body.model,choices:[{delta:{content:'Test answer '},finish_reason:null}]}) + '\n\n');
    res.end('data: ' + JSON.stringify({model:body.model,choices:[{delta:{content:'391'},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:6}}) + '\n\ndata: [DONE]\n\n');
  } else {
    res.writeHead(200, {'Content-Type':'application/json'});
    res.end(JSON.stringify({choices:[{message:{content:'391',reasoning_content:'Visible reasoning for this fixture.'},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:8}}));
  }
});

(async () => {
  await new Promise(resolve => upstream.listen(0,'127.0.0.1',resolve));
  const upstreamUrl = `http://127.0.0.1:${upstream.address().port}/v1`;
  const appPort = Number(process.env.UI_TEST_PORT || 18090), base = `http://127.0.0.1:${appPort}`;
  app = spawn(path.join(root,'.venv','bin','python'),['run.py','--port',String(appPort)],{cwd:root,env:{...process.env,PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:['ignore','pipe','pipe']});
  let logs = ''; app.stderr.on('data',chunk=>logs+=chunk.toString());
  for (let i=0; i<100; i++) { try { const r = await fetch(base+'/api/health'); if(r.ok) break; } catch {} await new Promise(resolve=>setTimeout(resolve,100)); }
  browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/channels/'); await page.locator('#add').click();
  await page.locator('#url').fill(upstreamUrl); await page.locator('#name').fill('UI reference');
  await page.locator('#key').fill('ui-saved-reference-key'); await page.locator('#multiplier').fill('0.7');
  await page.locator('#save').click(); await page.locator('#editor').waitFor({state:'hidden'});
  await page.getByRole('button',{name:'编辑',exact:true}).click(); assert.equal(await page.locator('#key').inputValue(),'');
  await page.locator('#note').fill('Edited from browser'); await page.locator('#save').click(); await page.locator('#editor').waitFor({state:'hidden'});
  const channels = await (await fetch(base+'/api/registry/channels')).json(); assert.equal(channels.channels.length,1); assert.equal(channels.channels[0].status,'online');
  assert.ok(!JSON.stringify(channels).includes('ui-saved-reference-key'));
  const channelId = channels.channels[0].id;
  await page.goto(base+'/admission/'); await page.locator('#candidate-import').fill(JSON.stringify({base_url:upstreamUrl,api_key:'ui-ephemeral-candidate-key'}));
  await page.locator('#extract').click(); await page.waitForFunction(()=>document.querySelector('#candidate-import').value==='');
  await page.locator('#candidate-model').fill('demo-model'); await page.locator('#reference-model').fill('demo-model');
  await page.locator('#reference-id').selectOption(String(channelId));
  await page.locator('#start').click(); await page.waitForFunction(()=>document.querySelector('#run-status').textContent.includes('本轮测试完成'),{},{timeout:40000});
  assert.equal(requests.length,10); assert.equal(requests.filter(x=>x.key==='Bearer ui-ephemeral-candidate-key').length,5);
  assert.equal(requests.filter(x=>x.key==='Bearer ui-saved-reference-key').length,5);
  assert.equal((await (await fetch(base+'/api/registry/channels')).json()).channels.length,1);
  const downloadPromise = page.waitForEvent('download'); await page.locator('#export-json').click();
  const download = await downloadPromise; const report = fs.readFileSync(await download.path(),'utf8');
  assert.ok(!report.includes('ui-ephemeral-candidate-key')); assert.ok(!report.includes('ui-saved-reference-key'));
  await page.screenshot({path:path.join(data,'admission-desktop.png'),fullPage:true});
  await page.goto(base+'/reasoning/'); await page.locator('#channel_id').selectOption(String(channelId)); await page.locator('#model').fill('demo-model');
  await page.locator('#start_btn').click(); await page.locator('#results').waitFor({state:'visible'}); assert.equal(requests.length,15);
  assert.equal(await page.locator('#details .result-card').count(),5);
  await page.screenshot({path:path.join(data,'reasoning-desktop.png'),fullPage:true});
  await page.goto(base+'/stability/'); await page.locator('[data-view="channels"]').click(); await page.locator('#new-channel').click();
  await page.locator('#channel-name').fill('UI scheduled target'); await page.locator('#channel-registry').selectOption(String(channelId)); await page.locator('#channel-model').fill('demo-model');
  await page.locator('#channel-form button[type="submit"]').click(); await page.locator('#channel-dialog').waitFor({state:'hidden'});
  assert.equal((await (await fetch(base+'/stability/api/channels')).json()).channels.length,1);
  assert.equal((await (await fetch(base+'/stability/api/schedules')).json()).schedules.length,0); assert.equal(requests.length,15);
  await page.screenshot({path:path.join(data,'stability-desktop.png'),fullPage:true});
  for (const url of ['/','/channels/','/admission/','/reasoning/','/stability/']) {
    await page.setViewportSize({width:390,height:844}); await page.goto(base+url); await page.locator('.platform-nav').waitFor();
    const overflow = await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+2);
    assert.equal(overflow,false,`Mobile overflow: ${url}`);
    await page.screenshot({path:path.join(data,(url.replaceAll('/','')||'home')+'-mobile.png'),fullPage:true,animations:'disabled'});
  }
  assert.deepEqual(errors,[]); console.log(JSON.stringify({status:'passed',mockRequests:requests.length,artifacts:data,checks:'frontend channel CRUD, ephemeral admission, report export, reasoning, explicit targets, desktop/mobile'}));
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  if(app) { app.kill('SIGTERM'); await new Promise(resolve=>app.once('exit',resolve)); }
  upstream.close();
});
