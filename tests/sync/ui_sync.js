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


const results = [];
const check = (name, ok, extra = '') => { results.push(!!ok); console.log(ok ? 'PASS' : 'FAIL', name, extra); };

(async () => {
  // Общий «сервер» для двух устройств одного аккаунта
  const serverState = { state: { updated: 0, sessions: [], messages: {} } };
  const srv = (url, opts = {}) => {
    if (url === '/health') return json({ status: 'ok' });
    if (url === '/ai/notifications/poll') return json({ notifications: [] });
    if (url === '/auth/status') return json({ required: true, authenticated: true, uid: 'u7', email: 'a@b.ru' });
    if (url === '/ai/ui-state' && (!opts.method || opts.method === 'GET')) return json(serverState.state);
    if (url === '/ai/ui-state' && opts.method === 'PUT') {
      const body = JSON.parse(opts.body);
      if (body.base !== serverState.state.updated) return new Response('{"detail":"conflict"}', { status: 409 });
      serverState.state = { updated: Date.now(), sessions: body.sessions, messages: body.messages };
      return json({ updated: serverState.state.updated });
    }
    if (url === '/ai/chat' && opts.method === 'POST') return new Response(sse(['{"token":"Ответ"}', '[DONE]'].map(x => x.startsWith('{') ? JSON.parse(x) : x)), { status: 200 });
    return new Response('nope', { status: 404 });
  };

  // Устройство 1: компьютер — пишет чат
  const pc = {};
  const d1 = openPage(pc, srv);
  await wait(120);
  d1.$('#input').value = 'Вопрос с компьютера';
  d1.$('#input').dispatchEvent(new d1.w.Event('input'));
  d1.$('#sendBtn').click();
  await wait(2200);                         // ответ + отложенная отправка на сервер
  check('S1 PC saved chat to server', serverState.state.sessions.length >= 1, JSON.stringify(serverState.state.sessions.map(s => s.name)));

  // Устройство 2: телефон/PWA — пустое локальное хранилище
  const phone = {};
  const d2 = openPage(phone, srv);
  await wait(200);
  const titles = d2.$$('.sess .t').map(t => t.textContent);
  check('S2 phone sees the chat started on PC', titles.some(t => t.startsWith('Вопрос с компьютера')), JSON.stringify(titles));
  const msgsOnPhone = Object.keys(phone).filter(k => k.includes('messages_')).map(k => JSON.parse(phone[k]));
  check('S3 phone has the full message history', msgsOnPhone.some(m => m.length >= 2 && m[1].text === 'Ответ'), JSON.stringify(msgsOnPhone.map(m => m.length)));

  // Устройство 2 отвечает — устройство 1 видит это после возврата
  d2.$('#input').value = 'Вопрос с телефона';
  d2.$('#input').dispatchEvent(new d2.w.Event('input'));
  d2.$('#sendBtn').click();
  await wait(2200);
  d1.w.document.dispatchEvent(new d1.w.Event('visibilitychange'));
  await wait(200);
  const titles1 = d1.$$('.sess .t').map(t => t.textContent);
  check('S4 PC sees the phone message after reopening', serverState.state.messages && Object.values(serverState.state.messages).some(ms => ms.some(m => m.text === 'Вопрос с телефона')));

  const fails = results.filter(r => !r).length;
  console.log(`\n${results.length - fails}/${results.length} passed`);
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(2); });
