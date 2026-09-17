import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const applied = [], calls = [], commands = [], sent = [], configured = [];
const watched = [], created = [], selected = [], archived = [];
let intent = {paused: false}, release, block = false, reject = false;
const composer = {
  state: {state: {sessions: {s: {paused: false}}}},
  watchSession: async args => {watched.push(args); return {cancel() {}};}, refresh: async () => {},
  getDraftAttachments() {},
  async setPaused(args) { applied.push(args.paused); return {ok: true}; },
  async sendMessage(args) { sent.push(args); return {ok: true}; }
};
const original = composer.setPaused;
const testExports = {
  workspace: {
    getWorkspace: async wid => wid === 'new' ? undefined : ({id: 'remote-w', localId: 'w', state: 'ready', activeSessionId: 's'}),
    getWorkspaces: async () => [],
    createWorkspaceWithSetup(args) {created.push(args); return true;},
    setActiveSessionId(args) {selected.push(args); return true;},
    archiveWorkspace(args) {archived.push(args); return true;},
    dependencies: {repo: {getRepos: async () => []}}
  },
  session: {getSession: async sid => ({localWorkspaceId: sid === 'foreign' ? 'elsewhere' : 'w'}),
    updateSessionModelConfiguration(args) { configured.push(args); return true; }},
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
  .replace('__CONFIG__', JSON.stringify({base: 'http://localhost/token', generation: 'g', bindings: {s: 'w', foreign: 'w'}}))
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
  assert.equal(watched[0].localWorkspaceId, 'w');
  assert.equal(watched[0].workspaceId, undefined);
  const dispatch = async (id, body) => {
    commands.push({id, body});
    await until(() => calls.some(c => c.route === 'result' && c.body.id === id));
    return calls.find(c => c.route === 'result' && c.body.id === id).body;
  };
  const configuration = {model: 'gpt-6-astra', agent: 'codex', thinkingLevel: 'high', fastMode: true};
  await dispatch('create', {op: 'create', wid: 'new', args: {localWorkspaceId: 'new'}});
  assert.equal(created[0].localWorkspaceId, 'new');
  await dispatch('select', {op: 'createdSession', wid: 'w', sid: 's'});
  assert.equal(selected[0].localWorkspaceId, 'w');
  assert.equal(selected[0].workspaceId, undefined);
  assert.equal(selected[0].activeSessionId, 's');
  assert.equal((await dispatch('configure', {op: 'configure', wid: 'w', sid: 's', configuration})).value, true);
  assert.equal(configured.length, 1);
  assert.equal(configured[0].sessionId, 's');
  assert.deepEqual(JSON.parse(JSON.stringify(configured[0].configuration)), configuration);
  for (const mode of ['queue', 'steer']) {
    const result = await dispatch(mode, {op: 'send', wid: 'w', sid: 's', message_id: mode, mode, prompt: 'hello'});
    assert.equal(result.value.ok, true);
    assert.equal(sent.at(-1).mode, mode);
    assert.equal(sent.at(-1).sessionId, 's');
    assert.equal(sent.at(-1).localWorkspaceId, 'w');
    assert.equal(sent.at(-1).workspaceId, undefined);
    assert.equal(sent.at(-1).messageId, mode);
    assert.equal(sent.at(-1).message, 'hello');
  }
  const before = sent.length;
  const invalid = await dispatch('foreign', {op: 'send', wid: 'w', sid: 'foreign', message_id: 'bad', mode: 'steer', prompt: 'wrong session'});
  assert.match(invalid.error, /target changed/);
  assert.equal(sent.length, before);
  await until(() => context.window.__flotilla.sessions.s);
  assert.equal(context.window.__flotilla.sessions.s.paused, false);
  assert.equal(context.window.__flotilla.sessions.foreign, undefined);
  assert.equal(context.window.__flotilla.watches.foreign, undefined);
  await dispatch('archive', {op: 'archive', wid: 'w', sid: 's'});
  assert.equal(archived[0].localWorkspaceId, 'w');
  assert.equal(archived[0].workspaceId, undefined);
  assert.equal(archived[0].stopRunningAgents, true);
  // Unbound desktop sessions are not intercepted or made dependent on the bridge.
  const recorded = calls.filter(c => c.route === 'pause').length;
  await composer.sendMessage({sessionId: 'desktop', message: 'local'});
  assert.equal(calls.filter(c => c.route === 'pause').length, recorded);
  assert.equal(sent.at(-1).sessionId, 'desktop');
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
  console.log('configuration, queue/steer identity, session isolation, pause recovery, and detach passed');
} finally {
  context.window.__flotilla?.detach();
  for (const timer of timers) clearInterval(timer);
}
