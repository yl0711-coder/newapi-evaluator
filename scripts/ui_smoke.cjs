const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const data = fs.mkdtempSync(path.join(os.tmpdir(), 'workbench-ui-'));
const python = process.env.PYTHON_EXECUTABLE || path.join(root,'.venv','bin','python');
const requests = [];
const heldImageResponses = new Set();
let openEndedImages = 0;
let app, appExit, appError, browser, logs = '';
const upstream = http.createServer(async (req, res) => {
  const parts = []; for await (const chunk of req) parts.push(chunk);
  const body = JSON.parse(Buffer.concat(parts)); requests.push({key:req.headers.authorization, body});
  if (req.url.endsWith('/images/generations')) {
    if (req.headers.authorization === 'Bearer synthetic-image-rejected') {
      res.writeHead(401, {'Content-Type':'application/json'});
      res.end(JSON.stringify({error:{message:'synthetic rejected credential'}})); return;
    }
    const image = fs.readFileSync(path.join(root,'tests/fixtures/image_quality/response.json'));
    if (body.prompt === 'Synthetic image fixture with geometric shapes.') {
      res.writeHead(200, {'Content-Type':'application/json','x-request-id':'synthetic-image-request'});
      res.write(image);
      openEndedImages++;
      heldImageResponses.add(res);
      res.on('close',()=>heldImageResponses.delete(res));
      return;
    }
    const finish = () => {res.writeHead(200, {'Content-Type':'application/json','x-request-id':'synthetic-image-request'}); res.end(image);};
    if (body.prompt === 'Synthetic delayed image fixture.') {
      const timer = setTimeout(finish, 5000); res.on('close',()=>clearTimeout(timer));
    } else finish();
    return;
  }
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
  const portCheck = net.createServer();
  await new Promise((resolve,reject)=>{portCheck.once('error',reject);portCheck.listen(appPort,'127.0.0.1',resolve);});
  await new Promise(resolve=>portCheck.close(resolve));
  app = spawn(python,['run.py','--port',String(appPort)],{cwd:root,env:{...process.env,PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:['ignore','pipe','pipe']});
  appExit = new Promise(resolve=>{app.once('exit',resolve);app.once('error',error=>{appError=error;resolve();});});
  app.stderr.on('data',chunk=>logs+=chunk.toString());
  let ready = false;
  for (let i=0; i<100; i++) {
    if(appError || app.exitCode!==null || app.signalCode!==null) throw new Error('Owned UI server failed to start');
    try { const r = await fetch(base+'/api/health'); if(r.ok) {ready=true;break;} } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.ok(ready && !appError && app.exitCode===null && app.signalCode===null,'Owned UI server failed to start');
  browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');
  await page.locator('#features a').nth(5).waitFor();
  assert.deepEqual(await page.locator('#features a').evaluateAll(cards=>cards.map(card=>card.getAttribute('href'))),
    ['/admission/','/stability/','/reasoning/','/capacity/','/diagnosis/','/image-quality/']);
  await page.locator('#features a[href="/diagnosis/"]').click();
  await page.getByText('准备就绪。',{exact:false}).waitFor();
  await page.goto(base+'/');
  await page.locator('#features a[href="/image-quality/"]').click();
  await page.locator('#prompt').waitFor();
  await page.goto(base+'/');
  await page.locator('#features a').nth(5).waitFor();
  await page.screenshot({path:path.join(data,'workbench-home-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
  await page.screenshot({path:path.join(data,'workbench-home-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(base+'/channels/'); await page.locator('#add').click();
  await page.locator('#url').fill(upstreamUrl); await page.locator('#name').fill('UI reference');
  await page.locator('#key').fill('ui-saved-reference-key'); await page.locator('#multiplier').fill('0.7');
  await page.locator('#state').selectOption('online');
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
  assert.equal(await page.locator('#summary-cards .summary-card').count(),8);
  assert.equal(await page.locator('#pair-summary tr').count(),5);
  assert.equal(await page.locator('#results .evidence-card').count(),10);
  assert.ok(!(await page.locator('#headline').textContent()).includes('测试进行中'));
  const historyPanel = page.locator('#report-history-panel'), historySummary = historyPanel.locator('summary');
  assert.equal(await historyPanel.getAttribute('open'),null); await historySummary.click();
  await page.locator('#report-history article').waitFor({state:'visible'});
  await historySummary.click(); await page.locator('#report-history article').waitFor({state:'hidden'});
  assert.equal(requests.length,20); assert.equal(requests.filter(x=>x.key==='Bearer ui-ephemeral-candidate-key').length,10);
  assert.equal(requests.filter(x=>x.key==='Bearer ui-saved-reference-key').length,10);
  assert.equal((await (await fetch(base+'/api/registry/channels')).json()).channels.length,1);
  const admissionReports = await (await fetch(base+'/admission/api/reports')).json(); assert.equal(admissionReports.reports.length,1);
  assert.ok(!JSON.stringify(admissionReports).includes('ui-ephemeral-candidate-key')); assert.ok(!JSON.stringify(admissionReports).includes('ui-saved-reference-key'));
  const downloadPromise = page.waitForEvent('download'); await page.locator('#export-json').click();
  const download = await downloadPromise; const report = fs.readFileSync(await download.path(),'utf8');
  assert.ok(!report.includes('ui-ephemeral-candidate-key')); assert.ok(!report.includes('ui-saved-reference-key'));
  const ordinaryPromise = page.waitForEvent('download'); await page.locator('#export-html').click();
  const ordinaryDownload = await ordinaryPromise; const ordinary = fs.readFileSync(await ordinaryDownload.path(),'utf8');
  assert.ok(!ordinary.includes('Test answer 391')); assert.ok(!ordinary.includes(upstreamUrl));
  await page.screenshot({path:path.join(data,'admission-desktop.png'),fullPage:true});
  await page.goto(base+'/reasoning/'); await page.locator('#channel_id').selectOption(String(channelId)); await page.locator('#model').fill('demo-model');
  await page.locator('#start_btn').click(); await page.locator('#results').waitFor({state:'visible'}); assert.equal(requests.length,25);
  assert.equal(await page.locator('#details .result-card').count(),5);
  await page.screenshot({path:path.join(data,'reasoning-desktop.png'),fullPage:true});
  await page.goto(base+'/stability/'); await page.locator('[data-view="channels"]').click(); await page.locator('#new-channel').click();
  await page.locator('#channel-name').fill('UI scheduled target'); await page.locator('#channel-registry').selectOption(String(channelId)); await page.locator('#channel-model').fill('demo-model');
  await page.locator('#channel-form button[type="submit"]').click(); await page.locator('#channel-dialog').waitFor({state:'hidden'});
  assert.equal((await (await fetch(base+'/stability/api/channels')).json()).channels.length,1);
  assert.equal((await (await fetch(base+'/stability/api/schedules')).json()).schedules.length,0); assert.equal(requests.length,25);
  await page.screenshot({path:path.join(data,'stability-desktop.png'),fullPage:true});
  await page.goto(base+'/capacity/'); await page.locator('.platform-nav').waitFor();
  assert.equal(await page.locator('.platform-nav a[aria-current="page"]').textContent(),'\u4e2d\u8f6c\u7ad9\u6781\u9650\u6d4b\u8bd5');
  await page.locator('#preset').selectOption('quick'); await page.locator('#preset').dispatchEvent('change');
  await page.locator('#test-form button[type="submit"]').click();
  await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='\u5df2\u5b8c\u6210',{},{timeout:40000});
  assert.equal(await page.locator('#downloads').getAttribute('hidden'),null);
  assert.ok(await page.locator('#history .history-card').count()>=1);
  await page.screenshot({path:path.join(data,'capacity-desktop.png'),fullPage:true});
  await page.goto(base+'/image-quality/'); await page.locator('.platform-nav').waitFor();
  await page.locator('#base-url').fill(upstreamUrl);
  await page.locator('#api-key').fill('synthetic-image-key');
  await page.locator('#prompt').fill('Synthetic image fixture with geometric shapes.');
  const imageCallsBefore = requests.length;
  await page.locator('#generate').click();
  assert.equal(requests.length,imageCallsBefore);
  await page.locator('#confirm-live').check(); await page.locator('#generate').click();
  await page.waitForFunction(()=>document.querySelectorAll('.sample-card').length===1);
  await page.locator('.sample-image').evaluate(img=>img.decode());
  assert.equal(await page.locator('.sample-image').evaluate(img=>img.naturalWidth),64);
  assert.equal(openEndedImages,1);
  await page.getByText('服务端总耗时', {exact:true}).waitFor();
  assert.equal(await page.locator('#api-key').inputValue(),'');
  assert.equal(await page.locator('#confirm-live').isChecked(),false);
  assert.equal(requests.at(-1).body.model,'gpt-image-2');
  assert.equal(requests.at(-1).body.n,1);
  await page.getByLabel('样本 1 构图').selectOption('4');
  const imageReportPromise = page.waitForEvent('download');
  await page.getByRole('button',{name:'导出指标',exact:true}).click();
  const imageReport = JSON.parse(fs.readFileSync(await (await imageReportPromise).path(),'utf8'));
  assert.equal(imageReport.scores.composition,4);
  for (const forbidden of ['synthetic-image-key','Synthetic image fixture',upstreamUrl,'b64_json']) {
    assert.ok(!JSON.stringify(imageReport).includes(forbidden));
  }
  const imageDownloadPromise = page.waitForEvent('download');
  await page.getByRole('link',{name:'下载 PNG',exact:true}).click();
  const imageBytes = fs.readFileSync(await (await imageDownloadPromise).path());
  assert.equal(imageBytes.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
  await page.locator('#api-key').fill('synthetic-image-rejected');
  await page.locator('#confirm-live').check(); await page.locator('#generate').click();
  await page.waitForFunction(()=>document.querySelectorAll('.sample-card').length===2);
  assert.match(await page.locator('.sample-card').last().textContent(),/接口拒绝鉴权/);
  await page.locator('#api-key').fill('synthetic-image-key');
  await page.locator('#prompt').fill('Synthetic delayed image fixture.');
  await page.locator('#confirm-live').check(); await page.locator('#generate').click();
  await page.locator('#cancel').click();
  await page.waitForFunction(()=>document.querySelector('#form-error').textContent.includes('停止本地等待'));
  assert.equal(await page.locator('#generate').isEnabled(),true);
  await page.screenshot({path:path.join(data,'image-quality-desktop.png'),fullPage:true});
  await page.locator('#clear').click(); assert.equal(await page.locator('.sample-card').count(),0);
  await page.reload(); assert.equal(await page.locator('#api-key').inputValue(),'');
  for (const url of ['/','/channels/','/admission/','/reasoning/','/stability/','/capacity/','/image-quality/']) {
    await page.setViewportSize({width:390,height:844}); await page.goto(base+url); await page.locator('.platform-nav').waitFor();
    const overflow = await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>window.innerWidth+2,width:document.documentElement.scrollWidth,
      elements:[...document.querySelectorAll('*')].filter(element=>element.getBoundingClientRect().right>window.innerWidth+2).sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right).slice(0,12).map(element=>`${element.tagName.toLowerCase()}${element.id?'#'+element.id:''}${element.className&&typeof element.className==='string'?'.'+element.className.trim().replace(/\s+/g,'.'):''}:${Math.round(element.getBoundingClientRect().right)}`)}));
    await page.screenshot({path:path.join(data,(url.replaceAll('/','')||'home')+'-mobile.png'),fullPage:true,animations:'disabled'});
    assert.equal(overflow.overflow,false,`Mobile overflow: ${url} (${overflow.width}px; ${overflow.elements.join(', ')})`);
  }
  assert.deepEqual(errors,[]); console.log(JSON.stringify({status:'passed',mockRequests:requests.length,artifacts:data,checks:'frontend channel CRUD, ephemeral admission, report export, reasoning, explicit targets, integrated capacity Mock, image-quality confirmation/generation/export/cancel, desktop/mobile'}));
})().catch(error=>{console.error(error);if(logs)console.error(logs);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  if(app) { if(!appError && app.exitCode===null && app.signalCode===null) app.kill('SIGTERM'); await appExit; }
  for (const response of heldImageResponses) response.destroy();
  upstream.close();
});
