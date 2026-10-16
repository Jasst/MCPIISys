// Тесты клиентского правила слияния (static/js/ai-app.js): mergeMsgFields, mergeMsgLists.
// Запуск: node tests/sync/test_merge_rules.js
// Функции извлекаются из исходника по имени (разбор скобок), без браузера.
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'ai-app.js'), 'utf8');

function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error('not found: ' + name);
  let depth = 0, i = src.indexOf('{', start);
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  return src.slice(start, i + 1);
}
const code = 'const MAX_STORED = 300;\n' + ['msgKey', 'dedupeByKeys', 'mergeMsgFields', 'mergeMsgLists'].map(extract).join('\n');
const { mergeMsgLists, mergeMsgFields } = new Function(code + '\nreturn { mergeMsgLists, mergeMsgFields };')();

let failed = 0;
function check(name, cond, detail) {
  console.log((cond ? 'OK   ' : 'FAIL ') + name + (cond ? '' : '  ' + JSON.stringify(detail)));
  if (!cond) failed++;
}

// 1. Пустой text с сервера не затирает известный
const f1 = mergeMsgFields({ id: 'b', text: 'final', pending: false }, { id: 'b', text: '', pending: true });
check('empty text does not erase known text', f1.text === 'final', f1);
check('finished message not reopened', f1.pending === false, f1);

// 2. Пустой reasoning не затирает известный
const f2 = mergeMsgFields({ id: 'b', reasoning: 'r' }, { id: 'b', reasoning: '' });
check('empty reasoning keeps known reasoning', f2.reasoning === 'r', f2);

// 3. Непустое значение от сервера побеждает
const f3 = mergeMsgFields({ id: 'b', text: 'old' }, { id: 'b', text: 'new' });
check('non-empty incoming wins', f3.text === 'new', f3);

// 4. Слияние списков: черновик не стирает финальный ответ
const local = [{ id: 'u', role: 'user', text: 'q' }, { id: 'b', role: 'assistant', text: 'final', pending: false }];
const server = [{ id: 'b', role: 'assistant', text: '', pending: true }];
const merged = mergeMsgLists(local, server);
const bm = merged.find((m) => m.id === 'b');
check('merge lists keeps final answer', bm.text === 'final' && bm.pending === false, bm);
check('merge lists keeps order and length', merged.map((m) => m.id).join() === 'u,b', merged.map((m) => m.id));

// 5. Новое сообщение с сервера дописывается в конец
const merged2 = mergeMsgLists([{ id: 'a', role: 'user', text: '1' }], [{ id: 'n', role: 'user', text: '2' }]);
check('new server message appended', merged2.map((m) => m.id).join() === 'a,n', merged2.map((m) => m.id));

console.log(failed ? `\nFAILED: ${failed}` : '\nALL PASSED');
process.exit(failed ? 1 : 0);
