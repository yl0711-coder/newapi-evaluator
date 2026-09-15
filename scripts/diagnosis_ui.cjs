const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
const data=fs.mkdtempSync(path.join(os.tmpdir(),'diagnosis-ui-'));
let app,browser,logs='';
(async()=>{
  const probe=net.createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
  const base=`http://127.0.0.1:${port}`;
  app=spawn(process.env.PYTHON_EXECUTABLE,['scripts/start_diagnosis.py','--data-dir',data,'--port',String(port)],{cwd:root,env:{...process.env,PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:['ignore','pipe','pipe']});
  app.stderr.on('data',x=>logs+=x.toString());
  let ready=false;
  for(let i=0;i<100;i++){try{const r=await fetch(base+'/api/health');if(r.ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,100));}
  assert.ok(ready,'owned server failed to start');
  const channels=[];
  for(const enabled of [true,false]){
    const response=await fetch(base+'/api/registry/channels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:`diagnosis fixture ${enabled}`,base_url:`https://${enabled?'enabled':'disabled'}.invalid`,api_key:'synthetic-diagnosis-credential',multiplier:1,enabled,status:'online'})});
    assert.equal(response.status,200);channels.push(await response.json());
  }
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/diagnosis/');await page.getByText('准备就绪。',{exact:false}).waitFor();
  assert.equal(await page.locator('#mode').inputValue(),'mock');
  assert.equal(await page.locator('#mode option[value=live]').evaluate(option=>option.disabled),false);
  await page.locator('#sample').click();await page.waitForFunction(()=>document.querySelector('#case-select').value.length===32);
  assert.match(await page.locator('#case-summary').textContent(),/历史输入 — \/ 输出 —/);
  await page.locator('#mode').selectOption('live');
  assert.equal(await page.locator('#channel-field').isVisible(),true);
  assert.deepEqual(await page.locator('#channel option').evaluateAll(options=>options.map(option=>option.value)),['',String(channels[0].id)]);
  await page.locator('#channel').selectOption(String(channels[0].id));await page.locator('#model').fill('fixture-model');
  await page.getByRole('button',{name:'预览本次计划',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'visible'});
  assert.equal(await page.locator('#live-confirm-field').isVisible(),true);
  await page.locator('#start').click();await page.getByText('本次真实请求需要勾选预览确认',{exact:true}).waitFor();
  assert.deepEqual(await (await fetch(base+'/diagnosis/api/runs')).json(),[]);
  await page.locator('#mode').selectOption('mock');
  await page.locator('#scenario').selectOption('large_input_error');await page.locator('#repetitions').fill('1');
  await page.getByRole('button',{name:'预览本次计划',exact:true}).click();await page.locator('#preview-panel').waitFor({state:'visible'});
  assert.match(await page.locator('#preview-summary').textContent(),/4 次串行请求/);
  await page.locator('#input-tokens').fill('1200');assert.equal(await page.locator('#preview-panel').isHidden(),true);
  await page.getByRole('button',{name:'预览本次计划',exact:true}).click();await page.locator('#start').click();
  await page.waitForFunction(()=>document.querySelector('#run-state').textContent.startsWith('已结束'));
  assert.ok((await page.locator('#group-summary').textContent()).includes('×'));
  assert.ok(!(await page.locator('#group-summary').textContent()).includes('比值 0×'));
  assert.equal(await page.locator('#results tr').count(),4);assert.match(await page.locator('#results tr').nth(1).textContent(),/正常结束/);
  for(const format of ['json','md']){const waiting=page.waitForEvent('download');await page.locator('#export-'+format).click();const download=await waiting;const content=fs.readFileSync(await download.path(),'utf8');assert.ok(!content.includes('Synthetic observation'));if(format==='json'){const r=JSON.parse(content);assert.equal(r.run.results[0].http_status,429);assert.equal(r.run.results[1].http_status,200);assert.equal(r.run.plan.case.input_tokens,null);}}
  await page.screenshot({path:path.join(data,'diagnosis-desktop.png'),fullPage:true});
  await page.reload();await page.locator('#run-select option').nth(1).waitFor({state:'attached'});await page.locator('#run-select').selectOption({index:1});await page.waitForFunction(()=>document.querySelector('#results').children.length===4);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(data,'diagnosis-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);
  await page.locator('#case-file').setInputFiles({name:'single-case.jsonl',mimeType:'application/json',buffer:Buffer.from('{\"total_tokens\":100,\"stream\":false}')});
  await page.getByText('已导入 1 条案例。',{exact:true}).waitFor();
  await page.locator('#case-file').setInputFiles({name:'synthetic-cases.jsonl',mimeType:'application/json',buffer:Buffer.from('{"total_tokens":100,"stream":false}\n{"total_tokens":200,"stream":true}')});
  await page.getByText('已导入 2 条案例。',{exact:true}).waitFor();
  await page.locator('#case-file').setInputFiles({name:'invalid.json',mimeType:'application/json',buffer:Buffer.from('[{"total_tokens":1,"stream":true,"prompt":"discarded-content"}]')});
  await page.waitForFunction(()=>document.querySelector('#status').classList.contains('error'));assert.ok(!(await page.locator('#status').textContent()).includes('discarded-content'));
  await page.locator('#delete-run').click();await page.getByText('运行记录已删除。',{exact:true}).waitFor();
  await page.locator('#scenario').selectOption('slow_first');await page.locator('#repetitions').fill('5');
  await page.getByRole('button',{name:'预览本次计划',exact:true}).click();await page.locator('#start').click();
  await page.locator('#stop:not([disabled])').waitFor();await page.locator('#stop').click();
  await page.waitForFunction(()=>document.querySelector('#run-state').textContent.startsWith('已停止'));
  assert.match(await page.locator('#results').textContent(),/未发送/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',checks:18,skipped:0,artifacts:data}));
})().catch(error=>{console.error(error);if(logs)console.error(logs);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(app&&app.exitCode===null){const done=new Promise(r=>app.once('exit',r));app.kill('SIGTERM');await done;}
});
