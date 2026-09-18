const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path'),http=require('node:http'),net=require('node:net');
const {spawn}=require('node:child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..'),data=fs.mkdtempSync(path.join(os.tmpdir(),'protocol-ui-'));
const requests=[],held=new Set();let app,appExit,browser;
const upstream=http.createServer(async(req,res)=>{
  let raw='';for await(const c of req)raw+=c;const body=JSON.parse(raw);requests.push({path:req.url,model:body.model,stream:body.stream,tool:Boolean(body.tools)});
  if(req.headers.authorization==='Bearer synthetic-held-key'){held.add(res);res.on('close',()=>held.delete(res));return;}
  assert.equal(body.model,'synthetic-upstream');
  const usage={input_tokens:9,output_tokens:2};let value,events;
  if(req.url==='/v1/alpha/search'){
    assert.equal(body.commands.search_query.length,1);assert.equal(body.commands.response_length,'short');assert.ok(!body.stream&&body.id);
    value={output:'RFC 9110 HTTP Semantics (https://www.rfc-editor.org/rfc/rfc9110.html)\n【turn0search0】 [wordlim: 200] Standard reference.'};
  }else if(req.url==='/v1/responses'){
    assert.equal(body.store,false);assert.equal(body.max_output_tokens,2048);
    value={status:'completed',model:body.model,usage,output:body.tools?[{type:'function_call',call_id:'fixture-call',name:'protocol_probe',arguments:'{"marker":"ready"}'}]:[{type:'message',content:[{type:'output_text',text:'READY'}]}]};
    events=[{type:'response.output_text.delta',delta:'READY'},{type:'response.completed',response:value}];
  }else if(req.url==='/v1/messages'){
    assert.equal(req.headers['anthropic-version'],'2023-06-01');assert.ok(req.headers['x-api-key']);
    value={type:'message',model:body.model,usage,content:body.tools?[{type:'tool_use',id:'fixture-tool',name:'protocol_probe',input:{marker:'ready'}}]:[{type:'text',text:'READY'}],stop_reason:body.tools?'tool_use':'end_turn'};
    events=[{type:'message_start',message:{...value,content:[],stop_reason:null}},{type:'content_block_start',index:0,content_block:{type:'text',text:''}},{type:'content_block_delta',index:0,delta:{type:'text_delta',text:'READY'}},{type:'content_block_stop',index:0},{type:'message_delta',delta:{stop_reason:'end_turn'},usage:{output_tokens:2}},{type:'message_stop'}];
  }else{
    assert.equal(req.url,'/v1/chat/completions');
    const usage={prompt_tokens:9,completion_tokens:2};
    value={model:body.model,choices:[{message:{content:'READY'},finish_reason:'stop'}],usage};
    events=[{model:body.model,choices:[{index:0,delta:{content:'READY'},finish_reason:null}]},{choices:[{index:0,delta:{},finish_reason:'stop'}],usage},'[DONE]'];
  }
  if(body.stream){res.writeHead(200,{'Content-Type':'text/event-stream'});res.end(events.map(e=>'data: '+(typeof e==='string'?e:JSON.stringify(e))+'\n\n').join(''));}
  else{res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(value));}
});
async function freePort(){const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r));return port;}
(async()=>{
  await new Promise(r=>upstream.listen(0,'127.0.0.1',r));const upstreamUrl=`http://127.0.0.1:${upstream.address().port}/v1`,port=await freePort(),base=`http://127.0.0.1:${port}`;
  async function startApp(mode){
    app=spawn(process.env.PYTHON_EXECUTABLE,['-B','run.py','--app',mode,'--port',String(port)],{cwd:root,env:{...process.env,PLATFORM_DATA_DIR:data,PLATFORM_EGRESS_ALLOWLIST:'127.0.0.1',PLATFORM_USERNAME:'',PLATFORM_PASSWORD:''},stdio:['ignore','pipe','pipe']});
    app.stdout.resume();app.stderr.resume();appExit=new Promise(resolve=>{app.once('exit',resolve);app.once('error',resolve);});
    let ready=false;for(let i=0;i<100;i++){if(app.exitCode!==null)throw new Error('Owned server exited');try{if((await fetch(base+'/api/health')).ok){ready=true;break;}}catch{}await new Promise(r=>setTimeout(r,100));}assert.ok(ready);
  }
  await startApp('admission');
  browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.name));
  await page.goto(base+'/channels/');await page.locator('#add').click();await page.locator('#url').fill(upstreamUrl);await page.locator('#name').fill('Synthetic protocol candidate');await page.locator('#key').fill('synthetic-saved-key');await page.locator('#multiplier').fill('1');
  await page.locator('#channel-upstream_type').selectOption('newapi');await page.locator('#channel-proposed_type').selectOption('newapi');await page.locator('#channel-confirmation_source').selectOption('supplier');await page.locator('#channel-confirmed_on').fill('2026-01-01');await page.locator('#save').click();await page.locator('#editor').waitFor({state:'hidden'});
  await page.getByRole('link',{name:'协议准入',exact:true}).click();await page.getByText('准备就绪。先预览请求数量，再开始探测。',{exact:true}).waitFor();
  assert.equal(await page.locator('#probe-upstream_type').inputValue(),'newapi');assert.equal(await page.locator('#temporary').isVisible(),false);
  await page.locator('#models').fill('synthetic-model=synthetic-upstream');for(const id of ['codex_standard','codex_search','openai_common','claude'])await page.locator('#use-'+id).check();
  await page.locator('#preview').click();await page.locator('#start:not([disabled])').waitFor();assert.match(await page.locator('#preview-list').textContent(),/共 10 次请求/);
  await page.locator('#models').fill('changed=synthetic-upstream');assert.equal(await page.locator('#start').isDisabled(),true);await page.locator('#models').fill('synthetic-model=synthetic-upstream');
  await page.locator('#mode').selectOption('live');await page.locator('#confirm-live').check();await page.locator('#preview').click();await page.locator('#start:not([disabled])').waitFor();await page.locator('#start').click();await page.getByText('本轮协议探测已结束。',{exact:true}).waitFor();
  assert.equal(requests.length,10);assert.equal(await page.locator('#results tr').count(),10);assert.equal(await page.locator('#group-results .internal_only').count(),4);
  const dataJson=await(await fetch(base+new URL(await page.locator('#export-json').getAttribute('href'),page.url()).pathname)).json();assert.equal(dataJson.conclusion.production_ready,false);assert.ok(dataJson.probes.every(p=>p.status==='passed'));assert.ok(!JSON.stringify(dataJson).includes('synthetic-saved-key'));
  const downloadPromise=page.waitForEvent('download');await page.locator('#export-html').click();const downloaded=await downloadPromise;const html=fs.readFileSync(await downloaded.path(),'utf8');assert.ok(html.includes('仅允许内部测试')&&!html.includes('synthetic-saved-key'));
  await page.screenshot({path:path.join(data,'protocol-desktop.png'),fullPage:true});await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+2),false);await page.screenshot({path:path.join(data,'protocol-mobile.png'),fullPage:true});
  await page.locator('#channel').selectOption('');await page.locator('#base-url').fill(upstreamUrl);await page.locator('#api-key').fill('synthetic-held-key');await page.locator('#confirm-live').check();await page.locator('#preview').click();await page.locator('#start:not([disabled])').waitFor();await page.locator('#start').click();
  for(let i=0;i<100&&!held.size;i++)await new Promise(r=>setTimeout(r,10));assert.equal(held.size,1);await page.locator('#stop').click();await page.getByText('本轮协议探测已结束。',{exact:true}).waitFor();assert.equal(requests.length,11);assert.equal(await page.locator('#api-key').inputValue(),'');
  await page.reload();await page.getByText('准备就绪。先预览请求数量，再开始探测。',{exact:true}).waitFor();assert.equal(await page.locator('#history button').count(),2);assert.deepEqual(errors,[]);
  app.kill('SIGTERM');await appExit;await startApp('channels');await page.goto(base+'/channels/');await page.locator('#list article.record').waitFor();assert.equal(await page.getByRole('link',{name:'协议准入',exact:true}).count(),0);assert.equal((await fetch(base+'/admission/protocol/')).status,404);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({status:'passed',checks:16,mockRequests:requests.length,real_upstream_tested:false,evidence:data}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();for(const res of held)res.destroy();if(app&&app.exitCode===null){app.kill('SIGTERM');await appExit;}upstream.closeAllConnections();await new Promise(r=>upstream.close(r));});
