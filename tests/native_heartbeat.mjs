import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/slack_conductor/conductor/frontend.js', import.meta.url), 'utf8')
  .replace('__CONFIG__', JSON.stringify({base: 'http://localhost/token', generation: 'g', release: 12000, bindings: {}}))
  .replace('await import(script.src)', 'testExports');

const timers = new Set();
const install = () => {
  const state = {clock: 1e9, mode: 'ok', beats: []};
  const composer = {
    state: {state: {sessions: {}}},
    watchSession: async () => ({cancel() {}}), refresh: async () => {},
    getDraftAttachments() {},
    async setPaused() { return {ok: true}; },
    async sendMessage() { return {ok: true}; }
  };
  const testExports = {
    workspace: {
      getWorkspace: async () => ({id: 'w', state: 'ready'}), getWorkspaces: async () => [],
      createWorkspaceWithSetup() {}, setActiveSessionId() {}, archiveWorkspace() {},
      dependencies: {repo: {getRepos: async () => []}}
    },
    session: {getSession: async () => ({}), setSessionAgentAndModel() {}, updateSessionModelConfiguration() {}},
    composer,
    sidecar: {getSessionStatus: async () => ({status: 'idle'}), query() {}, resolveToolApproval() {}}
  };
  const context = {
    testExports, console, crypto: {randomUUID: () => String(Math.random())},
    document: {scripts: [{type: 'module', src: '/assets/index-test.js'}]},
    window: {}, AbortSignal, setTimeout, clearTimeout, clearInterval,
    Date: {now: () => state.clock},
    setInterval: (fn, ms) => {const id = setInterval(fn, ms); timers.add(id); return id;},
    fetch: async (url, args) => {
      const route = url.split('/token/')[1].split('?')[0];
      const body = args?.body ? JSON.parse(args.body) : null;
      if (route === 'heartbeat' && body?.ready) {
        state.beats.push(state.mode);
        if (state.mode === 'stall') throw Error('request timed out');
        if (state.mode === 'reject') return {ok: false, status: 409, json: async () => false};
      }
      return {ok: true, json: async () => route === 'next' ? null : true};
    }
  };
  return {state, context, composer, installation: vm.runInNewContext(source, context)};
};

const until = async (predicate, message) => {
  for (let i = 0; i < 3000; i++) {
    if (predicate()) return;
    await new Promise(r => setTimeout(r, 5));
  }
  throw Error(message);
};

try {
  {
    const {state, context, composer, installation} = install();
    const originalPause = composer.setPaused;
    await installation;
    await until(() => state.beats.length > 0, 'attachment never heartbeat');
    const live = context.window.__flotilla;

    // A stalled daemon must not tear down an otherwise healthy attachment.
    state.mode = 'stall';
    const stalled = state.beats.length;
    await until(() => state.beats.length > stalled + 3, 'heartbeats stopped during the stall');
    assert.equal(context.window.__flotilla, live);
    assert.equal(live.stopped, false);
    assert.notEqual(composer.setPaused, originalPause); // interception still installed

    // Recovery rearms the window instead of leaving it primed to expire.
    state.mode = 'ok';
    await until(() => live.beatingSince === state.clock, 'heartbeat did not recover');
    state.clock += 11_000;
    state.mode = 'stall';
    const survived = state.beats.length;
    await until(() => state.beats.length > survived + 2, 'heartbeats stopped below the window');
    assert.equal(live.stopped, false);

    // Only an outage outlasting the injected release window frees the composer.
    state.clock += 2_000;
    await until(() => context.window.__flotilla === undefined, 'sustained outage never detached');
    assert.equal(live.stopped, true);
    assert.equal(composer.setPaused, originalPause);
  }
  {
    const {state, context, composer, installation} = install();
    const originalPause = composer.setPaused;
    await installation;
    await until(() => state.beats.length > 0, 'attachment never heartbeat');

    // A rejected generation means the daemon replaced this attachment: terminal.
    state.mode = 'reject';
    await until(() => context.window.__flotilla === undefined, 'superseded generation never detached');
    assert.equal(composer.setPaused, originalPause);
    assert.ok(state.clock === 1e9, 'detach must not depend on elapsed time');
  }
  console.log('heartbeat stall tolerance, window rearming, and superseded detach passed');
} finally {
  for (const timer of timers) clearInterval(timer);
}
