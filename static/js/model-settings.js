/* Окно «Настройки модели»: параметры загрузки GGUF (llama.cpp) для каждой модели.
 * Чат не затрагивается: данные меняются только по кнопкам «Сохранить» и
 * «Сохранить и загрузить». Сам модуль создаёт модальное окно и стили. */
(function () {
  'use strict';

  const FIELDS = [
    { key: 'ctx', label: 'Контекст, токенов', type: 'number', min: 512, max: 262144, step: 512,
      hint: 'Чем больше, тем больше VRAM уходит под KV-кэш' },
    { key: 'gpu_layers', label: 'Слоёв на GPU', type: 'number', min: -1, max: 999,
      hint: '-1 = все слои. Если не влезает — уменьшите' },
    { key: 'flash_attn', label: 'Flash attention', type: 'checkbox',
      hint: 'Экономит VRAM на длинном контексте' },
    { key: 'kv_cache_type', label: 'Тип KV-кэша', type: 'select', options: ['f16', 'q8_0', 'q4_0'],
      hint: 'q8_0 почти не влияет на качество и экономит VRAM' },
    { key: 'threads', label: 'Потоки CPU', type: 'number', min: 1, max: 128,
      hint: 'Для полной GPU-загрузки почти не важно' },
    { key: 'main_gpu', label: 'Основная GPU', type: 'select', options: ['0', '1', '2', '3'],
      hint: 'Номер карты (как в nvidia-smi при PCI_BUS_ID)' },
    { key: 'tensor_split', label: 'Доли по GPU', type: 'text', placeholder: 'например 12,6 (пусто = авто)',
      hint: 'Разнос слоёв по картам; пропорции через запятую' },
    { key: 'parallel', label: 'Параллельных слотов', type: 'number', min: 1, max: 8,
      hint: '1 — для одного пользователя. Больше = больше VRAM' },
  ];

  const CSS = `
  #msOverlay{position:fixed;inset:0;background:rgba(8,9,12,.6);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);
    display:none;z-index:9000;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
  #msOverlay.open{display:flex}
  #msModal{width:min(880px,100%);max-height:90vh;display:flex;flex-direction:column;overflow:hidden;
    background:var(--bg-secondary,#1b1d23);color:var(--text-main,#e8e8ea);border:1px solid var(--border-color,rgba(255,255,255,.08));
    border-radius:20px;box-shadow:0 24px 70px rgba(0,0,0,.5);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  #msModal header{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--border-color,rgba(255,255,255,.08))}
  #msModal header h2{font-size:16px;margin:0;font-weight:600;letter-spacing:-.01em}
  #msModal .ms-x{background:none;border:0;color:var(--text-muted,#888);font-size:22px;line-height:1;cursor:pointer;padding:4px 8px;border-radius:8px}
  #msModal .ms-x:hover{background:var(--bg-hover,rgba(255,255,255,.06));color:var(--text-main,#e8e8ea)}
  #msBody{display:grid;grid-template-columns:270px 1fr;gap:0;min-height:0;flex:1;overflow:hidden}
  @media (max-width:720px){#msBody{grid-template-columns:1fr}}
  #msList{overflow:auto;border-right:1px solid var(--border-color,rgba(255,255,255,.08));padding:10px}
  #msList .ms-item{display:block;width:100%;text-align:left;background:none;border:0;color:inherit;font:inherit;
    padding:9px 11px;border-radius:10px;cursor:pointer;transition:background .15s}
  #msList .ms-item:hover{background:var(--bg-hover,rgba(255,255,255,.05))}
  #msList .ms-item.sel{background:var(--accent-soft,rgba(120,140,255,.18))}
  #msList .ms-name{font-size:13px;font-weight:500;word-break:break-all}
  #msList .ms-meta{font-size:11.5px;color:var(--text-muted,#888);margin-top:2px}
  #msList .ms-dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--success-color,#3ba55d);
    margin-right:7px;box-shadow:0 0 8px rgba(59,165,93,.6);vertical-align:middle}
  #msForm{overflow:auto;padding:18px 20px;min-height:0}
  #msForm .ms-row{display:grid;grid-template-columns:190px 1fr;gap:4px 16px;align-items:center;margin-bottom:12px}
  @media (max-width:520px){#msForm .ms-row{grid-template-columns:1fr}}
  #msForm label{font-weight:500;color:var(--text-secondary,#b8bcc6)}
  #msForm .ms-hint{grid-column:2;font-size:12px;color:var(--text-muted,#888);margin-top:-2px}
  #msForm input[type=number],#msForm input[type=text],#msForm select{
    background:var(--bg-primary,#101115);color:var(--text-main,#e8e8ea);border:1px solid var(--border-color,rgba(255,255,255,.12));
    border-radius:10px;padding:8px 10px;width:100%;box-sizing:border-box;font:inherit;outline:none;transition:border-color .15s,box-shadow .15s}
  #msForm input:focus,#msForm select:focus{border-color:var(--border-focus,#6b8cff);box-shadow:0 0 0 3px var(--accent-glow,rgba(107,140,255,.25))}
  #msForm .ms-empty{color:var(--text-muted,#888);padding:24px 0}
  #msFoot{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:14px 20px;border-top:1px solid var(--border-color,rgba(255,255,255,.08))}
  #msFoot .ms-status{flex:1;min-width:180px;font-size:13px;color:var(--text-secondary,#b8bcc6)}
  #msFoot .ms-status.err{color:var(--danger-color,#ff7b7b)}
  #msFoot button{border:1px solid var(--border-color,rgba(255,255,255,.14));background:transparent;color:var(--text-main,#e8e8ea);
    border-radius:10px;padding:8px 14px;cursor:pointer;font:inherit;font-size:13.5px;transition:background .15s}
  #msFoot button:hover:not(:disabled){background:var(--bg-hover,rgba(255,255,255,.06))}
  #msFoot button.primary{background:var(--accent-gradient,#5b6cff);border-color:transparent;color:#fff;font-weight:600}
  #msFoot button:disabled{opacity:.45;cursor:default}`;

  let state = null;      // ответ GET /ai/model-settings
  let selected = null;   // id выбранной модели
  let busy = false;

  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function injectUi() {
    if ($('#msOverlay')) return;
    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    const wrap = document.createElement('div');
    wrap.id = 'msOverlay';
    wrap.innerHTML = `
      <div id="msModal" role="dialog" aria-modal="true" aria-labelledby="msTitle">
        <header>
          <h2 id="msTitle">Настройки модели</h2>
          <button class="ms-x" id="msClose" type="button" aria-label="Закрыть">×</button>
        </header>
        <div id="msBody">
          <div id="msList"></div>
          <div id="msForm"></div>
        </div>
        <div id="msFoot">
          <div class="ms-status" id="msStatus"></div>
          <button type="button" id="msReset">К умолчаниям</button>
          <button type="button" id="msUnload" disabled>Выгрузить из памяти</button>
          <button type="button" id="msSave" disabled>Сохранить</button>
          <button type="button" id="msLoad" class="primary" disabled>Сохранить и загрузить</button>
        </div>
      </div>`;
    document.body.appendChild(wrap);
    $('#msClose').addEventListener('click', close);
    wrap.addEventListener('click', (e) => { if (e.target === wrap && !busy) close(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && isOpen() && !busy) close(); });
    $('#msReset').addEventListener('click', resetToDefaults);
    $('#msSave').addEventListener('click', () => save(false));
    $('#msLoad').addEventListener('click', () => save(true));
    $('#msUnload').addEventListener('click', unloadModel);
  }

  const isOpen = () => $('#msOverlay') && $('#msOverlay').classList.contains('open');

  function setStatus(text, isErr) {
    const s = $('#msStatus');
    s.textContent = text || '';
    s.classList.toggle('err', !!isErr);
  }

  function setBusy(v) {
    busy = v;
    ['#msSave', '#msLoad', '#msReset'].forEach((id) => { $(id).disabled = v || !editable(); });
    $('#msUnload').disabled = v || !(state && state.loaded);
  }

  const editable = () => !!(state && state.editable);

  async function open() {
    injectUi();
    $('#msOverlay').classList.add('open');
    await reload();
  }

  function close() {
    $('#msOverlay').classList.remove('open');
  }

  async function reload() {
    setStatus('Загрузка списка моделей…');
    try {
      const r = await fetch('/ai/model-settings', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) throw new Error(r.status === 401 ? 'Нужно войти в систему' : `HTTP ${r.status}`);
      state = await r.json();
    } catch (e) {
      setStatus('Не удалось получить список: ' + e.message, true);
      return;
    }
    if (!selected || !state.models.some((m) => m.id === selected)) {
      selected = (state.loaded && state.models.find((m) => m.id === state.loaded)?.id)
              || (state.models[0] && state.models[0].id) || null;
    }
    renderList();
    renderForm();
    setStatus(state.editable ? '' : (state.note || ''));
  }

  function renderList() {
    const list = $('#msList');
    if (!state.models.length) {
      list.innerHTML = '<div class="ms-empty">Моделей не найдено' +
        (state.models_dir ? ` в ${esc(state.models_dir)}` : '') + '</div>';
      return;
    }
    list.innerHTML = state.models.map((m) => `
      <button class="ms-item ${m.id === selected ? 'sel' : ''}" type="button" data-id="${esc(m.id)}">
        <div class="ms-name" title="${esc(m.id)}">${m.loaded ? '<span class="ms-dot" title="сейчас в памяти"></span>' : ''}${esc(m.id.split('/').pop().replace(/\.gguf$/i, ''))}</div>
        <div class="ms-meta">${m.size_gb != null ? m.size_gb + ' ГБ' : ''}${m.saved ? ' · свои параметры' : ''}</div>
      </button>`).join('');
    list.querySelectorAll('.ms-item').forEach((b) => b.addEventListener('click', () => {
      if (busy) return;
      selected = b.dataset.id;
      renderList();
      renderForm();
      setStatus('');
    }));
  }

  function currentModel() {
    return state && state.models.find((m) => m.id === selected);
  }

  function renderForm() {
    const form = $('#msForm');
    const m = currentModel();
    if (!m) {
      form.innerHTML = '<div class="ms-empty">Выберите модель слева.</div>';
      setBusy(false);
      return;
    }
    const p = m.params || state.defaults || {};
    const rows = FIELDS.map((f) => {
      const v = p[f.key];
      let input;
      if (f.type === 'checkbox') {
        input = `<input type="checkbox" id="f_${f.key}" ${v ? 'checked' : ''}>`;
      } else if (f.type === 'select') {
        const opts = f.options.map((o) => `<option value="${o}" ${String(v) === o ? 'selected' : ''}>${o}</option>`).join('');
        input = `<select id="f_${f.key}">${opts}</select>`;
      } else if (f.type === 'number') {
        input = `<input type="number" id="f_${f.key}" value="${esc(v)}" min="${f.min}" max="${f.max}" step="${f.step || 1}">`;
      } else {
        input = `<input type="text" id="f_${f.key}" value="${esc(v)}" placeholder="${esc(f.placeholder || '')}">`;
      }
      return `<div class="ms-row"><label for="f_${f.key}">${esc(f.label)}</label>${input}` +
             (f.hint ? `<div class="ms-hint">${esc(f.hint)}</div>` : '') + `</div>`;
    }).join('');
    const info = m.loaded
      ? '<div class="ms-hint" style="grid-column:1/-1;margin-bottom:10px;color:#3ba55d">Сейчас загружена в llama-server.</div>'
      : '';
    form.innerHTML = `<div style="margin-bottom:12px"><b>${esc(m.id)}</b>` +
      (m.size_gb != null ? ` · ${m.size_gb} ГБ` : '') + `</div>${info}${rows}`;
    setBusy(false);
  }

  function readForm() {
    const out = {};
    for (const f of FIELDS) {
      const el = $('#f_' + f.key);
      if (!el) continue;
      if (f.type === 'checkbox') out[f.key] = !!el.checked;
      else if (f.type === 'number') out[f.key] = parseInt(el.value, 10);
      else if (f.key === 'main_gpu') out[f.key] = parseInt(el.value, 10);
      else out[f.key] = (el.value || '').trim();
    }
    return out;
  }

  function resetToDefaults() {
    if (!state || !state.defaults) return;
    const d = state.defaults;
    for (const f of FIELDS) {
      const el = $('#f_' + f.key);
      if (!el) continue;
      if (f.type === 'checkbox') el.checked = !!d[f.key];
      else el.value = d[f.key];
    }
    setStatus('Значения по умолчанию из .env подставлены. Нажмите «Сохранить».');
  }

  async function save(loadNow) {
    const m = currentModel();
    if (!m || busy) return;
    const params = readForm();
    setBusy(true);
    setStatus(loadNow ? 'Сохраняю и загружаю модель — это может занять несколько минут…' : 'Сохраняю…');
    try {
      const r1 = await fetch('/ai/model-settings', {
        method: 'PUT', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: m.id, params }),
      });
      const d1 = await r1.json().catch(() => ({}));
      if (!r1.ok) throw new Error(d1.detail || `HTTP ${r1.status}`);
      if (!loadNow) {
        setStatus('Параметры сохранены. Применятся при следующей загрузке модели.');
        await reload();
        return;
      }
      const r2 = await fetch('/ai/models/select', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: m.id, params }),
      });
      const d2 = await r2.json().catch(() => ({}));
      if (r2.status === 409) {
        setStatus('Сейчас идёт ответ. Модель загрузится автоматически после его окончания.');
      } else if (!r2.ok) {
        throw new Error(d2.error || d2.message || `HTTP ${r2.status}`);
      } else {
        try { localStorage.setItem('chatModel', m.id); } catch (_) { /* приватный режим */ }
        setStatus(d2.changed === false
          ? 'Модель уже загружена с этими параметрами.'
          : 'Модель загружена. Обновите страницу (F5), чтобы шапка чата показала её.');
      }
      await reload();
    } catch (e) {
      setStatus('Ошибка: ' + e.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function unloadModel() {
    if (busy || !state || !state.loaded) return;
    setBusy(true);
    setStatus('Выгружаю модель из памяти…');
    let msg = 'Модель выгружена, видеопамять освобождена.';
    let err = false;
    try {
      const r = await fetch('/ai/model-settings/unload', { method: 'POST', credentials: 'same-origin' });
      const d = await r.json().catch(() => ({}));
      if (r.status === 409) { msg = 'Сейчас идёт ответ. Выгрузка недоступна до его окончания.'; err = true; }
      else if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    } catch (e) {
      msg = 'Ошибка: ' + e.message;
      err = true;
    }
    setBusy(false);
    await reload();
    setStatus(msg, err);
  }

  function bindButton() {
    const btn = $('#modelSettingsBtn');
    if (btn) btn.addEventListener('click', open);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bindButton);
  } else {
    bindButton();
  }
})();
