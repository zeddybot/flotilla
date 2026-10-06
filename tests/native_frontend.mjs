import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const applied = [], calls = [], commands = [], sent = [], configured = [];
const watched = [], created = [], selected = [], archived = [];
const cancelled = [], answered = [], plans = [], approvals = [], opened = [], codexPermissions = [], elicitations = [];
const acpConfigured = [];
let intent = {paused: false}, release, block = false, reject = false;
let currentTurn = 'turn-1', questionRows = [], sessionStatus = 'working';
let losePoll = false, lostClaim;
const approvalsPending = new Map(), plansPending = new Map();
const codexPending = new Map(), elicitationPending = new Map();
const questionsState = {questions: [{question: 'Choose one', options: [{label: 'A'}]}]};
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
  session: {getSession: async sid => ({localWorkspaceId: sid === 'foreign' ? 'elsewhere' : 'w', status: sessionStatus, agentType: 'claude'}),
    getCurrentMessageTurnId: async () => currentTurn,
    getToolApproval: id => id === 'approval-1' ? {toolName: 'Bash'} : undefined,
    dependencies: {db: {select: async () => questionRows}},
    store: {getState: () => ({state: {cloudStateBySessionId: {s: {questions: questionsState}}}})},
    setSessionAgentAndModel(args) { acpConfigured.push(args); return args.model !== 'unavailable'; },
    updateSessionModelConfiguration(args) { configured.push(args); return true; }},
  composer,
  sidecar: {getSessionStatus: async () => ({status: 'idle'}), query() {},
    pendingToolApprovals: approvalsPending, pendingExitPlanModeApprovals: plansPending,
    pendingCodexPermissionApprovals: codexPending, pendingCodexMcpElicitations: elicitationPending,
    async resolveToolApproval(args) {approvals.push(args); approvalsPending.delete(args.toolUseId);},
    async resolveCodexPermissionApproval(args) {codexPermissions.push(args); codexPending.delete(args.sessionId);},
    async resolveCodexMcpElicitation(args) {elicitations.push(args); elicitationPending.delete(args.sessionId);}},
  control: {getInitOptions() {}, async cancelSession(args) {cancelled.push(args);},
    async sendAskUserQuestionResponse(...args) {answered.push(args);},
    async resolveExitPlanMode(args) {plans.push(args); plansPending.delete(args.sessionId);}}
};
const timers = new Set();
const context = {
  testExports, console, crypto: {randomUUID: () => String(Math.random())},
  document: {scripts: [{type: 'module', src: '/assets/index-test.js'}]},
  window: {__TAURI_INTERNALS__: {invoke: async (...args) => {opened.push(args);}}}, AbortSignal, setTimeout, clearTimeout,
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
    if (route === 'next' && (losePoll || lostClaim)) {
      const poll = new URL(url).searchParams.get('poll');
      assert.ok(poll, 'poll requests need a retry identity');
      if (lostClaim) {
        assert.equal(poll, lostClaim.poll);
        const command = lostClaim.command;
        lostClaim = undefined;
        return {ok: true, json: async () => command};
      }
      const command = commands.shift();
      if (command) {
        losePoll = false;
        lostClaim = {poll, command};
        return {ok: true, json: async () => { throw Error('response body lost after claim'); }};
      }
    }
    return {ok: true, json: async () => route === 'pause' ? intent : route === 'next' ? commands.shift() ?? null : true};
  }
};
const source = fs.readFileSync(new URL('../src/slack_conductor/conductor/frontend.js', import.meta.url), 'utf8')
  .replace('__CONFIG__', JSON.stringify({base: 'http://localhost/token', generation: 'g', release: 30000, bindings: {s: 'w', foreign: 'w'}}))
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
  losePoll = true;
  assert.equal((await dispatch('configure', {op: 'configure', wid: 'w', sid: 's', configuration})).value, true);
  assert.equal(lostClaim, undefined);
  assert.equal(configured.length, 1);
  assert.equal(configured[0].sessionId, 's');
  assert.deepEqual(JSON.parse(JSON.stringify(configured[0].configuration)), configuration);
  const alpha = {agent: 'acp', model: 'opencode:opencode/union-alpha', fastMode: false};
  assert.equal((await dispatch('alpha', {op: 'configure', wid: 'w', sid: 's', configuration: alpha})).value, true);
  assert.deepEqual(JSON.parse(JSON.stringify(acpConfigured[0])), {
    sessionId: 's', agentType: 'acp', model: 'opencode:opencode/union-alpha'
  });
  assert.equal(configured.length, 1); // ACP bypasses the Claude/Codex-only configuration validator.
  assert.match((await dispatch('alpha-failed', {
    op: 'configure', wid: 'w', sid: 's', configuration: {...alpha, model: 'unavailable'}
  })).error, /selection did not apply/);
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
  assert.equal((await dispatch('reveal', {op: 'reveal', wid: 'w', sid: 's'})).value.url,
    'conductor://workspace?id=w&session=s');
  assert.equal(opened.length, 0);
  assert.equal((await dispatch('stale-stop', {op: 'interrupt', wid: 'w', sid: 's', turn_id: 'old'})).value.state, 'stale');
  assert.equal(cancelled.length, 0);
  assert.equal((await dispatch('stop', {op: 'interrupt', wid: 'w', sid: 's', turn_id: 'turn-1'})).value.state, 'requested');
  assert.deepEqual(JSON.parse(JSON.stringify(cancelled[0])), {sessionId: 's', turnId: 'turn-1'});
  assert.equal(intent.paused, true);
  approvalsPending.set('approval-1', {sessionId: 's'});
  const approvalList = (await dispatch('list-approval', {op: 'questions', wid: 'w', sid: 's'})).value;
  assert.equal(approvalList[0].question_id, 'approval-1');
  assert.equal(approvalList[0].prompt, 'Approve Bash tool use');
  assert.equal((await dispatch('approval', {op: 'respond', wid: 'w', sid: 's', question_id: 'approval-1',
    response: {kind: 'approval', decision: 'allow'}})).value.state, 'answered');
  assert.equal(approvals[0].toolUseId, 'approval-1');
  assert.match((await dispatch('stale-approval', {op: 'respond', wid: 'w', sid: 's', question_id: 'approval-1',
    response: {kind: 'approval', decision: 'deny'}})).error, /no longer pending/);
  questionRows = [{content: JSON.stringify({type: 'assistant', message: {content: [
    {type: 'tool_use', name: 'AskUserQuestion', id: 'question-1'}]}})}];
  sessionStatus = 'needs_user_input';
  assert.equal((await dispatch('list-question', {op: 'questions', wid: 'w', sid: 's'})).value[0].question_id,
    'question-1');
  assert.match((await dispatch('stale-question', {op: 'respond', wid: 'w', sid: 's', question_id: 'old',
    response: {kind: 'question', answers: ['A']}})).error, /question changed/);
  assert.equal((await dispatch('question', {op: 'respond', wid: 'w', sid: 's', question_id: 'question-1',
    response: {kind: 'question', answers: ['A']}})).value.state, 'answered');
  assert.equal(answered[0][0], 's');
  assert.deepEqual(JSON.parse(JSON.stringify(answered[0][1])), ['A']);
  questionRows = [{content: JSON.stringify({type: 'assistant', message: {content: [
    {type: 'tool_use', name: 'ExitPlanMode', id: 'plan-1'}]}})}];
  sessionStatus = 'needs_plan_response'; plansPending.set('s', {});
  assert.equal((await dispatch('list-plan', {op: 'questions', wid: 'w', sid: 's'})).value[0].question_id,
    'plan-1');
  assert.equal((await dispatch('plan', {op: 'respond', wid: 'w', sid: 's', question_id: 'plan-1',
    response: {kind: 'plan', approved: false}})).value.state, 'answered');
  assert.equal(plans[0].approved, false);
  codexPending.set('s', {interactionId: 'codex-1'});
  assert.equal((await dispatch('list-codex', {op: 'questions', wid: 'w', sid: 's'})).value[0].question_id,
    'codex-1');
  assert.match((await dispatch('stale-codex', {op: 'respond', wid: 'w', sid: 's', question_id: 'old',
    response: {kind: 'codex_permission', action: 'decline'}})).error, /interaction changed/);
  assert.equal((await dispatch('codex', {op: 'respond', wid: 'w', sid: 's', question_id: 'codex-1',
    response: {kind: 'codex_permission', action: 'grant', scope: 'turn',
      granted_permissions: {filesystem: {read: ['/tmp']}}}})).value.state, 'answered');
  assert.deepEqual(JSON.parse(JSON.stringify(codexPermissions[0].response)), {
    action: 'grant', scope: 'turn', grantedPermissions: {filesystem: {read: ['/tmp']}}
  });
  elicitationPending.set('s', {interactionId: 'mcp-1'});
  assert.equal((await dispatch('list-mcp', {op: 'questions', wid: 'w', sid: 's'})).value[0].question_id,
    'mcp-1');
  assert.equal((await dispatch('mcp', {op: 'respond', wid: 'w', sid: 's', question_id: 'mcp-1',
    response: {kind: 'mcp_elicitation', action: 'accept', content: {name: 'example'}}})).value.state, 'answered');
  assert.deepEqual(JSON.parse(JSON.stringify(elicitations[0].response)), {
    action: 'accept', content: {name: 'example'}
  });
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
  assert.deepEqual(applied, [true, true, true]);
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

