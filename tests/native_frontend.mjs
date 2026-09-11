import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const applied = [], calls = [], commands = [];
let intent = {paused: false}, release, block = false, reject = false;
const composer = {
  state: {sessions: {s: {paused: false}}},
  watchSession: async () => ({cancel() {}}), refresh: async () => {},
  getDraftAttachments() {},
  async setPaused(args) { applied.push(args.paused); return {ok: true}; },
  async sendMessage() { return {ok: true}; }
};
const original = composer.setPaused;
const testExports = {
  workspace: {
    getWorkspace: async () => ({id: 'w', state: 'ready', activeSessionId: 's'}),
    getWorkspaces: async () => [], createWorkspaceWithSetup() {},
    dependencies: {repo: {getRepos: async () => []}}
  },
  session: {getSession: async () => ({workspaceId: 'w'}), updateSessionModelConfiguration() {}},
  composer,
  sidecar: {getSessionStatus: async () => ({status: 'idle'}), query() {}, resolveToolApproval() {}}
};
const timers = new Set();
const context = {
  testExports, console, crypto: {randomUUID: () => String(Math.random())},
  document: {scripts: [{type: 'module', src: '/assets/index-test.js'}]},
  window: {}, AbortSignal, setTimeout, clearTimeout,
  setInterval: (fn, ms) => {const id = setInterval(fn, ms); timers.add(id); return id;},
  clearInterval,
  fetch: async (url, args) => {
    const route = url.split('/token/')[1].split('?')[0];
    const body = args?.body ? JSON.parse(args.body) : null;
    calls.push({route, body});
    if (route === 'pause' && body) {
      if (reject) throw Error('journal unavailable');
      intent = {paused: body.paused}; // durable before HTTP acknowledgement
      if (block) await new Promise(resolve => {release = resolve;});
      return {ok: true, json: async () => intent};
    }
    return {ok: true, json: async () => route === 'pause' ? intent : route === 'next' ? commands.shift() ?? null : true};
  }
};
const source = fs.readFileSync(new URL('../src/slack_conductor/conductor/frontend.js', import.meta.url), 'utf8')
  .replace('__CONFIG__', JSON.stringify({base: 'http://localhost/token', generation: 'g', bindings: {s: 'w'}}))
  .replace('await import(script.src)', 'testExports');
const installation = vm.runInNewContext(source, context);
const until = async predicate => {
  for (let i = 0; i < 100; i++) {if (predicate()) return; await new Promise(r => setTimeout(r, 10));}
  throw Error('condition timed out');
};
try {
  await Promise.race([
    installation,
    new Promise((_, reject) => setTimeout(() => reject(Error('bootstrap did not return')), 1000))
  ]);
  await until(() => composer.setPaused !== original);
  block = true;
  const pause = composer.setPaused({sessionId: 's', paused: true});
  await until(() => release);
  assert.equal(intent.paused, true);
  assert.deepEqual(applied, []); // killed here: intent survives, native action not acknowledged
  commands.push({id: 'restore', body: {op: 'watch', wid: 'w', sid: 's'}});
  release(); block = false;
  await pause;
  await until(() => calls.some(c => c.route === 'result' && c.body.id === 'restore'));
  assert.deepEqual(applied, [true, true]); // recovery respects latest intent
  reject = true;
  await assert.rejects(composer.setPaused({sessionId: 's', paused: false}), /attachment disconnected/);
  assert.deepEqual(applied, [true, true]);
  const retired = context.window.__flotilla;
  commands.push({id: 'shutdown', body: {op: 'detach'}});
  await until(() => calls.some(c => c.route === 'result' && c.body.id === 'shutdown'));
  assert.equal(calls.find(c => c.route === 'result' && c.body.id === 'shutdown').body.value, true);
  assert.equal(context.window.__flotilla, undefined);
  assert.equal(composer.setPaused, original);
  const replacement = () => {};
  composer.setPaused = replacement;
  retired.detach();
  assert.equal(composer.setPaused, replacement); // late heartbeat rejection cannot undo a replacement
  console.log('pause write-ahead, recovery ordering, journal failure, and detach passed');
} finally {
  context.window.__flotilla?.detach();
  for (const timer of timers) clearInterval(timer);
}
