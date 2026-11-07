/* Мобильная шапка: редкие действия собраны в меню «⋯».
 * Пункты меню только нажимают исходные кнопки (#renameBtn и т.д.), поэтому
 * их обработчики и блокировка во время генерации работают как прежде.
 * На широком экране меню не создаётся, шапка выглядит как раньше. */
(function () {
  'use strict';

  const MQ = window.matchMedia('(max-width: 860px)');
  const ITEMS = [
    { label: 'Переименовать', src: '#renameBtn' },
    { label: 'Скачать чат (Markdown)', src: '#exportBtn' },
    { label: 'Очистить историю', src: '#clearBtn' },
    { label: 'Настройки модели', src: '#modelSettingsBtn' },
    { label: 'Удалить чат', src: '#deleteBtn', danger: true },
  ];

  let btn = null;
  let menu = null;

  function build() {
    if (btn || !document.querySelector('.top-actions')) return;
    btn = document.createElement('button');
    btn.id = 'moreBtn';
    btn.type = 'button';
    btn.className = 'icon-btn';
    btn.title = 'Действия с чатом';
    btn.setAttribute('aria-label', 'Действия с чатом');
    btn.setAttribute('aria-haspopup', 'menu');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
      '<circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';
    document.querySelector('.top-actions').appendChild(btn);

    menu = document.createElement('div');
    menu.id = 'tbMenu';
    menu.className = 'tb-menu';
    menu.setAttribute('role', 'menu');
    menu.hidden = true;
    menu.innerHTML = ITEMS.map((it, i) =>
      `<button type="button" role="menuitem" class="tb-item${it.danger ? ' danger' : ''}" data-i="${i}">${it.label}</button>`
    ).join('');
    document.body.appendChild(menu);

    btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden ? open() : close(); });
    menu.addEventListener('click', (e) => {
      const b = e.target.closest('.tb-item');
      if (!b) return;
      const src = document.querySelector(ITEMS[+b.dataset.i].src);
      close();
      if (src && !src.disabled) src.click();
    });
    document.addEventListener('click', (e) => {
      if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) close();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    window.addEventListener('resize', close);
  }

  // состояние пунктов повторяет состояние исходных кнопок (например, пока чат пуст)
  function syncItems() {
    menu.querySelectorAll('.tb-item').forEach((b) => {
      const src = document.querySelector(ITEMS[+b.dataset.i].src);
      b.disabled = !src || src.disabled;
    });
  }

  function open() {
    syncItems();
    const r = btn.getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + 'px';
    menu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }

  function close() {
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }

  function apply() {
    if (MQ.matches) build();
    else close();
  }

  apply();
  if (MQ.addEventListener) MQ.addEventListener('change', apply);
  else MQ.addListener(apply);
})();
