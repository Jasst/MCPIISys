/* Общие окна приложения: подтверждение, ввод, уведомление и контекстное меню.
   Заменяют системные confirm / prompt / alert, чтобы все окна выглядели одинаково.
   Подключается в ai.html и memory_viewer.html; стили — static/ui.css. */
(function () {
  "use strict";

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /* Базовое окно. Promise разрешается результатом: confirm -> true/false,
     prompt -> строка или null, alert -> undefined. */
  function dialog(opts) {
    return new Promise((resolve) => {
      const prevFocus = document.activeElement;
      const overlay = el("div", "ui-overlay");
      const box = el("div", "ui-dialog");
      box.setAttribute("role", "dialog");
      box.setAttribute("aria-modal", "true");
      if (opts.title) box.appendChild(el("h3", "ui-title", opts.title));
      if (opts.message) box.appendChild(el("p", "ui-message", opts.message));

      let field = null;
      let errorLine = null;
      if (opts.input) {
        field = el("input", "ui-input");
        field.type = "text";
        field.autocomplete = "off";
        field.value = opts.input.value || "";
        field.placeholder = opts.input.placeholder || "";
        box.appendChild(field);
        errorLine = el("div", "ui-error");
        box.appendChild(errorLine);
      }

      const actions = el("div", "ui-actions");
      let cancelBtn = null;
      if (opts.cancelText !== null) {
        cancelBtn = el("button", "ui-btn", opts.cancelText || "Отмена");
        cancelBtn.type = "button";
        actions.appendChild(cancelBtn);
      }
      const okBtn = el("button", "ui-btn primary" + (opts.danger ? " danger" : ""), opts.okText || "OK");
      okBtn.type = "button";
      actions.appendChild(okBtn);
      box.appendChild(actions);
      overlay.appendChild(box);
      document.body.appendChild(overlay);

      let finished = false;
      const finish = (value) => {
        if (finished) return;
        finished = true;
        document.removeEventListener("keydown", onKey, true);
        overlay.remove();
        if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus();
        resolve(value);
      };
      const cancel = () => finish(opts.cancelValue);
      const submit = () => {
        if (!field) return finish(opts.okValue);
        const v = field.value;
        if (opts.input.required && !v.trim()) {
          errorLine.textContent = "Поле обязательно для заполнения";
          field.focus();
          return;
        }
        finish(v);
      };

      const focusables = () => [field, cancelBtn, okBtn].filter(Boolean);
      const onKey = (e) => {
        if (e.key === "Escape") {
          e.preventDefault();
          cancel();
        } else if (e.key === "Enter" && !e.isComposing) {
          e.preventDefault();
          if (document.activeElement === cancelBtn) cancel(); else submit();
        } else if (e.key === "Tab") {
          const f = focusables();
          const i = f.indexOf(document.activeElement);
          e.preventDefault();
          const next = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i === f.length - 1 ? 0 : i + 1);
          f[next].focus();
        }
      };
      document.addEventListener("keydown", onKey, true);
      okBtn.addEventListener("click", submit);
      if (cancelBtn) cancelBtn.addEventListener("click", cancel);
      overlay.addEventListener("pointerdown", (e) => { if (e.target === overlay) cancel(); });

      setTimeout(() => {
        if (field) { field.focus(); field.select(); }
        else if (opts.danger && cancelBtn) cancelBtn.focus();
        else okBtn.focus();
      }, 0);
    });
  }

  /* ---------- контекстное меню ---------- */
  let menuEl = null;
  function closeMenu() {
    if (menuEl) { menuEl.remove(); menuEl = null; }
  }
  function menu(x, y, items) {
    closeMenu();
    const m = el("div", "ui-menu");
    m.setAttribute("role", "menu");
    for (const it of items) {
      if (it.sep) { m.appendChild(el("div", "ui-menu-sep")); continue; }
      const b = el("button", "ui-menu-item" + (it.danger ? " danger" : ""), it.label);
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.disabled = !!it.disabled;
      b.addEventListener("click", () => { closeMenu(); if (it.onClick) it.onClick(); });
      m.appendChild(b);
    }
    document.body.appendChild(m);
    const r = m.getBoundingClientRect();
    m.style.left = Math.max(8, Math.min(x, window.innerWidth - r.width - 8)) + "px";
    m.style.top = Math.max(8, Math.min(y, window.innerHeight - r.height - 8)) + "px";
    menuEl = m;
  }
  document.addEventListener("pointerdown", (e) => {
    if (menuEl && !menuEl.contains(e.target)) closeMenu();
  }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  window.addEventListener("scroll", closeMenu, true);
  window.addEventListener("resize", closeMenu);

  /* Правый клик и долгое нажатие (сенсорный экран) открывают меню на элементе. */
  function attachContext(target, getItems) {
    target.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      menu(e.clientX, e.clientY, getItems());
    });
    let timer = null;
    let swallowClick = false;
    const clear = () => { clearTimeout(timer); timer = null; };
    target.addEventListener("touchstart", (e) => {
      const t = e.touches[0];
      const x = t.clientX, y = t.clientY;
      clear();
      timer = setTimeout(() => {
        timer = null;
        swallowClick = true;
        setTimeout(() => { swallowClick = false; }, 600);
        menu(x, y, getItems());
      }, 500);
    }, {passive: true});
    target.addEventListener("touchmove", clear, {passive: true});
    target.addEventListener("touchend", clear);
    target.addEventListener("touchcancel", clear);
    // после долгого нажатия «отпускание» не должно открыть элемент
    target.addEventListener("click", (e) => {
      if (!swallowClick) return;
      swallowClick = false;
      e.stopPropagation();
      e.preventDefault();
    }, true);
  }

  window.UI = {
    confirm(message, o) {
      o = o || {};
      return dialog({title: o.title || "Подтвердите действие", message, okText: o.okText || "Подтвердить",
        cancelText: o.cancelText, danger: !!o.danger, okValue: true, cancelValue: false}).then(Boolean);
    },
    prompt(message, o) {
      o = o || {};
      return dialog({title: o.title || "Введите значение", message, okText: o.okText || "Сохранить",
        cancelText: o.cancelText, danger: !!o.danger,
        input: {value: o.value, placeholder: o.placeholder, required: !!o.required}, cancelValue: null});
    },
    alert(message, o) {
      o = o || {};
      return dialog({title: o.title || "Внимание", message, okText: o.okText || "Понятно", cancelText: null});
    },
    menu,
    closeMenu,
    attachContext,
  };
})();
