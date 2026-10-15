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
    media: '/ai/ui-media',
    genStatus: '/ai/gen-status',
    genLock: '/ai/gen-lock',
  };
  // Чаты хранятся отдельно для каждого аккаунта: иначе другой человек на этом же
  // браузере увидит чужие разговоры. Настройки общие для браузера.
  const KEY = { sessions: 'ai_sessions', msgs: 'ai_messages_', settings: 'ai_settings_v2', gen: 'ai_gen', cid: 'ai_cid' };
  function setScope(uid) {
    KEY.sessions = uid ? `ai:${uid}:sessions` : 'ai_sessions';
    KEY.msgs = uid ? `ai:${uid}:messages_` : 'ai_messages_';
    KEY.gen = uid ? `ai:${uid}:gen` : 'ai_gen';
    LS_MSGS = uid ? `ai:${uid}:messages_` : 'ai_messages_';
  }
  // Устойчивый id устройства (не аккаунта!): серверный генерационный лок знает,
  // КАКОЕ устройство ведёт ответ — это позволяет остальным безопасно смотреть
  // печать через attach?watch=1 и не давит чужими PUT'ами финальный commit.
  // Хранится в общем ключе без scope: перезаход в тот же браузер = то же устройство.
  function clientId() {
    try {
      let v = localStorage.getItem(KEY.cid);
      if (!v) { v = 'c_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10); localStorage.setItem(KEY.cid, v); }
      return v;
    } catch { return ''; }
  }
  const CID = clientId();
  let LS_MSGS = 'ai_messages_';   // ключ-префикс localStorage-зеркала (устанавливается в setScope)
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
      id: uid(),   // устойчивый id: по нему сервер схлопывает повторы при слиянии
      role: 'assistant', text: '', reasoning: '', sources: [], images: [], notes: [],
      error: null, time: Date.now(), pending: true,
    };
  }
  function resetBot(m) {
    m.text = ''; m.reasoning = ''; m.sources = []; m.images = []; m.notes = []; m.error = null;
  }
  // Для наблюдателя сбрасываем только НАКОПЛЕННОЕ из потока: его id и time
  // совпадают с серверным сообщением, поэтому финальный merge схлопнёт локальную
  // запись с серверной по ключу id (дублей не будет). Очистка id/time здесь
  // ломала бы это схлопывание.
  function resetBotWatch(m) {
    m.text = ''; m.reasoning = ''; m.sources = []; m.images = []; m.error = null;
    m.notes = (m.notes || []).filter((n) => !String(n).startsWith('👁') && !String(n).startsWith('⏳'));
  }

  // ------------------------------------------------------------------ состояние

  let settings = Object.assign({}, DEFAULTS, readJSON(KEY.settings, {}));
  let sessions = [];                 // загружаются в init() после определения аккаунта
  let cache = {};                 // id -> живой массив сообщений открытого чата
  let currentId = null;           // открытый в интерфейсе чат
  let gen = null;                 // активная генерация: {sessionId, idx, controller, stopRequested}
  let attachment = null;          // {url, base64, mime, name, file?, mediaUrl?}
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
  // Основное место — IndexedDB (ai-store.js): лимиты сотни МБ, асинхронные записи,
  // частичные обновления вместо перезаписи всего чата. localStorage остаётся
  // best-effort зеркалом (быстрый старт, старые вкладки). Синхронизация с сервером
  // передаёт только дельту — изменённые сообщения, а не весь контент.

  const store = () => window.AiStore;   // есть всегда: скрипт грузится до этого

  function saveSessions() {
    try { store().saveSessions(sessions); } catch { /* ignore */ }
    scheduleSync();
  }
  const currentSession = () => sessions.find((s) => s.id === currentId) || null;
  const findSession = (id) => sessions.find((s) => s.id === id) || null;

  // cache[id] — живой массив открытого/активного чата (его мутирует генерация).
  // Закрытые чаты читаются из хранилища (ai-store.js) и кэшируются снимком;
  // при расхождении версий берём более свежий снимок.
  const lastSeen = {};        // id -> массив
  const lastSeenAt = {};      // id -> время чтения снимка
  function loadMsgs(id) {
    if (!(id in cache)) {
      const v = readJSON(LS_MSGS + id, []);
      cache[id] = Array.isArray(v) ? v : [];
      lastSeenAt[id] = 0;
    }
    // dedupeByKeys — единый механизм против дублей при повторном открытии чата.
    // История может прийти из двух источников (IndexedDB и localStorage-зеркало),
    // а после слияний на сервере — содержать одинаковые сообщения на разных
    // позициях. Оставляем первое вхождение каждого устойчивого ключа (id либо
    // role+time+text): «открыл и закрыл диалог — он скопировался» становится
    // невозможным локально, ещё до отправки на сервер.
    cache[id] = dedupeByKeys(cache[id]);
    reconcile(id);
    return cache[id];
  }
  function dedupeByKeys(msgs) {
    let hasDup = false;
    for (let i = 1; i < msgs.length && !hasDup; i++) {
      if (!msgs[i]) continue;
      const k = msgKey(msgs[i]);
      if (!k) continue;
      for (let j = 0; j < i; j++) {
        if (msgs[j] && msgKey(msgs[j]) === k) { hasDup = true; break; }
      }
    }
    if (!hasDup) return msgs;                 // обычный путь: без аллокаций
    const seen = new Set();
    const out = [];
    for (const m of msgs) {
      if (!m) continue;
      const k = msgKey(m);
      if (k) {
        if (seen.has(k)) continue;
        seen.add(k);
      }
      out.push(m);
    }
    return out;
  }
  function msgKey(m) {
    if (typeof m.id === 'string' && m.id) return 'id:' + m.id;
    if (typeof m.time === 'number') return `ct:${m.role || ''}|${m.time}|${(m.text || '').slice(0, 80)}`;
    return null;
  }
  function reconcile(id) {
    const prev = lastSeen[id];
    if (!prev || cache[id].length >= prev.length) lastSeen[id] = cache[id];
    else cache[id] = prev;
  }
  // Снимок закрытого чата из хранилища; устаревший (<2 с) не перечитываем —
  // isPending вызывается на каждой отрисовке списка чатов
  async function peekMsgs(id) {
    if (id in cache) return cache[id];
    if (lastSeen[id] && Date.now() - (lastSeenAt[id] || 0) < 2000) return lastSeen[id];
    try {
      const arr = await store().loadMessages(id);
      const msgs = arr.map((x) => x.m).filter(Boolean);
      lastSeen[id] = dedupeByKeys(msgs).slice(-MAX_STORED);
      lastSeenAt[id] = Date.now();
    } catch { /* оставляем прежний снимок */ }
    return lastSeen[id] || [];
  }
  // Инвалидация кеша после записи в обход живых массивов (merge, remote-дельта):
  // перечитываем из хранилища только те чаты, что уже держатся в памяти
  async function reloadFromStore(id) {
    try {
      const arr = await store().loadMessages(id);
      const msgs = dedupeByKeys(arr.map((x) => x.m).filter(Boolean));
      if (msgs.length >= (cache[id] || []).length) { cache[id] = msgs.slice(-MAX_STORED); lastSeen[id] = cache[id]; }
    } catch { /* оставляем прежний кеш */ }
  }

  async function persist(id) {
    const msgs = loadMsgs(id);
    let ok = true;
    try { await store().commitMessages(id, msgs.slice(-MAX_STORED), null); }
    catch (e) { console.warn('persist:', e); ok = false; }
    scheduleSync();
    if (!ok) toast('Не удалось сохранить: переполнено локальное хранилище');
  }
  // Частые обновления во время генерации сохраняем не чаще раза в PERSIST_DELAY_MS.
  // Пока чат открыт — пишем только его; историю закрытых чатов дописываем сразу,
  // чтобы после переключения вкладок ответ не терялся.
  function persistSoon(id) {
    if (id === currentId || (gen && gen.sessionId === id)) {
      if (persistTimers[id]) return;
      persistTimers[id] = setTimeout(() => { delete persistTimers[id]; persist(id); }, PERSIST_DELAY_MS);
    } else {
      persist(id);
    }
  }
  async function commit(id) {
    if (persistTimers[id]) { clearTimeout(persistTimers[id]); delete persistTimers[id]; }
    await persist(id);
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
    lastSeen[id] = cache[id];
    saveSessions();
    persist(id);
    return id;
  }

  function removeSession(id) {
    sessions = sessions.filter((s) => s.id !== id);
    delete cache[id];
    delete lastSeen[id];
    // tombstone пишется внутри store().removeSession (deleteChat) — раньше его
    // записывали отдельно и гоняли транзакции одна за другой
    try { store().removeSession(id); } catch { /* ignore */ }
    localStorage.removeItem(LS_MSGS + id);
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
      const dot = document.createElement('span');
      dot.className = 'dot';
      dot.title = 'Ответ пишется';
      dot.hidden = !isPending(s.id);
      if (s.id !== currentId && !(gen && gen.sessionId === s.id)) {
        // закрытый чат: проверяем «пишется» асинхронно из хранилища, без блокировки списка
        peekMsgs(s.id).then((arr) => { dot.hidden = !arr.some((m) => m.pending); });
      }
      b.appendChild(dot);
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
        img.className = 'gen zoomable';
        img.src = m.image;
        img.alt = 'Прикреплённое изображение';
        img.loading = 'lazy';
        // тяжёлые data URL раньше молча не дорисовывались после перезагрузки
        // (квота хранилища резала запись); теперь это явный, кликабельный статус
        img.addEventListener('error', () => {
          img.style.display = 'none';
          const s = document.createElement('div');
          s.className = 'meta-line';
          s.textContent = '🖼 Фото недоступно локально — загрузите его заново';
          bubble.appendChild(s);
        });
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
      img.className = 'gen zoomable';
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

  async function selectSession(id) {
    if (id === currentId) return;
    await peekMsgs(id);            // подтянуть историю закрытого чата из IndexedDB
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

  // ------------------------------------------------------------------ генерационный лок
  // Единый механизм для всех устройств: ответ пишет ровно одно устройство,
  // сервер хранит активную генерацию как фоновую задачу и знает её владельца.
  // Флаг myGen (localStorage в scope аккаунта) переживает перезагрузку вкладки —
  // именно то устройство, которое начало генерацию, подхватывает её через attach
  // и имеет право синхронизировать чат; остальные ждут финального ответа.

  const genFlagGet = () => { try { return localStorage.getItem(KEY.gen) === '1'; } catch { return false; } };
  const genFlagSet = (v) => { try { v ? localStorage.setItem(KEY.gen, '1') : localStorage.removeItem(KEY.gen); } catch { /* ignore */ } };

  async function acquireGenLock() {
    if (!currentScopeUid) return true;              // локальный режим без входа — лок не нужен
    try {
      const r = await fetch(API.genLock, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client: !!(gen || genFlagGet()), cid: CID }),   // продление своей vs попытка занять чужой
      });
      if (!r.ok) return true;                       // старый/недоступный сервер — не блокируем отправку
      const j = await r.json();
      if (j.held_by_me) genFlagSet(true);
      return !!j.ok;
    } catch { return true; }                        // нет связи — решаем локально (как раньше)
  }

  async function releaseGenLock() {
    if (!currentScopeUid || !genFlagGet()) return;
    genFlagSet(false);
    try {
      await fetch(API.genLock, {
        method: 'DELETE', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client: true, cid: CID }),
      });
    } catch { /* TTL на сервере снимет лок сам */ }
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

  // mode: 'send' — новый запрос к /ai/chat; 'attach' — подхват уже идущей генерации.
  // attachMode: 'replay' — владелец (свой флаг myGen): он же допишет и закоммитит ответ;
  //               'watch'   — наблюдатель с другого устройства: только показывает печать,
  //                           финальный текст придёт через синхронизацию владельца.
  async function startGeneration(id, idx, mode, body, attachMode) {
    const controller = new AbortController();
    const watch = mode === 'attach' && attachMode === 'watch';
    const g = { sessionId: id, idx, controller, stopRequested: false, interrupted: false, authLost: false, watch };
    gen = g;
    if (mode === 'send') genFlagSet(true);   // это устройство — владелец генерации до её завершения
    refreshUI();

    const msgs = loadMsgs(id);
    const bot = msgs[idx];
    if (watch) pushNote(bot, '👁 Ответ печатается на другом устройстве');
    try {
      const res = mode === 'attach'
        ? await fetch(API.attach + (watch ? '?watch=1' : ''), { credentials: 'same-origin', signal: controller.signal })
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
      if (mode === 'attach') { watch ? resetBotWatch(bot) : resetBot(bot); }

      let none = false;
      const finished = await readSSE(res, (ev) => {
        if (ev.no_active_generation) { none = true; return; }
        applyEvent(ev, bot);
        // наблюдатель НЕ пишет в своё хранилище/на сервер: финальный commit
        // делает только владелец генерации — иначе два клиента гоняли бы
        // параллельные PUT по одному сообщению («каша» между устройствами)
        if (!watch) persistSoon(id);
        if (id === currentId) paintLive(idx);
      }, controller.signal);

      if (!controller.signal.aborted) {
        if (none) {
          if (watch) {
            // чужая генерация уже завершилась, пока мы подключались:
            // ждём финального состояния от синхронизации, не трогаем историю
            g.interrupted = true;
          } else if (!bot.text && !bot.error) {
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
        if (g.watch) {
          // Наблюдение окончено (ответ доработал или пропал): локальная запись —
          // временная, pending остаётся. Придёт commit владельца -> pullState/дельта
          // схлопнет это сообщение по id с серверным и очистит pending.
          if (gen === g) gen = null;
          if (!g.interrupted) { pushNote(bot, '⏳ Ожидаем сохранение ответа…'); persistSoon(id); }
          scheduleResume(5000);
          if (id === currentId) renderMessages({ scroll: false });
          refreshUI();
        } else if (g.stopRequested) {
          bot.notes.push('⏹ Остановлено');
          bot.pending = false;
        } else if (g.interrupted) {
          // Не помечаем готовым: сообщение остаётся «пишется», пока не получим ответ
          bot.pending = true;
          pushNote(bot, '📡 Соединение прервано, ждём ответ…');
        } else {
          if (!bot.text && !bot.error) bot.text = '(пустой ответ)';
          bot.pending = false;
          genFlagSet(false);            // ответ завершён — синхронизация снова открыта всем
        }
        if (gen === g) gen = null;
        if (!g.watch) commit(id);
        if (id === currentId) renderMessages({ scroll: false });
        refreshUI();
        if (!g.watch) {
          if (g.interrupted && !g.stopRequested) scheduleResume(5000);
          else els.input.focus();
          if (!g.interrupted) releaseGenLock();   // владение локом снято (await вне критической секции)
        }
      }
    }
  }

  let resumeTimer = null;
  function scheduleResume(delay) {
    clearTimeout(resumeTimer);
    resumeTimer = setTimeout(() => resumePending(), delay);
  }

  // Подхватываем незавершённые ответы, например после перезагрузки страницы.
  // Открытый/активный чат проверяем сразу, закрытые — асинхронно из хранилища.
  // Единый механизм для всех устройств (решение спрашивает СЕРВЕР, а не флаг):
  //  - mine=true   — генерацию ведёт это устройство: полный replay + право commit;
  //  - may_watch   — генерация жива на сервере (ведёт другое устройство):
  //                  подключаемся как наблюдатель (attach?watch=1) — печать
  //                  видна в реальном времени, но финальную запись делает владелец;
  //  - иначе       — активной генерации нет: подхватываем СВОЙ осиротевший ответ
  //                  (флаг myGen) или ждём синхронизацию, если pending чужой.
  async function resumePending() {
    clearTimeout(resumeTimer);
    if (gen) return;
    let st = null;              // ответ /gen-status (null — не проверено)
    const priority = [currentId, gen && gen.sessionId].filter(Boolean);
    const rest = sessions.map((s) => s.id).filter((id) => !priority.includes(id));
    for (const id of priority.concat(rest)) {
      if (gen) return;
      const msgs = await peekMsgs(id);
      const idx = msgs.findIndex((m) => m.pending);
      if (idx < 0) continue;
      if (st === null) {
        try {
          const r = await fetch(API.genStatus + '?cid=' + encodeURIComponent(CID),
                                { credentials: 'same-origin', cache: 'no-store' });
          st = (r.ok && (await r.json())) || {};
        } catch { st = {}; }    // нет связи — решаем по локальному флагу (как раньше)
      }
      const mine = st.mine || genFlagGet();
      if (mine) {
        await startGeneration(id, idx, 'attach', null, 'replay');
      } else if (st.active && st.may_watch) {
        await startGeneration(id, idx, 'attach', null, 'watch');   // смотрим чужую печать
      } else if (!st.active && !genFlagGet()) {
        continue;               // генерации нет и она была не наша — ждём sync владельца
      } else {
        await startGeneration(id, idx, 'attach', null, 'replay');  // свой оборванный ответ
      }
      if (currentId === id) renderMessages({ scroll: false });
    }
    refreshUI();
  }

  // Отправка — асинхронная обёртка над sync-логикой sendNow (см. ниже):
  // перед новым запросом занимаем генерационный лок, чтобы другие устройства
  // на время нашего ответа заморозили свою синхронизацию этого аккаунта.
  function send(override) {
    acquireGenLock().then((ok) => {
      if (!ok) { toast('Дождитесь окончания ответа на другом устройстве'); updateHint(); return; }
      sendNow(override);
    });
  }

  function sendNow(override) {
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
    // фото покажем ссылкой сразу после загрузки на сервер (см. ниже): тяжёлые
    // data URL в истории не храним — они забивали квоту и роняли показ после перезагрузки
    const userMsg = {
      id: uid(),   // устойчивый id: по нему сервер схлопывает повторы при слиянии
      role: 'user',
      text,
      time: Date.now(),
      image: img && img.url.length < MAX_IMAGE_STORE ? img.url : null,
    };
    msgs.push(userMsg);
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

    // Тяжёлое фото грузим в медиатеку параллельно с генерацией: ответ модели
    // не ждёт загрузки, а ссылка для истории появляется, как только готова.
    let pendingMedia = null;
    if (img && img.url.length >= MAX_IMAGE_STORE) {
      pendingMedia = ensureMediaUrl(img).then((u) => {
        if (!u) return;
        userMsg.image = u;
        commit(id);                       // короткая ссылка вместо «фото пропало»
        if (currentId === id) renderMessages({ scroll: false });
      });
    }
    startGeneration(id, idx, 'send', body).finally(() => {
      if (pendingMedia) pendingMedia.catch(() => {});
    });
  }

  async function stopGeneration() {
    if (!gen) return;
    gen.stopRequested = true;
    try { await fetch(API.stop, { method: 'POST', credentials: 'same-origin' }); } catch { /* сеть */ }
    gen.controller.abort();
  }

  async function regenerate(idx) {
    if (gen) { toast('Дождитесь окончания ответа'); return; }
    const id = currentId;
    const msgs = loadMsgs(id);
    let u = -1;
    for (let i = idx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') { u = i; break; }
    }
    if (u < 0) return;
    const prompt = msgs[u].text || 'Продолжи';
    // картинка вопроса: data URL режем до base64, ссылку медиатеки скачиваем обратно —
    // иначе «получить другой ответ» на вопрос с фото терял изображение
    let regenImg = null;
    const im = msgs[u].image;
    if (im && typeof im === 'string' && im.startsWith('data:')) {
      regenImg = { base64: im.split(',')[1] || '', mime: (im.slice(5).split(';')[0] || 'image/png') };
    } else if (im && !/^data:/i.test(im)) {
      try {
        const r = await fetch(im, { credentials: 'same-origin' });
        if (r.ok) {
          const buf = new Uint8Array(await r.arrayBuffer());
          let bin = '';
          for (let i = 0; i < buf.length; i += 0x8000) {
            bin += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
          }
          regenImg = { base64: btoa(bin), mime: r.headers.get('content-type') || 'image/png' };
        }
      } catch { /* сеть — перегенерируем без фото */ }
    }
    msgs.splice(u + 1);
    msgs.push(newBotMessage());
    const nidx = msgs.length - 1;
    commit(id);
    renderMessages({ scroll: false });
    refreshUI();
    const body = { message: prompt, stream: true, web_search: chips.web, reasoning: chips.reason };
    if (regenImg && regenImg.base64) { body.image_base64 = regenImg.base64; body.image_mime = regenImg.mime; }
    startGeneration(id, nidx, 'send', body);
  }

  function onSendButton() {
    if (isGenHere()) stopGeneration();
    else send();
  }

  // ------------------------------------------------------------------ вложения

  // Тяжёлые картинки (> MAX_IMAGE_STORE) храним не base64-строкой в истории,
  // а ссылкой на серверную медиатеку: иначе архив чатов быстро раздувал и
  // IndexedDB, и синхронизацию. Файл отдаётся по sha256 — повторная загрузка
  // того же изображения ничего не пишет (дедупликация на сервере).
  async function ensureMediaUrl(img) {
    if (!img) return null;
    if (img.mediaUrl) return img.mediaUrl;
    if (img.url.length < MAX_IMAGE_STORE) return img.url;   // маленький data URL оставим как есть
    try {
      let r;
      if (img.file) {
        // multipart с сырым файлом: без двойного раздувания base64 в памяти/сети
        const fd = new FormData();
        fd.append('file', img.file, img.name || 'image');
        r = await fetch(API.media, { method: 'POST', credentials: 'same-origin', body: fd });
      } else {
        r = await fetch(API.media, { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data: img.base64, mime: img.mime }) });
      }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      if (j && typeof j.url === 'string') {
        img.mediaUrl = j.url;
        return j.url;
      }
      throw new Error('нет url в ответе');
    } catch (e) {
      console.warn('media upload failed:', e);
      toast('Фото не сохранится в истории (не удалось загрузить на сервер), но отправлено');
      return null;
    }
  }

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
        // детерминированный id: одна и та же находка, полученная на двух
        // устройствах, схлопнётся в одну запись при слиянии на сервере
        const nid = typeof it === 'object' && it && (it.id || it.uid || it.hash);
        msgs.push({
          id: 'ntf_' + (nid || btoa(encodeURIComponent(String(text)).slice(0, 64))),
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

  async function exportAllChats() {
    const data = { exportedAt: new Date().toISOString(), sessions, messages: {} };
    for (const s of sessions) data.messages[s.id] = await peekMsgs(s.id);
    download('ai-chats-' + Date.now() + '.json', JSON.stringify(data, null, 2), 'application/json');
  }

  async function wipeAllChats() {
    if (gen) { toast('Дождитесь окончания ответа'); return; }
    if (!confirm('Удалить все чаты в этом браузере? Это нельзя отменить.')) return;
    for (const s of sessions) {
      delete cache[s.id]; delete lastSeen[s.id];
      localStorage.removeItem(LS_MSGS + s.id);
      try { await store().removeSession(s.id); } catch { /* ignore */ }
    }
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
      attachment = { url, base64: url.split(',')[1] || '', mime: f.type, name: f.name, file: f };
      renderAttachment();
    };
    reader.readAsDataURL(f);
  });

  // drag&drop изображения на область чата — тот же путь, что и через кнопку
  els.inner.addEventListener('dragover', (e) => { e.preventDefault(); });
  els.inner.addEventListener('drop', (e) => {
    const f = [...(e.dataTransfer?.files || [])].find((x) => x.type.startsWith('image/'));
    if (!f) return;
    e.preventDefault();
    if (f.size > MAX_IMAGE_BYTES) { toast('Изображение больше 8 МБ'); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      attachment = { url, base64: url.split(',')[1] || '', mime: f.type, name: f.name, file: f };
      renderAttachment();
    };
    reader.readAsDataURL(f);
  });

  // ------------------------------------------------------------------ лайтбокс фото
  // Клик по любому изображению в чате открывает полноэкранный просмотр:
  // колесо/пинч — масштаб, перетаскивание — панорама (особенно полезно для
  // скриншотов с мелким текстом), ←/→ — переход между фото этого же ответа,
  // Esc или клик по тёмному фону — закрыть.
  const lb = { root: null, img: null, cap: null, counter: null, prev: null, next: null,
               urls: [], idx: -1, scale: 1, tx: 0, ty: 0, drag: null };

  function buildLightbox() {
    if (lb.root) return;
    lb.root = document.createElement('div');
    lb.root.className = 'lightbox';
    lb.root.hidden = true;
    lb.root.setAttribute('role', 'dialog');
    lb.root.setAttribute('aria-modal', 'true');
    lb.root.setAttribute('aria-label', 'Просмотр изображения');

    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'lb-btn lb-close';
    close.textContent = '✕';
    close.setAttribute('aria-label', 'Закрыть');
    close.addEventListener('click', () => closeLightbox());

    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'lb-btn lb-nav lb-prev';
    prev.textContent = '‹';
    prev.setAttribute('aria-label', 'Предыдущее изображение');
    prev.addEventListener('click', () => stepLightbox(-1));

    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'lb-btn lb-nav lb-next';
    next.textContent = '›';
    next.setAttribute('aria-label', 'Следующее изображение');
    next.addEventListener('click', () => stepLightbox(1));

    lb.img = new Image();
    lb.img.alt = '';
    lb.img.draggable = false;

    lb.cap = document.createElement('div');
    lb.cap.className = 'lb-cap';
    lb.counter = document.createElement('span');
    lb.counter.className = 'lb-counter';
    lb.counter.hidden = true;

    lb.prev = prev;
    lb.next = next;
    lb.root.append(close, prev, next, lb.img, lb.cap, lb.counter);
    document.body.appendChild(lb.root);

    // клик по тёмному фону (не по картинке/кнопкам) закрывает
    lb.root.addEventListener('click', (e) => {
      if (e.target === lb.root) closeLightbox();
    });
    lb.root.addEventListener('wheel', (e) => {
      e.preventDefault();
      zoomBy(Math.exp(-e.deltaY * 0.0015));
    }, { passive: false });
    lb.root.addEventListener('pointerdown', (e) => {
      if (e.target !== lb.img) return;
      lb.drag = { x: e.clientX, y: e.clientY, tx: lb.tx, ty: lb.ty };
      lb.root.classList.add('grabbing');
      lb.img.setPointerCapture(e.pointerId);
    });
    lb.root.addEventListener('pointermove', (e) => {
      if (!lb.drag) return;
      lb.tx = lb.drag.tx + (e.clientX - lb.drag.x);
      lb.ty = lb.drag.ty + (e.clientY - lb.drag.y);
      applyLbTransform();
    });
    const endDrag = () => { lb.drag = null; lb.root.classList.remove('grabbing'); };
    lb.root.addEventListener('pointerup', endDrag);
    lb.root.addEventListener('pointercancel', endDrag);
    // двойной клик — сброс масштаба / быстрый зум 2x
    lb.img.addEventListener('dblclick', () => {
      if (lb.scale > 1.01) { lb.scale = 1; lb.tx = 0; lb.ty = 0; }
      else lb.scale = 2;
      applyLbTransform();
    });
  }

  function applyLbTransform() {
    lb.img.style.transform = `translate(${lb.tx}px, ${lb.ty}px) scale(${lb.scale})`;
  }

  function zoomBy(k) {
    lb.scale = Math.min(8, Math.max(1, lb.scale * k));
    if (lb.scale === 1) { lb.tx = 0; lb.ty = 0; }
    applyLbTransform();
  }

  function showLbImage() {
    lb.img.src = lb.urls[lb.idx];
    lb.img.alt = 'Изображение ' + (lb.idx + 1) + ' из ' + lb.urls.length;
    lb.counter.textContent = `${lb.idx + 1} / ${lb.urls.length}`;
    lb.counter.hidden = lb.urls.length < 2;
    lb.prev && (lb.prev.hidden = lb.urls.length < 2);
    lb.next && (lb.next.hidden = lb.urls.length < 2);
    lb.scale = 1; lb.tx = 0; lb.ty = 0;
    applyLbTransform();
  }

  function stepLightbox(d) {
    if (lb.urls.length < 2) return;
    lb.idx = (lb.idx + d + lb.urls.length) % lb.urls.length;
    showLbImage();
  }

  function openLightbox(imgEl) {
    buildLightbox();
    // галерея = все картинки этого пузыря сообщения (несколько картинок в ответе листаются)
    const bubble = imgEl.closest('.bubble');
    const list = [...(bubble ? bubble.querySelectorAll('img.zoomable') : [imgEl])];
    lb.urls = list.map((i) => i.src);
    lb.idx = Math.max(0, list.indexOf(imgEl));
    lb.cap.textContent = imgEl.alt || '';
    lb.root.hidden = false;
    document.documentElement.classList.add('lb-open');
    showLbImage();
    lb.root.querySelector('.lb-close').focus();
  }

  let lbReturnFocus = null;
  function closeLightbox() {
    if (!lb.root || lb.root.hidden) return;
    lb.root.hidden = true;
    lb.img.removeAttribute('src');       // не держим картинку в памяти/запросах
    document.documentElement.classList.remove('lb-open');
    if (lbReturnFocus && lbReturnFocus.isConnected) lbReturnFocus.focus();
    lbReturnFocus = null;
  }

  els.messages.addEventListener('click', (e) => {
    const img = e.target.closest('img.zoomable');
    if (!img) return;
    lbReturnFocus = img;
    openLightbox(img);
  });

  window.addEventListener('keydown', (e) => {
    if (!lb.root || lb.root.hidden) return;
    if (e.key === 'Escape') { e.stopPropagation(); closeLightbox(); }
    else if (e.key === 'ArrowLeft') stepLightbox(-1);
    else if (e.key === 'ArrowRight') stepLightbox(1);
    else if (e.key === '+' || e.key === '=') zoomBy(1.25);
    else if (e.key === '-') zoomBy(0.8);
    else if (e.key === '0') { lb.scale = 1; lb.tx = 0; lb.ty = 0; applyLbTransform(); }
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
  // Протокол дельтовый: PUT /ai/ui-state передаёт ТОЛЬКО изменённые с момента
  // последней отправки сообщения (версии > base) и id удалённых чатов, а не весь
  // контент. Сервер хранит снапшот, применяет дельту и отвечает новой версией.
  const SYNC = { base: 0, ready: false, timer: null, busy: false };

  // «Чужой pending» — сообщение, которое пишет НЕ это устройство (у наблюдателя
  // нет флага myGen). Его нельзя отправлять на сервер как своё: серверный merge
  // по ключу ct:role|time|text схлопнул бы его с эталонной записью владельца,
  // и незавершённый фрагмент перезатёр бы финальный ответ («каша»).
  // Отложенные записи наблюдателя перепроверяются при каждой паузе sync.
  function isForeignPending(m) {
    return !!(m && m.pending) && !genFlagGet() && !!currentScopeUid;
  }
  async function stripForeignPending(sid, list) {
    if (!list || !list.length) return list;
    let changed = false;
    for (const it of list) {
      if (it && it.m && isForeignPending(it.m)) { it.m = null; changed = true; }   // tombstone вместо черновика
    }
    if (!changed) return list;
    const kept = list.filter((it) => it && it.m !== null);
    return kept.length ? kept : null;
  }

  function scheduleSync() {
    if (!SYNC.ready || !currentScopeUid) return;
    // Пока это устройство ведёт генерацию, синхронизация не нужна: ответ ещё
    // не готов, а partial-дельты только создавали бы конфликты на других
    // устройствах. Финальный commit после окончания ответа всё равно вызовет
    // scheduleSync — данные уйдут на сервер ровно один раз и целиком.
    if (genFlagGet()) return;
    clearTimeout(SYNC.timer);
    SYNC.timer = setTimeout(pushState, 1500);
  }

  // Слияние полного состояния (первый вход/409): серверные версии приоритетнее
  // для тех же id, локальные чаты, которых на сервере нет, сохраняются и уйдут
  // на сервер следующей дельтой.
  async function mergeFull(st) {
    // tombstone-реестр сервера: чаты, удалённые на другом устройстве, не
    // должны пережить merge — иначе они «воскресали» при первом же pullState/409
    const dels = new Set(Array.isArray(st.deleted_chats) ? st.deleted_chats : []);
    const byId = new Map();
    for (const s of st.sessions || []) if (!dels.has(s.id)) byId.set(s.id, s);
    for (const s of sessions) if (!byId.has(s.id) && !dels.has(s.id)) byId.set(s.id, s);
    sessions = [...byId.values()];
    for (const id of dels) {
      delete cache[id]; delete lastSeen[id];
      // удаление чата, удалённого на другом устройстве: tombstone НЕ пишется
      // (иначе этот id удалил бы чат при возврате к прежнему аккаунту —
      // scope-реестр глобален в рамках одной вкладки хранилища)
      try { await store().deleteChatSilent(id); } catch { /* ignore */ }
    }
    // Единый механизм слияния на всех устройствах: серверная история не
    // затирает локальную молча, а мержится по устойчивым ключам сообщений.
    // Если оба устройства успели записать одно и то же сообщение (или чат
    // «копировался» при повторном открытии), схлопкаем дубли в одну запись —
    // ровно как это делает серверный _merge_msgs для PUT со снимком.
    for (const s of sessions) {
      const server = st.messages && st.messages[s.id];
      if (!Array.isArray(server)) continue;
      let local = [];
      try { local = (await store().loadMessages(s.id)).map((x) => x.m).filter(Boolean); } catch { /* нет локальной копии */ }
      const merged = mergeMsgLists(local, server.slice(-MAX_STORED));
      await store().commitMessages(s.id, merged, null);
    }
    saveSessions();
    cache = {};
    await reloadDirtySafe();
    if (currentId) renderMessages({ scroll: false });
    refreshUI();
  }

  // Клиентская партия серверного _merge_msgs: совпадения по ключу схлопаются,
  // новые сообщения вставляются в свою позицию, порядок сохраняется.
  function mergeMsgLists(base, incoming) {
    const result = base.map((m) => (m && typeof m === 'object' ? Object.assign({}, m) : m)).filter(Boolean);
    const keyToIdx = new Map();
    const usedKeys = new Set();
    result.forEach((m, i) => {
      const k = msgKey(m);
      if (k && !keyToIdx.has(k)) { keyToIdx.set(k, i); usedKeys.add(k); }
    });
    incoming.forEach((item, pos) => {
      if (!item || typeof item !== 'object') return;
      const k = msgKey(item);
      if (k && keyToIdx.has(k)) {
        Object.assign(result[keyToIdx.get(k)], item);   // тот же объект — патч полями
        return;
      }
      let j = Math.min(pos, result.length);
      while (j < result.length) {
        const ek = msgKey(result[j]);
        if ((!k && !ek) || (k && ek && k !== ek && !usedKeys.has(ek))) break;
        j++;
      }
      result.splice(j, 0, Object.assign({}, item));
      if (k) { keyToIdx.set(k, j); usedKeys.add(k); }
      for (let p = j + 1; p < result.length; p++) {
        const pk = msgKey(result[p]);
        if (pk) { keyToIdx.set(pk, p); usedKeys.add(pk); }
      }
    });
    return dedupeByKeys(result).slice(-MAX_STORED);
  }

  // После записи в обход живых массивов перечитываем в память только открытый
  // чат и чат с активной генерацией — остальные подтянутся при обращении
  async function reloadDirtySafe() {
    const ids = new Set([currentId, gen && gen.sessionId]);
    for (const id of ids) if (id) await reloadFromStore(id);
  }

  // Дельта с сервера: обновляем только пришедшие сообщения; открытые чаты
  // перечитываем из хранилища, закрытые дождутся loadMsgs при следующем обращении.
  // Удалённые чаты (tombstone) обрабатываются ДО применения сообщений: иначе
  // «припозднившаяся» дельта могла бы воскресить историю удалённого на другом
  // устройстве чата — это и есть второй источник «каши» в списке.
  async function applyServerDelta(delta) {
    if (Array.isArray(delta.deleted_chats) && delta.deleted_chats.length) {
      const before = sessions.length;
      sessions = sessions.filter((s) => !delta.deleted_chats.includes(s.id));
      for (const id of delta.deleted_chats) { delete cache[id]; delete lastSeen[id]; }
      if (sessions.length !== before) { saveSessions(); refreshUI(); }
    }
    const msgs = delta.messages || {};
    let touchedCurrent = false;
    for (const [sid, list] of Object.entries(msgs)) {
      if (sid === currentId || (gen && gen.sessionId === sid)) {
        await reloadFromStore(sid);       // живой кеш актуализируем из хранилища
        if (sid === currentId) touchedCurrent = true;
      } else {
        const cur = await store().loadMessages(sid).then((arr) => arr.map((x) => x.m));
        const next = cur.slice();
        for (const it of list) {
          if (it.i == null) continue;
          if (it.m === null || it.m === undefined) next.splice(it.i, 1);
          else if (it.i < next.length) next[it.i] = it.m;
          else next.push(it.m);
        }
        await store().commitMessages(sid, next.filter(Boolean), null);
        delete lastSeen[sid];
      }
    }
    if (Array.isArray(delta.sessions)) {
      const byId = new Map();
      for (const s of delta.sessions) byId.set(s.id, s);
      for (const s of sessions) if (!byId.has(s.id)) byId.set(s.id, s);
      sessions = [...byId.values()];
      refreshUI();
    }
    if (touchedCurrent) renderMessages({ scroll: false });
  }

  async function pullState(force) {
    // во время генерации локальные сообщения трогает ТОЛЬКО владелец генерации
    // (он же их пишет): ему нужен свежий base для финального PUT после ответа.
    // Остальные устройства ждут — иначе mergeFull затёр бы живой ответ.
    if (!currentScopeUid) return false;
    if (genFlagGet() && !force) return false;
    if (gen && !genFlagGet()) return false;
    try {
      const r = await fetch('/ai/ui-state', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return false;
      const st = await r.json();
      if (st.updated) await mergeFull(st);
      SYNC.base = st.updated || 0;
      return true;
    } catch { return false; }
  }

  async function pushState() {
    if (!SYNC.ready || SYNC.busy || !currentScopeUid) return;
    if (genFlagGet() && !gen) {
      // финальная синхронизация завершённого ответа: снимаем лок ДО PUT
      // (сервер пропустил бы запись только с флагом владельца, но точнее
      // отпустить лок явно — другие устройства сразу разморозятся)
      await releaseGenLock();
    }
    SYNC.busy = true;
    try {
      const localRev = await store().currentRev();
      const first = SYNC.base === 0;
      const body = { base: SYNC.base, rev: localRev };
      if (first) {
        // первое сохранение аккаунта: полный снимок (старые клиенты сервера его тоже примут)
        body.sessions = sessions;
        body.messages = {};
        for (const s of sessions) body.messages[s.id] = loadMsgs(s.id).slice(-MAX_STORED);
        // tombstone-реестр и здесь: удалённый чат не должен вернуться из
        // серверного снапшота при merge полного снимка
        body.delta = { deleted_chats: [], _allDeleted: await store().deletedRegistry() };
      } else {
        const d = await store().since(SYNC.base);
        // tombstone-реестр (все удаления за 7 дней): сервер обязан вырезать
        // удалённый чат из СЛИТОГО списка сессий — иначе устаревший снимок
        // другого устройства вернул бы его обратно при merge. Реестр маленький
        // (<200 id), слать его каждый раз дешевле, чем гадать по дельте.
        body.delta = { messages: d.messages, deleted_chats: d.deletedChats, _allDeleted: await store().deletedRegistry() };
        if (d.sessionsChanged) body.delta.sessions = sessions;
        if (Object.keys(d.messages).length === 0 && d.deletedChats.length === 0 && !d.sessionsChanged) {
          SYNC.base = d.rev;               // расходимостей нет — просто поднимаем версию
          return;
        }
      }
      const headers = { 'Content-Type': 'application/json' };
      if (genFlagGet()) headers['X-Client-Gen'] = '1';   // мы ведём генерацию — сервер не заморозит наш PUT
      const r = await fetch('/ai/ui-state', {
        method: 'PUT',
        headers,
        credentials: 'same-origin',
        body: JSON.stringify(body),
      });
      if (r.status === 409) {
        // Другое устройство уже сохранило свою версию: берём её и не перезаписываем
        await pullState(true);
        scheduleSync();
      } else if (r.status === 429) {
        // На другом устройстве идёт генерация — ждём её окончания и тянем статус
        await checkRemoteGen();
      } else if (r.ok) {
        const j = await r.json();
        if (j.conflicts) await resolveConflicts(j.conflicts);
        SYNC.base = j.updated || SYNC.base;
        await store().markSessionsSaved(SYNC.base);
      } else if (r.status === 413) {
        toast('Сервер отклонил синхронизацию: слишком много данных');
      } else if (r.status === 401) {
        location.href = '/login';
      }
    } catch { /* нет связи — повторим при следующем изменении или при возврате сети */ }
    finally { SYNC.busy = false; }
  }

  // Сервер вернул конфликты: позиции/версии наших сообщений не совпали с его
  // историей (другое устройство успел изменить или укоротить тот же чат).
  // Разрешаем детерминированно: эталон — серверная история; наши изменения
  // поверх неё будут отправлены следующей дельтой как обычные патчи.
  async function resolveConflicts(conflicts) {
    let dirty = false;
    for (const c of conflicts) {
      const sid = c && c.sid;
      if (!sid) continue;
      if (sid === currentId || (gen && gen.sessionId === sid)) {
        await reloadFromStore(sid);           // открытый чат: берём серверную версию целиком
        dirty = true;
      } else {
        try {
          const fresh = await store().loadMessages(sid);
          if (fresh.length >= (lastSeen[sid] ? lastSeen[sid].length : 0)) {
            if (cache[sid]) { cache[sid] = fresh.map((x) => x.m).filter(Boolean); lastSeen[sid] = cache[sid]; }
            else lastSeen[sid] = fresh.map((x) => x.m).filter(Boolean);
            delete lastSeenAt[sid];
          }
        } catch { /* оставляем прежний снимок */ }
      }
    }
    if (dirty) { renderMessages({ scroll: false }); refreshUI(); }
  }

  // Заморозка синхронизации, пока генерацию ведёт другое устройство. Периодически
  // проверяем сервер: как только ответ закончился — размораживаемся, подтягиваем
  // состояние и показываем готовый ответ (единый источник истины — сервер).
  let remoteGenTimer = null;
  let remoteGenPaused = false;
  async function checkRemoteGen() {
    if (!currentScopeUid) return;
    try {
      const r = await fetch(API.genStatus, { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const st = await r.json();
      if (st.active && !st.may_write) {
        remoteGenPaused = true;
        clearTimeout(SYNC.timer);                 // наша синхронизация на паузе до конца чужой генерации
        if (!remoteGenTimer) remoteGenTimer = setInterval(checkRemoteGen, 5000);
      } else if (!st.active && remoteGenPaused) {
        remoteGenPaused = false;
        clearInterval(remoteGenTimer); remoteGenTimer = null;
        SYNC.ready = await pullState(true);       // тянем финальный ответ другого устройства
        scheduleSync();
      } else if (st.active && st.may_write) {
        // сервер считает генерацию нашей, а мы её не ведём — осиротевший флаг
        // (например, вкладка закрылась в разгар ответа): снимаем, чтобы не
        // блокировать синхронизацию остальным устройствам бесконечно
        genFlagSet(false);
        await releaseGenLock();
        scheduleSync();
      }
    } catch { /* нет связи — попробуем позже */ }
  }

  async function bootstrap() {
    await store().init(currentScopeUid);
    await store().guardScope();   // в общей IndexedDB не должно быть чужого аккаунта
    await store().migrateLegacy(currentScopeUid);
    try { sessions = await store().getSessions(); } catch { sessions = []; }
    SYNC.ready = await pullState();
    init();
    if (SYNC.ready && (SYNC.base === 0 || sessions.length)) scheduleSync();
  }

  function init() {
    if (!Array.isArray(sessions) || !sessions.length) sessions = readJSON(KEY.sessions, []);
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
