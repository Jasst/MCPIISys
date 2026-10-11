const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const root = require('path').join(__dirname, '../../static');
const HTML = fs.readFileSync(path.join(root, 'ai.html'), 'utf8')
  .replace(/<link[^>]*cdnjs[^>]*>/g, '')
  .replace(/<script src="https:\/\/cdnjs[^"]*"><\/script>/g, '')
  .replace(/<script src="https:\/\/cdn\.jsdelivr[^"]*"><\/script>/g, '')
  .replace(/<link rel="stylesheet" href="\/static\/style.css">/, '')
  .replace(/<link rel="stylesheet" href="\/static\/ai.css">/, '')
  .replace(/<script src="\/static\/js\/ai-app.js" defer><\/script>/, '');
const APP = fs.readFileSync(path.join(root, 'js/ai-app.js'), 'utf8');
const PURIFY = fs.readFileSync(path.join(__dirname, 'node_modules/dompurify/dist/purify.js'), 'utf8');
const MARKED = fs.readFileSync(path.join(__dirname, 'node_modules/marked/marked.min.js'), 'utf8');
const wait = (ms) => new Promise(r => setTimeout(r, ms));

// Один "браузер": общий storage между перезагрузками, свой набор маршрутов сервера
function openPage(storage, server) {
  const dom = new JSDOM(HTML, { url: 'http://127.0.0.1:8000/', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  const calls = [];
  w.TextDecoder = TextDecoder; w.TextEncoder = TextEncoder; w.Response = Response; w.ReadableStream = ReadableStream;
  w.eval(PURIFY); w.eval(MARKED);
  w.hljs = { highlightElement() {} };
  w.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  w.confirm = () => true;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.URL.createObjectURL = () => 'blob:x'; w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () {};
  Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async () => {} }, configurable: true });
  Object.defineProperty(w, 'localStorage', { value: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  }});
  w.fetch = async (url, opts = {}) => {
    calls.push({ url, method: (opts.method || 'GET') });
    return server(url, opts, calls);
  };
  w.eval(APP);
  const $ = (s) => w.document.querySelector(s);
  const $$ = (s) => Array.from(w.document.querySelectorAll(s));
  return { w, $, $$, calls, storage };
}

const sse = (events, delay = 0) => {
  const enc = new TextEncoder();
  return new ReadableStream({ async start(c) {
    for (const e of events) {
      if (delay) await wait(delay);
      c.enqueue(enc.encode(`data: ${typeof e === 'string' ? e : JSON.stringify(e)}\n\n`));
    }
    c.close();
  }});
};
const json = (o) => new Response(JSON.stringify(o), { status: 200 });


const results=[]; const check=(n,ok,x='')=>{results.push(!!ok);console.log(ok?'PASS':'FAIL',n,x)};
(async()=>{
  const calls=[];
  const srv=(url,opts={})=>{
    calls.push(url+' '+(opts.method||'GET'));
    if(url==='/health') return json({status:'ok'});
    if(url==='/ai/notifications/poll') return json({notifications:[]});
    if(url==='/auth/status') return json({required:true,authenticated:true,uid:'u3',email:'x@y.ru'});
    if(url==='/ai/ui-state') return json({updated:0,sessions:[],messages:{}});
    if(url==='/auth/logout') return json({ok:true});
    return new Response('nope',{status:404});
  };
  const p=openPage({},srv);
  await wait(120);
  p.$('#logoutBtn').click();
  await wait(20);
  check('L1 click opens confirmation, no logout yet', p.$('#logoutDialog').open === true && !calls.includes('/auth/logout POST'));
  p.$('#logoutDialog').close();
  check('L2 cancel keeps session', !calls.includes('/auth/logout POST'));
  p.$('#logoutBtn').click();
  p.$('#logoutConfirm').click();
  await wait(50);
  check('L3 confirm performs logout', calls.includes('/auth/logout POST'));
  const fails=results.filter(r=>!r).length;
  console.log(`\n${results.length-fails}/${results.length} passed`);
  process.exit(fails?1:0);
})().catch(e=>{console.error('CRASH',e);process.exit(2)});
