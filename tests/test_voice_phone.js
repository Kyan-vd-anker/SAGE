// Run: node tests/test_voice_phone.js   (checks the phone page's script and its pure helper functions)
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const py = fs.readFileSync(path.join(__dirname, '..', 'sage', 'voice_phone.py'), 'utf8');
const script = py.split('<script>')[1].split('</script>')[0];
new vm.Script(script);                                              // the whole page script must at least parse
const helpers = script.split('// <helpers>')[1].split('// </helpers>')[0];
const ctx = {}; vm.createContext(ctx); vm.runInContext(helpers + '\nthis.api={concat,rmsOf,makeResampler,toPcm16,pcmBytesToFloat};', ctx);
const {concat, rmsOf, makeResampler, toPcm16, pcmBytesToFloat} = ctx.api;
const sine = (n, rate, hz) => Float32Array.from({length: n}, (_, i) => 0.5 * Math.sin(2 * Math.PI * hz * i / rate));

// resampling in chunks must match resampling in one go (no clicks at chunk borders)
for (const [from, to] of [[48000, 16000], [16000, 48000], [44100, 16000], [16000, 44100]]) {
  const x = sine(from, from, 440), whole = makeResampler(from, to)(x), r = makeResampler(from, to);
  const parts = []; for (let i = 0; i < x.length; i += 2048) parts.push(r(x.subarray(i, i + 2048)));
  const chunked = concat(parts);
  assert(Math.abs(chunked.length - whole.length) <= 1, `length ${from}->${to}: ${chunked.length} vs ${whole.length}`);
  // the last input sample is held back until the next chunk arrives, so a second comes out a few samples short
  assert(Math.abs(whole.length - to) <= Math.ceil(to / from) + 2, `one second stays one second ${from}->${to}: ${whole.length}`);
  let worst = 0; for (let i = 0; i < Math.min(whole.length, chunked.length); i++) worst = Math.max(worst, Math.abs(whole[i] - chunked[i]));
  assert(worst < 1e-4, `chunk borders ${from}->${to}: ${worst}`);
  let jump = 0; for (let i = 1; i < chunked.length; i++) jump = Math.max(jump, Math.abs(chunked[i] - chunked[i - 1]));
  assert(jump < 0.2, `no discontinuities ${from}->${to}: ${jump}`);
}
// 16-bit PCM both ways, odd byte carried to the next chunk
const pcm = toPcm16(Float32Array.of(0, 0.5, -0.5, 1, -1, 2, -2));
assert.deepStrictEqual(Array.from(pcm), [0, 16383, -16384, 32767, -32768, 32767, -32768]);
const bytes = new Uint8Array(pcm.buffer);
const a = pcmBytesToFloat(bytes.subarray(0, 5), null), b = pcmBytesToFloat(bytes.subarray(5), a.carry);
const back = concat([a.samples, b.samples]);
assert.strictEqual(back.length, 7); assert(Math.abs(back[1] - 0.5) < 1e-3 && Math.abs(back[4] + 1) < 1e-3);
assert.strictEqual(a.carry.length, 1); assert.strictEqual(b.carry.length, 0);
assert(Math.abs(rmsOf(sine(4800, 48000, 440)) - 0.5 / Math.SQRT2) < 0.01); assert.strictEqual(rmsOf(new Float32Array(0)), 0);
console.log('phone page script: all checks passed');
