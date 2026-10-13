const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const evidenceRoot = fs.realpathSync(process.env.UI_EVIDENCE_ROOT || os.tmpdir());
assert.ok(evidenceRoot !== root && !evidenceRoot.startsWith(root + path.sep),'Browser evidence belongs outside source');
const data = fs.mkdtempSync(path.join(evidenceRoot, 'workbench-ui-'));
const python = process.env.PYTHON_EXECUTABLE || path.join(root,'.venv','bin','python');
const requests = [];
const heldImageResponses = new Set();
let openEndedImages = 0;
let app, appExit, appError, browser, logs = '', phase = 'startup';
const startupFailure = () => Object.assign(new Error('owned_ui_server_start_failed'),{code:'OWNED_UI_START_FAILED'});
let rejectFixture;
const fixtureFailure = new Promise((resolve,reject)=>{rejectFixture=reject;});
const upstream = http.createServer(async (req, res) => {
  try {
  const parts = []; for await (const chunk of req) parts.push(chunk);
  const body = JSON.parse(Buffer.concat(parts)); requests.push({key:req.headers.authorization, body, path:req.url});
  if (req.url.endsWith('/responses')) {
    assert.equal(body.model,'gpt-6-astra'); assert.equal(body.max_output_tokens,4096);
    assert.equal(body.reasoning.effort,'low'); assert.equal(body.store,false);
    assert.ok(!('max_tokens' in body) && !('max_completion_tokens' in body) && !('messages' in body));
    if (!body.stream) {
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify({status:'completed',model:body.model,output:[{type:'message',content:[{type:'output_text',text:'Synthetic Astra answer'}]}],
        usage:{input_tokens:12,output_tokens:42,output_tokens_details:{reasoning_tokens:40}}})); return;
    }
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    const values=[{type:'response.created',response:{model:body.model,status:'in_progress'}},
      {type:'response.output_text.delta',delta:'Synthetic Astra '},{type:'response.output_text.delta',delta:'answer'},
      {type:'response.completed',response:{status:'completed',model:body.model,
        usage:{input_tokens:12,output_tokens:42,output_tokens_details:{reasoning_tokens:40}}}}];
    res.end(values.map(value=>'data: '+JSON.stringify(value)+'\n\n').join('')); return;
  }
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
  } catch(error) {res.destroy();rejectFixture(error);}
});

