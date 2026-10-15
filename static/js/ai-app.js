/**
 * ai-app.js — клиент ИИ-ассистента.
 *
 * Модель работы (как в современных ИИ-чатах):
 *  - Генерация идёт в фоне: переключение между чатами, новый чат и даже перезагрузка
 *    страницы её не прерывают. Ответ дописывается в хранилище и виден при возврате.
 *  - Сервер ведёт одну генерацию на пользователя, поэтому одновременно пишется
 *    только один ответ. Пока он идёт, можно открывать любые чаты, но отправить
 *    сообщение в другом чате нельзя (подсказка с кнопкой «Открыть»).
 *  - После перезагрузки или возврата на вкладку незавершённый ответ подхватывается
 *    через /ai/chat/attach, а если сервер уже закончил — через /ai/chat/last_response.
 *
 * API: POST /ai/chat, GET /ai/chat/attach, GET /ai/chat/last_response,
 *      POST /ai/chat/stop, GET /ai/notifications/poll, GET /health
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ константы

  const API = {
    chat: '/ai/chat',
    attach: '/ai/chat/attach',
    last: '/ai/chat/last_response',
    stop: '/ai/chat/stop',
    notify: '/ai/notifications/poll',
    health: '/health',
  };
  // Чаты хранятся отдельно для каждого аккаунта: иначе другой человек на этом же
  // браузере увидит чужие разговоры. Настройки общие для браузера.
  const KEY = { sessions: 'ai_sessions', msgs: 'ai_messages_', settings: 'ai_settings_v2' };
  function setScope(uid) {
    KEY.sessions = uid ? `ai:${uid}:sessions` : 'ai_sessions';
    KEY.msgs = uid ? `ai:${uid}:messages_` : 'ai_messages_';
  }
  const NOTIFY_ID = 'ai_notifications';
  const DEFAULT_NAME = 'Новый чат';
  const MAX_STORED = 300;
  const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
  const MAX_IMAGE_STORE = 350000;
  const PERSIST_DELAY_MS = 400;

  const DEFAULTS = {
    enterSends: true,
    autoScroll: true,
    webDefault: false,
    reasoningDefault: false,
    fontSize: 'm',
    compact: false,
    notify: true,
    notifyInterval: '30',
  };

  const SUGGESTIONS = [
    ['Объясни простыми словами', 'Как работает память ассистента и что он запоминает?'],
    ['Составь план', 'Помоги составить план на неделю с фокусом на один большой проект.'],
    ['Поищи в интернете', 'Какие сейчас главные новости в мире искусственного интеллекта?'],
    ['Напиши текст', 'Напиши короткий пост для канала о запуске нового проекта.'],
  ];

  const ICON_REFRESH =
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v5h-5"/></svg>';

  // ------------------------------------------------------------------ утилиты

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function readJSON(key, fallback) {
    try {
      const v = localStorage.getItem(key);
      return v === null ? fallback : JSON.parse(v);
    } catch { return fallback; }
  }
  function writeJSON(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); return true; }
    catch { return false; }
  }

  const uid = () => 'ai_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const fmtTime = (ts) => new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const dayNum = (ts) => { const d = new Date(ts); return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); };
  function groupLabel(ts) {
    const k = dayNum(ts);
    if (k === dayNum(Date.now())) return 'Сегодня';
    if (k === dayNum(Date.now() - 86400000)) return 'Вчера';
    return 'Раньше';
  }
  const safeFileName = (s) => (s || 'chat').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 60) || 'chat';

  function download(name, text, type) {
    const blob = new Blob([text], { type: type + ';charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function newBotMessage() {
    return {
      role: 'assistant', text: '', reasoning: '', sources: [], images: [], notes: [],
      error: null, time: Date.now(), pending: true,
    };
  }
  function resetBot(m) {
    m.text = ''; m.reasoning = ''; m.sources = []; m.images = []; m.notes = []; m.error = null;
  }

  // ------------------------------------------------------------------ состояние

  let settings = Object.assign({}, DEFAULTS, readJSON(KEY.settings, {}));
  let sessions = [];                 // загружаются в init() после определения аккаунта
  let cache = {};                 // id -> массив сообщений (живой объект для генерации)
  let currentId = null;           // открытый в интерфейсе чат
  let gen = null;                 // активная генерация: {sessionId, idx, controller, stopRequested}
  let attachment = null;          // {url, base64, mime, name}
  let currentScopeUid = null;     // null — локальный режим без входа, синхронизация выключена
  let notifyTimer = null;
  let chips = { web: !!settings.webDefault, reason: !!settings.reasoningDefault };
  const persistTimers = {};

  const els = {
    app: $('#app'),
    scrim: $('#scrim'),
    menuBtn: $('#menuBtn'),
    list: $('#sessionList'),
    search: $('#searchInput'),
    newBtn: $('#newChatBtn'),
    status: $('#status'),
    title: $('#chatTitle'),
    sub: $('#chatSub'),
    rename: $('#renameBtn'),
    exportBtn: $('#exportBtn'),
    clearBtn: $('#clearBtn'),
    deleteBtn: $('#deleteBtn'),
    messages: $('#messages'),
    inner: $('#inner'),
    input: $('#input'),
    send: $('#sendBtn'),
    counter: $('#counter'),
    hint: $('#hint'),
    hintText: $('#hintText'),
    hintBtn: $('#hintBtn'),
    toggleWeb: $('#toggleWeb'),
    toggleReason: $('#toggleReason'),
    attachBtn: $('#attachBtn'),
    attachRemove: $('#attachRemove'),
    attachChip: $('#attachChip'),
    attachThumb: $('#attachThumb'),
    attachName: $('#attachName'),
    fileInput: $('#fileInput'),
    scrollBottom: $('#scrollBottomBtn'),
    dialog: $('#settingsDialog'),
    openSettings: $('#openSettings'),
    exportAll: $('#exportAll'),
    wipeAll: $('#wipeAll'),
    toast: $('#toast'),
  };

  // ------------------------------------------------------------------ хранилище

  const saveSessions = () => { writeJSON(KEY.sessions, sessions); scheduleSync(); };
  const currentSession = () => sessions.find((s) => s.id === currentId) || null;
  const findSession = (id) => sessions.find((s) => s.id === id) || null;

  function loadMsgs(id) {
    if (!(id in cache)) cache[id] = readJSON(KEY.msgs + id, []);
    return cache[id];
  }

  function persist(id) {
    const ok = writeJSON(KEY.msgs + id, (cache[id] || []).slice(-MAX_STORED));
    scheduleSync();
    if (!ok) toast('Не удалось сохранить: переполнено локальное хранилище');
  }
  // Частые обновления во время генерации сохраняем не чаще раза в PERSIST_DELAY_MS
  function persistSoon(id) {
    if (persistTimers[id]) return;
    persistTimers[id] = setTimeout(() => { delete persistTimers[id]; persist(id); }, PERSIST_DELAY_MS);
  }
  function commit(id) {
    if (persistTimers[id]) { clearTimeout(persistTimers[id]); delete persistTimers[id]; }
    persist(id);
    const s = findSession(id);
    if (s) { s.updated = Date.now(); saveSessions(); }
  }
  const isPending = (id) => loadMsgs(id).some((m) => m.pending);
  const isEmptyDefault = (s) => !!s && s.name === DEFAULT_NAME && loadMsgs(s.id).length === 0;

  function createSession(name) {
    const id = uid();
    const now = Date.now();
    sessions.unshift({ id, name, created: now, updated: now });
    cache[id] = [];
    saveSessions();
    persist(id);
    return id;
  }

  function removeSession(id) {
    sessions = sessions.filter((s) => s.id !== id);
    delete cache[id];
    localStorage.removeItem(KEY.msgs + id);
    saveSessions();
  }

  function ensureNotifySession() {
    if (!findSession(NOTIFY_ID)) {
      sessions.push({ id: NOTIFY_ID, name: '🔔 Уведомления', created: Date.now(), updated: Date.now() });
      saveSessions();
    }
  }

  // ------------------------------------------------------------------ рендер: список чатов

  function renderList() {
    const q = els.search.value.trim().toLowerCase();
    const items = sessions
      .filter((s) => !q || s.name.toLowerCase().includes(q))
      .sort((a, b) => (b.updated || b.created) - (a.updated || a.created));

    els.list.innerHTML = '';
    if (!items.length) {
      const p = document.createElement('p');
      p.className = 'hint';
      p.style.padding = '12px';
      p.textContent = q ? 'Ничего не найдено' : 'Чатов пока нет';
      els.list.appendChild(p);
      return;
    }

    let lastGroup = '';
    for (const s of items) {
      const g = groupLabel(s.updated || s.created);
      if (g !== lastGroup) {
        lastGroup = g;
        const h = document.createElement('div');
        h.className = 'sb-group';
        h.textContent = g;
        els.list.appendChild(h);
      }
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'sess' + (s.id === currentId ? ' active' : '');
      const t = document.createElement('span');
      t.className = 't';
      t.textContent = s.name;
      b.appendChild(t);
      if (isPending(s.id)) {
        const dot = document.createElement('span');
        dot.className = 'dot';
        dot.title = 'Ответ пишется';
        b.appendChild(dot);
      }
      b.addEventListener('click', () => { selectSession(s.id); closeNav(); });
      els.list.appendChild(b);
    }
  }

  // ------------------------------------------------------------------ рендер: сообщения

  // Markdown-библиотеки грузятся с CDN и могут быть недоступны (офлайн, блокировка).
  // Fallback: выводим текст как есть — сначала HTML экранируется, поэтому вставкой
  // через innerHTML нельзя внедрить разметку; перенос строк даёт white-space: pre-wrap.
  function escapeHtml(s) {
    return s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }
  function renderMd(text) {
    const src = text || '';
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
      return escapeHtml(src);
    }
    try {
      return DOMPurify.sanitize(marked.parse(src, { breaks: true, gfm: true }));
    } catch (e) {
      console.warn('renderMd:', e);
      return escapeHtml(src);
    }
  }

  function highlight(root) {
    if (typeof hljs === 'undefined') return;
    root.querySelectorAll('pre code').forEach((el) => {
      if (!el.dataset.hl) { hljs.highlightElement(el); el.dataset.hl = '1'; }
    });
  }

  function sourcesNode(sources) {
    const box = document.createElement('div');
    box.className = 'sources';
    const label = document.createElement('span');
    label.textContent = 'Источники:';
    box.appendChild(label);
    for (const s of sources) {
      const url = typeof s === 'object' && s ? s.url : (typeof s === 'string' ? s : '');
      const title = (typeof s === 'object' && s && (s.title || s.url)) || (typeof s === 'string' ? s : 'источник');
      if (/^https?:\/\//.test(url || '')) {
        const a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
        a.textContent = title;
        box.appendChild(a);
      } else {
        const x = document.createElement('span');
        x.textContent = title;
        box.appendChild(x);
      }
    }
    return box;
  }

  function paintBubble(bubble, m) {
    bubble.innerHTML = '';

    if (m.role === 'user') {
      if (m.image) {
        const img = new Image();
        img.className = 'gen';
        img.src = m.image;
        img.alt = 'Прикреплённое изображение';
        bubble.appendChild(img);
      }
      if (m.text) {
        const p = document.createElement('div');
        p.textContent = m.text;
        bubble.appendChild(p);
      }
      return;
    }

    if (m.reasoning) {
      const thinking = !!m.pending && !m.text;          // модель ещё думает
      const d = document.createElement('details');
      d.className = 'reasoning';
      const s = document.createElement('summary');
      if (thinking) {
        s.textContent = 'Думаю…';
      } else {
        const sec = m.rStart && m.rEnd ? Math.max(1, Math.round((m.rEnd - m.rStart) / 1000)) : 0;
        s.textContent = sec ? `Размышлял ${sec} с` : 'Ход рассуждений';
      }
      const body = document.createElement('div');
      body.className = 'rbody';
      body.textContent = m.reasoning;
      d.append(s, body);
      // Открываем во время размышления, сворачиваем, когда пошёл ответ.
      // Если пользователь сам раскрыл блок, его выбор не сбиваем.
      if (m._rWant !== thinking) { d.open = thinking; m._rWant = thinking; }
      else d.open = d.open || thinking;
      bubble.appendChild(d);
    }

    if (m.pending && !m.text) {
      const t = document.createElement('span');
      t.className = 'typing';
      t.innerHTML = '<span></span><span></span><span></span>';
      bubble.appendChild(t);
    } else if (m.text) {
      const md = document.createElement('div');
      md.className = 'markdown-body';
      if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') md.classList.add('plain');
      md.innerHTML = renderMd(m.text);
      bubble.appendChild(md);
      highlight(md);
    }

    for (const url of m.images || []) {
      const img = new Image();
      img.className = 'gen';
      img.src = url;
      img.alt = 'Изображение';
      img.loading = 'lazy';
      bubble.appendChild(img);
    }
    if (m.sources && m.sources.length) bubble.appendChild(sourcesNode(m.sources));
    for (const note of m.notes || []) {
      const d = document.createElement('div');
      d.className = 'meta-line';
      d.textContent = note;
      bubble.appendChild(d);
    }
    if (m.error) {
      const d = document.createElement('div');
      d.className = 'err-text';
      d.textContent = 'Ошибка: ' + m.error;
      bubble.appendChild(d);
    }
  }

  function actBtn(act, title, icon) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'act';
    b.dataset.act = act;
    b.title = title;
    b.setAttribute('aria-label', title);
    if (icon.startsWith('<svg')) {
      b.innerHTML = icon;
    } else {
      const img = new Image();
      img.src = icon;
      img.alt = '';
      b.appendChild(img);
    }
    return b;
  }

  function buildMessage(m, idx) {
    const root = document.createElement('div');
    root.className = 'msg ' + (m.role === 'user' ? 'user' : 'bot') + (m.error ? ' error' : '');
    root.dataset.idx = String(idx);

    if (m.role !== 'user') {
      const av = document.createElement('div');
      av.className = 'avatar-bot';
      av.textContent = '✦';
      root.appendChild(av);
    }

    const body = document.createElement('div');
    body.className = 'msg-body';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    body.appendChild(bubble);

    const foot = document.createElement('div');
    foot.className = 'msg-foot';
    const time = document.createElement('span');
    time.className = 'time';
    time.textContent = fmtTime(m.time || Date.now());
    foot.appendChild(time);
    if (m.text && !m.pending) foot.appendChild(actBtn('copy', 'Копировать', '/static/icons/Copy.png'));
    if (m.role !== 'user' && !m.pending) foot.appendChild(actBtn('regen', 'Получить другой ответ', ICON_REFRESH));
    body.appendChild(foot);

    root.appendChild(body);
    paintBubble(bubble, m);
    return root;
  }

  function welcomeNode() {
    const w = document.createElement('div');
    w.className = 'welcome';
    const big = document.createElement('div');
    big.className = 'big';
    big.textContent = '✦';
    const h = document.createElement('h2');
    h.textContent = 'Чем помочь?';
    const p = document.createElement('p');
    p.textContent = 'Спросите что угодно. Ассистент помнит контекст и может искать в интернете.';
    const grid = document.createElement('div');
    grid.className = 'suggest-grid';
    for (const [title, prompt] of SUGGESTIONS) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'suggest';
      const bt = document.createElement('b');
      bt.textContent = title;
      const bp = document.createElement('span');
      bp.textContent = prompt;
      b.append(bt, bp);
      b.addEventListener('click', () => { els.input.value = prompt; autoSize(); send(prompt); });
      grid.appendChild(b);
    }
    w.append(big, h, p, grid);
    return w;
  }

  // ------------------------------------------------------------------ прокрутка

  // Пользователь «у дна», если от низа осталось меньше 80px. Пока он у дна,
  // новые токены генерации автоматически подскролливают вниз; если он ушёл
  // вверх читать — не мешаем и показываем кнопку «вниз». Настройка
  // «Автопрокрутка» (settings.autoScroll) разрешает вести вниз во время генерации;
  // при этом пользователя, ушедшего вверх читать, никогда не тянет за новый текст.
  const NEAR_BOTTOM = 80;
  const isNearBottom = () =>
    els.messages.scrollHeight - els.messages.scrollTop - els.messages.clientHeight < NEAR_BOTTOM;

  function updateScrollButton() {
    els.scrollBottom.hidden = isNearBottom();
  }

  function scrollEnd(force) {
    if (!force && !settings.autoScroll) return;   // настройка: никогда не прокручать само
    if (!force && !isNearBottom()) return;        // пользователь читает выше — не тянем вниз
    els.messages.scrollTop = els.messages.scrollHeight;
    updateScrollButton();
  }

  function scrollToBottom(smooth) {
    const behavior = smooth && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      ? 'smooth' : 'auto';
    els.messages.scrollTo({ top: els.messages.scrollHeight, behavior });
    updateScrollButton();
  }

  function renderMessages(opts = {}) {
    els.inner.innerHTML = '';
    const msgs = currentId ? loadMsgs(currentId) : [];
    if (!msgs.length) {
      els.inner.appendChild(welcomeNode());
      return;
    }
    msgs.forEach((m, i) => els.inner.appendChild(buildMessage(m, i)));
    if (opts.scroll !== false) scrollEnd(true);
    else updateScrollButton();
  }

  // Обновляет один пузырь во время генерации, не перерисовывая весь чат
  let paintQueued = false;
  function paintLive(idx) {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => {
      paintQueued = false;
      if (!currentId) return;
      const el = els.inner.querySelector(`.msg[data-idx="${idx}"] .bubble`);
      const m = loadMsgs(currentId)[idx];
      if (el && m) { paintBubble(el, m); scrollEnd(); }
    });
  }

  // ------------------------------------------------------------------ состояние панели

  const isGenHere = () => !!gen && gen.sessionId === currentId;
  const canSendNow = () => !!(els.input.value.trim() || attachment);

  function setSendState() {
    if (isGenHere()) {
      els.send.classList.add('stop');
      els.send.textContent = '■';
      els.send.title = 'Остановить';
      els.send.disabled = false;
    } else {
      els.send.classList.remove('stop');
      els.send.textContent = '➤';
      els.send.title = 'Отправить';
      els.send.disabled = !canSendNow() || !!gen;
    }
  }

  function updateHint() {
    const other = gen && gen.sessionId !== currentId;
    if (other) {
      const g = findSession(gen.sessionId);
      els.hintText.textContent =
        `Ответ ещё пишется в «${g ? g.name : 'другом чате'}». Новое сообщение можно отправить после него.`;
      els.hintBtn.hidden = false;
    } else {
      els.hintText.textContent = 'Enter — отправить · Shift+Enter — новая строка · / — поиск по чатам';
      els.hintBtn.hidden = true;
    }
  }

  function updateHeader() {
    const s = currentSession();
    els.title.textContent = s ? s.name : DEFAULT_NAME;
    if (!s) {
      els.sub.textContent = '';
    } else if (s.id === NOTIFY_ID) {
      els.sub.textContent = 'Фоновые находки ассистента';
    } else {
      const n = loadMsgs(s.id).length;
      els.sub.textContent = n ? `${n} сообщ. · ${fmtTime(s.updated || s.created)}` : 'Пустой чат';
    }
    const busyHere = isGenHere();
    els.rename.disabled = !s;
    els.exportBtn.disabled = !s;
    els.clearBtn.disabled = !s || busyHere;
    els.deleteBtn.disabled = !s || busyHere;
    setSendState();
    updateHint();
  }

  function refreshUI() {
    updateHeader();
    renderList();
  }

  function syncChips() {
    els.toggleWeb.setAttribute('aria-pressed', String(chips.web));
    els.toggleReason.setAttribute('aria-pressed', String(chips.reason));
  }

  function autoSize() {
    els.input.style.height = 'auto';
    els.input.style.height = Math.min(els.input.scrollHeight, 200) + 'px';
    const len = els.input.value.length;
    els.counter.textContent = len > 400 ? len + ' симв.' : '';
  }

  function applySettings() {
    document.body.dataset.fs = settings.fontSize;
    document.body.classList.toggle('compact', !!settings.compact);
  }

  // ------------------------------------------------------------------ навигация

  function openNav() { els.app.classList.add('nav-open'); }
  function closeNav() { els.app.classList.remove('nav-open'); }

  function selectSession(id) {
    if (id === currentId) return;
    // Пустой «Новый чат», который пользователь бросил, убираем из списка
    const prev = currentSession();
    if (prev && isEmptyDefault(prev) && prev.id !== (gen && gen.sessionId)) removeSession(prev.id);

    currentId = id;
    renderMessages();
    refreshUI();
  }

  function newChat() {
    const cur = currentSession();
    if (isEmptyDefault(cur)) {            // уже открыт пустой чат — не плодим новые
      els.input.focus();
      closeNav();
      return;
    }
    const id = createSession(DEFAULT_NAME);
    const prev = currentSession();
    if (prev && isEmptyDefault(prev) && prev.id !== (gen && gen.sessionId)) removeSession(prev.id);
    currentId = id;
    renderMessages({ scroll: false });
    refreshUI();
    closeNav();
    els.input.focus();
  }

  // ------------------------------------------------------------------ SSE

  // Возвращает true, если поток завершился маркером [DONE]; false — если оборвался
  async function readSSE(res, onEvent, signal) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    const onAbort = () => reader.cancel().catch(() => {});
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
    try {
      for (;;) {
        if (signal && signal.aborted) return false;
        const { value, done } = await reader.read();
        if (done) return false;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) !== -1) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of block.split('\n')) {
            if (!line.startsWith('data:')) continue;
            const data = line.slice(5).trim();
            if (data === '[DONE]') { reader.cancel().catch(() => {}); return true; }
            let ev;
            try { ev = JSON.parse(data); } catch { continue; }
            onEvent(ev);
          }
        }
      }
    } finally {
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }

  function applyEvent(ev, m) {
    if (ev.token !== undefined) {
      if (m.rStart && !m.rEnd) m.rEnd = Date.now();   // рассуждения закончились, пошёл ответ
      m.text += ev.token;
    }
    else if (ev.reasoning_token !== undefined) {
      if (!m.rStart) m.rStart = Date.now();
      m.reasoning = (m.reasoning || '') + ev.reasoning_token;
    }
    else if (ev.sources) m.sources = (m.sources || []).concat(ev.sources);
    else if (ev.image_url) m.images = (m.images || []).concat([ev.image_url]);
    else if (ev.tool_call) m.notes = (m.notes || []).concat(['🔧 ' + ev.tool_call]);
    else if (ev.warning) m.notes = (m.notes || []).concat(['⚠ ' + ev.warning]);
    else if (ev.error) m.error = String(ev.error);
  }

  // Если сервер уже закончил, берём готовый ответ по тексту вопроса
  // true — ответ найден, false — сервер ответа не знает, null — нет связи
  async function recoverFromLast(bot, userText) {
    try {
      const q = userText ? '?user_text=' + encodeURIComponent(userText) : '';
      const r = await fetch(API.last + q, { credentials: 'same-origin' });
      if (!r.ok) return false;
      const ex = await r.json();
      if (ex && ex.assistant && (!userText || ex.user === userText)) {
        bot.text = ex.assistant;
        return true;
      }
      return false;
    } catch {
      return null;
    }
  }

  function isNetworkError(err) {
    // Сетевые ошибки fetch — TypeError; сравниваем по имени, а не через instanceof (разные realm)
    return !!err && (err.name === 'TypeError' || err.name === 'NetworkError');
  }

  function pushNote(m, text) {
    if (!m.notes.includes(text)) m.notes.push(text);
  }

  // ------------------------------------------------------------------ генерация

  // mode: 'send' — новый запрос к /ai/chat; 'attach' — подхват уже идущей генерации
  async function startGeneration(id, idx, mode, body) {
    const controller = new AbortController();
    const g = { sessionId: id, idx, controller, stopRequested: false, interrupted: false, authLost: false };
    gen = g;
    refreshUI();

    const msgs = loadMsgs(id);
    const bot = msgs[idx];
    try {
      const res = mode === 'attach'
        ? await fetch(API.attach, { credentials: 'same-origin', signal: controller.signal })
        : await fetch(API.chat, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
            signal: controller.signal,
          });
      if (res.status === 401) { g.authLost = true; location.href = '/login'; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);

      // attach replays the whole buffer from the start: start from a clean slate
      if (mode === 'attach') resetBot(bot);

      let none = false;
      const finished = await readSSE(res, (ev) => {
        if (ev.no_active_generation) { none = true; return; }
        applyEvent(ev, bot);
        persistSoon(id);
        if (id === currentId) paintLive(idx);
      }, controller.signal);

      if (!controller.signal.aborted) {
        if (none) {
          if (!bot.text && !bot.error) {
            const userMsg = msgs[idx - 1] && msgs[idx - 1].role === 'user' ? msgs[idx - 1].text : null;
            const ok = await recoverFromLast(bot, userMsg);
            if (ok === null) g.interrupted = true;
            else if (!ok) bot.error = 'Ответ не найден на сервере (возможно, сервер перезапускался)';
          }
        } else if (!finished) {
          // Поток оборвался до [DONE]: ответ на сервере продолжается, его надо дождаться
          g.interrupted = true;
        }
      }
    } catch (err) {
      if (err.name === 'AbortError') { /* остановка по кнопке — обработано ниже */ }
      else if (isNetworkError(err)) g.interrupted = true;
      else bot.error = err.message || String(err);
    } finally {
      if (g.authLost) {
        if (gen === g) gen = null;
        refreshUI();
      } else {
        if (bot.rStart && !bot.rEnd) bot.rEnd = Date.now();
        if (g.stopRequested) {
          bot.notes.push('⏹ Остановлено');
          bot.pending = false;
        } else if (g.interrupted) {
          // Не помечаем готовым: сообщение остаётся «пишется», пока не получим ответ
          bot.pending = true;
          pushNote(bot, '📡 Соединение прервано, ждём ответ…');
        } else {
          if (!bot.text && !bot.error) bot.text = '(пустой ответ)';
          bot.pending = false;
        }
        if (gen === g) gen = null;
        commit(id);
        if (id === currentId) renderMessages({ scroll: false });
        refreshUI();
        if (g.interrupted && !g.stopRequested) scheduleResume(5000);
        else els.input.focus();
      }
    }
  }

  let resumeTimer = null;
  function scheduleResume(delay) {
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(() => resumePending(), delay);
  }

  // Подхватываем незавершённые ответы, например после перезагрузки страницы
  async function resumePending() {
    clearTimeout(resumeTimer);
    for (const s of sessions) {
      if (gen) return;
      const idx = loadMsgs(s.id).findIndex((m) => m.pending);
      if (idx >= 0) {
        await startGeneration(s.id, idx, 'attach', null);
        if (currentId === s.id) renderMessages({ scroll: false });
      }
    }
    refreshUI();
  }

  function send(override) {
    const text = (override !== undefined ? override : els.input.value).trim();
    if (!text && !attachment) return;

    if (gen) {
      if (gen.sessionId !== currentId) {
        toast('Дождитесь окончания ответа в другом чате');
        updateHint();
      }
      return;
    }

    const id = currentId || createSession(DEFAULT_NAME);
    currentId = id;
    const s = currentSession();
    const img = attachment;
    attachment = null;
    renderAttachment();
    els.input.value = '';
    autoSize();

    const msgs = loadMsgs(id);
    if (msgs.length === 0 && s && s.name === DEFAULT_NAME) {
      s.name = (text || 'Фото').replace(/\s+/g, ' ').slice(0, 48);
    }
    msgs.push({
      role: 'user',
      text,
      time: Date.now(),
      image: img && img.url.length < MAX_IMAGE_STORE ? img.url : null,
    });
    msgs.push(newBotMessage());
    const idx = msgs.length - 1;
    commit(id);
    renderMessages();
    refreshUI();

    const body = {
      message: text || 'Опиши, что изображено',
      stream: true,
      web_search: chips.web,
      reasoning: chips.reason,
    };
    if (img) { body.image_base64 = img.base64; body.image_mime = img.mime; }
    startGeneration(id, idx, 'send', body);
  }

  async function stopGeneration() {
    if (!gen) return;
    gen.stopRequested = true;
    try { await fetch(API.stop, { method: 'POST', credentials: 'same-origin' }); } catch { /* сеть */ }
    gen.controller.abort();
  }

  function regenerate(idx) {
    if (gen) { toast('Дождитесь окончания ответа'); return; }
    const id = currentId;
    const msgs = loadMsgs(id);
    let u = -1;
    for (let i = idx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') { u = i; break; }
    }
    if (u < 0) return;
    const prompt = msgs[u].text || 'Продолжи';
    msgs.splice(u + 1);
    msgs.push(newBotMessage());
    const nidx = msgs.length - 1;
    commit(id);
    renderMessages({ scroll: false });
    refreshUI();
    startGeneration(id, nidx, 'send', {
      message: prompt, stream: true, web_search: chips.web, reasoning: chips.reason,
    });
  }

  function onSendButton() {
    if (isGenHere()) stopGeneration();
    else send();
  }

  // ------------------------------------------------------------------ вложения

  function renderAttachment() {
    els.attachChip.hidden = !attachment;
    if (attachment) {
      els.attachThumb.src = attachment.url;
      els.attachName.textContent = attachment.name;
    }
    setSendState();
  }

  // ------------------------------------------------------------------ действия над чатом

  function renameCurrent() {
    const s = currentSession();
    if (!s) return;
    els.title.contentEditable = 'true';
    els.title.focus();
    const range = document.createRange();
    range.selectNodeContents(els.title);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  function finishRename(save) {
    if (els.title.contentEditable !== 'true') return;
    els.title.contentEditable = 'false';
    const s = currentSession();
    if (!s) return;
    if (save) {
      const name = els.title.textContent.replace(/\s+/g, ' ').trim().slice(0, 60);
      if (name && name !== s.name) { s.name = name; saveSessions(); }
    }
    els.title.textContent = s.name;
    renderList();
  }

  function exportCurrent() {
    const s = currentSession();
    if (!s) return;
    const lines = [`# ${s.name}`, '', `_Экспорт: ${new Date().toLocaleString('ru-RU')}_`, ''];
    for (const m of loadMsgs(s.id)) {
      lines.push(`**${m.role === 'user' ? 'Вы' : 'Ассистент'}** · ${fmtTime(m.time || Date.now())}`, '', m.text || '', '');
    }
    download(safeFileName(s.name) + '.md', lines.join('\n'), 'text/markdown');
  }

  function clearCurrent() {
    const s = currentSession();
    if (!s || isGenHere()) return;
    if (!confirm('Очистить историю этого чата?')) return;
    cache[s.id] = [];
    commit(s.id);
    renderMessages({ scroll: false });
    refreshUI();
    toast('История очищена');
  }

  function deleteCurrent() {
    const s = currentSession();
    if (!s || isGenHere()) return;
    if (!confirm('Удалить этот чат?')) return;
    removeSession(s.id);
    currentId = null;
    const next = sessions.slice().sort((a, b) => (b.updated || b.created) - (a.updated || a.created))[0];
    currentId = next ? next.id : null;
    renderMessages({ scroll: false });
    refreshUI();
    toast('Чат удалён');
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast('Скопировано');
    } catch {
      const t = document.createElement('textarea');
      t.value = text;
      document.body.appendChild(t);
      t.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      t.remove();
      toast(ok ? 'Скопировано' : 'Не удалось скопировать');
    }
  }

  // ------------------------------------------------------------------ фоновые находки

  async function pollNotifications() {
    if (!settings.notify) return;
    try {
      const r = await fetch(API.notify, { credentials: 'same-origin' });
      if (!r.ok) return;
      const data = await r.json();
      const items = Array.isArray(data.notifications) ? data.notifications : [];
      if (!items.length) return;

      ensureNotifySession();
      const msgs = loadMsgs(NOTIFY_ID);
      for (const it of items) {
        const text = typeof it === 'string'
          ? it
          : (it.text || it.message || it.content || it.summary || JSON.stringify(it));
        msgs.push({
          role: 'assistant', text, reasoning: '', sources: [], images: [], notes: [],
          error: null, time: Date.now(),
        });
      }
      commit(NOTIFY_ID);
      if (currentId === NOTIFY_ID) renderMessages({ scroll: false });
      refreshUI();
      toast(`🔔 Новых находок: ${items.length}`);
    } catch { /* сеть недоступна — попробуем позже */ }
  }

  function startNotify() {
    clearInterval(notifyTimer);
    notifyTimer = null;
    if (!settings.notify) return;
    const sec = Number(settings.notifyInterval) || 30;
    notifyTimer = setInterval(pollNotifications, sec * 1000);
  }

  async function ping() {
    try {
      const r = await fetch(API.health, { cache: 'no-store' });
      const j = r.ok ? await r.json() : null;
      setStatus(j && j.status === 'ok' ? 'ok' : 'bad');
    } catch {
      setStatus('bad');
    }
  }
  function setStatus(s) {
    els.status.className = 'status ' + s;
    els.status.title = s === 'ok' ? 'Сервер работает' : 'Нет связи с сервером';
  }

  // ------------------------------------------------------------------ уведомления

  let toastTimer = null;
  function toast(msg, ms = 2600) {
    els.toast.textContent = msg;
    els.toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => els.toast.classList.remove('show'), ms);
  }

  // ------------------------------------------------------------------ настройки

  function bindSettings() {
    $$('[data-key]', els.dialog).forEach((inp) => {
      const k = inp.dataset.key;
      if (inp.type === 'checkbox') inp.checked = !!settings[k];
      else inp.value = String(settings[k]);

      inp.addEventListener('change', () => {
        settings[k] = inp.type === 'checkbox' ? inp.checked : inp.value;
        writeJSON(KEY.settings, settings);
        if (k === 'webDefault') chips.web = !!settings.webDefault;
        if (k === 'reasoningDefault') chips.reason = !!settings.reasoningDefault;
        if (k === 'notify' || k === 'notifyInterval') startNotify();
        if (k === 'autoScroll' || k === 'fontSize' || k === 'compact') applySettings();
        syncChips();
      });
    });
  }

  function exportAllChats() {
    const data = { exportedAt: new Date().toISOString(), sessions, messages: {} };
    for (const s of sessions) data.messages[s.id] = loadMsgs(s.id);
    download('ai-chats-' + Date.now() + '.json', JSON.stringify(data, null, 2), 'application/json');
  }

  function wipeAllChats() {
    if (gen) { toast('Дождитесь окончания ответа'); return; }
    if (!confirm('Удалить все чаты в этом браузере? Это нельзя отменить.')) return;
    for (const s of sessions) { delete cache[s.id]; localStorage.removeItem(KEY.msgs + s.id); }
    sessions = [];
    saveSessions();
    const id = createSession(DEFAULT_NAME);
    currentId = id;
    renderMessages({ scroll: false });
    refreshUI();
    toast('Все чаты удалены');
  }

  // ------------------------------------------------------------------ обработчики

  els.newBtn.addEventListener('click', newChat);
  els.menuBtn.addEventListener('click', () => els.app.classList.toggle('nav-open'));
  els.scrim.addEventListener('click', closeNav);
  els.search.addEventListener('input', renderList);

  // Поиск по чатам: '/' из поля ввода — перейти к первому результату, Esc — очистить/закрыть
  els.search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const first = $('.sess', els.list);
      if (first) { first.click(); els.search.blur(); }
    } else if (e.key === 'Escape') {
      if (els.search.value) { e.stopPropagation(); els.search.value = ''; renderList(); }
      else { closeNav(); els.search.blur(); }
    }
  });

  // Прокрутка: кнопка «вниз» видна, только когда пользователь ушёл от конца
  els.messages.addEventListener('scroll', updateScrollButton, { passive: true });
  els.scrollBottom.addEventListener('click', () => scrollToBottom(true));
  els.hintBtn.addEventListener('click', () => { if (gen) selectSession(gen.sessionId); });

  els.send.addEventListener('click', onSendButton);
  els.input.addEventListener('input', () => { autoSize(); setSendState(); });
  els.input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const shouldSend = settings.enterSends ? !e.shiftKey : (e.ctrlKey || e.metaKey);
    if (shouldSend) { e.preventDefault(); onSendButton(); }
  });

  els.toggleWeb.addEventListener('click', () => { chips.web = !chips.web; syncChips(); });
  els.toggleReason.addEventListener('click', () => { chips.reason = !chips.reason; syncChips(); });

  // '/' вне поля ввода — фокус в поиск чатов (как в Notion/Slack)
  document.addEventListener('keydown', (e) => {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    if (els.dialog.open) return;
    e.preventDefault();
    openNav();                       // на мобильных панель скрыта — сначала открываем
    els.search.focus();
    els.search.select();             // предыдущий запрос выделяется, набираем поверх
  });

  els.attachBtn.addEventListener('click', () => els.fileInput.click());
  els.attachRemove.addEventListener('click', () => { attachment = null; renderAttachment(); });
  els.fileInput.addEventListener('change', () => {
    const f = els.fileInput.files[0];
    els.fileInput.value = '';
    if (!f) return;
    if (!f.type.startsWith('image/')) { toast('Можно прикреплять только изображения'); return; }
    if (f.size > MAX_IMAGE_BYTES) { toast('Изображение больше 8 МБ'); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      attachment = { url, base64: url.split(',')[1] || '', mime: f.type, name: f.name };
      renderAttachment();
    };
    reader.readAsDataURL(f);
  });

  els.rename.addEventListener('click', renameCurrent);
  els.title.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finishRename(true); els.title.blur(); }
    if (e.key === 'Escape') { finishRename(false); els.title.blur(); }
  });
  els.title.addEventListener('blur', () => finishRename(true));
  els.exportBtn.addEventListener('click', exportCurrent);
  els.clearBtn.addEventListener('click', clearCurrent);
  els.deleteBtn.addEventListener('click', deleteCurrent);

  els.inner.addEventListener('click', (e) => {
    const btn = e.target.closest('.act');
    if (!btn || !currentId) return;
    const root = btn.closest('.msg');
    if (!root) return;
    const idx = Number(root.dataset.idx);
    const m = loadMsgs(currentId)[idx];
    if (!m) return;
    if (btn.dataset.act === 'copy') copyText(m.text || '');
    if (btn.dataset.act === 'regen') regenerate(idx);
  });

  els.openSettings.addEventListener('click', () => els.dialog.showModal());

  // Выход только после подтверждения: случайное нажатие не разлогинивает
  const logoutDialog = $('#logoutDialog');
  $('#logoutBtn').addEventListener('click', () => {
    $('#logoutText').textContent = gen
      ? 'Ответ ещё пишется. Он продолжится на сервере и появится при следующем входе. Чаты сохранятся.'
      : 'Чаты сохранятся: они привязаны к аккаунту и откроются при следующем входе.';
    logoutDialog.showModal();
  });
  $('#logoutConfirm').addEventListener('click', async (e) => {
    e.preventDefault();
    try { await fetch('/auth/logout', { method: 'POST', credentials: 'same-origin' }); } catch { /* сеть */ }
    location.href = '/login';
  });

  $('#mcpCodeBtn').addEventListener('click', async () => {
    try {
      const r = await fetch('/ai/mcp_code', { method: 'POST', credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      $('#mcpCodeOut').textContent = j.code;
      toast('Код выдан. Прежние подключения отключены.');
    } catch (e) {
      toast('Не удалось получить код: ' + e.message);
    }
  });
  els.exportAll.addEventListener('click', exportAllChats);
  els.wipeAll.addEventListener('click', wipeAllChats);

  document.addEventListener('keydown', (e) => {
    // Esc закрывает открытое модальное окно настроек (браузер это делает сам
    // только при фокусе внутри <dialog>; клик по фону-оверлею тоже закрывает)
    if (e.key === 'Escape') {
      if (els.dialog.open) { els.dialog.close(); return; }
      closeNav();
    }
    // Ctrl/Cmd+K — новый чат, но не пока открыт диалог настроек
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !els.dialog.open) {
      e.preventDefault(); newChat();
    }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) resumePending(); });
  window.addEventListener('focus', () => resumePending());

  // ------------------------------------------------------------------ старт

  async function checkAuth() {
    try {
      const r = await fetch('/auth/status', { credentials: 'same-origin', cache: 'no-store' });
      const j = await r.json();
      if (j.required && !j.authenticated) { location.href = '/login'; return null; }
      if (j.email) { const el = $('#userEmail'); if (el) el.textContent = j.email; }
      return { uid: j.uid || null };
    } catch { /* сервер недоступен — работаем с локальными чатами, статус покажет ping */ }
    return { uid: null };
  }

  // ------------------------------------------------------------------ синхронизация с сервером
  // Чаты одного аккаунта видны на всех устройствах. Локальное хранилище — кеш.
  const SYNC = { base: 0, ready: false, timer: null, busy: false };

  function scheduleSync() {
    if (!SYNC.ready || !currentScopeUid) return;
    clearTimeout(SYNC.timer);
    SYNC.timer = setTimeout(pushState, 1500);
  }

  // Слияние: серверные версии приоритетнее для тех же id, локальные чаты, которых
  // на сервере нет, сохраняются и уйдут на сервер при следующей отправке.
  function mergeState(st) {
    const byId = new Map();
    for (const s of st.sessions || []) byId.set(s.id, s);
    for (const s of sessions) if (!byId.has(s.id)) byId.set(s.id, s);
    sessions = [...byId.values()];
    saveSessions();
    for (const s of sessions) {
      const server = st.messages && st.messages[s.id];
      if (server) writeJSON(KEY.msgs + s.id, server);
    }
    cache = {};
  }

  async function pullState() {
    if (!currentScopeUid || gen) return false;   // во время генерации не трогаем локальные сообщения
    try {
      const r = await fetch('/ai/ui-state', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return false;
      const st = await r.json();
      if (st.updated) mergeState(st);
      SYNC.base = st.updated || 0;
      return true;
    } catch { return false; }
  }

  async function pushState() {
    if (!SYNC.ready || SYNC.busy || !currentScopeUid) return;
    SYNC.busy = true;
    try {
      const messages = {};
      for (const s of sessions) messages[s.id] = loadMsgs(s.id);
      const r = await fetch('/ai/ui-state', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ base: SYNC.base, sessions, messages }),
      });
      if (r.status === 409) {
        // Другое устройство уже сохранило свою версию: берём её и не перезаписываем
        await pullState();
        scheduleSync();
      } else if (r.ok) {
        SYNC.base = (await r.json()).updated;
      } else if (r.status === 401) {
        location.href = '/login';
      }
    } catch { /* нет связи — повторим при следующем изменении или при возврате сети */ }
    finally { SYNC.busy = false; }
  }

  async function bootstrap() {
    SYNC.ready = await pullState();
    init();
    if (SYNC.ready && (SYNC.base === 0 || sessions.length)) scheduleSync();
  }

  function init() {
    sessions = readJSON(KEY.sessions, []);
    bindSettings();
    applySettings();
    syncChips();

    if (!sessions.length) createSession(DEFAULT_NAME);
    const latest = sessions.slice().sort((a, b) => (b.updated || b.created) - (a.updated || a.created))[0];
    currentId = latest.id;

    renderMessages({ scroll: false });
    refreshUI();
    autoSize();

    ping();
    setInterval(ping, 30000);
    startNotify();
    pollNotifications();
    resumePending();
  }

  checkAuth().then((st) => {
    if (!st) return;
    setScope(st.uid);
    currentScopeUid = st.uid;
    bootstrap();
  });

  window.addEventListener('online', async () => {
    if (!SYNC.ready && currentScopeUid) SYNC.ready = await pullState();
    scheduleSync();
    resumePending();
  });
})();
