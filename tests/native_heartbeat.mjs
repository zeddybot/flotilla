import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../src/slack_conductor/conductor/frontend.js', import.meta.url), 'utf8')
  .replace('__CONFIG__', JSON.stringify({base: 'http://localhost/token', generation: 'g', release: 12000, bindings: {}}))
  .replace('await import(script.src)', 'testExports');

const timers = new Set(), windows = [];
const install = () => {
  const state = {clock: 1e9, mode: 'ok', beats: [], recorded: [], polls: 0};
  const composer = {
    state: {state: {sessions: {}}},
    watchSession: async () => ({cancel() {}}), refresh: async () => {},
    getDraftAttachments() {},
    async setPaused(args) { return {applied: args.paused}; },
    async sendMessage() { return {sent: true}; }
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
        if (state.mode === 'rest') return {ok: true, json: async () => ({rest: true})};
      }
      if (route === 'pause' && body) state.recorded.push([body.sid, body.paused]);
      if (route === 'next') state.polls++;
      return {ok: true, json: async () => route === 'next' ? null : true};
    }
  };
  windows.push(context.window);
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

    // Only an outage outlasting the injected release window rests the attachment.
    state.clock += 2_000;
    await until(() => live.resting, 'sustained outage never rested the attachment');
    assert.equal(live.stopped, false);
    assert.equal(context.window.__flotilla, live);

    // Resting never blocks the user: bound sessions act at once, unrecorded.
    live.bindings.s = 'w';
    assert.deepEqual(await composer.setPaused({sessionId: 's', paused: false}), {applied: false});
    assert.deepEqual(await composer.setPaused({sessionId: 's', paused: true}), {applied: true});
    assert.deepEqual(await composer.sendMessage({sessionId: 'other'}), {sent: true});
    assert.deepEqual(state.recorded, []);

    // It probes far less often than it beats, and stops asking for commands.
    const probes = state.beats.length, polls = state.polls;
    await new Promise(r => setTimeout(r, 1500));
    assert.ok(state.beats.length - probes <= 1, 'resting attachment kept beating at full rate');
    assert.ok(state.polls - polls <= 1, 'resting attachment kept polling for commands');

    // The daemon that answers next resumes it and learns the latest missed intent.
    state.mode = 'ok';
    await until(() => !live.resting, 'returning daemon never resumed the attachment');
    await until(() => state.recorded.length > 0, 'missed pause intent never reached the journal');
    assert.deepEqual(state.recorded, [['s', true]]);
    await until(() => state.polls > polls + 1, 'resumed attachment never polled for commands');
    assert.notEqual(composer.setPaused, originalPause);
    await composer.setPaused({sessionId: 's', paused: false});
    assert.deepEqual(state.recorded, [['s', true], ['s', false]]);
  }
  {
    const {state, context, installation} = install();
    await installation;
    await until(() => state.beats.length > 0, 'attachment never heartbeat');
    const live = context.window.__flotilla;

    // A daemon that is shutting down releases the composer without waiting out a window.
    state.mode = 'rest';
    await until(() => live.resting, 'shutdown notice never rested the attachment');
    assert.equal(live.stopped, false);
    assert.ok(state.clock === 1e9, 'rest must not depend on elapsed time');
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
  console.log('heartbeat stall tolerance, rest and resume, and superseded detach passed');
} finally {
  // A resting attachment is still alive and would keep the process running.
  for (const window of windows) window.__flotilla?.detach();
  for (const timer of timers) clearInterval(timer);
}
