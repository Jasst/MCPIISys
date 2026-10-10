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
const sessionsOf = (storage) => JSON.parse(storage['ai_sessions'] || '[]');
const msgsOf = (storage, id) => JSON.parse(storage['ai_messages_' + id] || '[]');

(async () => {
  // ---------- A: новый чат сразу появляется; переключение и блокировка во время генерации
  {
    const storage = {};
    let slow = [{ token: 'Ответ ' }, { token: 'по ' }, { token: 'частям' }, '[DONE]'];
    const server = (url, opts) => {
      if (url === '/health') return json({ status: 'ok' });
      if (url === '/ai/notifications/poll') return json({ notifications: [] });
      if (url === '/ai/chat' && opts.method === 'POST') return new Response(sse(slow, 60), { status: 200 });
      return new Response('nope', { status: 404 });
    };
    const p = openPage(storage, server);
    await wait(40);
    check('A1 fresh start creates an open "Новый чат"', sessionsOf(storage).length === 1 && p.$('#chatTitle').textContent === 'Новый чат');

    // Отправляем в первом чате
    p.$('#input').value = 'Первый вопрос';
    p.$('#input').dispatchEvent(new p.w.Event('input'));
    p.$('#input').dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await wait(50);
    const chatA = sessionsOf(storage).find(s => s.name.startsWith('Первый'));
    check('A2 chat renamed from first message and sending started', !!chatA);
    check('A3 send button switched to stop', p.$('#sendBtn').classList.contains('stop'));

    // Во время генерации: "Новый чат" должен появиться и открыться
    p.$('#newChatBtn').click();
    await wait(20);
    const titles = sessionsOf(storage).map(s => s.name);
    check('A4 new chat appears during generation', titles.includes('Новый чат') && p.$('#chatTitle').textContent === 'Новый чат', titles.join(' | '));
    check('A5 hint offers to open busy chat', !p.$('#hintBtn').hidden);

    // Отправка в другом чате заблокирована, запросов нет
    const before = p.calls.filter(c => c.url === '/ai/chat' && c.method === 'POST').length;
    p.$('#input').value = 'Вопрос во втором';
    p.$('#input').dispatchEvent(new p.w.Event('input'));
    p.$('#input').dispatchEvent(new p.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await wait(10);
    const after = p.calls.filter(c => c.url === '/ai/chat' && c.method === 'POST').length;
    check('A6 send in another chat blocked while busy', before === 1 && after === 1, `before=${before} after=${after}`);

    // Возвращаемся в первый чат кнопкой «Открыть» — увидим идущий ответ
    p.$('#hintBtn').click();
    await wait(20);
    check('A7 "Открыть" returns to busy chat', p.$('#chatTitle').textContent.startsWith('Первый'));

    // Ждём окончания
    await wait(400);
    const a = msgsOf(storage, chatA.id);
    check('A8 finished answer stored in full', a.length === 2 && a[1].text === 'Ответ по частям' && a[1].pending === false, JSON.stringify(a[1] && a[1].text));
    check('A9 answer visible after finish', p.$$('.msg.bot').length === 1 && p.$('.msg.bot .bubble').textContent.includes('Ответ по частям'));
    check('A10 generation released, send works again', !p.$('#sendBtn').classList.contains('stop'));

    // Переключение на другой чат во время генерации не теряет поток
    slow = [{ token: 'Долго' }, { token: ' ' }, { token: 'думаю' }, '[DONE]'];
    p.$('#input').value = 'Ещё вопрос';
    p.$('#input').dispatchEvent(new p.w.Event('input'));
    p.$('#sendBtn').click();
    await wait(30);
    p.$('#newChatBtn').click();
    await wait(200);
    p.$$('.sess').find(s => s.textContent.startsWith('Первый')).click();
    await wait(300);
    const chatA2 = msgsOf(storage, chatA.id);
    check('A11 background generation completes after switching away', chatA2[chatA2.length - 1].text === 'Долго думаю' && chatA2[chatA2.length - 1].pending === false, JSON.stringify(chatA2[chatA2.length - 1].text));
  }

  // ---------- B: перезагрузка во время генерации — ответ дописывается при возврате
  {
    const storage = {};
    const chatId = 'ai_test_chat';
    storage['ai_sessions'] = JSON.stringify([{ id: chatId, name: 'Вопрос', created: Date.now(), updated: Date.now() }]);
    storage['ai_messages_' + chatId] = JSON.stringify([
      { role: 'user', text: 'Вопрос', time: Date.now() },
      { role: 'assistant', text: 'При', reasoning: '', sources: [], images: [], notes: [], error: null, time: Date.now(), pending: true },
    ]);
    const server = (url) => {
      if (url === '/health') return json({ status: 'ok' });
      if (url === '/ai/notifications/poll') return json({ notifications: [] });
      if (url === '/ai/chat/attach') return new Response(sse([{ token: 'Привет' }, { token: ', мир' }, '[DONE]'], 30), { status: 200 });
      return new Response('nope', { status: 404 });
    };
    const p = openPage(storage, server);
    await wait(200);
    const msgs = msgsOf(storage, chatId);
    check('B1 reload: interrupted answer completed from server replay', msgs[1].text === 'Привет, мир' && msgs[1].pending === false, JSON.stringify(msgs[1].text));
    check('B2 attach used, not a new /ai/chat POST', p.calls.some(c => c.url === '/ai/chat/attach') && !p.calls.some(c => c.url === '/ai/chat' && c.method === 'POST'));
    check('B3 finished answer shown after reload', p.$('.msg.bot .bubble').textContent.includes('Привет, мир'));
  }

  // ---------- C: перезагрузка, когда сервер уже закончил — берём last_response
  {
    const storage = {};
    const chatId = 'ai_test_chat2';
    storage['ai_sessions'] = JSON.stringify([{ id: chatId, name: 'Вопрос 2', created: Date.now(), updated: Date.now() }]);
    storage['ai_messages_' + chatId] = JSON.stringify([
      { role: 'user', text: 'Вопрос 2', time: Date.now() },
      { role: 'assistant', text: '', reasoning: '', sources: [], images: [], notes: [], error: null, time: Date.now(), pending: true },
    ]);
    const server = (url) => {
      if (url === '/health') return json({ status: 'ok' });
      if (url === '/ai/notifications/poll') return json({ notifications: [] });
      if (url === '/ai/chat/attach') return new Response(sse([{ no_active_generation: true }]), { status: 200 });
      if (url === '/ai/chat/last_response') return json({ user: 'Вопрос 2', assistant: 'Готовый ответ с сервера' });
      return new Response('nope', { status: 404 });
    };
    const p = openPage(storage, server);
    await wait(200);
    const msgs = msgsOf(storage, chatId);
    check('C1 server already finished: answer recovered from last_response', msgs[1].text === 'Готовый ответ с сервера' && !msgs[1].pending, JSON.stringify(msgs[1]));
    check('C2 no error shown', !msgs[1].error);
  }

  // ---------- D: остановка — отдельный маршрут на сервере, ответ помечен
  {
    const storage = {};
    let stopped = false;
    const server = (url, opts) => {
      if (url === '/health') return json({ status: 'ok' });
      if (url === '/ai/notifications/poll') return json({ notifications: [] });
      if (url === '/ai/chat' && opts.method === 'POST') return new Response(sse([{ token: 'Начало' }, { token: ' ещё' }, { token: ' тут' }], 80), { status: 200 });
      if (url === '/ai/chat/stop') { stopped = true; return json({ stopped: true }); }
      return new Response('nope', { status: 404 });
    };
    const p = openPage(storage, server);
    await wait(30);
    p.$('#input').value = 'Стоп-тест';
    p.$('#input').dispatchEvent(new p.w.Event('input'));
    p.$('#sendBtn').click();
    await wait(120);
    p.$('#sendBtn').click();            // в режиме «стоп»
    await wait(200);
    const id = sessionsOf(storage)[0].id;
    const m = msgsOf(storage, id);
    check('D1 stop calls server stop route', stopped);
    check('D2 stopped answer keeps partial text and is marked', m[1].notes.some(n => n.includes('Остановлено')) && !m[1].pending, JSON.stringify(m[1].notes));
    check('D3 send button back to send mode', !p.$('#sendBtn').classList.contains('stop'));
  }

  const fails = results.filter(r => !r).length;
  console.log(`\n${results.length - fails}/${results.length} passed`);
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error('CRASH', e); process.exit(2); });
