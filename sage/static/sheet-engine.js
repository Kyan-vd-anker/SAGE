/* Sage Studio: spreadsheet formula engine. Works in the browser (window.SageSheet) and in Node for tests. */
(function (root) {
  'use strict';
  const MAX_COL = 702, MAX_ROW = 99999;

  class Err { constructor(code) { this.code = code; } }
  const colNumber = letters => [...letters].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
  const colLetters = n => { let s = ''; while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); } return s; };
  const NUMBER = /^[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?$/i;

  function tokenize(src) {
    const tokens = []; let i = 0;
    while (i < src.length) {
      const ch = src[i];
      if (/\s/.test(ch)) { i++; continue; }
      let m;
      if ((m = /^\d+\.?\d*(?:e[-+]?\d+)?|^\.\d+(?:e[-+]?\d+)?/i.exec(src.slice(i)))) { tokens.push({ t: 'num', v: parseFloat(m[0]) }); i += m[0].length; continue; }
      if (ch === '"') { let j = i + 1, s = ''; while (j < src.length && !(src[j] === '"' && src[j + 1] !== '"')) { if (src[j] === '"') j++; s += src[j]; j++; } if (j >= src.length) throw new Err('#VALUE!'); tokens.push({ t: 'str', v: s }); i = j + 1; continue; }
      if ((m = /^\$?([A-Za-z]{1,3})\$?(\d+)(?![A-Za-z0-9(])/.exec(src.slice(i)))) { tokens.push({ t: 'ref', col: m[1].toUpperCase(), row: parseInt(m[2], 10) }); i += m[0].length; continue; }
      if ((m = /^[A-Za-z][A-Za-z0-9.]*/.exec(src.slice(i)))) { tokens.push({ t: 'name', v: m[0].toUpperCase() }); i += m[0].length; continue; }
      if ((m = /^(<>|<=|>=|[-+*\/^&=<>():,;%])/.exec(src.slice(i)))) { tokens.push({ t: 'op', v: m[0] === ';' ? ',' : m[0] }); i += m[0].length; continue; }
      throw new Err('#NAME?');
    }
    return tokens;
  }

  const flat = args => args.flatMap(a => Array.isArray(a) ? a : [a]);
  const toNumber = v => {
    if (v instanceof Err) throw v;
    if (typeof v === 'number') return v;
    if (typeof v === 'boolean') return v ? 1 : 0;
    if (v === '' || v === null || v === undefined) return 0;
    if (typeof v === 'string' && NUMBER.test(v.trim())) return parseFloat(v);
    throw new Err('#VALUE!');
  };
  const numbersIn = args => flat(args).filter(v => { if (v instanceof Err) throw v; return typeof v === 'number'; });
  const truthy = v => { if (v instanceof Err) throw v; return typeof v === 'string' ? v !== '' : !!v; };

  const FUNCTIONS = {
    SUM: a => numbersIn(a).reduce((x, y) => x + y, 0),
    AVERAGE: a => { const n = numbersIn(a); if (!n.length) throw new Err('#DIV/0!'); return n.reduce((x, y) => x + y, 0) / n.length; },
    MIN: a => { const n = numbersIn(a); return n.length ? Math.min(...n) : 0; },
    MAX: a => { const n = numbersIn(a); return n.length ? Math.max(...n) : 0; },
    COUNT: a => numbersIn(a).length,
    ABS: a => Math.abs(toNumber(a[0])),
    SQRT: a => { const x = toNumber(a[0]); if (x < 0) throw new Err('#VALUE!'); return Math.sqrt(x); },
    POWER: a => Math.pow(toNumber(a[0]), toNumber(a[1])),
    MOD: a => { const d = toNumber(a[1]); if (d === 0) throw new Err('#DIV/0!'); const n = toNumber(a[0]); return n - d * Math.floor(n / d); },
    ROUND: a => { const f = Math.pow(10, a.length > 1 ? toNumber(a[1]) : 0), x = Number((toNumber(a[0]) * f).toPrecision(15)); return (x < 0 ? -Math.round(-x) : Math.round(x)) / f; },
    EXP: a => Math.exp(toNumber(a[0])),
    LN: a => { const x = toNumber(a[0]); if (x <= 0) throw new Err('#VALUE!'); return Math.log(x); },
    LOG10: a => { const x = toNumber(a[0]); if (x <= 0) throw new Err('#VALUE!'); return Math.log10(x); },
    SIN: a => Math.sin(toNumber(a[0])), COS: a => Math.cos(toNumber(a[0])), TAN: a => Math.tan(toNumber(a[0])), ATAN: a => Math.atan(toNumber(a[0])),
    PI: () => Math.PI,
    AND: a => flat(a).every(truthy), OR: a => flat(a).some(truthy), NOT: a => !truthy(a[0]),
    CONCAT: a => flat(a).map(v => { if (v instanceof Err) throw v; return v === null || v === undefined ? '' : String(v); }).join(''),
    LEN: a => String(a[0] ?? '').length,
  };

  function compute(cells) {
    const values = {}, state = {};
    function valueOf(ref) {
      if (ref in values) return values[ref];
      if (state[ref] === 'busy') throw new Err('#CIRC!');
      const raw = cells[ref];
      if (raw === undefined || raw === '') return null;
      state[ref] = 'busy';
      let result;
      try {
        if (raw[0] === '=') result = evaluate(raw.slice(1));
        else result = NUMBER.test(raw.trim()) ? parseFloat(raw) : raw;
        if (Array.isArray(result)) result = new Err('#VALUE!');
      } catch (e) { if (e instanceof Err) result = e; else throw e; }
      state[ref] = 'done';
      values[ref] = result;
      return result;
    }
    function cellValue(col, row) {
      if (colNumber(col) > MAX_COL || row < 1 || row > MAX_ROW) throw new Err('#REF!');
      const v = valueOf(col + row);
      if (v instanceof Err && v.code === '#CIRC!') throw v;
      return v;
    }
    function evaluate(src) {
      const tokens = tokenize(src); let pos = 0;
      const peek = () => tokens[pos], next = () => tokens[pos++];
      const isOp = v => peek() && peek().t === 'op' && peek().v === v;
      function expect(v) { if (!isOp(v)) throw new Err('#VALUE!'); pos++; }
      function comparison() {
        let left = concat();
        while (peek() && peek().t === 'op' && ['=', '<>', '<', '>', '<=', '>='].includes(peek().v)) {
          const op = next().v, right = concat();
          if (left instanceof Err) throw left;
          if (right instanceof Err) throw right;
          const a = left, b = right;
          const x = typeof a === 'string' && typeof b === 'string' ? a.toLowerCase() : a, y = typeof a === 'string' && typeof b === 'string' ? b.toLowerCase() : b;
          left = op === '=' ? x === y : op === '<>' ? x !== y : op === '<' ? x < y : op === '>' ? x > y : op === '<=' ? x <= y : x >= y;
        }
        return left;
      }
      function concat() {
        let left = additive();
        while (isOp('&')) { next(); const right = additive(); left = String(left ?? '') + String(right ?? ''); }
        return left;
      }
      function additive() {
        let left = term();
        while (isOp('+') || isOp('-')) { const op = next().v, right = term(); left = op === '+' ? toNumber(left) + toNumber(right) : toNumber(left) - toNumber(right); }
        return left;
      }
      function term() {
        let left = power();
        while (isOp('*') || isOp('/')) {
          const op = next().v, right = power();
          if (op === '*') left = toNumber(left) * toNumber(right);
          else { const d = toNumber(right); if (d === 0) throw new Err('#DIV/0!'); left = toNumber(left) / d; }
        }
        return left;
      }
      function power() {
        let left = unary();
        while (isOp('^')) { next(); left = Math.pow(toNumber(left), toNumber(unary())); }
        return left;
      }
      function unary() {
        if (isOp('-')) { next(); return -toNumber(unary()); }
        if (isOp('+')) { next(); return toNumber(unary()); }
        let v = primary();
        while (isOp('%')) { next(); v = toNumber(v) / 100; }
        return v;
      }
      function args() {
        const list = [];
        if (isOp(')')) { next(); return list; }
        for (;;) { list.push(comparison()); if (isOp(',')) { next(); continue; } expect(')'); return list; }
      }
      function primary() {
        const tok = next();
        if (!tok) throw new Err('#VALUE!');
        if (tok.t === 'num' || tok.t === 'str') return tok.v;
        if (tok.t === 'op' && tok.v === '(') { const v = comparison(); expect(')'); return v; }
        if (tok.t === 'ref') {
          if (isOp(':')) {
            next(); const end = next();
            if (!end || end.t !== 'ref') throw new Err('#REF!');
            const c1 = Math.min(colNumber(tok.col), colNumber(end.col)), c2 = Math.max(colNumber(tok.col), colNumber(end.col));
            const r1 = Math.min(tok.row, end.row), r2 = Math.max(tok.row, end.row);
            if ((c2 - c1 + 1) * (r2 - r1 + 1) > 20000) throw new Err('#REF!');
            const out = [];
            for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) out.push(cellValue(colLetters(c), r));
            return out;
          }
          return cellValue(tok.col, tok.row);
        }
        if (tok.t === 'name') {
          if (tok.v === 'TRUE') return true;
          if (tok.v === 'FALSE') return false;
          if (isOp('(')) {
            next();
            if (tok.v === 'IF') {
              // Errors in the branch that is not chosen must not matter, so each branch is evaluated on its own.
              const attempt = fn => {
                const start = pos;
                try { return fn(); } catch (e) {
                  if (!(e instanceof Err)) throw e;
                  pos = start;
                  for (let depth = 0; pos < tokens.length; pos++) { // skip the whole branch
                    const t = tokens[pos];
                    if (t.t === 'op' && t.v === '(') depth++;
                    else if (t.t === 'op' && t.v === ')') { if (depth === 0) break; depth--; }
                    else if (t.t === 'op' && t.v === ',' && depth === 0) break;
                  }
                  return e;
                }
              };
              if (isOp(')')) throw new Err('#VALUE!');
              const cond = comparison(); let a, b;
              if (isOp(',')) { next(); a = attempt(comparison); }
              if (isOp(',')) { next(); b = attempt(comparison); }
              expect(')');
              const chosen = truthy(cond) ? (a === undefined ? true : a) : (b === undefined ? false : b);
              if (chosen instanceof Err) throw chosen;
              return chosen;
            }
            if (!FUNCTIONS[tok.v]) { args(); throw new Err('#NAME?'); }
            return FUNCTIONS[tok.v](args());
          }
          throw new Err('#NAME?');
        }
        throw new Err('#VALUE!');
      }
      const result = comparison();
      if (pos < tokens.length) throw new Err('#VALUE!');
      return result;
    }
    for (const ref of Object.keys(cells)) valueOf(ref);
    return values;
  }

  function display(value) {
    if (value === null || value === undefined) return '';
    if (value instanceof Err) return value.code;
    if (typeof value === 'boolean') return value ? 'TRUE' : 'FALSE';
    if (typeof value === 'number') return Number.isFinite(value) ? String(Number(value.toPrecision(10))) : '#VALUE!';
    return String(value);
  }

  function plain(values) {
    const out = {};
    for (const [ref, v] of Object.entries(values)) out[ref] = v instanceof Err ? v.code : v;
    return out;
  }

  const api = { compute, display, plain, colNumber, colLetters, Err };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.SageSheet = api;
})(typeof window !== 'undefined' ? window : globalThis);