// Modern Conductor requires its own identity class, including prototype getters.
for (const emptyInventory of [false, true]) {
  class WorkspaceId {
    static ids = new Map();
    static fromLocalId(localId) {
      if (!this.ids.has(localId)) this.ids.set(localId, new this(localId));
      return this.ids.get(localId);
    }
    constructor(localId) { this.localId = localId; }
    get serverId() { return 'server-' + this.localId; }
  }
  const workspaces = new Map();
  if (!emptyInventory) workspaces.set('w', {id: WorkspaceId.fromLocalId('w'), state: 'ready'});
  const requests = [], results = [], mutations = [], lookups = [], modernTimers = new Set();
  const target = args => {
    assert.ok(args.workspaceId instanceof WorkspaceId);
    assert.equal(args.workspaceId, WorkspaceId.fromLocalId(args.workspaceId.localId));
    assert.equal(args.workspaceId.serverId, 'server-' + args.workspaceId.localId);
    assert.equal(args.localWorkspaceId, undefined);
  };
  const modernComposer = {
    state: {state: {sessions: {}}}, getDraftAttachments() {},
    async watchSession(args) { target(args); mutations.push(['watch', args]); return {cancel() {}}; },
    async refresh() {}, async setPaused() {},
    async sendMessage(args) { target(args); mutations.push(['send', args]); return {ok: true}; }
  };
  const modernExports = {
    workspace: {
      async getWorkspaces() { return [...workspaces.values()]; },
      async getWorkspace(id) {
        assert.ok(id instanceof WorkspaceId);
        assert.equal(id, WorkspaceId.fromLocalId(id.localId));
        lookups.push(id.localId);
        return id.localId === 'wrong-workspace' ? workspaces.get('w') : workspaces.get(id.localId);
      },
      async createWorkspaceWithSetup(args) {
        // An empty installation cannot discover the class until create returns it.
        if (emptyInventory && !mutations.length) assert.equal(args.localWorkspaceId, 'requested');
        else target(args);
        mutations.push(['create', args]);
        const id = args.workspaceId ?? WorkspaceId.fromLocalId('w');
        const w = {id, state: 'ready'};
        workspaces.set(id.localId, w);
        return {workspace: w};
      },
      async setActiveSessionId(args) { target(args); mutations.push(['select', args]); return true; },
      async archiveWorkspace(args) { target(args); mutations.push(['archive', args]); return true; },
      dependencies: {repo: {getRepos: async () => []}}
    },
    session: {
      async getSession(sid) {
        return {workspaceId: WorkspaceId.fromLocalId(sid === 'foreign' ? 'elsewhere' :
          sid === 'wrong-workspace-session' ? 'wrong-workspace' : 'w'), status: 'idle'};
      },
      async getCurrentMessageTurnId() {}
    },
    modelSettings: {
      calls: [],
      async updateModelConfiguration(args) { this.calls.push(args); return true; },
      async updateAgentSelection(args) { this.calls.push(args); return args.model !== 'unavailable'; }
    },
    composer: modernComposer,
    sidecar: {async getSessionStatus() {return {status: 'idle'};}, query() {}, resolveToolApproval() {}},
    control: {getInitOptions() {}, cancelSession() {}, sendAskUserQuestionResponse() {}}
  };
  const modernContext = {
    testExports: modernExports, console, crypto: context.crypto, document: context.document,
    window: {}, AbortSignal, setTimeout, clearTimeout, clearInterval,
    setInterval(fn, ms) { const id = setInterval(fn, ms); modernTimers.add(id); return id; },
    async fetch(url, args) {
      const route = url.split('/token/')[1].split('?')[0];
      const body = args?.body ? JSON.parse(args.body) : null;
      if (route === 'result') results.push(body);
      return {ok: true, json: async () => route === 'next' ? requests.shift() ?? null :
        route === 'pause' ? {paused: false} : true};
    }
  };
  let commandId = 0;
  const dispatchModern = async body => {
    const id = String(++commandId);
    requests.push({id, body});
    await until(() => results.some(result => result.id === id));
    return results.find(result => result.id === id);
  };
  try {
    await vm.runInNewContext(source, modernContext);
    assert.ok(modernContext.window.__flotilla);
    const create = await dispatchModern({op: 'create', wid: emptyInventory ? 'requested' : 'new',
      args: {localWorkspaceId: emptyInventory ? 'requested' : 'new', title: 'Keep title'}});
    assert.equal(create.error, undefined);
    const actualId = emptyInventory ? 'w' : 'new';
    assert.equal(create.value.workspace.id.localId, actualId);
    assert.equal(mutations[0][1].title, 'Keep title');
    const retry = await dispatchModern({op: 'create', wid: actualId, args: {localWorkspaceId: actualId}});
    assert.equal(retry.error, undefined);
    assert.equal(mutations.filter(([op]) => op === 'create').length, 1);
    assert.ok(lookups.includes(actualId));
    for (const agent of ['codex', 'claude', 'acp']) {
      const configuration = {agent, model: 'test-model'};
      const configured = await dispatchModern({op: 'configure', wid: 'w', sid: 's', configuration});
      assert.equal(configured.error, undefined);
      assert.equal(configured.value, true);
      assert.equal(JSON.stringify(modernExports.modelSettings.calls.at(-1)), JSON.stringify(
        agent === 'acp' ? {sessionId: 's', agentType: 'acp', model: 'test-model'} :
          {sessionId: 's', configuration}));
    }
    const rejected = await dispatchModern({op: 'configure', wid: 'w', sid: 's',
      configuration: {agent: 'acp', model: 'unavailable'}});
    assert.match(rejected.error, /model selection did not apply/);
    for (const op of ['watch', 'createdSession', 'send', 'archive']) {
      const result = await dispatchModern({op, wid: 'w', sid: 's', message_id: 'modern',
        mode: 'steer', prompt: 'hello'});
      assert.equal(result.error, undefined, op);
    }
    for (const op of ['watch', 'select', 'send', 'archive']) {
      const args = mutations.find(([name]) => name === op)?.[1];
      assert.ok(args, op);
      assert.equal(args.workspaceId, WorkspaceId.fromLocalId('w'));
    }
    const mutationCount = mutations.length;
    for (const [wid, sid] of [['w', 'foreign'], ['wrong-workspace', 'wrong-workspace-session']]) {
      const result = await dispatchModern({op: 'send', wid, sid, message_id: 'bad', mode: 'steer', prompt: 'bad'});
      assert.match(result.error, /target changed/);
    }
    assert.equal(mutations.length, mutationCount);
    assert.equal(modernContext.window.__flotilla.watches.foreign, undefined);
  } finally {
    modernContext.window.__flotilla?.detach();
    for (const timer of modernTimers) clearInterval(timer);
  }
}
console.log('canonical workspace identity, create retry, empty inventory, and target isolation passed');