Promise.race([fixtureFailure,(async () => {
  await new Promise(resolve => upstream.listen(0,'127.0.0.1',resolve));
  const upstreamUrl = `http://127.0.0.1:${upstream.address().port}/v1`;
  const appPort = Number(process.env.UI_TEST_PORT || 18090), base = `http://127.0.0.1:${appPort}`;
  const portCheck = net.createServer();
  await new Promise((resolve,reject)=>{portCheck.once('error',reject);portCheck.listen(appPort,'127.0.0.1',resolve);});
  await new Promise(resolve=>portCheck.close(resolve));
  const appEnv = Object.fromEntries(['PATH','HOME','LANG','LC_ALL','SYSTEMROOT'].filter(name=>process.env[name]).map(name=>[name,process.env[name]]));
  Object.assign(appEnv,{TMPDIR:data,PYTHONDONTWRITEBYTECODE:'1',PLATFORM_DATA_DIR:data,RELAY_LAB_DATA_DIR:path.join(data,'relay-lab'),
    PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''});
  app = spawn(python,['-B','run.py','--port',String(appPort)],{cwd:root,env:appEnv,stdio:['ignore','pipe','pipe']});
  app.stdout.resume();
  appExit = new Promise(resolve=>{app.once('exit',resolve);app.once('error',error=>{appError=error;resolve();});});
  app.stderr.on('data',chunk=>logs+=chunk.toString());
  let ready = false;
  for (let i=0; i<100; i++) {
    if(appError || app.exitCode!==null || app.signalCode!==null) throw startupFailure();
    try { const r = await fetch(base+'/api/health'); if(r.ok) {ready=true;break;} } catch {}
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  if(!ready || appError || app.exitCode!==null || app.signalCode!==null) throw startupFailure();
  browser = await chromium.launch({headless:true, ...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL} : {})});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors = []; page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');
  await page.locator('#features a').nth(8).waitFor();
  assert.deepEqual(await page.locator('#features a').evaluateAll(cards=>cards.map(card=>card.getAttribute('href'))),
    ['/admission/protocol/','/admission/','/stability/','/reasoning/','/capacity/','/diagnosis/','/image-quality/', '/integrity/','/token-usage/']);
  await page.locator('#features a[href="/admission/protocol/"]').click();
  await page.getByText('先获取或输入模型，再预览并开始检测。',{exact:true}).waitFor();
  assert.equal(await page.locator('.platform-nav a[aria-current="page"]').textContent(),'模型与协议检测');
  await page.locator('.platform-nav').getByRole('link',{name:'模型测试工作台',exact:true}).click();
  await page.locator('#features a[href="/diagnosis/"]').click();
  await page.getByText('准备就绪。',{exact:false}).waitFor();
  await page.goto(base+'/');
  await page.locator('#features a[href="/image-quality/"]').click();
  await page.locator('#prompt').waitFor();
  await page.goto(base+'/');
  await page.locator('#features a').nth(8).waitFor();
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
  const catalog = await fetch(base+'/api/model-coverage/models',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({model:'demo-model',label:'Synthetic demo model',family:'synthetic',protocol:'openai'})});
  assert.ok(catalog.ok,'Synthetic model is registered for trusted Token comparisons');
  for (const [model,protocol] of [['demo-model','openai'],['gpt-6-astra','responses']]) {
    const binding = await fetch(base+'/api/token-usage/bindings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model,protocol,reference_channel_id:channelId,confirm_trusted:true})});
    assert.ok(binding.ok,'Token reference trust is configured explicitly');
  }
  assert.equal(requests.length,0,'Catalog and trusted binding configuration never sends tests');
  async function submitAdmission() {
    const accepted = page.waitForResponse(response=>new URL(response.url()).pathname==='/admission/api/jobs' && response.request().method()==='POST');
    await page.locator('#start').click();
    const response=await accepted; assert.equal(response.status(),202);
    const created=await response.json(); assert.ok(Number.isInteger(created.id)); return created.id;
  }
  phase = 'admission';
  await page.goto(base+'/admission/'); await page.locator('#candidate-import').fill(JSON.stringify({base_url:upstreamUrl,api_key:'ui-ephemeral-candidate-key'}));
  await page.locator('#extract').click(); await page.waitForFunction(()=>document.querySelector('#candidate-import').value==='');
  await page.locator('#candidate-model').fill('demo-model'); await page.locator('#reference-model').fill('demo-model');
  await page.locator('#reference-id').selectOption(String(channelId));
  const admissionJob = await submitAdmission();
  await page.waitForFunction(id=>{const text=document.querySelector('#run-status').textContent;return text.includes(`任务 #${id} ·`) && text.includes('已完成');},admissionJob,{timeout:40000});
  assert.equal(await page.locator('#summary-cards .summary-card').count(),8);
  assert.equal(await page.locator('#pair-summary tr').count(),6);
  assert.equal(await page.locator('#results .evidence-card').count(),12);
  assert.ok(!(await page.locator('#headline').textContent()).includes('测试进行中'));
  const historyPanel = page.locator('#report-history-panel'), historySummary = historyPanel.locator('summary');
  assert.equal(await historyPanel.getAttribute('open'),null); await historySummary.click();
  await page.locator('#report-history article').waitFor({state:'visible'});
  await historySummary.click(); await page.locator('#report-history article').waitFor({state:'hidden'});
  const tokenRequest = item => (typeof item.body.input === 'string' ? item.body.input : item.body.messages?.at(-1)?.content || '').startsWith('Read the synthetic inventory below. Reply only OK.\nrecord-0000:');
  assert.equal(requests.length,26); assert.equal(requests.filter(x=>x.key==='Bearer ui-ephemeral-candidate-key').length,13);
  assert.equal(requests.filter(x=>x.key==='Bearer ui-saved-reference-key').length,13);
  assert.equal(requests.filter(tokenRequest).length,2,'Token专项发送一次双端题对');
  assert.equal(requests.filter(item=>!tokenRequest(item)).length,24,'默认六题两轮双端仍发送24次');
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
  await page.locator('#start_btn').click(); await page.locator('#results').waitFor({state:'visible'}); assert.equal(requests.length,31);
  assert.equal(await page.locator('#details .result-card').count(),5);
  await page.screenshot({path:path.join(data,'reasoning-desktop.png'),fullPage:true});
  await page.goto(base+'/stability/'); await page.locator('[data-view="channels"]').click(); await page.locator('#new-channel').click();
  await page.locator('#channel-name').fill('UI scheduled target'); await page.locator('#channel-registry').selectOption(String(channelId)); await page.locator('#channel-model').fill('demo-model');
  await page.locator('#channel-form button[type="submit"]').click(); await page.locator('#channel-dialog').waitFor({state:'hidden'});
  assert.equal((await (await fetch(base+'/stability/api/channels')).json()).channels.length,1);
  assert.equal((await (await fetch(base+'/stability/api/schedules')).json()).schedules.length,0); assert.equal(requests.length,31);
  await page.screenshot({path:path.join(data,'stability-desktop.png'),fullPage:true});
  phase = 'admission_astra';
  await page.goto(base+'/admission/');
  await page.locator('#preset option[value="gpt-6-astra"]').waitFor({state:'attached'});
  await page.locator('#preset').selectOption('gpt-6-astra');
  for (const side of ['candidate','reference']) {
    assert.equal(await page.locator(`#${side}-protocol`).inputValue(),'responses');
    assert.equal(await page.locator(`#${side}-protocol`).isDisabled(),true);
  }
  await page.locator('#candidate-model').fill('demo-model');
  assert.equal(await page.locator('#candidate-protocol').isEnabled(),true);
  await page.locator('#candidate-model').fill('gpt-6-astra');
  assert.equal(await page.locator('#candidate-protocol').isDisabled(),true);
  await page.locator('#candidate-url').fill(upstreamUrl+'/responses');
  await page.locator('#candidate-name').fill('UI Astra candidate');
  await page.locator('#candidate-key').fill('ui-ephemeral-astra-key');
  await page.locator('#reference-id').selectOption(String(channelId));
  await page.locator('#rounds').selectOption('1');
  const astraJob = await submitAdmission();
  await page.waitForFunction(id=>{
    const text=document.querySelector('#run-status').textContent;
    return document.querySelector('#stop').dataset.jobId===String(id) && text.includes(`任务 #${id} ·`) && text.includes('已完成');
  },astraJob,{timeout:30000});
  const astraTask=await (await fetch(base+`/admission/api/jobs/${astraJob}`)).json();
  assert.equal(astraTask.status,'succeeded');
  assert.ok((await page.locator('#run-status').textContent()).includes(astraTask.candidate.name));
  assert.ok((await page.locator('#run-status').textContent()).includes(astraTask.reference.name));
  const astraCalls=requests.filter(item=>item.body.model==='gpt-6-astra');
  assert.equal(astraCalls.length,14); assert.ok(astraCalls.every(item=>item.path==='/v1/responses'));
  assert.equal(astraCalls.filter(tokenRequest).length,2,'Astra adds two specialized Token requests');
  assert.equal(astraCalls.filter(item=>!tokenRequest(item)).length,12,'Astra retains twelve six-question requests');
  assert.ok(JSON.stringify(astraCalls.filter(tokenRequest)[0].body)===JSON.stringify(astraCalls.filter(tokenRequest)[1].body),
    'Token comparison uses identical input, model, protocol and generation settings');
  assert.equal(await page.locator('#pair-summary tr').count(),6);
  const astraJsonEvent=page.waitForEvent('download'); await page.locator('#export-json').click();
  const astraJson=JSON.parse(fs.readFileSync(await (await astraJsonEvent).path(),'utf8'));
  assert.equal(astraJson.candidate.protocol,'responses'); assert.equal(astraJson.reference.protocol,'responses');
  assert.ok(astraJson.measurements.every(item=>item.ok && item.reasoning_tokens===40 && item.max_output_tokens===4096 && !item.speed_data_valid));
  assert.ok(!JSON.stringify(astraJson).includes('ui-ephemeral-astra-key'));
  assert.equal(astraJson.token_usage.conclusion,'normal');
  assert.equal(astraJson.token_usage.requests_sent,2);
  const astraHtmlEvent=page.waitForEvent('download'); await page.locator('#export-technical-html').click();
  const astraHtml=fs.readFileSync(await (await astraHtmlEvent).path(),'utf8');
  assert.ok(astraHtml.includes('推理 Token') && astraHtml.includes('4096') && astraHtml.includes('Synthetic Astra answer'));
  assert.ok(astraHtml.includes('实际输出 Token：42') && astraHtml.includes('最大输出 Token（含推理）：4096'));
  const astraOrdinaryEvent=page.waitForEvent('download'); await page.locator('#export-html').click();
  const astraOrdinary=fs.readFileSync(await (await astraOrdinaryEvent).path(),'utf8');
  assert.ok(!astraOrdinary.includes('Synthetic Astra answer'));
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
  await page.screenshot({path:path.join(data,'admission-astra-mobile.png'),fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  await page.screenshot({path:path.join(data,'admission-astra-desktop.png'),fullPage:true});
  phase = 'admission_cancel';
  await page.locator('#candidate-key').fill('ui-ephemeral-astra-key');
  const cancelJob = await submitAdmission();
  await page.waitForFunction(id=>document.querySelector('#run-status').textContent.includes(`任务 #${id} · `),cancelJob);
  await page.locator('#stop').waitFor({state:'visible'}); await page.locator('#stop').click();
  await page.waitForFunction(id=>{const text=document.querySelector('#run-status').textContent;return text.includes(`任务 #${id} ·`) && text.includes('已停止');},cancelJob);
  assert.equal(await page.locator('#candidate-protocol').isDisabled(),true);
  phase = 'capacity_and_image';
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
  phase = 'shared_theme_responsive';
  for (const width of [390,900,1440,1920]) {
    let theme;
    for (const url of ['/','/channels/','/admission/protocol/','/admission/','/reasoning/','/stability/','/capacity/','/diagnosis/','/image-quality/','/token-usage/']) {
      phase = `shared_theme_responsive:${url}:${width}`;
      await page.setViewportSize({width,height:1000}); await page.goto(base+url); await page.locator('.platform-nav').waitFor();
      const appearance = await page.evaluate(() => {
        const body=getComputedStyle(document.body), heading=getComputedStyle(document.querySelector('h1'));
        return {font:body.fontFamily,background:body.backgroundColor,ink:body.color,
          headingFont:heading.fontFamily,headingSize:heading.fontSize};
      });
      if (!theme) theme=appearance;
      assert.deepEqual(appearance,theme,`Shared page theme: ${url} at ${width}px`);
      const overflow = await page.evaluate(()=>({overflow:document.documentElement.scrollWidth>window.innerWidth+2,width:document.documentElement.scrollWidth,
        elements:[...document.querySelectorAll('*')].filter(element=>element.getBoundingClientRect().right>window.innerWidth+2).sort((a,b)=>b.getBoundingClientRect().right-a.getBoundingClientRect().right).slice(0,12).map(element=>`${element.tagName.toLowerCase()}${element.id?'#'+element.id:''}${element.className&&typeof element.className==='string'?'.'+element.className.trim().replace(/\s+/g,'.'):''}:${Math.round(element.getBoundingClientRect().right)}`)}));
      await page.screenshot({path:path.join(data,(url.replaceAll('/','')||'home')+`-${width}.png`),fullPage:true,animations:'disabled'});
      assert.equal(overflow.overflow,false,`Mobile overflow: ${url} (${overflow.width}px; ${overflow.elements.join(', ')})`);
    }
  }
  await page.setViewportSize({width:390,height:844});
  await page.goto(base+'/stability/');
  await page.locator('[data-view="schedules"]').click(); await page.locator('#new-schedule').click();
  await page.locator('#schedule-dialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#schedule-dialog').evaluate(el=>el.scrollWidth>el.clientWidth+2),false,'Schedule form fits mobile dialog');
  await page.locator('[data-close="schedule-dialog"]').last().click();
  await page.goto(base+'/capacity/');
  await page.waitForFunction(()=>document.querySelector('#connection').textContent.includes('已连接'));
  for (const width of [390,800,900]) {
    await page.setViewportSize({width,height:1000});
    for (const mode of ['faders','mixed_burst']) {
      await page.locator('#load-mode').selectOption(mode);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false,`Capacity ${mode} at ${width}px`);
      if(mode==='faders') {
        for(const profile of ['short','medium','long']) {
          const range=page.locator(`#fader-${profile}`);
          await range.focus(); await page.keyboard.press('ArrowRight');
          assert.equal(await range.inputValue(),'1');
          assert.equal(await page.locator(`#fader-${profile}-number`).inputValue(),'1');
          await page.keyboard.press('ArrowLeft');
        }
      }
      await page.screenshot({path:path.join(data,`capacity-${mode}-${width}.png`),fullPage:true,animations:'disabled'});
    }
  }
  const environment=page.locator('input[name="environment"][value="mock"]');
  await environment.focus(); await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('input[name="environment"][value="live"]').isChecked(),true);
  assert.equal(await page.locator('input[name="environment"][value="live"] + span').evaluate(el=>getComputedStyle(el).outlineStyle),'solid');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await environment.isChecked(),true);
  await page.locator('#preset').selectOption('mixed_burst');
  for (const [index,profile] of ['short','medium','long'].entries()) {
    await page.locator(`#burst-${profile}-count`).fill('1');
    await page.locator(`#burst-${profile}-limit`).fill(String([64,128,256][index]));
  }
  await page.locator('#burst-capacity').fill('2');
  await page.locator('#test-form button[type="submit"]').click();
  await page.waitForFunction(()=>document.querySelector('#result-status').textContent==='已完成',{},{timeout:40000});
  for (const width of [390,800,900]) {
    await page.setViewportSize({width,height:1000});
    await page.locator('#burst-panel').waitFor({state:'visible'});
    assert.equal(await page.locator('#burst-rows tr').count(),3);
    assert.equal(await page.locator('#burst-chart svg').isVisible(),true);
    assert.equal(await page.locator('#downloads a').first().evaluate(el=>getComputedStyle(el).color),
      await page.locator('.platform-nav a[aria-current="page"]').evaluate(el=>getComputedStyle(el).color),
      'Result downloads use the shared action color');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false,`Capacity batch results at ${width}px`);
    await page.screenshot({path:path.join(data,`capacity-batch-result-${width}.png`),fullPage:true,animations:'disabled'});
  }
  assert.deepEqual(errors,[]);
  const result = {status:'passed',mockRequests:requests.length,artifacts:data,checks:'frontend channel CRUD, ephemeral admission, trusted Token comparison, job-bound report export/cancel, reasoning, explicit targets, integrated capacity Mock, image-quality confirmation/generation/export/cancel, desktop/mobile'};
  fs.writeFileSync(path.join(data,'result.json'),JSON.stringify(result,null,2)); console.log(JSON.stringify(result));
})()]).catch(error=>{
  let errorCode='owned_browser_operation_failed';
  if(error.code==='EADDRINUSE') errorCode='ui_port_in_use';
  else if(error.code==='OWNED_UI_START_FAILED') errorCode='owned_ui_server_start_failed';
  else if(error instanceof assert.AssertionError) errorCode='browser_assertion_failed';
  const result={status:'failed',phase,error_name:error.name,error:errorCode,mockRequests:requests.length,stderrBytes:Buffer.byteLength(logs),artifacts:data};
  fs.writeFileSync(path.join(data,'result.json'),JSON.stringify(result,null,2)); console.error(JSON.stringify(result));process.exitCode=1;
}).finally(async()=>{
  if(browser) await browser.close();
  if(app) {
    if(!appError && app.exitCode===null && app.signalCode===null) app.kill('SIGTERM');
    let cleanupTimer;
    const ended = await Promise.race([appExit.then(()=>true),new Promise(resolve=>{cleanupTimer=setTimeout(()=>resolve(false),5000);})]);
    clearTimeout(cleanupTimer);
    if(!ended) {app.kill('SIGKILL');await appExit;console.error('Owned UI server exceeded cleanup budget');process.exitCode=1;}
  }
  for (const response of heldImageResponses) response.destroy();
  upstream.closeAllConnections();
  if(upstream.listening) await new Promise(resolve=>upstream.close(resolve));
});
