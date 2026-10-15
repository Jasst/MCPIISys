/**
 * ai-store.js — хранилище чатов: IndexedDB как основное место, localStorage как зеркало.
 *
 * Почему не только localStorage: его лимит ~5 МБ на сайт и он синхронный — при
 * генерации каждое сохранение переписывает весь чат целиком. IndexedDB вмещает
 * сотни МБ, работает асинхронно и позволяет читать отдельные сообщения по id
 * вместо полной загрузки истории.
 *
 * Как устроено (версионная модель, «передаётся только дельта»):
 *  - Каждое изменение сообщения — новая версия смонотонным rev; в базе остаётся
 *    несколько последних версий каждого сообщения (окно отката для sync-слияния).
 *  - Синхронизация с сервером отправляет ТОЛЬКО изменённые сообщения (дельту)
 *    и удалённые id, а не весь контент всех чатов.
 *  - localStorage обновляется best-effort зеркалом открытых чатов — для старых
 *    вкладок и мгновенного старта без ожидания БД.
 *
 * API (все методы возвращают Promise):
 *   open(uid)                      — выбрать аккаунт (свой scope данных)
 *   getSessions() / saveSessions(list)
 *   loadMessages(id)               — [{v, m}] версии сообщений по порядку
 *   commitMessages(id, msgs, removedIdx) — полная запись (после clear/regenerate)
 *   patchMessage(id, idx, fields)  — частичная запись: сохраняются только changed поля
 *   appendMessage(id, msg)         — добавить сообщение в конец
 *   removeSession(id)              — удалить чат целиком
 *   since(rev)                     — дельта: всё, что изменилось после версии rev
 *   currentRev()                   — текущая версия базы (для first-sync сравнения)
 *   onEvent(fn)                    — слушатель изменений (для live-обновлений)
 */
