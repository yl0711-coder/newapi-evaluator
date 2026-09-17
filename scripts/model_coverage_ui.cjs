const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'..');
const data = fs.mkdtempSync(path.join(os.tmpdir(),'model-coverage-ui-'));
let browser, app, appExit, logs='', listCalls=0, testCalls=0, failList=false;
const upstream = http.createServer(async (req,res) => {
  if (req.method === 'GET' && req.url === '/v1/models') {
    listCalls++; res.writeHead(failList ? 503 : 200,{'Content-Type':'application/json'});
    res.end(JSON.stringify(failList ? {error:'Synthetic list failure'} : {data:['gpt-5.5','gpt-6-astra','synthetic-new-model'].map(id=>({id}))})); return;
  }
  const parts=[]; for await (const part of req) parts.push(part);
  const body=JSON.parse(Buffer.concat(parts)); testCalls++;
  const prompt=body.input || body.messages?.[0]?.content || '';
  const answer=prompt.includes('17') ? '391' : prompt.includes('单词') ? 'OK' : 'Synthetic answer';
  if (req.url === '/v1/responses') {
    assert.equal(body.max_output_tokens,4096); assert.equal(body.store,false);
    const response={status:'completed',model:body.model,output:[{type:'message',content:[{type:'output_text',text:answer}]}],
      usage:{input_tokens:10,output_tokens:20,output_tokens_details:{reasoning_tokens:10}}};
    if (body.stream) { res.writeHead(200,{'Content-Type':'text/event-stream'}); res.end('data: '+JSON.stringify({type:'response.output_text.delta',delta:answer})+'\n\ndata: '+JSON.stringify({type:'response.completed',response})+'\n\n'); }
    else { res.writeHead(200,{'Content-Type':'application/json'}); res.end(JSON.stringify(response)); }
  } else if (body.stream) {
    res.writeHead(200,{'Content-Type':'text/event-stream'});
    res.end('data: '+JSON.stringify({model:body.model,choices:[{delta:{content:answer},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:20}})+'\n\ndata: [DONE]\n\n');
  } else {
    res.writeHead(200,{'Content-Type':'application/json'}); res.end(JSON.stringify({model:body.model,choices:[{message:{content:answer},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:20}}));
  }
});
(async()=>{
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  const probe=net.createServer(); await new Promise(resolve=>probe.listen(0,'127.0.0.1',resolve));
  const port=probe.address().port; await new Promise(resolve=>probe.close(resolve));
  const base=`http://127.0.0.1:${port}`, upstreamUrl=`http://127.0.0.1:${upstream.address().port}/v1`;
  app=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['run.py','--port',String(port)],{cwd:root,
    env:{...process.env,PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:['ignore','pipe','pipe']});
  appExit=new Promise(resolve=>{app.once('exit',resolve);app.once('error',resolve);});
  app.stderr.on('data',chunk=>logs+=chunk.toString());
  let ready=false;
  for (let i=0;i<100;i++) { try { if ((await fetch(base+'/api/health')).ok) {ready=true;break;} } catch {} await new Promise(resolve=>setTimeout(resolve,100)); }
  assert.ok(ready,'Owned coverage server must start');
  async function request(url,body) { const response=await fetch(base+url,body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {}); assert.ok(response.ok,`API ${url}: ${response.status}`); return response.json(); }
  const channel=await request('/api/registry/channels',{name:'Synthetic coverage channel',base_url:upstreamUrl,api_key:'synthetic-browser-key',multiplier:1});
  const target=await request('/stability/api/channels',{name:'Synthetic existing',registry_channel_id:channel.id,model:'existing-synthetic-model',protocol:'openai',enabled:true});
  const future=new Date(Date.now()+3600*1000).toLocaleTimeString('en-GB',{timeZone:'Asia/Shanghai',hour12:false,hour:'2-digit',minute:'2-digit'});
  const plan=await request('/stability/api/schedules',{name:'Synthetic coverage plan',channel_ids:[target.id],daily_times:future,rounds:1,round_interval_seconds:0,enabled:true});
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL ? {channel:process.env.PLAYWRIGHT_CHANNEL}: {})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/channels/');
  await page.locator('.model-coverage summary').first().waitFor();
  assert.equal(listCalls,0,'Opening the page never fetches upstream models');
  await page.locator('.model-coverage summary').first().click();
  assert.equal(await page.locator('.coverage-table tbody tr').count(),12);
  await page.getByRole('button',{name:'获取模型',exact:true}).click();
  await page.locator('#fetch-submit').click(); assert.equal(listCalls,0);
  await page.locator('#fetch-confirm').check(); await page.locator('#fetch-submit').click();
  await page.locator('#fetch-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('[data-model="gpt-5.5"]').textContent.includes('已列出'));
  assert.equal(listCalls,1);
  const gpt=page.locator('[data-model="gpt-6-astra"]');
  await gpt.getByRole('button',{name:'验证',exact:true}).click(); await page.locator('#verify-confirm').check(); await page.locator('#verify-submit').click();
  await page.locator('#verify-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(async()=>{
    const data=await (await fetch('/api/model-coverage')).json();
    return data.channels[0].models.find(m=>m.model==='gpt-6-astra').measurement.status==='observing';
  },{},{timeout:20000});
  assert.equal(testCalls,6);
  await page.locator('#refresh').click();
  await page.waitForFunction(()=>document.querySelector('[data-model="gpt-6-astra"]').textContent.includes('可用，待观察'));
  await gpt.locator('input[type=checkbox]').check(); await page.locator('#enroll-models').click();
  await page.locator('#enroll-schedule').selectOption(String(plan.id));
  await page.waitForFunction(()=>!document.getElementById('enroll-submit').disabled);
  assert.match(await page.locator('#enroll-preview').innerText(),/每批新增 6 个请求/);
  await page.locator('#enroll-confirm').check(); await page.locator('#enroll-submit').click();
  await page.locator('#enroll-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('[data-model="gpt-6-astra"]').textContent.includes('已加入计划'));
  const schedules=(await request('/stability/api/schedules')).schedules;
  assert.equal(schedules[0].channel_ids.length,2); assert.equal(testCalls,6,'Enrollment does not immediately send tests');
  await page.locator('#enroll-models').click(); await page.locator('#enroll-schedule').selectOption(String(plan.id));
  await page.waitForFunction(()=>document.getElementById('enroll-preview').textContent.includes('每批新增 0 个请求'));
  await page.locator('#enroll-confirm').check(); await page.locator('#enroll-submit').click(); await page.locator('#enroll-dialog').waitFor({state:'hidden'});
  assert.equal((await request('/stability/api/channels')).channels.length,2);
  await page.locator('#catalog-add').click();
  await page.locator('#catalog-model').fill('synthetic-new-model'); await page.locator('#catalog-label').fill('Synthetic new model');
  await page.locator('#catalog-submit').click(); await page.locator('#catalog-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelectorAll('.coverage-table tbody tr').length===13);
  const mapped=page.locator('[data-model="claude-opus-5"]');
  await mapped.getByRole('button',{name:'映射',exact:true}).click();
  await page.locator('#mapping-model').fill('synthetic-claude-alias'); await page.locator('#mapping-submit').click();
  await page.locator('#mapping-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('[data-model="claude-opus-5"]').textContent.includes('synthetic-claude-alias'));
  await page.locator('#coverage-filter').selectOption('unmeasured');
  assert.equal(await page.locator('.coverage-table tbody tr').count(),2);
  await page.locator('#coverage-filter').selectOption('');
  await page.screenshot({path:path.join(data,'coverage-success-1440.png'),fullPage:true});
  failList=true;
  await page.getByRole('button',{name:'获取模型',exact:true}).click(); await page.locator('#fetch-confirm').check(); await page.locator('#fetch-submit').click();
  await page.locator('#fetch-dialog').waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.querySelector('[data-model="gpt-5.5"]').textContent.includes('上次清单曾列出'));
  for (const width of [390,900,1440]) {
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false,`Coverage page at ${width}px`);
    if (width === 1440) assert.ok(await page.locator('.record').first().evaluate(el=>el.getBoundingClientRect().width > 1000), 'Expanded models use the whole content width');
    await page.screenshot({path:path.join(data,`coverage-${width}.png`),fullPage:true});
  }
  const before=listCalls; await page.locator('#refresh').click();
  await page.locator('.coverage-table tbody tr').first().waitFor(); assert.equal(listCalls,before);
  assert.deepEqual(errors,[]);
  assert.ok(!fs.readFileSync(path.join(data,'stability','stability.db')).includes(Buffer.from('synthetic-browser-key')));
  console.log(JSON.stringify({status:'passed',mockRequests:listCalls+testCalls,artifacts:data,checks:18}));
})().catch(error=>{console.error(error); if(logs)console.error(logs);process.exitCode=1;}).finally(async()=>{
  if(browser) await browser.close();
  if(app && app.exitCode===null && app.signalCode===null) app.kill('SIGTERM');
  if(appExit) await appExit;
  upstream.closeAllConnections(); await new Promise(resolve=>upstream.close(resolve));
});
