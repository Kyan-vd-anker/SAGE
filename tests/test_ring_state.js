// Run: node tests/test_ring_state.js
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const script = fs.readFileSync(path.join(__dirname, '..', 'sage', 'static', 'ring-state.js'), 'utf8');
const pendingMutations = [];
let observer;
let streaming = false;
let labelText = 'IDLE';
const styles = new Map();
const rootDataset = {};
const panel = {dataset: {state: 'idle'}, style: {
  getPropertyValue: name => styles.get(name) || '',
  setProperty: (name, value) => styles.set(name, value),
}};
const label = {};
Object.defineProperty(label, 'textContent', {
  get: () => labelText,
  set: value => { labelText = value; pendingMutations.push(() => observer()); },
});
const document = {
  documentElement: {dataset: rootDataset},
  body: {},
  querySelector(selector) {
    if (selector === '#ringPanel') return panel;
    if (selector === '#ringLabel') return label;
    if (selector === '.bubble.streaming') return streaming ? {} : null;
    return null;
  },
};
const window = {speechSynthesis: {}, addEventListener() {}};
const context = {
  document, window, location: {search: ''}, URLSearchParams,
  MutationObserver: class {
    constructor(callback) { observer = callback; }
    observe() {}
  },
  EventSource: class {},
  fetch: async () => ({ok: true, json: async () => ({ring: 'idle'})}),
  setInterval() {},
};
vm.runInNewContext(script, context);

function flushMutations() {
  let count = 0;
  while (pendingMutations.length && count < 10) {
    pendingMutations.shift()();
    count++;
  }
  assert.strictEqual(pendingMutations.length, 0, 'ring updates must not keep triggering the observer');
}

observer();
flushMutations();
assert.strictEqual(labelText, 'IDLE');

streaming = true;
window.setRingState('thinking');
flushMutations();
assert.strictEqual(panel.dataset.state, 'thinking');
assert.strictEqual(labelText, 'THINKING');

window.setRingState('thinking');
assert.strictEqual(pendingMutations.length, 0, 'setting the same state should not mutate the label');
console.log('ring state observer: all checks passed');
