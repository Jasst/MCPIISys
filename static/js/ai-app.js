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

  // Пул подсказок: много вариантов, на экране каждый раз показывается
  // случайная выборка (см. pickSuggestions).
  const SUGGESTION_POOL = [
    // Объясни простыми словами
    ['Объясни простыми словами', 'Как работает память ассистента и что он запоминает?'],
    ['Объясни простыми словами', 'Что такое нейросеть и как её обучают?'],
    ['Объясни простыми словами', 'Чем облако отличается от локального компьютера?'],
    ['Объясни простыми словами', 'Как устроен интернет — от ввода адреса до загрузки страницы?'],
    ['Объясни простыми словами', 'Что такое API и зачем оно разработчику?'],
    ['Объясни простыми словами', 'Как работают пароли и почему их нельзя хранить в открытом виде?'],
    ['Объясни простыми словами', 'Что такое промпт и как его составлять эффективно?'],
    ['Объясни простыми словами', 'Чем Git помогает при работе над проектом?'],
    // Составь план
    ['Составь план', 'Помоги составить план на неделю с фокусом на один большой проект.'],
    ['Составь план', 'Распиши план запуска продукта на месяц: от идеи до первых пользователей.'],
    ['Составь план', 'Составь план изучения новой профессии с нуля за 3 месяца.'],
    ['Составь план', 'Спланируй переезд в другой город, чтобы ничего не забыть.'],
    ['Составь план', 'Составь план подготовки к важной презентации за одну неделю.'],
    ['Составь план', 'Разработай план семейного бюджета на полгода с накоплениями.'],
    ['Составь план', 'Составь план ремонта квартиры поэтапно, с чего начать?'],
    // Поищи в интернете
    ['Поищи в интернете', 'Какие сейчас главные новости в мире искусственного интеллекта?'],
    ['Поищи в интернете', 'Найди последние обзоры новых смартфонов этого года.'],
    ['Поищи в интернете', 'Узнай, какие конференции по разработке пройдут в этом сезоне.'],
    ['Поищи в интернете', 'Найди актуальные курсы по аналитике данных для новичков.'],
    ['Поищи в интернете', 'Собери информацию о лучших коворкингах в моём городе.'],
    ['Поищи в интернете', 'Найди свежие исследования о влиянии сна на продуктивность.'],
    ['Поищи в интернете', 'Узнай прогноз трендов на рынке фриланса в этом году.'],
    // Напиши текст
    ['Напиши текст', 'Напиши короткий пост для канала о запуске нового проекта.'],
    ['Напиши текст', 'Напиши дружелюбное письмо клиенту о переносе сроков.'],
    ['Напиши текст', 'Составь продающее описание для товара на маркетплейсе.'],
    ['Напиши текст', 'Напиши текст приглашения на встречу команды в неформальном стиле.'],
    ['Напиши текст', 'Подготовь короткое резюме проекта для руководства.'],
    ['Напиши текст', 'Напиши поздравление с днём рождения коллеге, без пафоса.'],
    ['Напиши текст', 'Сочини три варианта слогана для кофейни у дома.'],
    // Идея и brainstorm
    ['Подкинь идею', 'Придумай пять идей для побочного проекта на выходные.'],
    ['Подкинь идею', 'Что приготовить на ужин из курицы, риса и овощей?'],
    ['Подкинь идею', 'Придумай оригинальные подарки для коллеги на Новый год.'],
    ['Подкинь идею', 'Назови способы разгрузить голову после тяжёлой недели.'],
    ['Подкинь идею', 'Придумай тему для блога, которая точно заинтересует друзей.'],
    ['Подкинь идею', 'Как интересно провести выходной в своём городе без больших трат?'],
    // Код и техника
    ['Помоги с кодом', 'Объясни, как ускорить медленный SQL-запрос?'],
    ['Помоги с кодом', 'Напиши функцию на Python для проверки надёжности пароля.'],
    ['Помоги с кодом', 'Как организовать структуру файлов в небольшом веб-проекте?'],
    ['Помоги с кодом', 'Найди типичные ошибки в коде на JavaScript и подскажи, как их исправить.'],
    ['Помоги с кодом', 'Как настроить автоматический бэкап базы данных?'],
    ['Помоги с кодом', 'Объясни разницу между потоками и процессами в программировании.'],
    // Переведи и исправь
    ['Переведи и исправь', 'Переведи деловое письмо на английский, сохранив вежливый тон.'],
    ['Переведи и исправь', 'Проверь текст на ошибки и сделай его читабельнее.'],
    ['Переведи и исправь', 'Сократи этот абзац вдвое, не потеряв смысл.'],
    ['Переведи и исправь', 'Перепиши сложный технический пункт простым языком для клиента.'],
    ['Переведи и исправь', 'Переведи меню ресторана на английский язык.'],
    ['Переведи и исправь', 'Сделай из заметок структурированный конспект встречи.'],
    // Сравни и выбери
    ['Сравни и выбери', 'Что выбрать для учёбы: ноутбук на Windows или MacBook?'],
    ['Сравни и выбери', 'Сравни подписку и покупку программы навсегда — что выгоднее?'],
    ['Сравни и выбери', 'Какой мессенджер безопаснее для рабочих переписок?'],
    ['Сравни и выбери', 'Аренда или покупка квартиры: взвесь плюсы и минусы.'],
    ['Сравни и выбери', 'Сравні два подхода к обучению: курсы против самообразования.'],
    ['Сравни и выбери', 'Какое кресло для работы за компьютером удобнее и долговечнее?'],
  ];

  const SUGGESTIONS_COUNT = 4;

  // Случайная выборка N разных подсказок из пула (Fisher–Yates partial shuffle)
  function pickSuggestions(n = SUGGESTIONS_COUNT) {
    const pool = SUGGESTION_POOL.slice();
    for (let i = 0; i < n && i < pool.length; i++) {
      const j = i + Math.floor(Math.random() * (pool.length - i));
      const t = pool[i]; pool[i] = pool[j]; pool[j] = t;
    }
    return pool.slice(0, Math.min(n, pool.length));
  }

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
    sbPullIndicator: $('#sbPullIndicator'),
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
    pullIndicator: $('#pullIndicator'),
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
  // Источник сообщений чата — IndexedDB. localStorage-зеркало больше не читается
  // (оно неполное и устаревает). Чат считается загруженным (hydrated) только после
  // чтения из хранилища; пока не загружен, в хранилище ничего не пишем — иначе
  // пустой кеш стёр бы историю.
  const hydrated = new Set();
  const hydrating = new Map();
  function hydrateChat(id) {
    if (hydrated.has(id)) return Promise.resolve();
    if (!hydrating.has(id)) {
      hydrating.set(id, (async () => {
        try {
          const arr = await store().loadMessages(id);
          const disk = dedupeByKeys(arr.map((x) => x.m).filter(Boolean));
          const mem = cache[id] || [];          // то, что успели записать до загрузки
          cache[id] = disk.length ? mergeMsgLists(disk, mem).slice(-MAX_STORED) : mem;
          lastSeen[id] = cache[id];
          hydrated.add(id);
          if (currentId === id) renderMessages({ scroll: false });
        } catch (e) {
          console.warn('hydrateChat:', e);      // не помечаем загруженным: запись в этот чат пропустим
        } finally {
          hydrating.delete(id);
        }
      })());
    }
    return hydrating.get(id);
  }
  function loadMsgs(id) {
    if (!(id in cache)) {
      cache[id] = [];
      lastSeenAt[id] = 0;
      hydrateChat(id);
    }
    // dedupeByKeys — единый механизм против дублей при повторном открытии чата.
    // История может прийти из двух источников (IndexedDB и localStorage-зеркало),
    // а после слияний на сервере — содержать одинаковые сообщения на разных
    // позициях. Оставляем первое вхождение каждого устойчивого ключа (id либо
    // role+time+text): «открыл и закрыл диалог — он скопировался» становится
    // невозможным локально, ещё до отправки на сервер.
    cache[id] = dedupeByKeys(cache[id]);
    adoptLive(id);
    reconcile(id);
    return cache[id];
  }
  // Сообщение, которое сейчас пишет стрим (gen.bot), должно быть ОДНИМ объектом
  // с тем, что лежит в cache. Перезагрузки кеша (reloadFromStore, merge, разрешение
  // конфликтов) создают новые объекты: стрим писал бы в «осиротевший» объект, а на
  // экран и в хранилище уходила бы копия без текста — отсюда вечные точки.
  function adoptLive(id) {
    const live = gen && gen.sessionId === id && gen.bot;
    if (!live || !cache[id]) return;
    const arr = cache[id];
    const i = arr.findIndex((m) => m && m.id === live.id);
    if (i >= 0) arr[i] = live;
    else arr.push(live);
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
      hydrated.add(id);
    } catch { /* оставляем прежний кеш */ }
  }

  async function persist(id) {
    if (!hydrated.has(id)) await hydrateChat(id);
    if (!hydrated.has(id)) return;          // история не прочитана — не перезаписываем её
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
    hydrated.add(id);                       // новый чат: на диске ничего нет
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
      UI.attachContext(b, () => [
        { label: 'Открыть', onClick: () => { selectSession(s.id); closeNav(); } },
        { sep: true },
        { label: 'Удалить чат', danger: true, onClick: () => deleteSession(s.id) },
      ]);
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
    if (typeof hljs !== 'undefined') {
      root.querySelectorAll('pre code').forEach((el) => {
        if (!el.dataset.hl) { hljs.highlightElement(el); el.dataset.hl = '1'; }
      });
    }
    // Кнопка «Копировать» на каждом блоке кода: копируется только код, без обёртки
    root.querySelectorAll('pre').forEach((pre) => {
      if (pre.dataset.copyBtn) return;
      pre.dataset.copyBtn = '1';
      const wrap = document.createElement('div');
      wrap.className = 'code-wrap';
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'copy-code-btn';
      btn.textContent = 'Копировать';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const code = pre.querySelector('code');
        copyText((code || pre).textContent || '');
      });
      wrap.appendChild(btn);
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

  // ------------------------------------------------------------------ изображения в чате
  // Скачивание: data-URL — напрямую; ссылки медиатеки — через blob, потому что
  // атрибут download у обычной ссылки браузеры игнорируют для внешних адресов.
  function imageExt(mime) {
    return ({ 'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp', 'image/gif': 'gif' })[mime] || 'png';
  }
  async function downloadImage(url, baseName) {
    let objUrl = null;
    try {
      let href = url, mime = '';
      if (/^data:/i.test(url)) {
        mime = url.slice(5, url.indexOf(';') > 0 ? url.indexOf(';') : url.indexOf(','));
      } else {
        const r = await fetch(url, { credentials: 'same-origin', cache: 'force-cache' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const blob = await r.blob();
        mime = blob.type || '';
        objUrl = URL.createObjectURL(blob);
        href = objUrl;
      }
      const a = document.createElement('a');
      a.href = href;
      a.download = `${baseName || 'image'}.${imageExt(mime)}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      toast('Не удалось сохранить изображение');
    } finally {
      if (objUrl) setTimeout(() => URL.revokeObjectURL(objUrl), 2000);
    }
  }
  // Блок «изображение + кнопка сохранить под ним» (фото пользователя и картинки модели)
  function imageBlock(url, alt, baseName) {
    const wrap = document.createElement('div');
    wrap.className = 'img-block';
    const img = new Image();
    img.className = 'gen zoomable';
    img.src = url;
    img.alt = alt;
    img.loading = 'lazy';
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'img-save';
    save.textContent = '⬇ Сохранить';
    save.addEventListener('click', (e) => { e.stopPropagation(); downloadImage(url, baseName); });
    img.addEventListener('error', () => { save.hidden = true; });
    wrap.append(img, save);
    return { wrap, img, save };
  }

  function paintBubble(bubble, m) {
    bubble.innerHTML = '';

    if (m.role === 'user') {
      if (m.image) {
        const fig = imageBlock(m.image, 'Прикреплённое изображение', 'photo');
        // тяжёлые data URL раньше молча не дорисовывались после перезагрузки
        // (квота хранилища резала запись); теперь это явный, кликабельный статус
        fig.img.addEventListener('error', () => {
          fig.img.style.display = 'none';
          const s = document.createElement('div');
          s.className = 'meta-line';
          s.textContent = '🖼 Фото недоступно локально — загрузите его заново';
          bubble.appendChild(s);
        });
        bubble.appendChild(fig.wrap);
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
      bubble.appendChild(imageBlock(url, 'Изображение', 'image').wrap);
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
    for (const [title, prompt] of pickSuggestions()) {
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
    if (typeof updateModelBusy === 'function') updateModelBusy();
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
  // Признак владельца для финальной синхронизации. genFlag снимается сразу после
  // завершения ответа (чтобы синхронизация открылась всем), а финальный PUT всё
  // равно должен уйти от владельца с X-Client-Gen, и лок снимается только после него.
  let finalCommitOwner = false;

  async function acquireGenLock() {
    if (!currentScopeUid) return true;              // локальный режим без входа — лок не нужен
    // Жёсткий таймаут 1.5 с: проверка лока НЕ должна задерживать отправку.
    // При медленной сети/вешающемся сервере раньше fetch тянул до полного
    // сетевого таймаута, и всё это время нажатие «отправить» выглядело мёртвым.
    // Истекаем по timeout как при «нет связи»: решаем локально, а настоящую
    // параллельную генерацию остановит server-side лок на старте POST /ai/chat.
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    try {
      const r = await fetch(API.genLock, {
        method: 'POST', credentials: 'same-origin', signal: ctrl.signal,
        headers: { 'Content-Type': 'application/json' },
        // ВСЕГДА объявляем себя владельцем: перед POST /ai/chat мы хотим
        // ЗАНЯТЬ серверный лок, чтобы /gen-status для другого устройства
        // возвращал active:true, may_watch:true и оно могло подключиться к
        // живому потоку через attach?watch=1. Раньше здесь стояло
        // `client: !!(gen || genFlagGet())` — на первом сообщении это false,
        // и сервер НЕ захватывал лок (put_state зависел от того, есть ли
        // активная генерация в _active_generations). Симптом: на телефоне
        // печать появлялась только после того, как ПК завершал ответ и
        // присылал финальный PUT.
        body: JSON.stringify({ client: true, cid: CID }),
      });
      if (!r.ok) return true;                       // старый/недоступный сервер — не блокируем отправку
      const j = await r.json();
      if (j.held_by_me) genFlagSet(true);
      return !!j.ok;
    } catch { return true; }                        // нет связи/таймаут — решаем локально (как раньше)
    finally { clearTimeout(timer); }
  }

  async function releaseGenLock(force = false) {
    if (!currentScopeUid || (!force && !genFlagGet())) return;
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
    else if (ev.error) m.error = ev.error === 'generation_busy' ? 'Ответ уже пишется на другом устройстве или в другом чате. Дождитесь окончания и попробуйте снова.' : String(ev.error);
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
      // сервер знает причину (ответ не сохранился): показываем её сразу
      if (ex && ex.failure && (!userText || ex.user === userText)) {
        bot.error = 'Ответ не получен: ' + ex.failure;
      }
      return false;
    } catch {
      return null;
    }
  }

  function isNetworkError(err) {
    // Сетевые ошибки fetch — TypeError с сообщением о сбое сети (у каждого браузера свой текст).
    // Прочие TypeError — это баги в коде, их не маскируем под обрыв связи.
    if (!err || (err.name !== 'TypeError' && err.name !== 'NetworkError')) return false;
    return /fetch|network|load failed/i.test(String(err.message || ''));
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
    if (!bot) { if (gen === g) gen = null; refreshUI(); return; }
    g.bot = bot;
    if (watch) pushNote(bot, '👁 Ответ печатается на другом устройстве');
    // Потоковая синхронизация: владелец генерации регулярно (не чаще раза в
    // STREAM_SYNC_MS) отправляет на сервер недописанный ответ. Без этого шага
    // дельты во время genFlag блокировались и другие устройства видели ответ
    // только после финального commit'а по окончании всей генерации.
    // persistSoon -> persist -> scheduleSync ставит дебаунс-таймер на каждый
    // чанк; этот интервал гарантирует, что пуш уходит ритмично, даже когда
    // токены сыплются непрерывно (таймер иначе вечный перестановка не давала бы
    // sync'у состояться до конца ответа).
    let streamSyncTimer = null;
    if (!watch) {
      streamSyncTimer = setInterval(() => {
        if (gen !== g || g.watch) return;
        if (!genFlagGet()) return;               // генерация завершена — дальше обычный путь
        if (SYNC.busy) return;                   // предыдущий PUT ещё в полёте
        clearTimeout(SYNC.timer);                // не дублируем: один внеочередной пуш
        pushState();
      }, STREAM_SYNC_MS);
    }
    try {
      const res = mode === 'attach'
        ? await fetch(API.attach + '?' + [watch ? 'watch=1' : '', 'key=' + encodeURIComponent(bot.id)].filter(Boolean).join('&'),
            { credentials: 'same-origin', signal: controller.signal })
        : await fetch(API.chat, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body),
            signal: controller.signal,
          });
      if (res.status === 401) { g.authLost = true; location.href = '/login'; return; }
      if (res.status === 409) { bot.error = 'Ответ уже пишется на другом устройстве или в другом чате. Дождитесь окончания и попробуйте снова.'; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);

      // attach replays the whole buffer from the start: start from a clean slate
      // reasoning, уже сохранённый на этом устройстве/сервере, запоминаем: если буфер
      // генерации уже не хранит поток, replay его не вернёт, а текст подтянется с сервера.
      const keepReasoning = bot.reasoning || '';
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
          // поток уже не реплеится: вернём reasoning, который был до сброса
          if (!bot.reasoning && keepReasoning) bot.reasoning = keepReasoning;
          const userMsg = msgs[idx - 1] && msgs[idx - 1].role === 'user' ? msgs[idx - 1].text : null;
          if (watch) {
            // чужая генерация уже завершилась: готовый ответ забираем с сервера
            if (!bot.text && !bot.error) {
              const ok = await recoverFromLast(bot, userMsg);
              if (ok !== true) g.interrupted = true;   // ответа пока нет — повторим позже
            }
          } else if (!bot.text && !bot.error) {
            const ok = await recoverFromLast(bot, userMsg);
            if (ok === null) g.interrupted = true;
            else if (!ok && !bot.error) bot.error = 'Ответ не найден на сервере (возможно, сервер перезапускался во время генерации)';
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
      // потоковая синхронизация больше не нужна: интервал снимаем на любом
      // выходе (успех, ошибка, остановка) — финальный commit делает commit(id)
      if (streamSyncTimer) { clearInterval(streamSyncTimer); streamSyncTimer = null; }
      if (g.authLost) {
        // 401 в разгар генерации: иначе myGen-флаг залипнет и на следующем
        // логине блокировал pullState/scheduleSync до истечения TTL сервера.
        genFlagSet(false);
        releaseGenLock();
        if (gen === g) gen = null;
        refreshUI();
      } else {
        if (bot.rStart && !bot.rEnd) bot.rEnd = Date.now();
        if (g.watch) {
          // Наблюдение окончено (ответ доработал или пропал): локальная запись —
          // временная, pending остаётся. Придёт commit владельца -> pullState/дельта
          // схлопнет это сообщение по id с серверным и очистит pending.
          if (gen === g) gen = null;
          if (!g.interrupted) {
            // ответ получен целиком — завершаем локально; владелец не нужен
            bot.pending = false;
            bot.notes = (bot.notes || []).filter((n) => !String(n).startsWith('👁'));
            persistSoon(id);
          }
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
          finalCommitOwner = true;      // финальный PUT уйдёт от владельца, даже если genFlag уже снят
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
      const idx = loadMsgs(id).findIndex((m) => m.pending);
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
        // Генерации нет и она была не наша. Сначала спрашиваем сервер: финальный
        // ответ сохраняется в history на сервере и не зависит от устройства-владельца.
        const msgs = loadMsgs(id);
        const m = msgs[idx];
        const userMsg = msgs[idx - 1] && msgs[idx - 1].role === 'user' ? msgs[idx - 1].text : null;
        if (m) {
          const ok = await recoverFromLast(m, userMsg);
          if (ok === null) continue;          // нет связи — не объявляем потерю, повторим позже
          if (m.text) {
            m.pending = false;
            await commit(id);
            if (currentId === id) renderMessages({ scroll: false });
          } else if (m.error || Date.now() - (m.time || 0) > 180000) {
            // сервер не знает ответа: причина уже в m.error (от сервера) либо сообщение висит давно
            if (!m.error) m.error = 'Ответ не найден на сервере (возможно, сервер перезапускался во время генерации)';
            m.pending = false;
            await commit(id);
            if (currentId === id) renderMessages({ scroll: false });
          }
        }
        continue;
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
    // Оптимистичная отправка: интерфейс НЕ ждёт сетевой проверки генерационного
    // лока. Раньше каждое нажатие «отправить» сначала блокировалось запросом
    // POST /gen-lock (а при плохой сети — до полного таймаута fetch), и всё
    // это время поле ввода и список сообщений не реагировали — «интерфейс
    // виснет на несколько секунд, сообщение сразу не отправляется». Теперь
    // сообщение мгновенно появляется в чате (sendNow сам берёт текст/вложение
    // и рисует их до любого сетевого раунда), а лок проверяется параллельно;
    // если он занят другим устройством — старт генерации не произойдёт, а
    // сервер разберётся с конфликтом по своим кодам (429/409).
    const text = (override !== undefined ? override : els.input.value).trim();
    if (!text && !attachment) return;
    if (gen) {
      if (gen.sessionId !== currentId) {
        toast('Дождитесь окончания ответа в другом чате');
        updateHint();
      }
      return;
    }

    let started = false;
    const go = () => { if (!started) { started = true; sendNow(override); } };
    if (!currentScopeUid) { go(); return; }   // локальный режим без входа — лок не нужен
    acquireGenLock().then((ok) => {
      if (!ok) {
        toast('Дождитесь окончания ответа на другом устройстве');
        updateHint();
        return;
      }
      go();
    });
    // Страховка от «зависшей кнопки»: если проверка лока не отвечает дольше
    // 1.2 с (медленная сеть), запускаем отправку сразу — server-side лок
    // по-прежнему защищает от двух параллельных генераций одного аккаунта.
    setTimeout(go, 1200);
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
    scheduleSync(true);   // форс-пул начального состояния ДО genFlag в startGeneration
    renderMessages();
    refreshUI();

    const body = {
      message: text || 'Опиши, что изображено',
      stream: true,
      model: currentModel(),
      web_search: chips.web,
      reasoning: chips.reason,
      gen_key: msgs[idx].id,
      chat_id: id, user_msg_id: userMsg.id || null,
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
    const userRef = msgs[u];
    const userKey = userRef.id || null;   // ищем вопрос по id: индекс может сдвинуться за время ожидания
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
    const goRegen = () => {
      if (gen) { toast('Дождитесь окончания ответа'); return; }   // не допускать второй параллельный стрим
      // Массив сообщений мог смениться за время загрузки картинки и лока (полный pull
      // пересоздаёт кеш): работаем с актуальным массивом и находим вопрос заново.
      const cur = loadMsgs(id);
      const uu = userKey
        ? cur.findIndex((m) => m && m.id === userKey)
        : cur.findIndex((m) => m === userRef);
      if (uu < 0) { toast('Не удалось найти вопрос для повтора'); return; }
      // старый ответ сохраняем: если новый не появится (409, ошибка, стоп без текста) — вернём его
      const snapshot = cur.slice(uu + 1).map((m) => Object.assign({}, m));
      cur.splice(uu + 1);
      cur.push(newBotMessage());
      const nidx = cur.length - 1;
      const nid = cur[nidx].id;
      commit(id);
      scheduleSync(true);   // форс-пул начального состояния ДО genFlag в startGeneration
      renderMessages({ scroll: false });
      refreshUI();
      const body = { message: prompt, stream: true, model: currentModel(), web_search: chips.web, reasoning: chips.reason,
                     gen_key: nid, chat_id: id, user_msg_id: userKey };
      if (regenImg && regenImg.base64) { body.image_base64 = regenImg.base64; body.image_mime = regenImg.mime; }
      startGeneration(id, nidx, 'send', body).then(() => {
        const now = loadMsgs(id);
        const at = now.findIndex((m) => m && m.id === nid);
        if (at < 0) return;
        const b = now[at];
        if (b.text) return;                     // ответ получен (или «пустой ответ») — оставляем новый
        now.splice(at, 1, ...snapshot);         // нового ответа нет — возвращаем прежний
        if (b.error) toast(b.error);
        commit(id);
        if (currentId === id) renderMessages({ scroll: false });
        refreshUI();
      });
    };
    // Как и send(), regenerate обязан занять серверный генерационный лок:
    // /gen-status читает _active_generations (ui_state.py), а не _active_stream
    // (ai_assistant.py). Без POST /gen-lock устройство B, увидев pending через
    // дельту, получит active:false / may_watch:false и не сможет подключиться
    // к живому стриму — печатать ответ оно не будет, дождётся лишь финального
    // коммита. Страховка по таймауту — как в send(): проверка лока не должна
    // задерживать старт дольше 1.2 с (настоящий конфликт остановит server-side
    // лок на POST /ai/chat кодом 409/429).
    // Exactly-once-гард, как в send(): без него и .then(), и setTimeout() ниже
    // успевали вызвать goRegen() (на быстрой сети лок приходит раньше 1.2 с),
    // что давало второй splice/push/startGeneration поверх уже идущего стрима.
    let started = false;
    const start = () => { if (started) return; started = true; goRegen(); };
    if (!currentScopeUid) { start(); return; }
    acquireGenLock().then((ok) => {
      if (!ok) { started = true; toast('Дождитесь окончания ответа на другом устройстве'); updateHint(); return; }
      start();
    });
    setTimeout(start, 1200);
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

  async function clearCurrent() {
    const s = currentSession();
    if (!s || isGenHere()) return;
    if (!(await UI.confirm('Очистить историю этого чата?', { title: 'Очистить историю', okText: 'Очистить', danger: true }))) return;
    cache[s.id] = [];
    commit(s.id);
    renderMessages({ scroll: false });
    refreshUI();
    toast('История очищена');
  }

  // Удаление любого чата из списка (кнопка или правый клик); чат, который сейчас пишет ответ, не трогаем.
  async function deleteSession(id) {
    const s = sessions.find((x) => x.id === id);
    if (!s || (gen && gen.sessionId === id)) return;
    const ok = await UI.confirm(`Удалить чат «${s.name}»?`, { title: 'Удалить чат', okText: 'Удалить', danger: true });
    if (!ok) return;
    removeSession(id);
    if (currentId === id) {
      const next = sessions.slice().sort((a, b) => (b.updated || b.created) - (a.updated || a.created))[0];
      currentId = next ? next.id : null;
    }
    renderMessages({ scroll: false });
    refreshUI();
    toast('Чат удалён');
  }

  function deleteCurrent() {
    const s = currentSession();
    if (!s || isGenHere()) return;
    deleteSession(s.id);
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
    if (!(await UI.confirm('Удалить все чаты в этом браузере? Это нельзя отменить.', { title: 'Удалить все чаты', okText: 'Удалить все', danger: true }))) return;
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

  // Pull-to-refresh: потянуть диалог вниз — мгновенное обновление чатов с
  // сервера (не дожидаясь тика pollRemoteState). Работает на touch-устройствах;
  // на десктопе тот же эффект даёт focus/visibilitychange -> resumePending.
  initPullToRefresh();

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
  const lb = { root: null, img: null, cap: null, counter: null, prev: null, next: null, save: null,
               urls: [], idx: -1, scale: 1, tx: 0, ty: 0, drag: null,
               pointers: new Map(), pinch: null, lastTap: 0, lastPointer: 'mouse' };

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
    const save = document.createElement('button');
    save.type = 'button';
    save.className = 'lb-btn lb-save';
    save.textContent = '⬇ Сохранить';
    save.addEventListener('click', () => downloadImage(lb.urls[lb.idx], 'image'));
    lb.save = save;
    lb.root.append(close, prev, next, lb.img, lb.cap, lb.counter, save);
    document.body.appendChild(lb.root);

    // клик по тёмному фону (не по картинке/кнопкам) закрывает
    lb.root.addEventListener('click', (e) => {
      if (e.target === lb.root) closeLightbox();
    });
    lb.root.addEventListener('wheel', (e) => {
      e.preventDefault();
      zoomBy(Math.exp(-e.deltaY * 0.0015));
    }, { passive: false });
    // Жесты: мышь — перетаскивание и колесо; касание — панорама одним пальцем,
    // щипок двумя пальцами для масштаба, двойной тап — приблизить / сбросить.
    lb.root.addEventListener('pointerdown', (e) => {
      if (e.target !== lb.img) return;
      lb.lastPointer = e.pointerType;
      lb.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      try { lb.img.setPointerCapture(e.pointerId); } catch { /* нет поддержки — не критично */ }
      if (lb.pointers.size === 2) {
        const [a, b] = [...lb.pointers.values()];
        lb.pinch = { dist: Math.hypot(a.x - b.x, a.y - b.y), scale: lb.scale };
        lb.drag = null;
      } else if (lb.pointers.size === 1) {
        lb.drag = { x: e.clientX, y: e.clientY, tx: lb.tx, ty: lb.ty };
        lb.root.classList.add('grabbing');
      }
      if (e.pointerType === 'touch' && lb.pointers.size === 1) {
        const now = Date.now();
        if (now - lb.lastTap < 300) { lb.lastTap = 0; toggleLbZoom(); }
        else lb.lastTap = now;
      }
    });
    lb.root.addEventListener('pointermove', (e) => {
      if (!lb.pointers.has(e.pointerId)) return;
      lb.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (lb.pinch && lb.pointers.size >= 2) {
        const [a, b] = [...lb.pointers.values()];
        const d = Math.hypot(a.x - b.x, a.y - b.y);
        lb.scale = Math.min(8, Math.max(1, lb.pinch.scale * d / (lb.pinch.dist || 1)));
        if (lb.scale <= 1.01) { lb.scale = 1; lb.tx = 0; lb.ty = 0; }
        applyLbTransform();
        return;
      }
      if (!lb.drag || lb.scale <= 1.01) return;     // панорама — только при увеличении
      lb.tx = lb.drag.tx + (e.clientX - lb.drag.x);
      lb.ty = lb.drag.ty + (e.clientY - lb.drag.y);
      applyLbTransform();
    });
    const endPointer = (e) => {
      lb.pointers.delete(e.pointerId);
      if (lb.pointers.size < 2) lb.pinch = null;
      if (lb.pointers.size === 0) {
        lb.drag = null;
        lb.root.classList.remove('grabbing');
      } else if (lb.pointers.size === 1) {
        const p = [...lb.pointers.values()][0];       // второй палец убрали — продолжаем панораму
        lb.drag = { x: p.x, y: p.y, tx: lb.tx, ty: lb.ty };
      }
    };
    lb.root.addEventListener('pointerup', endPointer);
    lb.root.addEventListener('pointercancel', endPointer);
    // мышь: двойной клик. На касаниях двойной тап уже обработан в pointerdown,
    // а браузер присылает и dblclick — второе переключение отменяло бы первое.
    lb.img.addEventListener('dblclick', () => { if (lb.lastPointer !== 'touch') toggleLbZoom(); });
  }

  function toggleLbZoom() {
    if (lb.scale > 1.01) { lb.scale = 1; lb.tx = 0; lb.ty = 0; }
    else lb.scale = 2;
    applyLbTransform();
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

  // Показывает уже выданный код (не выпускает новый, поэтому не отключает другие устройства).
  async function loadMcpCode() {
    const out = $('#mcpCodeOut');
    try {
      const r = await fetch('/ai/mcp_code', { method: 'GET', credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      out.textContent = j.code || 'Кода ещё нет — нажмите «Выпустить новый код».';
    } catch (e) {
      out.textContent = 'Не удалось загрузить код: ' + e.message;
    }
  }

  els.openSettings.addEventListener('click', () => {
    els.dialog.showModal();
    loadMcpCode();
  });

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

  // Выпуск нового кода: старый код и все MCP-сессии отключаются — просим подтверждение.
  $('#mcpCodeBtn').addEventListener('click', async () => {
    if (!(await UI.confirm('Выпустить новый код? Старый код перестанет работать, все подключённые внешние ИИ отключатся.', { title: 'Новый код подключения', okText: 'Выпустить', danger: true }))) return;
    try {
      const r = await fetch('/ai/mcp_code', { method: 'POST', credentials: 'same-origin' });
      if (r.status === 401) { location.href = '/login'; return; }
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const j = await r.json();
      $('#mcpCodeOut').textContent = j.code;
      toast('Новый код выдан. Прежние подключения отключены.');
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
  const SYNC = { base: 0, ready: false, timer: null, busy: false, etag: '', etagMisses: 0 };

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

  // Интервал потоковой синхронизации во время генерации: владелец periodically
  // отправляет на сервер недописанный ответ (pending-чанки), чтобы соседние
  // устройства видели печать в реальном времени, а не дожидались финального
  // commit'а. 2000 мс — компромисс между плавностью «стрима» и нагрузкой.
  const STREAM_SYNC_MS = 2000;

  function scheduleSync(force) {
    if (!SYNC.ready || !currentScopeUid) return;
    // Потоковая синхронизация: пока это устройство ведёт генерацию, дельты с
    // частичным текстом ответа УХОДЯТ НА СЕРВЕР регулярно (каждые STREAM_SYNC_MS,
    // см. streamSyncTimer в startGeneration). Раньше при genFlag любой плановый
    // sync блокировался и на сервер попадал только начальный снапшот (по force)
    // да финальный commit после [DONE]: первое сообщение синхронизировалось
    // сразу, а все последующие — лишь после полной генерации.
    // Защита от «каши» при этом сохраняется:
    //  - PUT владельца идёт с заголовком X-Client-Gen, сервер его принимает,
    //    остальные устройства в это время получают 429 и ждут;
    //  - наблюдатели не пишут в своё хранилище промежуточный текст (watch-ветка
    //    в startGeneration), их собственные pending-черновики срезаются
    //    stripForeignPending перед PUT;
    //  - серверный merge идемпотентен по устойчивым id сообщений, повторная
    //    доставка того же чанка не создаёт дублей.
    // force — немедленный пул начального состояния (вопрос + pending-бот) ДО
    // startGeneration: иначе второе устройство не увидит state-changed по SSE
    // и не сможет подхватить поток через gen-status/attach.
    if (force) {
      clearTimeout(SYNC.timer);
      SYNC.timer = setTimeout(pushState, 0);   // немедленный форс-пул начального снапшота
      return;
    }
    // Во время генерации плановые sync'и идут чаще обычного дебаунса (1500 мс):
    // persist() дергается каждые PERSIST_DELAY_MS, но таймер переставляется на
    // каждый чанк, поэтому реально пуш уходит по нижнему лимиту — STREAM_SYNC_MS
    // (см. streamSyncTimer в startGeneration). Вне генерации — обычный дебаунс.
    clearTimeout(SYNC.timer);
    SYNC.timer = setTimeout(pushState, genFlagGet() ? Math.min(1500, STREAM_SYNC_MS) : 1500);
  }

  // Слияние полного состояния (первый вход/409): серверные версии приоритетнее
  // для тех же id, локальные чаты, которых на сервере нет, сохраняются и уйдут
  // на сервер следующей дельтой.
  async function mergeFull(st, opts = {}) {
    // quiet-режим (фоновый опрос): если данные побайтово не изменились —
    // ничего не перерисовываем. Это главный источник «дёрганья» чата:
    // раньше каждый pullState безусловно гнал commitMessages по всем чатам,
    // сбрасывал кеш и перестраивал весь DOM, хотя сервер мог просто поднять
    // версию updated без реальных изменений.
    if (opts.quiet && !stateChanged(st)) return false;
    lastStateJson = JSON.stringify(st);
    // tombstone-реестр сервера: чаты, удалённые на другом устройстве, не
    // должны пережить merge — иначе они «воскресали» при первом же pullState/409.
    // Сервер отдаёт deleted_chats как [{sid, at}, ...]. Раньше Set собирался из
    // объектов, а проверялось dels.has(s.id) — строки в множестве объектов. Всегда
    // false: чаты, удалённые на другом устройстве, не фильтровались и «воскресали».
    const dels = new Set(
        (Array.isArray(st.deleted_chats) ? st.deleted_chats : [])
            .map(d => (typeof d === 'string' ? d : d && d.sid))
            .filter(Boolean)
    );
    const byId = new Map();
    for (const s of st.sessions || []) if (!dels.has(s.id)) byId.set(s.id, s);
    for (const s of sessions) if (!byId.has(s.id) && !dels.has(s.id)) byId.set(s.id, s);
    sessions = [...byId.values()];
    for (const id of dels) {
      delete cache[id]; delete lastSeen[id]; hydrated.delete(id);
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
      // quiet: чат не менялся побайтово — не трогаем ни IndexedDB, ни кеш
      if (opts.quiet && merged.length === local.length &&
          JSON.stringify(merged) === JSON.stringify(local)) continue;
      await store().commitMessages(s.id, merged, null);
      if (cache[s.id]) { cache[s.id] = merged; lastSeen[s.id] = merged; }
    }
    saveSessions();
    // Раньше при quiet сброс cache не делался совсем. Теперь принудительно
    // перечитываем currentId из хранилища (удаляем его кеш-строку), чтобы
    // фоновый merge, изменивший открытый диалог, гарантированно попал на
    // экран — иначе пользователь видел «ничего не изменилось». Остальной
    // cache при quiet не трогаем: перерисовка всё равно только для currentId.
    if (!opts.quiet) { cache = {}; hydrated.clear(); }
    else if (currentId) { delete cache[currentId]; hydrated.delete(currentId); }
    await reloadDirtySafe();
    if (currentId) renderMessages({ scroll: false });
    refreshUI();
    return true;
  }

  // Побайтовая сигнатура значимых данных снапшота (без поля updated — оно
  // меняется даже когда содержимое идентично). Равные строки = равное состояние.
  let lastStateJson = '';
  function stateSig(st) {
    return JSON.stringify([st.sessions || [], st.messages || {},
      (st.deleted_chats || []).map((d) => (typeof d === 'string' ? d : d.sid))]);
  }
  function stateChanged(st) {
    try { return stateSig(st) !== stateSig(JSON.parse(lastStateJson)); }
    catch { return true; }   // первой сверки нет — считаем, что изменилось
  }

  // Клиентская партия серверного _merge_msgs: совпадения по ключу схлопаются,
  // новые сообщения вставляются в свою позицию, порядок сохраняется.
  // Общее правило слияния одного сообщения (зеркало _merge_msg_fields на сервере):
  // пустые text/reasoning не затирают известные; pending:false не возвращается в pending.
  function mergeMsgFields(dst, item) {
    const out = Object.assign({}, item);
    if (dst.text && !item.text) out.text = dst.text;
    if (dst.reasoning && !item.reasoning) out.reasoning = dst.reasoning;
    if (dst.pending === false && item.pending) out.pending = false;
    return out;
  }
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
        // тот же объект — патч полями по общему правилу (см. mergeMsgFields)
        const dst = result[keyToIdx.get(k)];
        Object.assign(dst, mergeMsgFields(dst, item));
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
      if (Array.isArray(list) && list.length && list.every((x) => x && typeof x === 'object' && 'i' in x)) {
        // Дельта PUT: [{i, m}] — точечные вставки/патчи по позициям.
        if (sid === currentId || (gen && gen.sessionId === sid)) {
          await reloadFromStore(sid);   // живой кеш актуализируем из хранилища
          if (sid === currentId) touchedCurrent = true;
        } else {
          const cur = await store().loadMessages(sid).then((arr) => arr.map((x) => x.m));
          const next = cur.slice();
          // КРИТИЧНО: серверный _apply_delta трактует it.i как ПОЗИЦИЮ С 1
          // (см. `if not isinstance(i, int) or i < 1: continue` и hist[i-1]).
          // Раньше здесь it.i использовался как 0-based индекс массива:
          // splice(it.i,1) срезал НЕ то сообщение, a next[it.i]=m при i==len
          // перезаписывало последнее/уходило в дыру — чужие точечные правки
          // портили локальную историю («сообщения путались/пропадали»).
          for (const it of list) {
            if (it.i == null || it.i < 1) continue;
            const pos = it.i - 1;                          // 1-based -> 0-based
            if (it.m === null || it.m === undefined) {
              if (pos < next.length) next[pos] = null;     // tombstone
            } else if (pos < next.length) {
              next[pos] = it.m;                            // патч существующего
            } else {
              while (next.length < pos) next.push(null);   // вставка в хвост
              next.push(it.m);
            }
          }
          await store().commitMessages(sid, next.filter(Boolean), null);
          delete lastSeen[sid];
        }
      } else if (Array.isArray(list)) {
        // Снимок чата из GET /ui-state/delta: серверная история уже смержена,
        // остаётся слить её с локальной по ключам — идемпотентно, без дублей.
        let local = [];
        try { local = (await store().loadMessages(sid)).map((x) => x.m).filter(Boolean); } catch { /* нет копии */ }
        const merged = mergeMsgLists(local, list.slice(-MAX_STORED));
        // «Дёрганье» убирается здесь же: если после слияния побайтово ничего не
        // изменилось (чат прислан перестраховочно), не пишем IndexedDB и не
        // трогаем кеш/DOM открытого диалога.
        if (merged.length === local.length && JSON.stringify(merged) === JSON.stringify(local)) continue;
        await store().commitMessages(sid, merged, null);
        if (cache[sid]) { cache[sid] = merged; lastSeen[sid] = merged; }
        if (sid === currentId || (gen && gen.sessionId === sid)) touchedCurrent = true;
      }
    }
    if (Array.isArray(delta.sessions)) {
      const dels = new Set(Array.isArray(delta.deleted_chats) ? delta.deleted_chats : []);
      const byId = new Map();
      for (const s of delta.sessions) if (s && !dels.has(s.id)) byId.set(s.id, s);
      for (const s of sessions) if (!byId.has(s.id) && !dels.has(s.id)) byId.set(s.id, s);
      const next = [...byId.values()];
      // состав и порядок не изменились — боковую панель не перерисовываем
      const same = next.length === sessions.length && next.every((s, i) => s === sessions[i]);
      sessions = next;
      if (!same) { saveSessions(); refreshUI(); }
    }
    if (touchedCurrent) renderMessages({ scroll: false });
  }

  async function pullState(force, opts = {}) {
    // во время генерации локальные сообщения трогает ТОЛЬКО владелец генерации
    // (он же их пишет): ему нужен свежий base для финального PUT после ответа.
    // Остальные устройства ждут — иначе mergeFull затёр бы живой ответ.
    if (!currentScopeUid) return false;
    if (genFlagGet() && !force) return false;
    if (gen && !genFlagGet()) return false;
    // opts.timeoutMs — жест «потянуть»: не даём спиннеру висеть на зависшем запросе
    const ac = opts.timeoutMs ? new AbortController() : null;
    const killTimer = ac ? setTimeout(() => ac.abort(), opts.timeoutMs) : null;
    try {
      const r = await fetch('/ai/ui-state', {
        credentials: 'same-origin', cache: 'no-store',
        headers: SYNC.etag ? { 'If-None-Match': SYNC.etag } : {},
        signal: ac ? ac.signal : undefined,
      });
      if (r.status === 304) {                 // снапшот не менялся — ничего не делаем
        SYNC.etagMisses = 0;                  // сервер поддерживает ETag: канал рабочий
        return true;
      }
      if (!r.ok) return false;
      const etag = r.headers.get('etag');
      if (etag) { SYNC.etag = etag; SYNC.etagMisses = 0; }
      else if (opts.quiet) {                  // ETag не поддерживается и это фоновый опрос:
        SYNC.etagMisses = (SYNC.etagMisses || 0) + 1;
        if (SYNC.etagMisses >= 3) {           // три тика подряд без ETag — сервер старый,
          stopRemotePoll();                   // перестаём гонять мегабайтные снимки каждые
          return true;                        // POLL_MS; мгновенные события SSE живы
        }
      }
      const st = await r.json();
      if (st.updated) await mergeFull(st, { quiet: !!opts.quiet });
      // SYNC.base — СЕРВЕРНАЯ версия. Присваиваем максимум, а не замену:
      // если параллельный PUT уже поднял base выше, чем вернул этот GET
      // (гонка чтения/записи), откат базы назад ломает delta-канал —
      // следующий since=<старый> не покрывается журналом -> need_full.
      SYNC.base = Math.max(st.updated || 0, SYNC.base || 0);
      return true;
    } catch { return false; }
    finally { if (killTimer) clearTimeout(killTimer); }
  }

  // Дельта-канал: GET /ui-state/delta?since=base отдаёт только изменённые чаты.
  // Применяется точечно (как дельта от PUT), поэтому DOM открытого диалога не
  // перестраивается целиком — именно это и убирало периодическое «дёрганье».
  async function pullDelta() {
    if (!currentScopeUid || !SYNC.ready || SYNC.busy) return null;
    try {
      const r = await fetch('/ai/ui-state/delta?since=' + (SYNC.base || 0),
        { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return null;
      const d = await r.json();
      if (d.need_full) return 'full';         // базы нет — нужен полный снимок
      if (d.changed) await applyServerDelta(d);
      if (d.updated) SYNC.base = d.updated;
      return true;
    } catch { return null; }
  }

  async function pushState() {
    if (!SYNC.ready || SYNC.busy || !currentScopeUid) return;
    // Раньше releaseGenLock() вызывался ДО PUT: genFlagSet(false) внутри
    // снимал флаг, и заголовок X-Client-Gen ниже уже не выставлялся.
    // PUT уходил на сервер без признака владельца, и при живой чужой
    // генерации (или после гонки при снятии лока) сервер отвечал 429,
    // вместо того чтобы принять финальный commit завершённого ответа.
    // Теперь лок отпускается ПОСЛЕ успешного PUT (в finally), а во время
    // запроса флаг остаётся взведённым — заголовок владельца уходит вместе
    // с финальным снапшотом.
    const wasOwner = (genFlagGet() || finalCommitOwner) && !gen;
    SYNC.busy = true;
    try {
      const localRev = await store().currentRev();
      const first = SYNC.base === 0;
      const body = { base: SYNC.base, rev: localRev };
      if (first) {
        // первое сохранение аккаунта: ТОЛЬКО полный снимок, без delta.
        // С пустой delta-заглушкой {deleted_chats: [], _allDeleted: []} сервер
        // (routes/ui_state.py -> put_state) классифицировал PUT как no-op-дельту
        // и молча отбрасывал весь снапшот: sessions/messages на диск не писались,
        // SYNC.base оставался нулевым, и чаты этого устройства никогда не
        // появлялись на других. Пустая delta здесь не нужна: на пустом сервере
        // воскрешать нечего, tombstone-ы не с чем сверять.
        body.sessions = sessions;
        body.messages = {};
        for (const s of sessions) body.messages[s.id] = loadMsgs(s.id).slice(-MAX_STORED);
      } else {
        const sentRev = SYNC.pushedRev ?? await store().getPushedRev();
        const d = await store().since(sentRev);
        // Явная защита от «чужого pending»: наблюдатель (у него нет genFlag)
        // не должен отправлять на сервер черновик pending-сообщения, которое
        // пишет другое устройство, — серверный merge по id схлопнул бы его с
        // эталонной записью владельца, и незавершённый фрагмент перезатёр бы
        // финальный ответ. Раньше stripForeignPending была объявлена, но не
        // вызывалась; теперь она подключена здесь. ВАЖНО: очистка выполняется
        // ПОСЛЕ проверки «пустая дельта» ниже — иначе удалённые записи сделали
        // бы messages пустым, PUT молча пропустился бы как no-op, а реальные
        // удаления никогда бы не ушли на сервер («чат удалился только тут»).
        // tombstone-реестр (все удаления за 7 дней): сервер обязан вырезать
        // удалённый чат из СЛИТОГО списка сессий — иначе устаревший снимок
        // другого устройства вернул бы его обратно при merge. Реестр маленький
        // (<200 id), слать его каждый раз дешевле, чем гадать по дельте.
        body.delta = { messages: d.messages, deleted_chats: d.deletedChats, _allDeleted: await store().deletedRegistry() };
        if (d.sessionsChanged) body.delta.sessions = sessions;
        if (Object.keys(d.messages).length === 0 && d.deletedChats.length === 0 && !d.sessionsChanged) {
          // Расходимостей нет. Раньше здесь SYNC.base присваивался локальный
          // rev (Date.now()), и следующий pollRemoteState сравнивал серверный
          // updated с этим ЛОКАЛЬНЫМ значением — при отставании серверных
          // часов от клиентских условие `server.updated <= SYNC.base` было
          // истинным, и опрос навсегда переставал тянуть свежие данные.
          // SYNC.base всегда должен оставаться СЕРВЕРНОЙ версией; пустую
          // дельту всё равно фиксируем как отправленную (маркер pushed_rev):
          // выходим раньше, поэтому делаем это здесь.
          SYNC.pushedRev = localRev;
          try { await store().markPushed(localRev); } catch { /* ignore */ }
          finalCommitOwner = false;    // отправлять нечего
          return;
        }
        // Чужие pending-черновики на сервер не отправляем (см. комментарий
        // выше); очистка — после проверки no-op, чтобы tombstone-удаления
        // не потерялись. Если после очистки дельта стала пустой, PUT всё
        // равно уходит: он фиксирует server-side применения предыдущих версий
        // и содержит полный tombstone-реестр (_allDeleted) — сервер отвечает
        // своей текущей версией, и SYNC.base синхронизируется корректно.
        for (const sid of Object.keys(d.messages)) {
          const cleaned = await stripForeignPending(sid, d.messages[sid]);
          if (cleaned === null) delete d.messages[sid];
          else d.messages[sid] = cleaned;
        }
      }
      const headers = { 'Content-Type': 'application/json' };
      if (genFlagGet() || finalCommitOwner) headers['X-Client-Gen'] = '1';   // мы ведём/завершаем генерацию — сервер не заморозит наш PUT
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
        finalCommitOwner = false;      // сервер принял версию, включая финальный ответ
        // КРИТИЧНО: сервер на no-op-дельте (пустые messages/deleted_chats при
        // фоновом тике) НЕ двигает updated и отвечает {'updated': <текущий>}.
        // Присвоение SYNC.base = j.updated могло откатить базу НАЗАД, если наш
        // base уже был поднят более ранним PUT'ом этой же версии — после чего
        // следующий delta-запрос with since=<старый base> не покрывался
        // журналом и давал need_full -> лишний полный снимок на каждом тике.
        // Поэтому берём максимум, а не замену. Конфликты разрешаем ВСЕГДА,
        // даже когда версия не выросла: они означают, что сервер отклонил
        // часть наших записей (позиции не совпали) — молча оставлять локальную
        // историю расходящейся нельзя, иначе соседние устройства никогда не
        // увидят эти сообщения («чат есть на ПК, пустой на телефоне»).
        const newBase = Math.max(j.updated || 0, SYNC.base || 0);
        if (j.conflicts && j.conflicts.length) await resolveConflicts(j.conflicts);
        if (newBase !== SYNC.base) SYNC.base = newBase;
        // маркер отправки двигаем только если сервер принял всё без конфликтов:
        // отклонённые позиции должны уйти повторно, а не потеряться
        if (!(j.conflicts && j.conflicts.length)) {
          SYNC.pushedRev = localRev;
          await store().markPushed(localRev);
        }
      } else if (r.status === 413) {
        toast('Сервер отклонил синхронизацию: слишком много данных');
      } else if (r.status === 401) {
        location.href = '/login';
      }
    } catch { /* нет связи — повторим при следующем изменении или при возврате сети */ }
    finally {
      SYNC.busy = false;
      // Финальный PUT завершённого ответа отправлен (или не удался) — только
      // теперь отпускаем лок генерации, чтобы другие устройства разморозились.
      // лок снимаем только после того, как финальная версия принята сервером
      if (wasOwner && !finalCommitOwner) await releaseGenLock(true);
    }
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
  let remoteGenGuard = null;   // аварийный выход из паузы, если статус-канал врёт
  async function checkRemoteGen() {
    if (!currentScopeUid) return;
    try {
      const r = await fetch(API.genStatus + '?cid=' + encodeURIComponent(CID), { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const st = await r.json();
      if (st.active && !st.may_write) {
        remoteGenPaused = true;
        clearTimeout(SYNC.timer);                 // наша синхронизация на паузе до конца чужой генерации
        if (!remoteGenTimer) remoteGenTimer = setInterval(checkRemoteGen, 5000);
        // Страховка: если сервер возвращает active:true, may_write:false,
        // may_watch:false (задача «жива», но ни писать, ни смотреть нельзя —
        // например, зависший лок на другом конце), эта ветка повторялась бы
        // вечно. Раз в 30 с всё равно проверяем версию состояния и
        // размораживаемся, если снапшот стал новее нашей базы.
        if (!remoteGenGuard) remoteGenGuard = setTimeout(async () => {
          remoteGenGuard = null;
          try {
            const rv = await fetch('/ai/ui-state/version', { credentials: 'same-origin', cache: 'no-store' });
            if (rv.ok) {
              const v = await rv.json();
              if ((v.updated || 0) > (SYNC.base || 0)) {
                remoteGenPaused = false;
                clearInterval(remoteGenTimer); remoteGenTimer = null;
                SYNC.ready = await pullState(true);
                scheduleSync();
              }
            }
          } catch { /* нет связи — следующий интервал checkRemoteGen повторит */ }
        }, 30000);
      } else if (!st.active && remoteGenPaused) {
        remoteGenPaused = false;
        clearTimeout(remoteGenGuard); remoteGenGuard = null;
        clearInterval(remoteGenTimer); remoteGenTimer = null;
        SYNC.ready = await pullState(true);       // тянем финальный ответ другого устройства
        scheduleSync();
      } else if (st.active && st.may_write) {
        // сервер считает генерацию нашей, а мы её не ведём — осиротевший флаг
        // (например, вкладка закрылась в разгар ответа): снимаем, чтобы не
        // блокировать синхронизацию остальным устройствам бесконечно.
        // ВАЖНО: не снимать genFlagSet(false) заранее — releaseGenLock
        // проверяет genFlagGet() и при снятом флаге уйдёт в ранний return,
        // DELETE на сервер не уйдёт и лок будет висеть до TTL. Флаг снимает
        // сам releaseGenLock.
        await releaseGenLock();
        scheduleSync();
      }
    } catch { /* нет связи — попробуем позже */ }
  }

  async function bootstrap() {
    await store().init(currentScopeUid);
    await store().guardScope();   // в общей IndexedDB не должно быть чужого аккаунта
    await store().migrateLegacy(currentScopeUid);

    // Восстановление от залипшего myGen-флага. Флаг ставится в localStorage
    // при старте генерации и снимается при нормальном завершении. Если
    // вкладку закрыли посреди ответа (или процесс убит), флаг остаётся '1',
    // и он блокирует pullState/scheduleSync — второе устройство навсегда
    // остаётся без синхронизации. Спрашиваем сервер, ведёт ли он вообще
    // активную генерацию: если нет — флаг наш, осиротевший, снимаем.
    if (genFlagGet()) {
      try {
        const r = await fetch(API.genStatus + '?cid=' + encodeURIComponent(CID),
                              { credentials: 'same-origin', cache: 'no-store' });
        if (r.ok) {
          const st = await r.json();
          if (!st.active) {
            // сервер ничего не пишет — наш флаг не подкреплён живой задачей
            genFlagSet(false);
            try {
              await fetch(API.genLock, {
                method: 'DELETE', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ client: true, cid: CID }),
              });
            } catch { /* TTL на сервере снимет лок сам */ }
          }
          // st.active && st.mine: генерация ЖИВА на сервере и наша. Флаг и лок
          // НЕ трогаем: раньше здесь их снимали при каждой перезагрузке во время
          // ответа — локальное сообщение переставало считаться своим, уходило
          // в tombstone и «терялось» («генерация потеряна»). resumePending сам
          // подхватит поток через attach (mine=true, replay).
        }
      } catch { /* нет связи — решаем консервативно, флаг оставляем */ }
    }

    try { sessions = await store().getSessions(); } catch { sessions = []; }
    // открытый чат читаем из IndexedDB до первой отрисовки (а не из localStorage-зеркала)
    const firstChat = sessions.slice().sort((a, b) => (b.updated || b.created) - (a.updated || a.created))[0];
    if (firstChat) await hydrateChat(firstChat.id);
    SYNC.ready = await pullState();
    init();
    startRemotePoll();
    startStateEvents();   // мгновенное получение чужих сохранений (откат к poll, если канал недоступен)
    if (SYNC.ready && (SYNC.base === 0 || sessions.length)) scheduleSync();
  }

  // Живая синхронизация между устройствами без перезагрузки страницы.
  // Раньше новое устройство получало чаты только при старте (pullState в
  // bootstrap) — пока не перезагрузишь страницу, список пустой. Теперь раз в
  // POLL_MS сравниваем дешёвую версию состояния (/ui-state/version) со своей
  // SYNC.base: если сервер стал новее (другое устройство что-то сохранило) —
  // тянем полное состояние и мержим. Во время НАШЕЙ генерации пропускаем цикл
  // (мы и так источник изменений), при ошибке сети — молчим, повторим позже.
  const POLL_MS = 15000;
  let pollTimer = null;
  function startRemotePoll() {
    if (pollTimer) return;
    pollTimer = setInterval(pollRemoteState, POLL_MS);
  }
  function stopRemotePoll() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }
  async function pollRemoteState() {
    if (!currentScopeUid || !SYNC.ready || SYNC.busy) return;
    if (genFlagGet() || remoteGenPaused) return;   // своя генерация / пауза до конца чужой
    if (document.hidden) return;                   // фоновая вкладка не дёргает сеть и DOM
    // Сначала дешёвая сверка версии: если сервер не новее — на этом цикл кончается
    // (именно так убирается «дёрганье» каждые несколько секунд при простое).
    try {
      const r = await fetch('/ai/ui-state/version', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const v = await r.json();
      if ((v.updated || 0) <= (SYNC.base || 0)) return;
      // Сервер новее: пробуем дельту (только изменённые чаты, точечное применение).
      const res = await pullDelta();
      if (res === 'full' || res === null) await pullState(true, { quiet: true });
      // Тот же смысл, что и в SSE-подписчике: после sync проверяем, не
      // появилось ли pending-сообщение (чужая генерация) — если да, цепляемся
      // к живому потоку через attach?watch=1. Без этого клиент узнавал о
      // чужой генерации только по focus/visibilitychange.
      scheduleResume(200);
    } catch { /* нет связи — следующий тик */ }
  }

  // Принудительное обновление «по требованию» (pull-to-refresh, кнопка в шапке):
  // не ждём сравнения версий — тянем состояние целиком. Возвращает true, если
  // сервер оказался новее (что-то реально подтянулось).
  async function refreshFromServer() {
    if (!currentScopeUid) { toast('Нет активного аккаунта'); return false; }
    if (SYNC.busy) { toast('Синхронизация уже выполняется…'); return false; }
    if (genFlagGet()) { await scheduleSync(); return false; }  // своя генерация: свой же поток и так виден
    const prevBase = SYNC.base;
    const ok = await pullState(true, { timeoutMs: 8000 });
    if (!ok) { toast('Не удалось обновить: нет связи с сервером'); return false; }
    // Сообщение, которое пришло с сервера как pending (точки), разрешаем сразу:
    // resumePending возьмёт готовый ответ с сервера или подключится к живой генерации.
    // Не ждём его — спиннер уже можно убрать.
    resumePending();
    if (SYNC.base > prevBase) { toast('Чаты обновлены'); return true; }
    toast('Обновлений нет'); return false;
  }

  // ------------------------------------------------------------------ pull-to-refresh
  // На мобильных браузеры отдают touchstart/touchmove/touchend ПЕРЕД native
  // scroll-событиями, поэтому слушатели навешаны прямо на контейнер сообщений
  // с { passive: false }. Тянем вниз только когда прокрутка уже в самом верху
  // (scrollTop <= 0) и нет активной ГЕНЕРАЦИИ В ЭТОМ ЧАТЕ (иначе сдвинули бы
  // живой поток). Пороги: START_PX — после какого натяжения считаем жест
  // «обновлением»; TRIGGER_PX — при каком смещении отпускание запускает refresh.
  // Общий жест «потянуть вниз, чтобы обновить» для любого прокручиваемого списка.
  // box       — прокручиваемый контейнер (жест стартует только у его верха);
  // ind       — индикатор с .pi-spin и .pi-text;
  // shiftEl   — элемент, который сдвигается за пальцем (или null);
  // canStart  — дополнительное условие старта (например, нет генерации в этом чате);
  // onRefresh — что сделать при отпускании после порога; texts — подписи индикатора.
  function bindPullToRefresh(opts) {
    if (!('ontouchstart' in window) && navigator.maxTouchPoints <= 0) return; // десктоп — не нужен
    const { box, ind, shiftEl = null, canStart = () => true, onRefresh, texts } = opts;
    if (!box || !ind) return;
    const MAX_PX = 90, START_PX = 8, TRIGGER_PX = 56;
    let startY = null, pulling = false, dist = 0, refreshing = false;

    const setShift = (px) => {
      if (shiftEl) shiftEl.style.transform = px ? `translateY(${px}px)` : '';
      ind.classList.toggle('visible', px > 4);
      const pct = Math.min(1, px / TRIGGER_PX);
      ind.querySelector('.pi-spin').style.transform = `rotate(${Math.round(pct * 360)}deg)`;
      ind.querySelector('.pi-text').textContent =
        refreshing ? 'Обновляем…' : (px >= TRIGGER_PX ? texts.ready : texts.idle);
    };
    const settle = () => {
      if (shiftEl) shiftEl.style.transition = 'transform .2s ease';
      setShift(refreshing ? 34 : 0);
      if (!refreshing && shiftEl) setTimeout(() => { shiftEl.style.transition = ''; }, 220);
    };

    box.addEventListener('touchstart', (e) => {
      if (refreshing || e.touches.length !== 1) return;
      if (box.scrollTop > 0) return;                    // не в самом верху — обычный скролл
      if (!canStart()) return;
      startY = e.touches[0].clientY; pulling = false; dist = 0;
    }, { passive: true });

    box.addEventListener('touchmove', (e) => {
      if (startY == null) return;
      const dy = e.touches[0].clientY - startY;
      if (dy <= 0) {                                    // пошёл вверх — отменяем жест
        if (pulling) { pulling = false; setShift(0); }
        return;
      }
      if (box.scrollTop > 0) { startY = null; if (pulling) { pulling = false; setShift(0); } return; }
      if (!pulling) {
        if (dy < START_PX) return;                      // ещё не жест
        pulling = true;
      }
      e.preventDefault();                               // забираем скролл у браузера
      dist = Math.min(MAX_PX, dy * 0.5);                // сопротивление резинки
      setShift(dist);
    }, { passive: false });

    const endGesture = async () => {
      if (startY == null) return;
      const wasPulling = pulling, wasTrigger = dist >= TRIGGER_PX;
      startY = null; pulling = false; dist = 0;
      if (!wasPulling) return;
      if (wasTrigger) {
        refreshing = true;
        ind.classList.add('refreshing');
        setShift(34);
        try { await onRefresh(); } finally {
          refreshing = false;
          ind.classList.remove('refreshing');
          settle();
        }
      } else {
        settle();
      }
    };
    box.addEventListener('touchend', endGesture, { passive: true });
    box.addEventListener('touchcancel', endGesture, { passive: true });
  }

  // Два списка: сообщения текущего чата (сдвигается весь блок) и список диалогов
  // в боковой панели (индикатор над списком, сам список не сдвигается).
  function initPullToRefresh() {
    bindPullToRefresh({
      box: els.messages, ind: els.pullIndicator, shiftEl: els.inner,
      canStart: () => !(gen && gen.sessionId === currentId),   // активная генерация здесь — не мешаем
      onRefresh: refreshFromServer,
      texts: { idle: 'Потяните вниз, чтобы обновить чаты', ready: 'Отпустите — обновим чаты' },
    });
    bindPullToRefresh({
      box: els.list, ind: els.sbPullIndicator, shiftEl: null,
      canStart: () => true,
      onRefresh: refreshFromServer,
      texts: { idle: 'Потяните вниз, чтобы обновить список', ready: 'Отпустите — обновим список' },
    });
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

  // Когда ПК (или любое другое устройство) сохраняет состояние, сервер может
  // прислать событие SSE. Подписываемся один раз: на «state-changed» мгновенно
  // тянем свежий снимок — чаты появляются без перезагрузки страницы и без
  // ожидания тика поллинга. Если сервер не поддерживает этот канал или обрыв —
  // тихий откат к штатному опросу версии (startRemotePoll).
  let stateES = null;
  function startStateEvents() {
    if (stateES || !currentScopeUid) return;
    try { stateES = new EventSource('/ai/ui-state/events', { withCredentials: true }); }
    catch { return; }
    stateES.addEventListener('state-changed', async () => {
      if (genFlagGet() || SYNC.busy) return;   // своя запись / идёт синхронизация
      const res = await pullDelta();            // только изменённое; без перерисовки всего
      if (res === 'full' || res === null) await pullState(true, { quiet: true });
      // После sync в локальной истории мог появиться чат с сообщением
      // pending:true — это значит, что другое устройство прямо сейчас пишет
      // ответ. Раньше клиент узнавал об этом только по focus/visibilitychange
      // или по таймеру после своего же attach — то есть на практике НИКОГДА,
      // пока пользователь сам не переключит вкладку. Теперь после каждого
      // подтягивания состояния мы сами проверяем /gen-status и, если на
      // сервере идёт чужая генерация (may_watch), подключаемся к её
      // живому потоку через attach?watch=1 и показываем печать в реальном
      // времени.
      scheduleResume(200);
    });
    stateES.onerror = () => { /* браузер сам переподключится; если нет — работает poll */ };
  }

  // ---- выбор модели LM Studio ----
  const MODEL_KEY = 'chatModel';
  let modelList = [];        // последний ответ /ai/models: {models, loaded, default}
  function currentModel() {
    const sel = $('#modelSelect');
    return sel && sel.value ? sel.value : null;
  }
  // Видимость контрола и подпись кнопки — теперь это кнопка-триггер, а не select:
  // системный dropdown рисуется ОС и не совпадает со стилем приложения.
  function setModelControlVisible(v) {
    const btn = $('#modelSelectBtn');
    if (btn) btn.hidden = !v;
    const sel = $('#modelSelect');
    if (sel) sel.hidden = !v;
  }
  function updateModelLabel() {
    const name = $('#modelName');
    if (!name) return;
    const cur = currentModel();
    name.textContent = cur || 'Модель…';
    const btn = $('#modelSelectBtn');
    if (btn) btn.title = cur ? ('Модель: ' + cur + ' — нажмите, чтобы сменить') : 'Выбрать модель';
    // точка «модель реально в VRAM» на кнопке в шапке — тот же маркер, что и
    // в выпадающем списке (.model-dd-dot); если LM Studio сообщает, что в
    // памяти висит другая модель, кнопка честно показывает это, а не только галочку «выбрано»
    if (btn) btn.classList.toggle('loaded', !!(cur && modelList.loaded === cur));
    // ЧЕСТНЫЙ СТАТУС: когда выбрана одна модель, а в памяти держится ДРУГАЯ
    // (её грузят в фоне / переключили вручную / другой вкладок сменил веса),
    // подпись под именем показывает реальное положение дел. Раньше UI молча
    // подсвечивал модель из VRAM и создавал ощущение «выбор не применяется».
    if (btn && cur) {
      const other = modelList.loaded && modelList.loaded !== cur ? modelList.loaded : null;
      const st = $('#modelStatus');
      if (st) {
        if (other) { st.hidden = false; st.textContent = 'в памяти: ' + shortModelName(other, 26); }
        else { st.hidden = true; st.textContent = ''; }
      }
    }
  }
  // Селектор НИКОГДА не блокируется через disabled (браузер закрашивает его серым,
  // и он выглядит «мёртвым»). Вместо этого вешаем класс .busy с подсказкой:
  // выбор остаётся возможным — сервер сам ответит 409 и поставит модель в очередь.
  function updateModelBusy() {
    const ctrl = $('#modelSelectBtn') || $('#modelSelect');
    if (!ctrl) return;
    // ВАЖНО: «занято» показывает ТОЛЬКО наша активная печать (gen).
    // genFlagGet() — это localStorage-флаг владения локом, который снимается
    // не сразу по окончании ответа, а только после финального commit/pushState.
    const busy = !!gen;
    if (busy) {
      ctrl.classList.add('busy');
      ctrl.title = 'Идёт генерация: смена модели применится автоматически после окончания ответа';
    } else {
      ctrl.classList.remove('busy');   // явное снятие: обычное состояние контрола
      updateModelLabel();              // возвращаем обычный title с именем модели
    }
  }
  async function loadModels() {
    try {
      const r = await fetch('/ai/models', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) return;
      const data = await r.json();
      const models = Array.isArray(data.models) ? data.models : [];
      if (!models.length) return;
      modelList = data;
      const sel = $('#modelSelect');
      let saved = localStorage.getItem(MODEL_KEY);
      // САМО-ВЫЛЕЧИВАНИЕ «МЕЛКОГО БАГА»: если в сохранённом выборе лежит не id
      // модели (класс/подпись «current», «в памяти» и т.п.), а модель с таким
      // id реально стоит в VRAM — чиним хранилище на месте: берём реальный id.
      // Иначе такой мусор уходил в /models/select и LM Studio ругалась.
      if (saved && !models.includes(saved)) {
        if (data.loaded && ((saved === 'current') || (/памят/i.test(saved)))) {
          saved = data.loaded;
          localStorage.setItem(MODEL_KEY, saved);
        }
      }
      // КЛЮЧЕВОЕ ПРАВИЛО UI: интерфейс выбирает ту модель, которая СЕЙЧАС В
      // ПАМЯТИ (data.loaded) — она реально готова к работе. Но «выбранная» ≠
      // «в памяти»: если пользователь явно выбрал ДРУГУЮ модель (сервер
      // подтверждает это через data.desired — намерение последнего выбора),
      // НЕ перебиваем его выбор моделью из VRAM, иначе клик по X мгновенно
      // «возвращал» подсветку на Y и казалось, что выбор не применяется.
      // При неизвестном состоянии loaded — прежний приоритет saved > default > первый.
      const explicitChoice = data.desired && models.includes(data.desired)
                             && (!data.loaded || data.desired !== data.loaded);
      const pick = (explicitChoice ? data.desired : null)
                 || (models.includes(data.loaded) ? data.loaded
                 : (models.includes(saved) ? saved
                 : (models.includes(data.default) ? data.default : models[0])));
      if (sel) {
        sel.innerHTML = '';
        for (const id of models) sel.appendChild(new Option(id, id));
        sel.value = pick;
      }
      if (pick !== saved) localStorage.setItem(MODEL_KEY, pick);
      setModelControlVisible(true);
      updateModelLabel();
      // сохранённая модель исчезла из LM Studio (удалена/выключена) — предупреждаем
      if (saved && !models.includes(saved)) toast('Сохранённая модель недоступна в LM Studio, выбран другой вариант');
      // После перезагрузки страницы: сверка идёт ТОЛЬКО если сервер знает и про
      // выбранную, и про загруженную модель, и они различаются. Когда loaded
      // неизвестен (пусто) — ничего не отправляем: фронтенд не должен вслепую
      // дёргать LM Studio и получать «загрузится автоматически, как только
      // освободится» для модели, которая уже работает.
      verifyLoadedModel(pick, data.loaded);
    } catch { /* LM Studio недоступна: контрол остаётся скрытым */ }
  }
  let startupCheckInFlight = false;
  async function verifyLoadedModel(pick, loaded) {
    if (!pick || startupCheckInFlight) return;
    // Модель уже в памяти — ничего не делаем; состояние неизвестно — тоже
    // (не отправляем запрос «вслепую», чтобы не словить ложный queued-toast).
    if (!loaded || loaded === pick) return;
    startupCheckInFlight = true;
    try {
      // check_only:true — сервер проверит VRAM и, если модели там нет, поставит
      // её на фоновую дозарядку в момент покоя, НЕ выгружая то, что сейчас
      // используется другой генерацией. Тяжёлый unload+load при каждой
      // перезагрузке страницы больше не запускается вслепую.
      const r = await fetch('/ai/models/select', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: pick, check_only: true }),
      });
      if (!r.ok) return;
      const data = await r.json().catch(() => ({}));
      if (data.already_loaded) {                  // гонка: модель успели загрузить
        modelList = Object.assign({}, modelList, { loaded: pick });
        updateModelLabel();
      } else if (data.queued) {
        // ЧЕСТНЫЙ ТЕКСТ: раньше здесь всегда писалось «загрузится
        // автоматически, как только LM Studio освободится» — даже когда LM
        // Studio полностью свободна и просто держит в памяти ДРУГУЮ модель
        // (её пользователь мог переключить вручную или в другом окне).
        // Формулировка зависит от реального состояния: идёт печать — ждём
        // освобождения; печати нет — веса перезаряжаются прямо сейчас.
        const busyNow = !!(gen || genFlagGet());
        toast(busyNow
          ? `В памяти ${shortModelName(loaded)}. ${shortModelName(pick)} загрузится автоматически, как только генерация закончится.`
          : `В памяти ${shortModelName(loaded)}. ${shortModelName(pick)} загружается сейчас — это займёт несколько секунд.`);
        // Подтянем статус не одним таймером, а короткими шагами: тяжёлая
        // модель может грузиться минуту, и пользователь должен увидеть, как
        // метка «в памяти» переезжает на выбранную модель, а не ждать 8 секунд
        // с устаревшим состоянием.
        pollUntilLoaded(pick);
      }
    } catch { /* сервер недоступен — попробуем при следующем открытии списка */ }
    finally { startupCheckInFlight = false; }
  }
  let vramPollTimer = null;
  function pollUntilLoaded(id, deadlineMs = 120000) {
    if (vramPollTimer) clearTimeout(vramPollTimer);
    const t0 = Date.now();
    const step = async () => {
      vramPollTimer = null;
      if (modelList.loaded === id) return;               // цель достигнута
      if (Date.now() - t0 > deadlineMs) return;          // дальше решает сервер
      await loadModels();                                 // обновим метку «в VRAM»
      if (modelList.loaded !== id) vramPollTimer = setTimeout(step, 4000);
    };
    vramPollTimer = setTimeout(step, 3500);
  }
  // ---- выпадающий список моделей в стиле приложения (.model-dd) ----
  // Окно живёт во временном layer-контейнере на body: position:fixed внутри
  // шапки с backdrop-filter давал бы «двойную» подложку и чужие тени.
  let ddEl = null, ddLayer = null, ddKeyLis = null, ddScrollLis = null,
      ddWheelLis = null, ddTouchLis = null, ddReposLis = null;
  function closeModelDd() {
    if (!ddEl) return;
    const gone = ddEl;
    ddEl = null;
    const goneLayer = ddLayer;
    if (ddLayer) { ddLayer.remove(); ddLayer = null; }
    if (ddKeyLis) {
      // слушатель вешался на само окно и терялся вместе с ним: после этого
      // стрелки/Enter переставали работать даже при открытии списка заново
      document.removeEventListener('keydown', ddKeyLis);
      ddKeyLis = null;
    }
    // wheel/touchmove вешаются на слой-обёртку с passive:false — снимаем их
    // явно, иначе после нескольких открытий накопятся дубли обработчиков
    if (goneLayer) {
      if (ddWheelLis) goneLayer.removeEventListener('wheel', ddWheelLis);
      if (ddTouchLis) goneLayer.removeEventListener('touchmove', ddTouchLis);
    }
    ddWheelLis = null; ddTouchLis = null;
    void gone;   // окно удаляется вместе со слоем; явного снятия не требует
    // scroll-слушатель окна больше не вешается: скролл списка НЕ должен его
    // закрывать (см. openModelDd/ddReposLis); переменная обнуляется для верности
    ddScrollLis = null;
    // слушатель скролла страницы/чата, перепозиционирующий окно — тоже снимаем
    // явно: window-слушатели не «собираются» сами и после закрытия окна начали
    // бы дублироваться при каждом новом открытии
    if (ddReposLis) {
      window.removeEventListener('scroll', ddReposLis, true);
      document.removeEventListener('scroll', ddReposLis, true);
      ddReposLis = null;
    }
    const btn = $('#modelSelectBtn');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }
  // Закрытие по клику вне окна — через прозрачный полноэкранный «щит» слоя, а
  // не глобальным слушателем на document. Глобальный pointerdown в capture-фазе
  // убивал выбор: он срабатывал РАНЬШЕ click'а, закрывал окно и снимал слой с
  // документа → обработчик пункта уже не вызывался («список сломался, выбор не
  // работает»). Щит лежит под окном (порядок детей в слое), поэтому тапы мимо
  // закрывают список, а тапы по пунктам доживают до click.
  function bindDdDismiss(d) {
    d.addEventListener('click', (e) => {
      // клик по окну, но мимо пункта (заголовок, отступы, пустая область) — закрыть
      if (!e.target.closest('.model-dd-item')) closeModelDd();
    });
  }
  window.addEventListener('resize', closeModelDd);
  // имя модели в LM Studio длинное ("qwen2.5-coder-32b-instruct-q4_k_m"):
  // режем середину для показа, полное имя всегда остаётся в title/атрибутах
  function shortModelName(id, max = 34) {
    if (!id || id.length <= max) return id || '';
    const half = Math.floor((max - 1) / 2);
    return id.slice(0, half) + '…' + id.slice(id.length - half);
  }
  function positionModelDd() {
    if (!ddEl) return;
    const btn = $('#modelSelectBtn');
    if (!btn) return;
    // Сначала сбрасываем зафиксированные размеры: без этого повторный вызов
    // (после дозарядки списка, смены темы или поворота экрана) измерял бы
    // scrollWidth пунктов внутри уже заданной ширины и список «залипал» в
    // прежнем размере, а пункты с длинными именами обрезались и не скроллились.
    ddEl.style.width = '';
    ddEl.style.height = '';           // сброс «урезанной» высоты после поворота экрана
    ddEl.style.display = '';          // и flex-режима, выставленного позиционером
    ddEl.style.flexDirection = '';
    const list = ddEl.querySelector('.model-dd-list');
    if (list) { list.style.maxHeight = ''; list.style.flex = ''; }
    // Ширина: берём РЕАЛЬНУЮ ширину содержимого (getBoundingClientRect у flex-
    // контейнера с min-width:0 корректно возвращает полную ширину текста, в отличие
    // от scrollWidth, который при overflow:hidden возвращал ширину видимой части).
    // Старый расчёт по scrollWidth+PAD занижал ширину → имена обрезались, а из-за
    // переполнения возникал «мёртвый» горизонтальный скролл внутри списка.
    const PAD = 30;   // паддинги пункта (10+10) + окна (4+4) + полоса прокрутки (~8)
    let natural = 0;
    ddEl.querySelectorAll('.model-dd-item').forEach((it) => {
      const name = it.querySelector('.model-dd-name');
      const meta = it.querySelector('.model-dd-meta');
      const inner = (name ? name.getBoundingClientRect().width : 0) +
                    (meta ? meta.getBoundingClientRect().width : 0);
      natural = Math.max(natural, inner + PAD);
    });
    const w = Math.min(Math.max(natural, btn.offsetWidth), window.innerWidth - 16);
    ddEl.style.width = w + 'px';
    // высота списка — по доступному месту вокруг кнопки, иначе при большом
    // числе моделей окно упиралось за экран и часть списка была недостижима.
    // Считаем её НЕ от середины экрана, а от реальной позиции окна (выше/ниже
    // кнопки) — иначе на низких экранах max-height выходил больше фактической
    // «урезанной» высоты окна и нижняя часть списка уходила за вьюпорт:
    // вертикальная прокрутка как бы «не доходила» до последних пунктов.
    const r = btn.getBoundingClientRect();
    const below = r.bottom + 6;
    const spaceBelow = window.innerHeight - below - 8;
    const spaceAbove = r.top - 6 - 8;
    const preferAbove = spaceBelow < 200 && spaceAbove > spaceBelow;
    const avail = Math.max(120, Math.max(spaceBelow, spaceAbove));
    if (list) list.style.maxHeight = Math.min(360, avail) + 'px';
    const dr = ddEl.getBoundingClientRect();
    ddEl.style.left = Math.max(8, Math.min(r.left, window.innerWidth - dr.width - 8)) + 'px';
    // Если окно ниже кнопки не влезает целиком — поднимаем его над кнопкой,
    // но НЕ выше верхней границы экрана: иначе верх списка (и заголовок)
    // уходил за вьюпорт и часть пунктов становилась недостижима ни кликом,
    // ни прокруткой («список сломался»). Вместо подъёма урезаем высоту
    // самого окна, чтобы оно всегда помещалось под кнопкой.
    if (preferAbove && dr.height <= spaceAbove) {
      ddEl.style.top = (r.top - dr.height - 6) + 'px';
    } else if (below + dr.height > window.innerHeight - 8) {
      const fit = Math.max(140, spaceBelow);
      if (dr.height > fit) {
        ddEl.style.height = fit + 'px';
        ddEl.style.display = 'flex';
        ddEl.style.flexDirection = 'column';
        if (list) { list.style.flex = '1 1 auto'; list.style.minHeight = '0'; }
      }
      ddEl.style.top = below + 'px';
    } else {
      ddEl.style.top = below + 'px';
    }
  }
  function openModelDd() {
    const btn = $('#modelSelectBtn');
    if (!btn || !modelList.models || !modelList.models.length) return;
    if (ddEl) { closeModelDd(); return; }        // повторный клик — закрыть
    ddLayer = document.createElement('div');
    ddLayer.style.position = 'fixed';            // дочерние .model-dd позиционируются от вьюпорта
    ddLayer.style.inset = '0';
    ddLayer.style.pointerEvents = 'none';        // сам слой кликов не ловит — только щит и окно
    ddLayer.style.zIndex = '10002';
    // прозрачный полноэкранный «щит» под окном: тап мимо списка закрывает его,
    // не перехватывая события пунктов (см. комментарий к bindDdDismiss)
    const shield = document.createElement('div');
    shield.style.position = 'absolute';
    shield.style.inset = '0';
    shield.style.pointerEvents = 'auto';         // единственный кликабельный фон слоя
    shield.addEventListener('pointerdown', closeModelDd);
    ddLayer.appendChild(shield);
    const d = document.createElement('div');
    d.className = 'model-dd';
    d.setAttribute('role', 'listbox');
    d.setAttribute('aria-label', 'Модели LM Studio');
    const head = document.createElement('div');
    head.className = 'model-dd-head';
    head.textContent = 'Модель LM Studio';
    d.appendChild(head);
    const list = document.createElement('div');
    list.className = 'model-dd-list';
    const cur = currentModel();
    for (const id of modelList.models) {
      const loaded = modelList.loaded && id === modelList.loaded;
      const b = document.createElement('button');
      b.type = 'button';
      b.dataset.model = id;                       // реальный id модели — единственный источник для API
      b.className = 'model-dd-item' + (id === cur ? ' current' : '');
      b.setAttribute('role', 'option');
      b.setAttribute('aria-selected', String(id === cur));
      const nm = document.createElement('span');
      nm.className = 'model-dd-name';
      nm.textContent = shortModelName(id);
      nm.title = id;                              // полное имя — при наведении
      b.appendChild(nm);
      const meta = document.createElement('span');
      meta.className = 'model-dd-meta';
      if (loaded) {
        const dot = document.createElement('span');
        dot.className = 'model-dd-dot';
        dot.title = 'Модель сейчас в памяти (VRAM)';
        meta.appendChild(dot);
        const badge = document.createElement('span');
        badge.className = 'model-dd-badge';
        badge.textContent = 'в памяти';
        meta.appendChild(badge);
      }
      if (id === cur) {
        const chk = document.createElement('span');
        chk.className = 'model-dd-check';
        chk.textContent = '✓';
        chk.title = 'Выбранная модель';
        meta.appendChild(chk);
      }
      if (meta.childNodes.length) b.appendChild(meta);
      b.addEventListener('click', (e) => {
        // stopPropagation — чтобы обработчик закрытия «по клику мимо пункта»
        // на окне не отработал вторым и не закрыл список раньше времени
        e.stopPropagation();
        closeModelDd();
        if (id === cur) {
          // Клик по МОДЕЛИ, КОТОРАЯ УЖЕ В ПАМЯТИ: это не смена модели.
          // Раньше такой клик всё равно слал /models/select, сервер отвечал
          // «уже в памяти / загрузится автоматически», и пользователь видел
          // ложный toast про ожидание LM Studio. Просто подтверждаем выбор.
          localStorage.setItem(MODEL_KEY, id);
          updateModelLabel();
          if (modelList.loaded === id) toast('Модель уже в памяти — готова к работе', 2600);
          return;
        }
        onModelPicked(id);
      });
      list.appendChild(b);
    }
    const sep = document.createElement('div');
    sep.className = 'model-dd-sep';
    list.appendChild(sep);
    const upd = document.createElement('button');
    upd.type = 'button';
    upd.className = 'model-dd-item muted';
    upd.textContent = '↻ Обновить список';
    upd.addEventListener('click', async (e) => {
      e.stopPropagation();
      closeModelDd();
      await loadModels();
      toast('Список моделей обновлён');
    });
    list.appendChild(upd);
    d.appendChild(list);
    ddLayer.appendChild(d);
    document.body.appendChild(ddLayer);
    ddEl = d;
    btn.setAttribute('aria-expanded', 'true');
    bindDdDismiss(d);
    positionModelDd();
    // Прокрутка колесом/тапом: блокируем дефолт только для событий, пришедших
    // НЕ из прокручиваемого списка (заголовок, отступы окна) — иначе чат под
    // выпадашкой начинал скроллиться «сквозь» неё. Внутри .model-dd-list
    // default не отменяем: браузер сам скроллит список.
    // Слушатели вешаем на СЛОЙ (а не на окно): прозрачный щит лежит внутри слоя
    // и перекрывает весь экран — раньше wheel/touchmove над щитом вообще не
    // обрабатывались, и колесо крутило чат «сквозь» открытое меню.
    const nonListScroll = (e) => {
      if (!ddEl) return;
      if (!(e.target && e.target.closest && e.target.closest('.model-dd-list'))) {
        e.preventDefault();
      }
    };
    ddWheelLis = nonListScroll; ddTouchLis = nonListScroll;
    ddLayer.addEventListener('wheel', nonListScroll, { passive: false });
    ddLayer.addEventListener('touchmove', nonListScroll, { passive: false });
    const first = list.querySelector('.model-dd-item.current') || list.querySelector('.model-dd-item');
    if (first) first.focus({ preventScroll: true });
    // Клавиатура: Esc — закрыть, стрелки — переход по пунктам (как в .ui-menu).
    // Слушатель вешаем на document (а не на окно) и снимаем в closeModelDd():
    // вложенные span'ы имени/бейджа не видны closest('button'), если слушать
    // только на кнопках, а слушатель на удалённом из DOM окне терялся навсегда.
    ddKeyLis = (e) => {
      if (!ddEl) return;
      // Enter/Пробел на самой кнопке-триггере: не перехватываем — иначе клик по
      // кнопке открывал список и тут же «выбирал» первый пункт
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('#modelSelectBtn')) return;
      if (e.key === 'Escape') { e.preventDefault(); closeModelDd(); btn.focus(); return; }
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        // только пункты-кнопки с реальным id модели: разделитель/статус-строка
        // не должны участвовать в навигации и «выборе»
        const items = Array.from(ddEl.querySelectorAll('.model-dd-item[data-model]'));
        if (!items.length) return;
        const active = e.target.closest ? e.target.closest('.model-dd-item') : null;
        let i = items.indexOf(active);
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          (active || items[0]).click();
          return;
        }
        e.preventDefault();
        if (i < 0) i = e.key === 'ArrowUp' ? items.length - 1 : 0;
        else i = (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
        items[i].focus({ preventScroll: true });
        keepItemVisible(items[i]);
        return;
      }
      // Tab уводит фокус из списка — закрываем, как системные меню
      if (e.key === 'Tab') closeModelDd();
    };
    document.addEventListener('keydown', ddKeyLis);
    // Скролл страницы/чата при открытом списке: раньше любое scroll-событие
    // закрывал окно: прокрутка чата под выпадашкой мгновенно её захлопывала.
    // Теперь окно остаётся открытым и перепозиционируется вслед за кнопкой.
    // ВАЖНО: скролл самого списка (.model-dd-list) игнорируем — иначе
    // перепозиционирование сбрасывало бы его scrollTop (окно «прыгало» на
    // первый пункт, а нижняя часть списка становилась недостижимой).
    ddReposLis = (e) => {
      if (!ddEl) return;
      const t = e && e.target;
      if (t && t.closest && t.closest('.model-dd')) return;   // внутренний скролл окна
      positionModelDd();
    };
    window.addEventListener('scroll', ddReposLis, true);
  }
  // Доводимый до видимости пункт при стрелочной навигации: focus() с
  // preventScroll сам ничего не прокручивает, а без этого последний пункт
  // можно было «потерять» за нижней границей списка. Прокручиваем именно
  // контейнер списка (а не window.scrollIntoView, который мог прокрутить и
  // страницу под меню): если пункт ушёл выше верхней или ниже нижней границы
  // .model-dd-list — подтягиваем его в зону видимости минимальным сдвигом.
  function keepItemVisible(it) {
    if (!it || !ddEl) return;
    const list = ddEl.querySelector('.model-dd-list');
    if (!list) return;
    const lr = list.getBoundingClientRect(), ir = it.getBoundingClientRect();
    if (ir.top < lr.top) list.scrollTop -= (lr.top - ir.top) + 4;
    else if (ir.bottom > lr.bottom) list.scrollTop += (ir.bottom - lr.bottom) + 4;
  }
  function ddSetStatus(text, isErr) {
    // строка-статус внутри открытого списка («Загрузка модели в память…», ошибки)
    if (!ddEl) return;
    let s = ddEl.querySelector('.model-dd-status');
    if (!s) {
      s = document.createElement('div');
      s.className = 'model-dd-item spin model-dd-status';
      const sp = document.createElement('span');
      sp.className = 'model-dd-spinner';
      s.appendChild(sp);
      s.appendChild(document.createElement('span'));
      ddEl.querySelector('.model-dd-list').appendChild(s);
    }
    s.classList.toggle('err', !!isErr);
    s.classList.toggle('spin', !isErr);
    s.lastChild.textContent = text;
  }
  function onModelPicked(model) {
    const sel = $('#modelSelect');
    const prev = sel ? sel.value : null;
    if (sel) sel.value = model;
    updateModelLabel();
    selectModel(model, prev);
  }
  let modelSwitching = false;
  async function selectModel(model, previous) {
    // ГИГИЕНА ID: в API уходит только реальный идентификатор модели из
    // /ai/models. Служебные/случайные строки ('current', 'default', пустые)
    // раньше долетали до LM Studio и давали «Model with identifier
    // 'current' not found».
    if (!model || typeof model !== 'string'
        || model === 'current' || model === 'default') {
      toast('Не удалось определить выбранную модель — откройте список и выберите заново');
      loadModels();                        // подтянем актуальный список/состояние VRAM
      return;
    }
    // Выбрали модель, которая УЖЕ стоит в памяти (по данным последнего
    // /ai/models) — это НЕ смена: не дёргаем LM Studio и не показываем
    // «загрузится автоматически, как только освободится». Просто запоминаем.
    if (modelList.loaded === model) {
      localStorage.setItem(MODEL_KEY, model);
      updateModelLabel();
      toast('Модель уже в памяти — готова к работе', 2600);
      return;
    }
    const sel = $('#modelSelect');
    // Смена модели = выгрузка текущей и загрузка выбранной в LM Studio.
    // Пока модель участвует в генерации (наша печать или чужой стрим на этом
    // аккаунте), сервер ответит 409 и поставит модель в очередь дозарядки.
    if (gen || genFlagGet()) {
      if (sel) sel.value = previous;              // откатываем выбор: сейчас нельзя
      localStorage.setItem(MODEL_KEY, model);     // запомнили намерение
      updateModelLabel();
      try {
        const r = await fetch('/ai/models/select', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model }),
        });
        if (r.status === 409) {
          toast('Модель нельзя сменить: идёт генерация. Она будет загружена автоматически после окончания ответа.', 4200);
          return;
        }
      } catch { /* сеть недоступна — намерение осталось в localStorage */ }
      return;
    }
    if (modelSwitching) return;
    modelSwitching = true;
    // НЕ ставим disabled — браузер закрасит контрол серым и он будет выглядеть
    // мёртвым. Вместо этого подсветка .busy + подсказка в title.
    const ctrl = $('#modelSelectBtn') || sel;
    if (ctrl) { ctrl.classList.add('busy'); ctrl.title = 'Загрузка модели в память…'; }
    ddSetStatus('Загрузка модели в память…');
    try {
      const r = await fetch('/ai/models/select', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok) {
        localStorage.setItem(MODEL_KEY, model);
        toast(data.changed ? `Модель ${model} загружена` : `Модель ${model} уже в памяти`, 3200);
        loadModels();                              // обновим пометку «в VRAM»
      } else if (r.status === 409) {
        localStorage.setItem(MODEL_KEY, model);
        toast(data.message || 'Идёт генерация — модель загрузится автоматически после ответа.', 4200);
      } else {
        if (sel) { sel.value = previous; updateModelLabel(); }   // откат значения при отказе сервера
        const em = data.message || data.error || 'Не удалось переключить модель';
        ddSetStatus(em, true);   // красный текст без спиннера: ошибка, а не загрузка
        toast(em, 4200);
      }
    } catch {
      if (sel) { sel.value = previous; updateModelLabel(); }
      toast('Сервер недоступен: модель не переключена');
    } finally {
      modelSwitching = false;
      updateModelBusy();   // снимает .busy/title, вернёт обычный вид (или «занято», если печать началась)
    }
  }
  const modelBtn = $('#modelSelectBtn');
  if (modelBtn) {
    modelBtn.addEventListener('click', openModelDd);
    modelBtn.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openModelDd();
      }
    });
  }
  loadModels();

  window.addEventListener('online', async () => {
    if (!SYNC.ready && currentScopeUid) SYNC.ready = await pullState();
    scheduleSync();
    resumePending();
    startStateEvents();
  });
})();