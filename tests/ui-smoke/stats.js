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

const results = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS' : 'FAIL'), name, extra); };
const wait2 = (ms) => new Promise(r => setTimeout(r, ms));
const $m = (s) => w.document.querySelector(s);
const $all = (s) => Array.from(w.document.querySelectorAll(s));

// Сервер шлёт финальный status с usage (как llm_client.py после изменения)
chatScript = [
  { token: 'Ответ' },
  { gen_phase: { phase: 'done', elapsed: 4.2, tokens: 318, prompt_tokens: 1240, tps: 75.6, context_length: 32768 } },
  '[DONE]',
];

(async () => {
  await wait2(60);
  await (async () => {
    $m('#input').value = 'Проверка метрик';
    $m('#input').dispatchEvent(new w.Event('input'));
    $m('#input').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  })();
  await wait2(300);
  const stat = $m('.msg.bot .msg-stats');
  check('stats line rendered under finished answer', !!stat, stat && stat.textContent);
  const txt = stat ? stat.textContent : '';
  check('shows elapsed seconds', txt.includes('4.2'), txt);
  check('shows prompt tokens (1 240)', /1\s?240/.test(txt), txt);
  check('shows output tokens (318)', txt.includes('318'), txt);
  check('shows speed (75.6 ток/с)', txt.includes('75.6 ток/с'), txt);
  check('shows context fill (1558/32768 = 5%)', txt.includes('контекст 5%'), txt);
  check('tooltip explains context', stat && stat.title.includes('32'), stat && stat.title);

  // Без usage (старый сервер / обрыв) — строки нет, сообщение не ломается
  chatScript = [{ token: 'Без метрик' }, '[DONE]'];
  $m('#input').value = 'Второй';
  $m('#input').dispatchEvent(new w.Event('input'));
  $m('#input').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
  await wait2(300);
  const bots = $all('.msg.bot');
  const last = bots[bots.length - 1];
  check('no stats line when usage absent', !last.querySelector('.msg-stats'));
  check('answer still rendered without stats', last.textContent.includes('Без метрик'));

  // Композеру выставляется высота для отступов
  const h = w.document.documentElement.style.getPropertyValue('--composer-h');
  check('--composer-h set from composer height', /px$/.test(h), h);

  const ok = results.every(Boolean);
  console.log(`\n${results.filter(Boolean).length}/${results.length} passed`);
  process.exit(ok ? 0 : 1);
})();
