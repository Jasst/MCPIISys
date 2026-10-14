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
  const server=(url,opts={})=>{
    if(url==='/health') return json({status:'ok'});
    if(url==='/ai/notifications/poll') return json({notifications:[]});
    if(url==='/auth/status') return json({required:false,authenticated:true,uid:null});
    if(url==='/ai/ui-state') return json({updated:0,sessions:[],messages:{}});
    if(url==='/ai/chat'&&opts.method==='POST'){
      const enc=new TextEncoder();
      const ev=[{reasoning_token:'Сначала '},{reasoning_token:'посчитаю.'},{token:'Ответ: 4'},'[DONE]'];
      const rs=new ReadableStream({async start(c){
        for(const e of ev){ await wait(40); c.enqueue(enc.encode('data: '+(typeof e==='string'?e:JSON.stringify(e))+'\n\n')); }
        c.close();
      }});
      return new Response(rs,{status:200});
    }
    return new Response('nope',{status:404});
  };
  const p=openPage({},server);
  await wait(60);
  check('R0 reasoning is on by default', p.$('#toggleReason').getAttribute('aria-pressed')==='true');
  p.$('#input').value='2+2?'; p.$('#input').dispatchEvent(new p.w.Event('input'));
  p.$('#sendBtn').click();
  await wait(60);
  const d1=p.$('.msg.bot details.reasoning');
  check('R1 while thinking: block open and labelled "Думаю"', d1 && d1.open && d1.querySelector('summary').textContent==='Думаю…', d1&&d1.querySelector('summary').textContent);
  await wait(200);
  const d2=p.$('.msg.bot details.reasoning');
  check('R2 after answer starts: block collapsed with duration', d2 && !d2.open && /Размышлял \d+ с/.test(d2.querySelector('summary').textContent), d2&&d2.querySelector('summary').textContent);
  check('R3 answer text shown', p.$('.msg.bot .markdown-body') && p.$('.msg.bot .markdown-body').textContent.includes('Ответ: 4'));
  const fails=results.filter(r=>!r).length;
  console.log(`\n${results.length-fails}/${results.length} passed`);
  process.exit(fails?1:0);
})().catch(e=>{console.error('CRASH',e);process.exit(2)});
