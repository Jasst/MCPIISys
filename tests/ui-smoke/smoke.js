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

w.eval(fs.readFileSync(path.join(root, 'js/ai-app.js'), 'utf8'));

const $ = (s) => w.document.querySelector(s);
const $$ = (s) => Array.from(w.document.querySelectorAll(s));
const wait = (ms) => new Promise(r => setTimeout(r, ms));
const results = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS' : 'FAIL'), name, extra); };
const chatSession = () => JSON.parse(storage['ai_sessions']).find(s => !s.id.startsWith('ai_notifications') && s.id !== 'ai_notifications');
const storedMsgs = (id) => JSON.parse(storage['ai_messages_' + id] || '[]');
const typeAndSend = async (text) => {
  $('#input').value = text;
  $('#input').dispatchEvent(new w.Event('input'));
  $('#input').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
};

(async () => {
  await wait(60);
  check('welcome on empty start', !!$('.welcome'));
  check('status ok', $('#status').classList.contains('ok'));
  check('notify session appears from poll', $$('.sess').some(s => s.textContent.includes('Уведомления')));

  await typeAndSend('Расскажи про память');
  await wait(120);
  const s1 = chatSession();
  check('chat session created', !!s1 && s1.name.startsWith('Расскажи'), s1 && s1.name);
  check('chat messages stored as 2', storedMsgs(s1.id).length === 2);
  check('bot text assembled (raw markdown kept)', storedMsgs(s1.id)[1].text === 'Привет, **мир**', storedMsgs(s1.id)[1].text);
  check('bot bubble has bold', !!$('.msg.bot .bubble strong'));

  // копирование
  $('.msg.bot .act[data-act="copy"]').click();
  await wait(20);
  check('copy writes raw text to clipboard', clip.includes('Привет, **мир**'), JSON.stringify(clip));

  // повтор ответа
  chatScript = [{ token: 'Второй' }, '[DONE]'];
  $('.msg.bot .act[data-act="regen"]').click();
  await wait(120);
  const m2 = storedMsgs(s1.id);
  check('regenerate: user + new bot only', m2.length === 2 && m2[0].role === 'user' && m2[1].text === 'Второй', JSON.stringify(m2.map(m => m.role + ':' + m.text)));
  check('regenerate resent same prompt', calls.filter(c => c[0] === 'body').pop()[1].message === 'Расскажи про память');

  // остановка: медленный поток
  chatScript = [{ token: 'долго' }, { token: ' ещё' }, { token: ' и ещё' }, '[DONE]'];
  slowMs = 40;
  await typeAndSend('Ещё вопрос');
  await wait(60);
  check('busy: send button in stop mode', $('#sendBtn').classList.contains('stop'));
  $('#sendBtn').click();          // нажатие в режиме «стоп»
  await wait(150);
  check('stop mode released after abort', !$('#sendBtn').classList.contains('stop'));
  const m3 = storedMsgs(s1.id);
  console.log('DEBUG last:', JSON.stringify(m3[m3.length-1]));
  check('aborted reply keeps partial text and note', m3[m3.length - 1].notes.some(n => n.includes('Остановлено')), JSON.stringify(m3[m3.length - 1].notes));
  slowMs = 0;

  // настройки
  const enterBox = $('[data-key="enterSends"]');
  enterBox.checked = false; enterBox.dispatchEvent(new w.Event('change'));
  check('setting saved', JSON.parse(storage['ai_settings_v2']).enterSends === false);
  const webBox = $('[data-key="webDefault"]');
  webBox.checked = true; webBox.dispatchEvent(new w.Event('change'));
  check('webDefault syncs to chip', $('#toggleWeb').getAttribute('aria-pressed') === 'true');

  // новый чат Ctrl+K
  w.document.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  await wait(20);
  check('ctrl+K starts new chat', $('#chatTitle').textContent === 'Новый чат' && !!$('.welcome'));

  // поиск
  $('#searchInput').value = 'Расскажи';
  $('#searchInput').dispatchEvent(new w.Event('input'));
  check('search filters list', $$('.sess').length === 1, String($$('.sess').length));
  $('#searchInput').value = '';
  $('#searchInput').dispatchEvent(new w.Event('input'));

  // переименование через Edit
  $$('.sess').find(s => s.textContent.startsWith('Расскажи')).click();
  await wait(20);
  $('#renameBtn').click();
  $('#chatTitle').textContent = 'Мой вопрос про память';
  $('#chatTitle').dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  await wait(20);
  check('rename saved', chatSession().name === 'Мой вопрос про память', chatSession().name);

  // экспорт
  let exported = false;
  w.URL.createObjectURL = () => { exported = true; return 'blob:x'; };
  $('#exportBtn').click();
  check('export downloads markdown', exported);

  // вложение: файл отклоняется, если не картинка
  const fi = $('#fileInput');
  Object.defineProperty(fi, 'files', { value: [new w.File(['x'], 'a.txt', { type: 'text/plain' })], configurable: true });
  fi.dispatchEvent(new w.Event('change'));
  await wait(20);
  check('non-image attachment rejected', $('#attachChip').hidden === true);

  const fails = results.filter(r => !r).length;
  console.log(`\n${results.length - fails}/${results.length} passed`);
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(2); });
