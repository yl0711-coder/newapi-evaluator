const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fs.realpathSync(path.resolve(__dirname,'..'));
const artifactRoot = process.env.EVAL_TEST_ARTIFACT_ROOT;
if (!artifactRoot || !path.isAbsolute(artifactRoot)) throw new Error('External artifact root required');
const safeRoot = fs.realpathSync(artifactRoot);
if (safeRoot === root || safeRoot.startsWith(root+path.sep) || root.startsWith(safeRoot+path.sep)) throw new Error('Artifact root overlaps source');
const data = fs.mkdtempSync(path.join(safeRoot,'daily-checks-ui-'));
let app, appExit, browser, failure, mockRequests=0;
const errors=[];
const watchdog=setTimeout(()=>{failure=failure || new Error('Daily browser budget exceeded');if(app)app.kill('SIGTERM');if(browser)browser.close().catch(()=>{});},160000);
(async()=>{
  const probe=net.createServer();await new Promise(resolve=>probe.listen(0,'127.0.0.1',resolve));
  const port=probe.address().port;await new Promise(resolve=>probe.close(resolve));
  const base=`http://127.0.0.1:${port}`;
  const env=Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','HOME','LANG','TMPDIR','PYTHONDONTWRITEBYTECODE'].includes(key)));
  app=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['-B','tests/fixtures/daily_app.py',String(port)],
    {cwd:root,env:{...env,PLATFORM_DATA_DIR:data,RELAY_LAB_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:'ignore'});
  appExit=new Promise(resolve=>{app.once('exit',resolve);app.once('error',resolve);});
  let ready=false;
  for(let i=0;i<100;i++){try{if((await fetch(base+'/api/health')).ok){ready=true;break;}}catch{}if(app.exitCode!==null)break;await new Promise(resolve=>setTimeout(resolve,100));}
  assert.ok(ready,'Owned fixture starts');
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL}: {})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});page.on('pageerror',e=>errors.push(e.name));
  const state=async()=>await(await fetch(base+'/fixture/state')).json();
  await page.goto(base+'/');await page.getByRole('link',{name:'已上线渠道每日检测'}).first().click();
  await page.getByRole('heading',{name:'已上线渠道每日检测',exact:true}).waitFor();
  await page.waitForFunction(()=>document.querySelector('#plan-status').textContent.includes('暂停'));
  assert.equal((await state()).mockRequests,0);assert.equal((await state()).notifications,0);
  await page.locator('#gpt-model').selectOption('gpt-6-astra');await page.locator('#claude-model').selectOption('claude-opus-4.7');
  await page.locator('#gpt-reference').selectOption({label:'Synthetic reference · #3'});await page.locator('#claude-reference').selectOption({label:'Synthetic reference · #3'});
  await page.locator('#webhook').fill('https://open.feishu.cn/open-apis/bot/v2/hook/synthetic');await page.locator('#save-webhook').click();
  await page.getByText('Webhook 已保存，未发送通知。',{exact:true}).waitFor();assert.equal(await page.locator('#webhook').inputValue(),'');
  assert.equal(await page.locator('#gpt-model').inputValue(),'gpt-6-astra');
  assert.equal(await page.locator('#claude-model').inputValue(),'claude-opus-4.7');
  await page.locator('#trust-confirm').check();await page.locator('#save').click();
  await page.getByText('配置已保存，计划保持暂停。',{exact:true}).waitFor();
  assert.equal((await state()).mockRequests,0);assert.equal((await state()).notifications,0);
  await page.locator('#preview').click();await page.waitForFunction(()=>document.querySelector('#preview-summary').textContent.includes('每日首次 40'));
  assert.equal(await page.locator('#preview-rows tr').count(),2);
  await page.locator('#enable').click();await page.waitForFunction(()=>document.querySelector('#message').textContent.includes('明确确认'));
  assert.equal((await state()).mockRequests,0);
  await page.locator('#live-confirm').check();await page.locator('#notify-confirm').check();await page.locator('#enable').click();
  await page.getByText('计划已启用，从下一轮开始检测。',{exact:true}).waitFor();
  assert.equal(await page.locator('#live-confirm').isChecked(),false);
  let result=await(await fetch(base+'/fixture/start',{method:'POST'})).json();assert.equal(result.mockRequests,10);assert.equal(result.notifications,0);
  await page.locator('#refresh').click();await page.locator('#batches button').first().click();
  await page.waitForFunction(()=>document.querySelector('#report-rows').textContent.includes('固定输入双端用量未发现异常'));
  assert.equal(await page.locator('#report-rows article').count(),2);
  result=await(await fetch(base+'/fixture/report',{method:'POST'})).json();assert.equal(result.notifications,1);
  await page.locator('#refresh').click();await page.waitForFunction(()=>document.querySelector('#coverage').textContent.includes('已送达'));
  for(const [id,extension] of [['download-json','json'],['download-markdown','md']]){
    const pending=page.waitForEvent('download');await page.locator('#'+id).click();const download=await pending;
    const target=path.join(data,'report.'+extension);await download.saveAs(target);const text=fs.readFileSync(target,'utf8');
    assert.ok(text.includes('Synthetic GPT'));for(const secret of ['synthetic-candidate','synthetic-reference','synthetic-claude-candidate','hook/synthetic'])assert.ok(!text.includes(secret));
    if(extension==='json'){const report=JSON.parse(text);assert.equal(report.coverage.fully_measured_rows,2);assert.equal(report.billing_status,'unverified');}
  }
  for(const width of [390,900,1440]){
    await page.setViewportSize({width,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
    assert.equal(await page.evaluate(()=>{const ids=[...document.querySelectorAll('[id]')].map(n=>n.id);return new Set(ids).size===ids.length;}),true);
    await page.screenshot({path:path.join(data,`daily-${width}.png`),fullPage:true});
    await page.locator('#detail').screenshot({path:path.join(data,`daily-report-${width}.png`)});
  }
  await page.locator('#pause').focus();assert.equal(await page.locator('#pause').evaluate(el=>el===document.activeElement),true);
  await page.keyboard.press('Enter');await page.getByText('计划已暂停，后续请求停止。',{exact:true}).waitFor();
  result=await(await fetch(base+'/fixture/second-start',{method:'POST'})).json();assert.equal(result.mockRequests,10);
  assert.deepEqual(errors,[]);mockRequests=(await state()).mockRequests;
})().catch(e=>{failure=e;}).finally(async()=>{
  clearTimeout(watchdog);
  try{if(browser)await browser.close();}catch{failure=failure || new Error('Browser cleanup failed');}
  if(app && app.exitCode===null){app.kill('SIGTERM');const ended=await Promise.race([appExit.then(()=>true),new Promise(resolve=>setTimeout(()=>resolve(false),5000))]);if(!ended){app.kill('SIGKILL');await appExit;failure=failure || new Error('Owned app cleanup failed');}}
  if(failure){console.error(failure);process.exitCode=1;}
  console.log(JSON.stringify({status:failure?'failed':'passed',mockRequests,evidence:data,errors}));
});
