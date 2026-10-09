// Self-authored fixture responses on an owned loopback upstream; no real traffic.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const http=require('node:http'), net=require('node:net'), os=require('node:os');
const {spawn,execFileSync}=require('node:child_process');
const execFileAsync=require('node:util').promisify(require('node:child_process').execFile);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'), data=fs.mkdtempSync(path.join(os.tmpdir(),'integrity-ui-'));
const python=process.env.PYTHON_EXECUTABLE||'python3';
let app,browser,appExit,logs='',calls=0,kbfCalls=0,delayKbf=true,delayUnified=false,checks=0;
const canary=new Map(JSON.parse(fs.readFileSync(path.join(root,'features/integrity/assets/canary/probes.json'))).map(p=>[p.prompt,String(p.expected)]));
const check=(v,m)=>{assert.ok(v,m);checks++;};
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const n=s.address().port;await new Promise(r=>s.close(r));return n;}
const upstream=http.createServer(async(req,res)=>{
  const pieces=[];for await(const piece of req)pieces.push(piece);
  const body=JSON.parse(Buffer.concat(pieces));calls++;
  assert.equal(req.url,'/v1/responses');assert.equal(body.stream,false);assert.equal(body.store,false);
  const prompt=body.input;let answer='OK', usage={input_tokens:10,output_tokens:1};
  if(canary.has(prompt)){answer=canary.get(prompt);usage={input_tokens:10,output_tokens:512,output_tokens_details:{reasoning_tokens:490}};}
  else if(prompt.startsWith('Synthetic owned choice')){kbfCalls++;answer='1';if(delayKbf){delayKbf=false;await new Promise(r=>setTimeout(r,3000));}}
  else if(prompt.includes('随机选择一个数字'))answer='42';
  else if(body.max_output_tokens===2500)answer=JSON.stringify(Array.from({length:9},()=>Array(35).fill(42)));
  else if(body.max_output_tokens===2048){answer=Array(331).fill('42').join(' ');if(delayUnified){delayUnified=false;await new Promise(r=>setTimeout(r,3000));}}
  res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({status:'completed',model:body.model,output:[{type:'message',content:[{type:'output_text',text:answer}]}],usage}));
});
async function until(fn,message,timeout=30000){const end=performance.now()+timeout;while(performance.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,100));}assert.fail(message);}
(async()=>{
  await new Promise(r=>upstream.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${await port()}`;
  const env={...process.env,PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:'',PYTHONDONTWRITEBYTECODE:'1',EVAL_INTEGRITY_EXECUTOR:'live',EVAL_MONITOR_EXECUTOR:'off'};
  const hashes=JSON.parse(execFileSync(python,['scripts/integrity_browser_fixture.py','references'],{cwd:root,env,timeout:20000,encoding:'utf8'}));
  const packages=JSON.parse(fs.readFileSync(path.join(data,'references.json')));
  app=spawn(python,['run.py','--port',new URL(base).port],{cwd:root,env,stdio:['ignore','pipe','pipe']});
  appExit=new Promise(r=>{app.once('exit',r);app.once('error',r);});app.stderr.on('data',c=>logs+=c.toString());
  await until(async()=>{try{return(await fetch(base+'/api/health')).ok;}catch{return false;}},'owned server startup');
  async function request(url,body,method='POST'){const res=await fetch(base+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});check(res.ok,`API ${url}: ${res.status}`);return res.json();}
  const channel=await request('/api/registry/channels',{name:'Synthetic first recorded',base_url:`http://127.0.0.1:${upstream.address().port}/v1`,api_key:'synthetic-integrity-ui-credential',multiplier:1,status:'recorded'});
  browser=await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL||undefined});const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base+'/');await page.getByRole('link',{name:/模型完整性复核/}).first().waitFor();
  await page.goto(base+'/integrity/');await page.locator('#unified-channel option').nth(1).waitFor({state:'attached'});
  check((await request('/stability/api/channels')).channels.length===0,'recorded channel has zero existing targets');
  await page.locator('#unified-channel').selectOption(String(channel.id));
  check(await page.locator('#unified-model').inputValue()==='gpt-6-astra','unified default Astra');
  async function unifiedSubmit(){await page.locator('#unified-confirm').check();const accepted=page.waitForResponse(r=>r.url()===base+'/api/integrity/tests'&&r.request().method()==='POST');await page.locator('#unified-submit').click();const res=await accepted;check(res.status()===202,'one click creates async unified task');return(await res.json()).task_id;}
  const unifiedFirst=await unifiedSubmit();let unifiedResult;
  await until(async()=>{unifiedResult=await request('/api/integrity/tests/'+unifiedFirst);return unifiedResult.status==='completed';},'unified three methods complete');
  check(unifiedResult.consumed.requests===8,'three methods use eight independent HTTP attempts');
  check(JSON.stringify(unifiedResult.reports.map(r=>r.valid))==='[1,3,3]','three independent valid denominators');
  for(const method of ['traceone','modeltrace','nerfed-api'])await page.locator(`[data-unified-id="${unifiedFirst}"] [data-method="${method}"]`).waitFor();
  const unifiedDownload=page.waitForEvent('download');await page.locator(`[data-unified-id="${unifiedFirst}"] a.export`).click();await(await unifiedDownload).saveAs(path.join(data,'synthetic-unified-export.json'));
  check(!fs.readFileSync(path.join(data,'synthetic-unified-export.json'),'utf8').includes('synthetic-integrity-ui-credential'),'unified report export excludes credential');
  await page.locator('#unified-new').click();delayUnified=true;const beforeCancel=calls;
  const unifiedSecond=await unifiedSubmit();await until(()=>calls>=beforeCancel+3,'unified first fingerprint in flight');
  await page.locator(`[data-unified-id="${unifiedSecond}"]`).getByRole('button',{name:'取消三项任务'}).click();
  await until(async()=> (await request('/api/integrity/tests/'+unifiedSecond)).status==='cancelled','unified cancellation');
  await page.locator(`[data-unified-id="${unifiedSecond}"]`).getByRole('button',{name:'安全恢复三项任务'}).click();
  await until(async()=>{unifiedResult=await request('/api/integrity/tests/'+unifiedSecond);return unifiedResult.status==='partially_completed';},'unified safe resume');
  check(unifiedResult.consumed.requests===8&&unifiedResult.consumed.unknown_requests===1,'unified unknown retains cap, no resend');
  check(unifiedResult.reports[2].valid===3,'later method completes after earlier unknown');
  await page.reload();await page.locator(`[data-unified-id="${unifiedFirst}"]`).waitFor();check(true,'unified historical three reports survive reload');
  const currentChannel=(await request('/api/registry/channels')).channels.find(c=>c.id===channel.id);
  const edited=Object.fromEntries(['name','base_url','scope','multiplier','note','enabled','version','protocol_profile'].map(k=>[k,currentChannel[k]]));
  await request(`/api/registry/channels/${channel.id}`,{...edited,status:'online',api_key:''},'PUT');
  await page.goto(base+'/stability/');await page.getByRole('button',{name:'定时计划',exact:true}).click();await page.getByRole('button',{name:'新增计划',exact:true}).click();
  await page.locator('#schedule-dialog').waitFor();await page.locator('#schedule-pack').selectOption('layered-integrity-v1');check(await page.getByRole('checkbox',{name:'选择公共渠道 Synthetic first recorded'}).isEnabled(),'zero target user-defined online Registry channel selectable');
  await page.locator('#schedule-name').fill('Synthetic layered browser');await page.getByRole('checkbox',{name:'选择公共渠道 Synthetic first recorded'}).check();await page.locator('#schedule-enabled').uncheck();
  const saved=page.waitForResponse(r=>r.url()===base+'/stability/api/schedules'&&r.request().method()==='POST');
  await page.locator('#schedule-form button[type=submit]').click();check((await saved).ok(),'layered plan saved');await page.locator('#schedule-dialog').waitFor({state:'hidden'});
  const planId=(await request('/stability/api/schedules')).schedules[0].id;
  // Keep the Node event loop free to serve this test's owned HTTP upstream.
  const driven=JSON.parse((await execFileAsync(python,['scripts/integrity_browser_fixture.py','execute',String(planId)],{cwd:root,env,timeout:90000,encoding:'utf8'})).stdout);
  check(driven.attempted===202,'one-channel weekday executes 7 light + 3 fingerprint + 192 canary');
  await page.reload();await page.getByRole('button',{name:'查看',exact:true}).first().click();
  await page.getByText('显式锁定可信 baseline',{exact:true}).waitFor();
  const run=await request(`/stability/api/runs/${driven.run_id}`);const slot=run.slots.find(s=>s.method==='canary');
  check(slot.summary.attempted===192&&slot.summary.score.status==='current_only','complete current-only canary, no automatic baseline');
  check(slot.summary.fees.estimated_usd>3&&slot.status==='completed','over three USD continues all 192 attempts');
  page.once('dialog',d=>d.accept('Synthetic explicitly trusted baseline'));await page.getByText('显式锁定可信 baseline',{exact:true}).click();
  await until(async()=>((await request('/stability/api/baselines')).baselines.length===1),'baseline saved');
  await page.goto(base+'/integrity/');await page.locator('#review-channel option').nth(1).waitFor({state:'attached'});
  async function importRef(method){await page.locator('#reference-file').setInputFiles({name:`synthetic-${method}.json`,mimeType:'application/json',buffer:Buffer.from(JSON.stringify(packages[method]))});await page.locator('#reference-sha').fill(hashes[method]);await page.locator('#reference-authorized').check();check(await page.locator('#reference-form').evaluate(f=>f.reportValidity()),'reference form valid');const imported=page.waitForResponse(r=>r.url()===base+'/api/integrity/references'&&r.request().method()==='POST');await page.locator('#reference-form button').click();const response=await imported;check(response.ok(),`reference rejected: ${response.status()} ${await response.text()}`);await until(async()=> (await page.locator('#reference-status').innerText()).includes(hashes[method].slice(0,12)),`${method} reference import`);}
  async function submit(){await page.locator('#review-channel').selectOption(String(channel.id));await page.locator('#review-model').selectOption('gpt-6-astra');await page.locator('#review-confirm').check();const accepted=page.waitForResponse(r=>r.url()===base+'/api/integrity/reviews'&&r.request().method()==='POST');await page.locator('#review-submit').click();const res=await accepted;check(res.status()===202,'active review queued asynchronously');return(await res.json()).task_id;}
  await importRef('kbf');const kbf=await submit();await until(()=>kbfCalls===1,'first KBF send observed');
  await page.locator(`[data-task-id="${kbf}"]`).getByRole('button',{name:'取消任务'}).click();
  await until(async()=> (await request('/api/integrity/reviews/'+kbf)).status==='cancelled','review cancellation');
  const resumeResponse=page.waitForResponse(r=>r.url()===base+'/api/integrity/reviews/'+kbf+'/resume'&&r.request().method()==='POST');
  await page.locator(`[data-task-id="${kbf}"]`).getByRole('button',{name:'安全恢复'}).click();
  const resumed=await resumeResponse;check(resumed.ok(),`resume rejected ${resumed.status()}`);
  let reviewState;
  await until(async()=> {reviewState=await request('/api/integrity/reviews/'+kbf);return reviewState.status==='partially_completed';},'review resume completion').catch(e=>{console.error({status:reviewState.status,reason:reviewState.reason,consumed:reviewState.consumed});throw e;});
  const reviewed=await request('/api/integrity/reviews/'+kbf);check(reviewed.consumed.requests===4&&kbfCalls===4,'unknown in-flight is retained and never resent');
  check(reviewed.fees.unknown_requests>=1&&reviewed.fees.stopping_limit===null,'unknown fees visible without money cap');
  const download=page.waitForEvent('download');await page.locator(`[data-task-id="${kbf}"] a.export`).click();const exported=await download;await exported.saveAs(path.join(data,'synthetic-review-export.json'));
  check(!fs.readFileSync(path.join(data,'synthetic-review-export.json'),'utf8').includes('synthetic-integrity-ui-credential'),'export excludes credential');
  await importRef('hlwy');const hlwy=await submit();await until(async()=> (await request('/api/integrity/reviews/'+hlwy)).status==='completed','HLwY consumer completion');
  check((await request('/api/integrity/reviews/'+hlwy)).report.target_valid===4,'HLwY actual HTTP projections');
  const evidence={schema:'integrity-official-account-evidence/v1',account_alias:'synthetic-browser-account',expected_model:'gpt-6-astra',authorization:{authorized:true,basis:'self_authored',source:'synthetic_regression_fixture'},provenance:{source:'official_account',trusted:true,complete:true},events_coverage_complete:false,events:[],catalog:[]};
  await page.locator('#evidence-file').setInputFiles({name:'synthetic-account.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(evidence))});await page.locator('#evidence-authorized').check();await page.locator('#evidence-form button').click();
  await until(async()=> (await request('/api/integrity/evidence')).tasks.some(t=>t.status==='completed'),'offline evidence terminal');
  check((await request('/api/integrity/evidence')).tasks[0].results[0].metadata_status==='unavailable','missing metadata stays unavailable');
  for(const width of [390,900,1440]){await page.setViewportSize({width,height:1000});check(!(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2)),`no overflow ${width}`);await page.screenshot({path:path.join(data,`integrity-${width}.png`),fullPage:true});}
  check(errors.length===0,'no browser errors');check(calls>=210,'actual local Mock requests');
  console.log(JSON.stringify({status:'passed',mockRequests:calls,checks,artifacts:data,canaryAttempts:192,realUpstream:false}));
})().catch(e=>{console.error(e);console.error(logs);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();if(app&&app.exitCode===null&&app.signalCode===null)app.kill('SIGTERM');if(appExit)await appExit;upstream.closeAllConnections();await new Promise(r=>upstream.close(r));});
