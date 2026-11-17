// Run: node tests/test_chat_rendering.js
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '..', 'sage', 'static', 'app.js'), 'utf8');
const markup = source.match(/function assistantMarkup\(text\)\{[^\n]*\}/);
const stripThinking = source.match(/function stripPrivateThinking\(text\)\{[^}]*\}/);
assert.ok(markup, 'assistant markup renderer should exist');
assert.ok(stripThinking, 'private-thinking filter should exist');

const context = {esc: value => String(value ?? '')};
vm.runInNewContext(
  `${markup[0]}\n${stripThinking[0]}\nconst render = text => assistantMarkup(stripPrivateThinking(text));`,
  context,
);
assert.strictEqual(
  vm.runInNewContext("render('<think>private</think>visible')", context),
  'visible',
);
console.log('chat rendering: all checks passed');
