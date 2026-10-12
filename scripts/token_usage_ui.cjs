// Synthetic loopback browser fixture: explicit trust, schedule, manual test and cancellation.
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const fs=require('node:fs');
const http=require('node:http');
const net=require('node:net');
const os=require('node:os');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
const artifactRoot=path.resolve(process.env.EVAL_TEST_ARTIFACT_ROOT || os.tmpdir());
assert.ok(artifactRoot!==root && !artifactRoot.startsWith(root+path.sep),'external artifact root required');
fs.mkdirSync(artifactRoot,{recursive:true});
const data=fs.mkdtempSync(path.join(artifactRoot,'token-usage-ui-'));
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
  app=spawn(process.env.PYTHON_EXECUTABLE || 'python3',['-B','run.py','--port',new URL(base).port],{cwd:root,env:{...safeEnv,TMPDIR:data,PYTHONDONTWRITEBYTECODE:'1',PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:'',EVAL_TOKEN_USAGE_EXECUTOR:'live',EVAL_INTEGRITY_EXECUTOR:'off',EVAL_MONITOR_EXECUTOR:'off'},stdio:['ignore','ignore','pipe']});
  app.stderr.on('data',chunk=>{stderrBytes+=chunk.length;stderrHash.update(chunk);});
  appExit=new Promise(resolve=>{app.once('exit',(code,signal)=>resolve({code,signal}));app.once('error',()=>resolve({error:'spawn_failed'}));});
  let ready=false;
  for(let i=0;i<100;i++){if(app.exitCode!==null) break;try{ready=(await fetch(base+'/api/health')).ok;if(ready)break;}catch{}await new Promise(r=>setTimeout(r,100));}
  check(ready,'Owned token workbench ready');
  async function api(url,body){const response=await fetch(base+url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});assert.ok(response.ok,'local API '+response.status);return response.json();}
  const candidate=await api('/api/registry/channels',{name:'Synthetic candidate',base_url:'http://127.0.0.1:'+upstream.address().port,api_key:'synthetic_candidate',multiplier:1});
  const reference=await api('/api/registry/channels',{name:'Synthetic reference',base_url:'http://127.0.0.1:'+upstream.address().port,api_key:'synthetic_reference',multiplier:1});
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.name));
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
  await page.locator('#configs button').click();await page.locator('#configs').getByText(/已暂停/).waitFor();
  await page.locator('#confirm').check();await page.locator('#manualcheck').click();await page.locator('#results strong').filter({hasText:'固定输入双端用量未发现异常'}).waitFor();
  check(calls===2,'Manual paired test has two actual HTTP requests');
  check(!(await page.locator('#results article').first().innerText()).includes('2048'),'Metrics hidden in folded evidence');
  await page.locator('#results summary').first().click();check((await page.locator('#results pre').first().textContent()).includes('2048'),'Folded trace contains reported usage');
  await page.locator('#refresh').click();await page.waitForTimeout(100);check(await page.locator('#results details').first().evaluate(e=>e.open),'Refresh preserves open evidence');
  hold=true;await page.locator('#confirm').check();await page.locator('#manualcheck').click();
  await page.waitForFunction(()=>[...document.querySelectorAll('#results article')].some(e=>e.innerText.includes('running')));
  await page.locator('#results article').first().getByRole('button',{name:'停止'}).click();
  await page.waitForFunction(()=>document.querySelector('#results article')?.innerText.includes('cancelled'));
  check(calls<=4,'Cancelled job remains bounded');
  hold=false;
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
