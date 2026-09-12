'use strict';
// Source-level UI contract tests with a small DOM fixture; no browser or real requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'relay_lab/web/index.html'), 'utf8');
class Element {
  constructor(value = '') {
    this.value = value; this.innerHTML = ''; this.textContent = ''; this.hidden = false;
    this.disabled = false; this.checked = false; this.style = {setProperty(name,value) {this[name]=value;}}; this.dataset = {}; this.handlers = {};
    this.classList = {toggle() {}}; this.options = new Map();
  }
  addEventListener(name, action) {this.handlers[name] = action;}
  setAttribute(name, value) {this[name] = value;}
  removeAttribute(name) {delete this[name];}
  closest() {return this.parent ??= new Element();}
  querySelector(selector) {if (!this.options.has(selector)) this.options.set(selector, new Element()); return this.options.get(selector);}
}
const elements = new Map();
for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
  assert(!elements.has(match[3]), 'Duplicate HTML ID: ' + match[3]);
  let value = match[2].match(/\bvalue="([^"]*)"/)?.[1] ?? '';
  if (match[1] === 'select') {
    const rest = html.slice(match.index + match[0].length).split('</select>')[0];
    value = rest.match(/<option\b[^>]*value="([^"]*)"[^>]*selected/)?.[1] ?? rest.match(/<option\b[^>]*value="([^"]*)"/)?.[1] ?? '';
  }
  elements.set(match[3], new Element(value));
}
const get = id => {assert(elements.has(id), 'Missing HTML element: ' + id); return elements.get(id);};
const live = new Element('live'), mock = new Element('mock'), eyebrow = new Element();
mock.checked = true;
const document = {
  getElementById: get,
  querySelector(selector) {
    if (selector === '.eyebrow') return eyebrow;
    if (selector.includes(':checked')) return live.checked ? live : mock;
    if (selector.includes('value="live"')) return live;
    if (selector.includes('value="mock"')) return mock;
    throw Error('Unexpected selector: ' + selector);
  },
  querySelectorAll(selector) {return selector === '.nav-item' ? [] : [live, mock];}
};
const burst = {planned_requests:10,issued_requests:10,finished_requests:2,reference_capacity:5,
  peak_inflight:10,peak_receiving:5,total_output_chars:64,launch_spread_ms:2,elapsed_seconds:3,
  release_window_seconds:10,release_observations:[{released_label:'S1',at_seconds:1,
    waiting_labels:['L1'],later_output:[{label:'L1',delay_seconds:.1}],ended_without_output:[]}],
  timeline:[{label:'S1',profile:'short',state:'complete',http_status:200,start_seconds:0,first_output_seconds:.2,
    end_seconds:1,wait_seconds:.2,output_chars:64,after_release:null,max_gap_seconds:.01,silent_seconds:.01},
    {label:'L1',profile:'long',state:'receiving',http_status:200,start_seconds:.001,first_output_seconds:1.1,
    end_seconds:null,wait_seconds:1.099,output_chars:100,after_release:'S1',release_delay_seconds:.1,max_gap_seconds:.02,silent_seconds:.01}]};
const job = {id:'a'.repeat(32),name:'单号测试',environment:'mock',status:'running',started_at:1,elapsed_seconds:3,
  metrics:{requests:2,success_rate:1,completeness_rate:1,p50_ttft_ms:200,p95_latency_ms:1000,errors:{}},
  downloads:[],stages:[],analysis:{},burst,phase:'mixed_burst'};
let submitted, adjusted;
let responseJob;
const context = vm.createContext({document,location:{host:'127.0.0.1:8890'},setInterval() {},setTimeout,clearTimeout,
  fetch:async (url, options) => ({ok:true,json:async () => {
    if (url === '/api/state') return {csrf:'test-session',ui_schema_version:4,jobs:[]};
    if (url === '/api/jobs') {submitted = JSON.parse(options.body); return job;}
    if(url.endsWith('/faders')){adjusted=JSON.parse(options.body);return responseJob;}
    return responseJob||job;
  }})});
vm.runInContext(fs.readFileSync(path.join(root,'relay_lab/web/faders.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(path.join(root,'relay_lab/web/app.js'),'utf8'),context);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('load-mode').value,'faders');
  assert.equal(get('burst-count-fields').hidden,true);
  assert.equal(get('fader-panel').hidden,false);
  vm.runInContext("$('load-mode').value='mixed_burst'; syncLoad()",context);
  assert.equal(get('samples').disabled,true);
  assert.equal(get('timeout').disabled,true);
  assert.equal(get('limit-field').disabled,false);
  assert.match(get('request-estimate').textContent,/同时发出 10 条/);
  vm.runInContext("setMode('pool-test')",context);
  assert.equal(get('burst-fields').hidden,true);
  assert.equal(get('burst-total-timeout').disabled,true);
  vm.runInContext("setMode('account-test'); $('load-mode').value='mixed_burst'; syncLoad()",context);
  await get('test-form').handlers.submit({preventDefault() {}});
  assert.deepEqual(submitted.stages,[10]);
  assert.deepEqual(submitted.mixed_burst.counts,[2,2,6]);
  assert.deepEqual(submitted.mixed_burst.output_limits,[64,512,4096]);
  assert.equal(submitted.mixed_burst.total_timeout,900);
  assert.equal(get('burst-panel').hidden,false);
  assert.match(get('burst-rows').innerHTML,/完整结束/);
  assert.match(get('burst-releases').innerHTML,/L1 在其后 0.10s 开始输出/);
  assert.equal(get('metric-stable-label').textContent,'峰值接收中');
  assert.equal(get('metric-stable').textContent,'5');
  assert.equal(get('form-error').hidden,true);
  const faders={targets:[0,0,3],version:1,accepting:true,paused:false,refill_interval:1,
    issued_requests:3,inflight:3,channels:['short','medium','long'].map(profile=>({profile,waiting:profile==='long'?1:0,receiving:profile==='long'?2:0,complete:0})),
    timeline:[{label:'L3',profile:'long',state:'waiting_headers',http_status:null,wait_seconds:2,start_seconds:0,first_output_seconds:null,end_seconds:null,output_chars:0}],events:[{action:'adjust',at_seconds:0,targets:[0,0,3],paused:false}]};
  responseJob={...job,burst:null,faders,phase:'faders'};
  context.faderJob=responseJob;
  vm.runInContext("$('load-mode').value='faders';active=faderJob.id;renderJob(faderJob);syncLoad()",context);
  assert.equal(get('fader-long-number').value,3);
  assert.equal(get('fader-long').disabled,false);
  assert.equal(get('fader-long-limit').disabled,true);
  assert.match(get('fader-rows').innerHTML,/等待响应头/);
  assert.equal(get('metric-stable-label').textContent,'峰值客户端在途');
  get('fader-short').value='2';get('fader-short').handlers.input();
  await new Promise(resolve=>setTimeout(resolve,260));
  assert.deepEqual(adjusted.targets,[2,0,3]);
  get('fader-pause').handlers.click();
  await new Promise(resolve=>setTimeout(resolve,260));
  assert.equal(adjusted.paused,true);
  console.log('UI contract tests passed: live fader adjustments, pause, request states and locked output settings; form modes, exact cohort payload, live rows, release observations and capacity label.');
})().catch(error => {console.error(error); process.exitCode=1;});
