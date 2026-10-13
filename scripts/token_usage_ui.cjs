// Synthetic loopback browser fixture: explicit trust, schedule, manual test and cancellation.
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const http=require('node:http');
const net=require('node:net');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const root=fs.realpathSync(path.resolve(__dirname,'..'));
function actualPath(input) {
  let existing=path.resolve(input);const suffix=[];
  while(!fs.existsSync(existing)) {const parent=path.dirname(existing);assert.notEqual(parent,existing,'artifact parent missing');suffix.unshift(path.basename(existing));existing=parent;}
  return path.join(fs.realpathSync(existing),...suffix);
}
const forbidden=[root,actualPath(process.env.PLATFORM_DATA_DIR || path.join(root,'data'))];
function outsideBusiness(input) {
  const actual=actualPath(input);
  assert.ok(forbidden.every(base=>actual!==base && !actual.startsWith(base+path.sep)),'external artifact root required: resolved path enters source or business data');
  return actual;
}
const artifactRoot=outsideBusiness(process.env.EVAL_TEST_ARTIFACT_ROOT || os.tmpdir());
if(process.argv.includes('--check-artifact-root')) {console.log(JSON.stringify({status:'passed',artifactRoot}));process.exit(0);}
fs.mkdirSync(artifactRoot,{recursive:true});
outsideBusiness(artifactRoot);
const data=outsideBusiness(fs.mkdtempSync(path.join(artifactRoot,'token-usage-ui-')));
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const activeMode=!process.argv.includes('--active-off') && process.env.TOKEN_USAGE_UI_EXECUTOR !== 'off';
let app,appExit,browser,calls=0,hold=false,checks=0,finalResult;
let stderrBytes=0;const stderrHash=crypto.createHash('sha256');
const upstream=http.createServer(async(req,res)=>{
  for await(const chunk of req) { /* synthetic bodies remain transient */ }
  calls++;
  if(hold) { await new Promise(resolve=>setTimeout(resolve,2000)); }
  if(res.destroyed) return;
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  res.end('data: '+JSON.stringify({choices:[{delta:{content:'OK'},finish_reason:'stop'}],usage:{prompt_tokens:2048,completion_tokens:1}})+'\n\ndata: [DONE]\n\n');
});
const check=(value,message)=>{assert.ok(value,message);checks++;};
async function port(){const s=net.createServer();await new Promise(r=>s.listen(0,'127.0.0.1',r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
(async()=>{
  await new Promise(r=>upstream.listen(0,'127.0.0.1',r));
  const base='http://127.0.0.1:'+await port();
  const safeEnv={};for(const key of ['PATH','HOME','LANG','LC_ALL','SYSTEMROOT']) if(process.env[key]!==undefined)safeEnv[key]=process.env[key];
  app=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['-B','run.py','--port',new URL(base).port],{cwd:root,env:{...safeEnv,TMPDIR:data,PYTHONDONTWRITEBYTECODE:'1',PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:'',EVAL_TOKEN_USAGE_EXECUTOR:activeMode?'live':'off',EVAL_INTEGRITY_EXECUTOR:'off',EVAL_MONITOR_EXECUTOR:'off'},stdio:['ignore','ignore','pipe']});
  app.stderr.on('data',chunk=>{stderrBytes+=chunk.length;stderrHash.update(chunk);});
  appExit=new Promise(resolve=>{app.once('exit',(code,signal)=>resolve({code,signal}));app.once('error',()=>resolve({error:'spawn_failed'}));});
  let ready=false;
  for(let i=0;i<100;i++){if(app.exitCode!==null) break;try{ready=(await fetch(base+'/api/health')).ok;if(ready)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  check(ready,'Owned token workbench ready');
  async function api(url,body,method='POST'){const response=await fetch(base+url,body===undefined?{}:{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert.ok(response.ok,'local API '+response.status);return response.json();}
  const candidate=await api('/api/registry/channels',{name:'Synthetic candidate',base_url:'http://127.0.0.1:'+upstream.address().port,api_key:'synthetic_candidate',multiplier:1});
  const reference=await api('/api/registry/channels',{name:'Synthetic reference',base_url:'http://127.0.0.1:'+upstream.address().port,api_key:'synthetic_reference',multiplier:1});
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.name));
  await page.addInitScript(()=>Object.defineProperty(globalThis.crypto,'randomUUID',{value:undefined,configurable:true}));
  await page.goto(base+'/token-usage/');await page.locator('.platform-nav').waitFor();await page.locator('#model option').first().waitFor({state:'attached'});
  check(calls===0,'Opening page does not send upstream');
  await page.locator('#model').selectOption('gpt-5.5');await page.locator('#reference').selectOption(String(reference.id));await page.locator('#candidate').selectOption(String(candidate.id));
  await page.locator('#trusted').check();await page.locator('#save-binding').click();await page.locator('#bindings').getByText(/可信参照 Synthetic reference/).waitFor();
  check(calls===0,'Trust save is zero network');
  await page.locator('#preview').click();await page.locator('#preview-result').getByText(/首轮 2 请求/).waitFor();
  await page.locator('#save-config').click();await page.locator('#configs').getByText(/已暂停/).waitFor();
  check(calls===0,'Disabled config is zero network');
  await page.locator('#confirm').check();await page.locator('#configs button').click();await page.locator('#configs').getByText(/每半小时监测/).waitFor();
  check(calls===0,'Enabled schedule starts at next due');
  if(!activeMode) {
    check(await page.locator('#manualcheck').isDisabled(),'Active-off disables manual test');
    check((await page.locator('#active-help').textContent()).includes('半小时监测独立运行'),'Active-off explains schedule independence');
    const jobsBefore=await api('/api/token-usage/results');check(jobsBefore.jobs.length===0,'Disabled button did not queue active work');
    // Move only this fixture's due time; the scheduler still creates and runs
    // the occurrence itself. Never open a business database.
    const tick=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['-B','-c','import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute("UPDATE configs SET next_due=0"); db.commit(); db.close()',path.join(data,'token-usage','token-usage.db')],{env:{...safeEnv,PYTHONDONTWRITEBYTECODE:'1'},stdio:'ignore'});
    await new Promise((resolve,reject)=>{tick.on('exit',code=>code===0?resolve():reject(new Error('fixture_due_update_failed')));tick.on('error',reject);});
    await page.locator('#results strong').filter({hasText:'固定输入双端用量未发现异常'}).waitFor({timeout:25000});
    check(calls===2,'Active-off schedule runs two bounded loopback requests');
    check((await page.locator('#updated').textContent()).includes('最近更新'),'Idle page discovers scheduled result without refresh click');
    check(errors.length===0,'No browser errors');
    await page.screenshot({path:path.join(data,'token-active-off.png'),fullPage:true});
    finalResult={status:'passed',mode:'off',mockRequests:calls,checks,evidence:data};return;
  }
  await page.locator('#configs button').click();await page.locator('#configs').getByText(/已暂停/).waitFor();
  await page.locator('#confirm').check();await page.locator('#manualcheck').click();await page.locator('#results strong').filter({hasText:'固定输入双端用量未发现异常'}).waitFor();
  check(calls===2,'Manual paired test has two actual HTTP requests');
  check((await page.locator('#results article').first().innerText()).includes('Synthetic candidate') && (await page.locator('#results article').first().innerText()).includes('Synthetic reference'),'Both frozen channel names visible');
  const originalJob=(await api('/api/token-usage/results')).jobs[0];
  await api('/api/registry/channels/'+candidate.id,{name:'Renamed candidate',base_url:candidate.base_url,multiplier:1,version:candidate.version},'PUT');
  await page.locator('#refresh').click();
  check((await page.locator('#results article').first().innerText()).includes('Synthetic candidate'),'History retains frozen name after rename');
  check(!(await page.locator('#results article').first().innerText()).includes('2048'),'Metrics hidden in folded evidence');
  await page.locator('#results summary').first().click();check((await page.locator('#results pre').first().textContent()).includes('2048'),'Folded trace contains reported usage');
  await page.locator('#refresh').click();await page.waitForTimeout(100);check(await page.locator('#results details').first().evaluate(e=>e.open),'Refresh preserves open evidence');
  hold=true;await page.locator('#confirm').check();await page.locator('#manualcheck').click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#results article')].some(e=>e.innerText.includes('任务执行中')));
  await page.locator('#results article').first().getByRole('button',{name:'停止此任务'}).click();
  await page.waitForFunction(()=>document.querySelector('#results article')?.innerText.includes('任务已取消'));
  check(calls<=4,'Cancelled job remains bounded');
  hold=false;
  let refreshFails=true;
  await page.route('**/api/token-usage/status',async route=>{if(refreshFails) await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'synthetic_refresh_failure'})});else await route.continue();});
  await page.locator('#refresh').click();await page.locator('#updated').getByText(/刷新失败/).waitFor();
  check((await page.locator('#updated').textContent()).includes('上次成功更新'),'Refresh failure keeps last successful timestamp');
  refreshFails=false;await page.locator('#refresh').click();await page.locator('#updated').getByText(/最近更新/).waitFor();
  await page.unroute('**/api/token-usage/status');
  // Seed an old record without name metadata only inside this new mock DB.
  const legacy=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['-B','-c','import json,sqlite3,sys; db=sqlite3.connect(sys.argv[1]); row=db.execute("SELECT spec FROM jobs WHERE id=?",(sys.argv[2],)).fetchone(); spec=json.loads(row[0]); spec.pop("display_identity",None); db.execute("UPDATE jobs SET spec=? WHERE id=?",(json.dumps(spec,ensure_ascii=False,sort_keys=True,separators=(",",":")),sys.argv[2])); db.commit(); db.close()',path.join(data,'token-usage','token-usage.db'),originalJob.job_id],{env:{...safeEnv,PYTHONDONTWRITEBYTECODE:'1'},stdio:'ignore'});
  await new Promise((resolve,reject)=>{legacy.on('exit',code=>code===0?resolve():reject(new Error('fixture_legacy_update_failed')));legacy.on('error',reject);});
  await page.locator('#refresh').click();
  await page.locator('#results').getByText(/未记录渠道名称/).waitFor();
  const legacyText=await page.locator('article[data-job-id="'+originalJob.job_id+'"]').innerText();
  check(legacyText.includes('未记录渠道名称') && !legacyText.includes('Renamed candidate'),'Legacy history labels missing name instead of current Registry name');
  for(const width of [390,900,1440]){await page.setViewportSize({width,height:1000});check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No page overflow '+width);await page.screenshot({path:path.join(data,'token-'+width+'.png'),fullPage:true});}
  check(errors.length===0,'No browser errors');
  const history=await api('/api/token-usage/results');check(!JSON.stringify(history).includes('synthetic_candidate'),'No upstream credential in history');
  check(history.jobs.every(j=>j.result?.billing_status==='unverified'),'Billing stays unverified');
  finalResult={status:'passed',mockRequests:calls,checks,evidence:data};
})().catch(error=>{finalResult={status:'failed',error:error.message,mockRequests:calls,evidence:data};process.exitCode=1;}).finally(async()=>{
  try {
    if(browser) await browser.close();
    if(app && app.exitCode===null && app.signalCode===null){app.kill('SIGTERM');let exited=await Promise.race([appExit,new Promise(r=>setTimeout(()=>r(null),3000))]);if(!exited){app.kill('SIGKILL');exited=await Promise.race([appExit,new Promise(r=>setTimeout(()=>r(null),3000))]);}assert.ok(exited,'owned app terminated');}
    await new Promise((resolve,reject)=>upstream.close(error=>error?reject(error):resolve()));
    assert.equal(upstream.listening,false,'owned upstream closed');
  } catch(error) {finalResult={...finalResult,status:'failed',cleanup:'owned_resource_cleanup_failed'};process.exitCode=1;}
  console.log(JSON.stringify({...finalResult,stderr:{bytes:stderrBytes,sha256:stderrHash.digest('hex')}}));
});
