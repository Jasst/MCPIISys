/**
 * ai-app.js — клиент ИИ-ассистента.
 *
 * Работает с сервером по API:
 *   POST /ai/chat                — потоковый ответ (SSE)
 *   GET  /ai/notifications/poll  — фоновые находки
 *   GET  /health                 — статус сервера
 *
 * SSE-события: token, reasoning_token, sources, image_url, tool_call, warning, error, [DONE].
 * Чаты и настройки хранятся в localStorage этого браузера.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ константы

  const API = { chat: '/ai/chat', notify: '/ai/notifications/poll', health: '/health' };
  const KEY = { sessions: 'ai_sessions', msgs: 'ai_messages_', settings: 'ai_settings_v2' };
  const NOTIFY_ID = 'ai_notifications';
  const MAX_STORED = 300;                // сообщений на чат
  const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
  const MAX_IMAGE_STORE = 350000;        // превью картинки сохраняем только если короче (символов)

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

  // ------------------------------------------------------------------ состояние

  let settings = Object.assign({}, DEFAULTS, readJSON(KEY.settings, {}));
  let sessions = readJSON(KEY.sessions, []);
  let currentId = null;
  let busy = false;
  let controller = null;
  let attachment = null;                 // {url, base64, mime, name}
  let notifyTimer = null;
  let chips = { web: !!settings.webDefault, reason: !!settings.reasoningDefault };

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
    toggleWeb: $('#toggleWeb'),
    toggleReason: $('#toggleReason'),
    attachBtn: $('#attachBtn'),
    attachRemove: $('#attachRemove'),
    attachChip: $('#attachChip'),
    attachThumb: $('#attachThumb'),
    attachName: $('#attachName'),
    fileInput: $('#fileInput'),
    dialog: $('#settingsDialog'),
    openSettings: $('#openSettings'),
    exportAll: $('#exportAll'),
    wipeAll: $('#wipeAll'),
    toast: $('#toast'),
  };

  // ------------------------------------------------------------------ хранилище

  const saveSessions = () => writeJSON(KEY.sessions, sessions);
  const getMsgs = (id) => readJSON(KEY.msgs + id, []);
  function setMsgs(id, msgs) {
    const ok = writeJSON(KEY.msgs + id, msgs.slice(-MAX_STORED));
    if (!ok) toast('Не удалось сохранить: переполнено локальное хранилище');
    const s = sessions.find((x) => x.id === id);
    if (s) { s.updated = Date.now(); saveSessions(); }
  }
  const currentSession = () => sessions.find((s) => s.id === currentId) || null;

  function createSession(firstText) {
    const id = uid();
    let name = (firstText || '').replace(/\s+/g, ' ').trim().slice(0, 48);
    if (!name) name = 'Чат ' + (sessions.length + 1);
    const now = Date.now();
    sessions.unshift({ id, name, created: now, updated: now });
    saveSessions();
    return id;
  }
  function ensureNotifySession() {
    if (!sessions.find((s) => s.id === NOTIFY_ID)) {
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
      b.addEventListener('click', () => { selectSession(s.id); closeNav(); });
      els.list.appendChild(b);
    }
  }

  // ------------------------------------------------------------------ рендер: сообщения

  function renderMd(text) {
    return DOMPurify.sanitize(marked.parse(text || '', { breaks: true, gfm: true }));
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
      const d = document.createElement('details');
      d.className = 'reasoning';
      const s = document.createElement('summary');
      s.textContent = 'Ход рассуждений';
      const body = document.createElement('div');
      body.className = 'rbody';
      body.textContent = m.reasoning;
      d.append(s, body);
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

  const ICON_REFRESH = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 4v5h-5"/></svg>';

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

  function scrollEnd(force) {
    if (!force && !settings.autoScroll) return;
    els.messages.scrollTop = els.messages.scrollHeight;
  }

  function renderMessages(opts = {}) {
    els.inner.innerHTML = '';
    const msgs = currentId ? getMsgs(currentId) : [];
    if (!msgs.length) {
      els.inner.appendChild(welcomeNode());
      return;
    }
    msgs.forEach((m, i) => els.inner.appendChild(buildMessage(m, i)));
    if (opts.scroll !== false) scrollEnd(true);
  }

  // ------------------------------------------------------------------ состояние панели

  function canSend() { return !!(els.input.value.trim() || attachment); }

  function setSendState() {
    if (busy) {
      els.send.classList.add('stop');
      els.send.textContent = '■';
      els.send.title = 'Остановить';
      els.send.disabled = false;
    } else {
      els.send.classList.remove('stop');
      els.send.textContent = '➤';
      els.send.title = 'Отправить';
      els.send.disabled = !canSend();
    }
  }

  function updateHeader() {
    const s = currentSession();
    els.title.textContent = s ? s.name : 'Новый чат';
    if (!s) {
      els.sub.textContent = 'Напишите первое сообщение';
    } else if (s.id === NOTIFY_ID) {
      els.sub.textContent = 'Фоновые находки ассистента';
    } else {
      const n = getMsgs(s.id).length;
      els.sub.textContent = n ? `${n} сообщ. · ${fmtTime(s.updated || s.created)}` : 'Пустой чат';
    }
    const has = !!s;
    els.rename.disabled = !has;
    els.exportBtn.disabled = !has;
    els.clearBtn.disabled = !has || busy;
    els.deleteBtn.disabled = !has || busy;
    setSendState();
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
    if (busy) { toast('Дождитесь окончания ответа'); return; }
    currentId = id;
    renderList();
    renderMessages();
    updateHeader();
  }

  function newChat() {
    if (busy) { toast('Дождитесь окончания ответа'); return; }
    currentId = null;
    renderList();
    renderMessages({ scroll: false });
    updateHeader();
    els.input.focus();
  }

  // ------------------------------------------------------------------ SSE

  async function readSSE(res, onEvent, signal) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    const onAbort = () => reader.cancel().catch(() => {});
    if (signal) signal.addEventListener('abort', onAbort, { once: true });
    try {
      await readLoop(reader, dec, (b) => { buf = b; }, () => buf, onEvent, signal);
    } finally {
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }

  async function readLoop(reader, dec, setBuf, getBuf, onEvent, signal) {
    for (;;) {
      if (signal && signal.aborted) throw new DOMException('Остановлено', 'AbortError');
      const { value, done } = await reader.read();
      if (done) return;
      let buf = getBuf() + dec.decode(value, { stream: true });
      setBuf(buf);
      let i;
      while ((i = buf.indexOf('\n\n')) !== -1) {
        const block = buf.slice(0, i);
        buf = buf.slice(i + 2);
        setBuf(buf);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const data = line.slice(5).trim();
          if (data === '[DONE]') { reader.cancel().catch(() => {}); return; }
          try { onEvent(JSON.parse(data)); } catch { /* битый кадр пропускаем */ }
        }
      }
    }
  }

  function applyEvent(ev, m) {
    if (ev.token !== undefined) m.text += ev.token;
    else if (ev.reasoning_token !== undefined) m.reasoning = (m.reasoning || '') + ev.reasoning_token;
    else if (ev.sources) m.sources = (m.sources || []).concat(ev.sources);
    else if (ev.image_url) m.images = (m.images || []).concat([ev.image_url]);
    else if (ev.tool_call) m.notes = (m.notes || []).concat(['🔧 ' + ev.tool_call]);
    else if (ev.warning) m.notes = (m.notes || []).concat(['⚠ ' + ev.warning]);
    else if (ev.error) m.error = String(ev.error);
  }

  // ------------------------------------------------------------------ отправка и генерация

  async function send(override) {
    if (busy) return;
    const text = (override !== undefined ? override : els.input.value).trim();
    if (!text && !attachment) return;

    if (!currentId) currentId = createSession(text || 'Фото');
    const id = currentId;
    const img = attachment;
    attachment = null;
    renderAttachment();
    els.input.value = '';
    autoSize();

    const msgs = getMsgs(id);
    msgs.push({
      role: 'user',
      text,
      time: Date.now(),
      image: img && img.url.length < MAX_IMAGE_STORE ? img.url : null,
    });
    setMsgs(id, msgs);
    renderList();
    await generate(id, text || 'Опиши, что изображено', img);
  }

  async function generate(id, text, img) {
    const msgs = getMsgs(id);
    const bot = {
      role: 'assistant', text: '', reasoning: '', sources: [], images: [], notes: [],
      error: null, time: Date.now(), pending: true,
    };
    msgs.push(bot);
    setMsgs(id, msgs);
    const idx = msgs.length - 1;
    if (id === currentId) renderMessages();

    const bubbleEl = () => els.inner.querySelector(`.msg[data-idx="${idx}"] .bubble`);
    let queued = false;
    const paint = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        const b = id === currentId ? bubbleEl() : null;
        if (b) { paintBubble(b, bot); scrollEnd(); }
      });
    };

    busy = true;
    controller = new AbortController();
    updateHeader();

    try {
      const payload = { message: text, stream: true, web_search: chips.web, reasoning: chips.reason };
      if (img) { payload.image_base64 = img.base64; payload.image_mime = img.mime; }
      const res = await fetch(API.chat, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      if (!res.ok) {
        const t = await res.text();
        throw new Error(`HTTP ${res.status}: ${t.slice(0, 200)}`);
      }
      await readSSE(res, (ev) => { applyEvent(ev, bot); paint(); }, controller.signal);
      if (controller.signal.aborted) {
        bot.notes.push('⏹ Остановлено');
      } else if (!bot.text && !bot.error) {
        bot.text = '(пустой ответ)';
      }
    } catch (err) {
      if (err.name === 'AbortError') bot.notes.push('⏹ Остановлено');
      else bot.error = err.message || String(err);
    } finally {
      bot.pending = false;
      busy = false;
      controller = null;
      setMsgs(id, msgs);
      if (id === currentId) renderMessages();
      renderList();
      updateHeader();
      els.input.focus();
    }
  }

  function stopGeneration() {
    if (controller) controller.abort();
  }

  function regenerate(idx) {
    if (busy || !currentId) return;
    const msgs = getMsgs(currentId);
    let userIdx = -1;
    for (let i = idx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') { userIdx = i; break; }
    }
    if (userIdx < 0) return;
    const prompt = msgs[userIdx].text;
    msgs.splice(userIdx + 1);          // удаляем ответ и всё после вопроса
    setMsgs(currentId, msgs);
    generate(currentId, prompt, null);
  }

  function onSendButton() {
    if (busy) stopGeneration();
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
    for (const m of getMsgs(s.id)) {
      lines.push(`**${m.role === 'user' ? 'Вы' : 'Ассистент'}** · ${fmtTime(m.time || Date.now())}`, '', m.text || '', '');
    }
    download(safeFileName(s.name) + '.md', lines.join('\n'), 'text/markdown');
  }

  function clearCurrent() {
    const s = currentSession();
    if (!s || busy) return;
    if (!confirm('Очистить историю этого чата?')) return;
    setMsgs(s.id, []);
    renderMessages({ scroll: false });
    updateHeader();
    toast('История очищена');
  }

  function deleteCurrent() {
    const s = currentSession();
    if (!s || busy) return;
    if (!confirm('Удалить этот чат?')) return;
    sessions = sessions.filter((x) => x.id !== s.id);
    saveSessions();
    localStorage.removeItem(KEY.msgs + s.id);
    currentId = null;
    renderList();
    renderMessages({ scroll: false });
    updateHeader();
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
      const msgs = getMsgs(NOTIFY_ID);
      for (const it of items) {
        const text = typeof it === 'string'
          ? it
          : (it.text || it.message || it.content || it.summary || JSON.stringify(it));
        msgs.push({
          role: 'assistant', text, reasoning: '', sources: [], images: [], notes: [],
          error: null, time: Date.now(),
        });
      }
      setMsgs(NOTIFY_ID, msgs);
      renderList();
      if (currentId === NOTIFY_ID && !busy) renderMessages();
      toast(`🔔 Новых находок: ${items.length}`);
    } catch { /* сеть недоступна — попробуем в следующий раз */ }
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
    for (const s of sessions) data.messages[s.id] = getMsgs(s.id);
    download('ai-chats-' + Date.now() + '.json', JSON.stringify(data, null, 2), 'application/json');
  }

  function wipeAllChats() {
    if (!confirm('Удалить все чаты в этом браузере? Это нельзя отменить.')) return;
    for (const s of sessions) localStorage.removeItem(KEY.msgs + s.id);
    sessions = [];
    saveSessions();
    currentId = null;
    renderList();
    renderMessages({ scroll: false });
    updateHeader();
    toast('Все чаты удалены');
  }

  // ------------------------------------------------------------------ обработчики

  els.newBtn.addEventListener('click', () => { newChat(); closeNav(); });
  els.menuBtn.addEventListener('click', () => els.app.classList.toggle('nav-open'));
  els.scrim.addEventListener('click', closeNav);
  els.search.addEventListener('input', renderList);

  els.send.addEventListener('click', onSendButton);
  els.input.addEventListener('input', () => { autoSize(); setSendState(); });
  els.input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return;
    const shouldSend = settings.enterSends ? !e.shiftKey : (e.ctrlKey || e.metaKey);
    if (shouldSend) { e.preventDefault(); onSendButton(); }
  });

  els.toggleWeb.addEventListener('click', () => { chips.web = !chips.web; syncChips(); });
  els.toggleReason.addEventListener('click', () => { chips.reason = !chips.reason; syncChips(); });

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
    const m = getMsgs(currentId)[idx];
    if (!m) return;
    if (btn.dataset.act === 'copy') copyText(m.text || '');
    if (btn.dataset.act === 'regen') regenerate(idx);
  });

  els.openSettings.addEventListener('click', () => els.dialog.showModal());
  els.exportAll.addEventListener('click', exportAllChats);
  els.wipeAll.addEventListener('click', wipeAllChats);

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
    if (e.key === 'Escape') closeNav();
  });

  // ------------------------------------------------------------------ старт

  function init() {
    bindSettings();
    applySettings();
    syncChips();
    renderList();
    if (sessions.length) {
      const latest = sessions.slice().sort((a, b) => (b.updated || b.created) - (a.updated || a.created))[0];
      currentId = latest.id;
    }
    renderList();
    renderMessages({ scroll: false });
    updateHeader();
    autoSize();
    ping();
    setInterval(ping, 30000);
    startNotify();
    pollNotifications();
  }

  init();
})();
