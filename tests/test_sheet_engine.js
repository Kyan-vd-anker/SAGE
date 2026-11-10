// Run with: node tests/test_sheet_engine.js
const assert = require('assert');
const path = require('path');
const S = require(path.join(__dirname, '..', 'sage', 'static', 'sheet-engine.js'));

const show = cells => Object.fromEntries(Object.entries(S.compute(cells)).map(([k, v]) => [k, S.display(v)]));

// arithmetic, precedence, percent, strings
let v = show({ A1: '2', A2: '3', A3: '=A1+A2*4', A4: '=(A1+A2)*4', A5: '=2^3^2', A6: '=-A1^2', A7: '=50%', A8: '="a"&"b"&A1', A9: '=10/4' });
assert.deepStrictEqual(v, { A1: '2', A2: '3', A3: '14', A4: '20', A5: '64', A6: '4', A7: '0.5', A8: 'ab2', A9: '2.5' });

// ranges and functions, including the Dutch ; separator
v = show({ B1: '1', B2: '2', B3: '3', B4: 'text', C1: '=SUM(B1:B4)', C2: '=AVERAGE(B1:B3)', C3: '=MIN(B1:B3)', C4: '=MAX(B1:B3)', C5: '=COUNT(B1:B4)', C6: '=ROUND(2.345;2)', C7: '=IF(B1<B2;"yes";"no")', C8: '=SUM(B1:B2;B3)' });
assert.deepStrictEqual(v, { B1: '1', B2: '2', B3: '3', B4: 'text', C1: '6', C2: '2', C3: '1', C4: '3', C5: '3', C6: '2.35', C7: 'yes', C8: '6' });

// errors
v = show({ A1: '=1/0', A2: '=FOO(1)', A3: '="x"*2', A4: '=A1+1', A5: '=Z99999999', A6: '=B1', B1: '=A6', A7: '=SQRT(-1)' });
assert.deepStrictEqual(v, { A1: '#DIV/0!', A2: '#NAME?', A3: '#VALUE!', A4: '#DIV/0!', A5: '#REF!', A6: '#CIRC!', B1: '#CIRC!', A7: '#VALUE!' });

// IF only evaluates the branch it needs; empty cells count as 0; case-insensitive names and references
v = show({ A1: '=IF(1=1;5;1/0)', A2: '=B9+1', A3: '=sum(a1:a2)', A4: '=1+', A5: '=(1', A6: '="a"="A"', A7: '=IF(0;(1/0)*2;7)', A8: '=IF(0;5;1/0)' });
assert.deepStrictEqual(v, { A1: '5', A2: '1', A3: '6', A4: '#VALUE!', A5: '#VALUE!', A6: 'TRUE', A7: '7', A8: '#DIV/0!' });

// absolute references, reversed ranges, floating point display, plain numbers and text
v = show({ A1: '0.1', A2: '0.2', A3: '=$A$1+$A$2', A4: '=SUM(A2:A1)', A5: '1e3', A6: ' 7 ', A7: '1,5', A8: '=A7*2' });
assert.deepStrictEqual(v, { A1: '0.1', A2: '0.2', A3: '0.3', A4: '0.3', A5: '1000', A6: '7', A7: '1,5', A8: '#VALUE!' });

// the plain() helper that the server receives: only values and error codes
const plain = S.plain(S.compute({ A1: '=1/0', A2: '5', A3: 'hi' }));
assert.deepStrictEqual(plain, { A1: '#DIV/0!', A2: 5, A3: 'hi' });

// column helpers
assert.strictEqual(S.colLetters(1), 'A'); assert.strictEqual(S.colLetters(27), 'AA'); assert.strictEqual(S.colNumber('AB'), 28);

console.log('sheet engine tests passed');
