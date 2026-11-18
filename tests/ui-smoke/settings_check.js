const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');
const root = require('path').join(__dirname, '../../static');
const html = fs.readFileSync(path.join(root, 'ai.html'), 'utf8')
  .replace(/<link[^>]*cdnjs[^>]*>/g, '')
  .replace(/<script src="https:\/\/cdnjs[^"]*"><\/script>/g, '')
  .replace(/<script src="https:\/\/cdn\.jsdelivr[^"]*"><\/script>/g, '')
  .replace(/<link rel="stylesheet" href="\/static\/style.css">/, '')
  .replace(/<link rel="stylesheet" href="\/static\/ai.css">/, '')
  .replace(/<script src="\/static\/js\/ai-app.js" defer><\/script>/, '');

const dom = new JSDOM(html, { url: 'http://127.0.0.1:8000/', runScripts: 'outside-only', pretendToBeVisual: true });
const w = dom.window;
w.TextDecoder = TextDecoder; w.TextEncoder = TextEncoder; w.Response = Response; w.ReadableStream = ReadableStream;
w.eval(fs.readFileSync(path.join(__dirname, 'node_modules/dompurify/dist/purify.js'), 'utf8'));
w.eval(fs.readFileSync(path.join(__dirname, 'node_modules/marked/marked.min.js'), 'utf8'));
w.hljs = { highlightElement() {} };
w.requestAnimationFrame = (cb) => setTimeout(cb, 0);
w.confirm = () => true;
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; };
w.URL.createObjectURL = () => 'blob:x';
w.HTMLAnchorElement.prototype.click = function () { if (this.download) this.dataset.downloaded = this.download; };
w.URL.revokeObjectURL = () => {};
const clip = [];
Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async (t) => { clip.push(t); } }, configurable: true });

const storage = {};
Object.defineProperty(w, 'localStorage', { value: {
  getItem: (k) => (k in storage ? storage[k] : null),
  setItem: (k, v) => { storage[k] = String(v); },
  removeItem: (k) => { delete storage[k]; },
}});

const calls = [];
let chatScript = null, slowMs = 0;
function sseBody(events, delay) {
  const enc = new TextEncoder();
  return new ReadableStream({ async start(c) {
    for (const e of events) {
      if (delay) await new Promise(r => setTimeout(r, delay));
      c.enqueue(enc.encode(`data: ${typeof e === 'string' ? e : JSON.stringify(e)}\n\n`));
    }
    c.close();
  }});
}
w.fetch = async (url, opts = {}) => {
  calls.push([url, opts.method || 'GET']);
  if (url === '/health') return new Response(JSON.stringify({ status: 'ok' }), { status: 200 });
  if (url === '/ai/notifications/poll') return new Response(JSON.stringify({ notifications: [{ text: 'Нашёл статью про память' }] }), { status: 200 });
  if (url === '/ai/chat') {
    const body = JSON.parse(opts.body);
    calls.push(['body', body]);
    const script = chatScript || [{ token: 'Привет' }, { token: ', ' }, { token: '**мир**' }, '[DONE]'];
    return new Response(sseBody(script, slowMs), { status: 200 });
  }
  return new Response('nope', { status: 404 });
};

for (const f of ['js/ui.js', 'js/ai-store.js']) w.eval(fs.readFileSync(path.join(root, f), 'utf8'));
w.eval(fs.readFileSync(path.join(root, 'js/ai-app.js'), 'utf8'));

const $ = (s) => w.document.querySelector(s);
const $$ = (s) => Array.from(w.document.querySelectorAll(s));
const wait = (ms) => new Promise(r => setTimeout(r, ms));

const q=(s)=>w.document.querySelector(s);
const results=[]; const check=(n,ok,x='')=>{results.push(!!ok);console.log(ok?'PASS':'FAIL',n,x);};
const wait3=(ms)=>new Promise(r=>setTimeout(r,ms));
const set = (key, val) => {
  const el = w.document.querySelector(`[data-key="${key}"]`);
  if (el.type === 'checkbox') { el.checked = val; } else { el.value = val; }
  el.dispatchEvent(new w.Event('change'));
};
(async()=>{
  await wait3(60);
  set('fontSize','l');
  check('fontSize l -> body[data-fs=l]', w.document.body.dataset.fs==='l', w.document.body.dataset.fs);
  set('fontSize','s');
  check('fontSize s -> body[data-fs=s]', w.document.body.dataset.fs==='s');
  set('compact',true);
  check('compact on -> body.compact', w.document.body.classList.contains('compact'));
  set('compact',false);
  check('compact off -> class removed', !w.document.body.classList.contains('compact'));
  set('enterSends',false);

  q('#input').value='строка';
  q('#input').dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  await wait3(40);
  const sent = JSON.parse(w.localStorage.getItem('ai_sessions')||'[]').length;
  check('enterSends off: plain Enter does NOT send', q('#input').value==='строка', 'value='+q('#input').value);
  set('enterSends',true);
  check('enterSends setting persisted', JSON.parse(w.localStorage.getItem('ai_settings_v2')||'{}').enterSends===true);
  set('webDefault',true);
  check('webDefault on -> toggleWeb pressed', q('#toggleWeb').getAttribute('aria-pressed')==='true');
  set('reasoningDefault',true);
  check('reasoningDefault on -> toggleReason pressed', q('#toggleReason').getAttribute('aria-pressed')==='true');
  set('notifyInterval','60');
  check('notifyInterval persisted', JSON.parse(w.localStorage.getItem('ai_settings_v2')||'{}').notifyInterval==='60');
  set('notify',false);
  check('notify off persisted', JSON.parse(w.localStorage.getItem('ai_settings_v2')||'{}').notify===false);
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
})();