(function (global) {
  'use strict';

  const DB = 'ai_chats';
  const VER = 1;
  const KEEP_VERSIONS = 3;          // сколько версий сообщения хранить для отката дельты
  const LS_MSG_LIMIT = 2_000_000;   // байт на один localStorage-ключ с историей
  const LS_TOTAL_LIMIT = 4_000_000; // байт на все ключи ai_messages_*

  let db = null;
  let scopeUid = null;              // uid аккаунта или '' для локального режима
  let memRev = 0;                   // monotonic counter per page
  let idbBroken = false;            // true → работаем поверх localStorage
  const listeners = [];

  const emit = (ev) => { for (const fn of listeners) { try { fn(ev); } catch { /* ignore */ } } };
  const tx = (store, mode, fn) => new Promise((res, rej) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    req.onsuccess = () => res(req.result);
    t.onerror = t.onabort = () => rej(t.error || new Error('transaction aborted'));
  });
  const keyOf = (sid, i, v) => [sid, i, v];

  function lsKey(k) { return scopeUid ? `ai:${scopeUid}:${k}` : 'ai_' + k; }
  function lsGet(k) { try { return localStorage.getItem(lsKey(k)); } catch { return null; } }
  function lsSet(k, val) { try { localStorage.setItem(lsKey(k), val); return true; } catch { return false; } }
  function lsDel(k) { try { localStorage.removeItem(lsKey(k)); } catch { /* ignore */ } }

  // ------------------------------------------------------------------ открытие

  function openIndexedDb() {
    return new Promise((resolve, reject) => {
      if (!('indexedDB' in global)) return reject(new Error('no indexedDB'));
      let req;
      try { req = indexedDB.open(DB, VER); } catch (e) { return reject(e); }
      req.onupgradeneeded = () => {
        const d = req.result;
        if (!d.objectStoreNames.contains('messages')) {
          const s = d.createObjectStore('messages', { keyPath: ['sid', 'idx', 'ver'] });
          s.createIndex('bySid', 'sid', { unique: false });
          s.createIndex('byRev', 'rev', { unique: false });
        }
        if (!d.objectStoreNames.contains('meta')) d.createObjectStore('meta');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
      req.onblocked = () => reject(new Error('blocked'));
      setTimeout(() => { try { req.transaction && req.transaction.abort(); } catch { /* ignore */ } reject(new Error('timeout')); }, 4000);
    });
  }

  async function init(uid) {
    scopeUid = uid || '';
    try {
      db = await openIndexedDb();
      idbBroken = false;
      const last = await readMetaRev();
      memRev = Math.max(last, Date.now());
    } catch (e) {
      console.warn('IndexedDB недоступен, хранилище на localStorage:', e);
      db = null; idbBroken = true;
      memRev = Number(lsGet('ai_rev') || 0) || Date.now();
    }
  }

  async function readMetaRev() {
    if (!db) return Number(lsGet('ai_rev') || 0);
    try { return (await tx('meta', 'readonly', (s) => s.get('rev'))) || 0; } catch { return 0; }
  }
  function bumpMem() { memRev = Math.max(memRev + 1, Date.now()); return memRev; }
  function writeRev(r) {
    if (idbBroken) { lsSet('ai_rev', String(r)); return Promise.resolve(); }
    try { return tx('meta', 'readwrite', (s) => s.put(r, 'rev')); } catch { return Promise.resolve(); }
  }

  // ------------------------------------------------------------------ sessions

  async function getSessions() {
    if (!idbBroken) {
      try {
        const v = await tx('meta', 'readonly', (s) => s.get('sessions'));
        if (Array.isArray(v)) return v;
      } catch { /* fall through to mirror */ }
    }
    const raw = lsGet('sessions');
    if (raw == null) return [];
    try { const j = JSON.parse(raw); return Array.isArray(j) ? j : []; } catch { return []; }
  }

  async function saveSessions(list) {
    const json = JSON.stringify(list);
    if (!idbBroken) { try { await tx('meta', 'readwrite', (s) => s.put(list, 'sessions')); } catch { /* mirror still written */ } }
    lsSet('sessions', json);   // зеркало: мгновенный холодный старт, старые вкладки
  }

  // ------------------------------------------------------------------ messages

  async function loadMessages(sid) {
    if (!idbBroken) {
      try {
        const all = await tx('messages', 'readonly', (s) => s.index('bySid').getAll(sid));
        const latest = {};
        for (const r of all) {
          const c = latest[r.idx];
          if (!c || r.ver > c.ver) latest[r.idx] = r;
        }
        const out = Object.values(latest).sort((a, b) => a.idx - b.idx).map((r) => ({ v: r.ver, m: r.m }));
        const live = out.filter((x) => x.m).map((x) => x.m);
        if (live.length) mirrorLs(sid, live);
        return out;
      } catch (e) { console.warn('loadMessages IDB failed:', e); }
    }
    const raw = lsGet('messages_' + sid);
    if (raw == null) return [];
    try {
      const arr = JSON.parse(raw);
      if (!Array.isArray(arr)) return [];
      return arr.map((m, i) => ({ v: i + 1, m }));
    } catch { return []; }
  }

  // best-effort зеркало в localStorage: последние сообщения, пока хватает места
  function mirrorLs(sid, msgs) {
    try {
      const slice = [];
      let bytes = 2;
      for (let i = msgs.length - 1; i >= 0; i--) {
        const s = JSON.stringify(msgs[i]);
        const add = s.length * 2; // грубая оценка в байтах UTF-16
        if (bytes + add > LS_MSG_LIMIT) break;
        slice.unshift(s); bytes += add;
      }
      while (lsTotalBytes() > LS_TOTAL_LIMIT && slice.length > 8) { slice.pop(); }
      lsSet('messages_' + sid, '[' + slice.join(',') + ']');
    } catch { /* quota — зеркало необязательно */ }
  }

  function lsTotalBytes() {
    let n = 0;
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && (k.startsWith('ai_messages_') || k.includes(':messages_'))) {
        n += (localStorage.getItem(k) || '').length * 2;
      }
    }
    return n;
  }

  async function putVersion(sid, idx, ver, m, rev) {
    if (idbBroken) return;
    await tx('messages', 'readwrite', (s) => s.put({ sid, idx, ver, m, rev }));
    await trimVersions(sid, idx, ver);
  }

  // Храним только KEEP_VERSIONS последних версий сообщения: старые версии уже не
  // нужны ни для чтения, ни для дельты (серверу отправляются версии > base)
  async function trimVersions(sid, idx, newestVer) {
    if (idbBroken) return;
    try {
      const lo = IDBKeyRange.bound(keyOf(sid, idx, 0), keyOf(sid, idx, newestVer));
      const rows = await tx('messages', 'readonly', (s) => s.getAll(lo));
      const del = rows.slice(0, Math.max(0, rows.length - KEEP_VERSIONS));
      if (!del.length) return;
      await tx('messages', 'readwrite', (s) => { for (const r of del) s.delete(['sid', 'idx', 'ver'].map((k) => r[k])); });
    } catch { /* cleanup is optional */ }
  }

  async function deleteChat(sid, uptoVer, rev) {
    if (!idbBroken) {
      try {
        const rows = await tx('messages', 'readonly', (s) => s.index('bySid').getAll(sid));
        const keep = rows.filter((r) => r.ver <= uptoVer); // versions needed by delta window
        const del = rows.filter((r) => !keep.includes(r));
        if (del.length) await tx('messages', 'readwrite', (s) => { for (const r of del) s.delete([r.sid, r.idx, r.ver]); });
      } catch { /* ignore */ }
    }
    lsDel('messages_' + sid);
    emit({ type: 'chatDeleted', sid, rev });
  }

  // Полная перезапись истории: diff предыдущей и новой, удалённые индексы помечаются tombstone
  async function commitMessages(sid, msgs, removedFrom) {
    const prev = await loadMessages(sid);
    const rev = bumpMem();
    const changed = [];
    const maxOld = prev.length;
    for (let i = 0; i < msgs.length; i++) {
      const old = prev[i] && prev[i].m;
      const nv = prev[i] ? prev[i].v + 1 : 1;
      if (!old || JSON.stringify(old) !== JSON.stringify(msgs[i])) {
        await putVersion(sid, i, nv, msgs[i], rev);
        changed.push({ idx: i, ver: nv, m: msgs[i], rev });
      }
    }
    const uptoVer = 0;   // удалённый чат: серверу достаточно tombstone-дельты
    if (removedFrom != null && removedFrom < maxOld) {
      for (let i = removedFrom; i < maxOld; i++) {
        const nv = prev[i].v + 1;
        await putVersion(sid, i, nv, null, rev);
        changed.push({ idx: i, ver: nv, m: null, rev });
      }
    }
    mirrorLs(sid, msgs);
    await writeRev(rev);
    emit({ type: 'commit', sid, rev, changed, deletedFrom: removedFrom });
    return rev;
  }

  // Частичное обновление одного сообщения: пишем только изменённые поля
  async function patchMessage(sid, idx, fields) {
    const prev = await loadMessages(sid);
    const cur = prev[idx];
    if (!cur) return commitMessages(sid, prev.concat(), null);
    const clean = {};
    let any = false;
    for (const k in fields) {
      if (k === 'id' || k.startsWith('_')) continue;
      if (JSON.stringify(cur.m[k]) !== JSON.stringify(fields[k])) { clean[k] = fields[k]; any = true; }
    }
    if (!any) return cur.v;
    const merged = Object.assign({}, cur.m, clean);
    const nv = cur.v + 1;
    const rev = bumpMem();
    await putVersion(sid, idx, nv, merged, rev);
    const arr = prev.map((x) => x.m); arr[idx] = merged;
    mirrorLs(sid, arr);
    await writeRev(rev);
    emit({ type: 'patch', sid, idx, ver: nv, changed: clean, full: merged, rev });
    return nv;
  }

  async function appendMessage(sid, msg) {
    const prev = await loadMessages(sid);
    const idx = prev.length;
    const rev = bumpMem();
    await putVersion(sid, idx, 1, msg, rev);
    mirrorLs(sid, prev.map((x) => x.m).concat([msg]));
    await writeRev(rev);
    emit({ type: 'append', sid, idx, ver: 1, m: msg, rev });
    return { idx, ver: 1 };
  }

  async function removeSession(sid) {
    await deleteChat(sid, 0, bumpMem());
  }

  // ------------------------------------------------------------------ дельты

  async function currentRev() {
    if (idbBroken) return Number(lsGet('ai_rev') || 0);
    return (await readMetaRev()) || 0;
  }

  // Всё, что изменилось после версии base: новые версии сообщений (m:null — удалено),
  // удалённые целиком чаты, и список сессий если он менялся позже base.
  async function since(base) {
    const rev = await currentRev();
    if (idbBroken) {
      const msgs = {};
      for (const s of await getSessions()) {
        const arr = await loadMessages(s.id);
        if (arr.some((x) => x.v > base)) msgs[s.id] = arr.map((x) => ({ i: x.v, m: x.m }));
      }
      return { rev, messages: msgs, deletedChats: [], sessionsChanged: false };
    }
    const rows = await tx('messages', 'readonly', (s) => s.index('byRev').getAll(IDBKeyRange.lowerBound(base, true)));
    const messages = {};
    for (const r of rows) {
      (messages[r.sid] = messages[r.sid] || []).push({ i: r.ver, m: r.m });
    }
    const sessionsRev = await tx('meta', 'readonly', (s) => s.get('sessions_rev'));
    const deletedChats = await tx('meta', 'readonly', (s) => s.get('deleted_chats')) || [];
    const fresh = deletedChats.filter((d) => d.rev > base);
    return { rev, messages, deletedChats: fresh.map((d) => d.sid), sessionsChanged: (sessionsRev || 0) > base };
  }

  async function markSessionsSaved(rev) {
    if (idbBroken) return;
    try { await tx('meta', 'readwrite', (s) => s.put(rev, 'sessions_rev')); } catch { /* ignore */ }
  }
  async function recordDeletedChat(sid, rev) {
    if (idbBroken) return;
    try {
      const list = (await tx('meta', 'readonly', (s) => s.get('deleted_chats'))) || [];
      list.push({ sid, rev });
      const cut = Date.now() - 7 * 86400000;
      const trimmed = list.filter((d) => d.rev >= cut || typeof d.rev === 'string');
      await tx('meta', 'readwrite', (s) => s.put(trimmed.slice(-200), 'deleted_chats'));
    } catch { /* ignore */ }
  }

  // Принять дельту с сервера (или из legacy-снапшота): применяем только пришедшие сообщения
  async function applyDelta(sid, items, replaceAll) {
    const prev = replaceAll ? [] : await loadMessages(sid);
    const rev = bumpMem();
    const arr = prev.map((x) => x.m);
    for (const it of items) {
      if (it.i == null) continue;
      if (replaceAll) { arr[it.i - 1] = it.m; await putVersion(sid, it.i - 1, it.i, it.m, rev); }
      else { arr[it.i] = it.m; await putVersion(sid, it.i, it.v || (prev[it.i] ? prev[it.i].v + 1 : 1), it.m, rev); }
    }
    mirrorLs(sid, arr.filter(Boolean));
    await writeRev(rev);
    emit({ type: 'remote', sid, rev });
    return rev;
  }

  // Однократная миграция истории из старого localStorage-кеша в IndexedDB
  async function migrateLegacy(uid) {
    if (idbBroken) return false;
    const flag = lsKey('migrated_v1');
    if (localStorage.getItem(flag)) return false;
    const prefix = uid ? `ai:${uid}:` : 'ai_';
    const legacyKey = prefix + 'sessions';
    let moved = 0;
    try {
      const raw = localStorage.getItem(legacyKey);
      if (raw) {
        const list = JSON.parse(raw);
        if (Array.isArray(list)) {
          for (const s of list) {
            const mk = prefix + 'messages_' + s.id;
            const mr = localStorage.getItem(mk);
            if (!mr) continue;
            const arr = JSON.parse(mr);
            if (Array.isArray(arr) && arr.length) { await commitMessages(s.id, arr, null); moved++; }
          }
          if (!uid) await saveSessions(list);
        }
      }
      // чистим устаревшие ключи, освобождая квоту localStorage
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && (k.startsWith('ai_messages_') || k === 'ai_sessions')) localStorage.removeItem(k);
      }
    } catch (e) { console.warn('migrateLegacy:', e); }
    try { localStorage.setItem(flag, String(moved)); } catch { /* ignore */ }
    return moved > 0;
  }

  global.AiStore = {
    init, getSessions, saveSessions, loadMessages, commitMessages, patchMessage,
    appendMessage, removeSession, since, currentRev, applyDelta, migrateLegacy,
    markSessionsSaved, recordDeletedChat, onEvent: (fn) => listeners.push(fn),
    get broken() { return idbBroken; },
  };
})(window);
