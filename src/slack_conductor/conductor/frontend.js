((config) => (async () => {
  await fetch(config.base + '/heartbeat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({generation: config.generation, ready: false, stage: 'bootstrap'}),
    signal: AbortSignal.timeout(5000)
  });
  const script = [...document.scripts].find(s => s.type === 'module' && s.src.includes('/assets/index-'));
  if (!script) throw Error('Conductor frontend entrypoint unavailable');
  const exports = Object.values(await import(script.src));
  const service = (...methods) => {
    const candidates = exports.filter(v => v && methods.every(k => typeof v[k] === 'function'));
    if (candidates.length !== 1) throw Error(`Conductor interface changed: ${methods.join(', ')}`);
    return candidates[0];
  };
  const workspace = service('getWorkspace', 'createWorkspaceWithSetup');
  const session = service('getSession', 'updateSessionModelConfiguration', 'setSessionAgentAndModel');
  const composer = service('watchSession', 'sendMessage', 'getDraftAttachments');
  const sidecar = service('getSessionStatus', 'query', 'resolveToolApproval');
  const control = service('getInitOptions', 'cancelSession', 'sendAskUserQuestionResponse');
  // Probe initialized services, not just exported method names.
  let workspaceIdType;
  const learnWorkspaceId = w => {
    const type = w?.id?.constructor;
    if (typeof type?.fromLocalId === 'function') workspaceIdType = type;
    return w;
  };
  for (const w of await workspace.getWorkspaces()) learnWorkspaceId(w);
  const workspaceId = wid => workspaceIdType ? workspaceIdType.fromLocalId(wid) : wid;
  const workspaceTarget = wid => workspaceIdType ? {workspaceId: workspaceId(wid)} : {localWorkspaceId: wid};
  const localWorkspaceId = w => w?.localId ?? w?.id?.localId ?? w?.id;
  const getWorkspace = async wid => learnWorkspaceId(await workspace.getWorkspace(workspaceId(wid)));
  await workspace.dependencies.repo.getRepos();
  window.__flotilla?.detach();
  const p = {stopped: false, resting: false, missed: {}, sessions: {}, bindings: {...config.bindings}, watches: {}, serial: {}};
  window.__flotilla = p;
  class Disconnected extends Error {
    // A rejected generation is terminal; an unanswered request may still be a stall.
    constructor(message, superseded) { super(message); this.superseded = superseded; }
  }
  const request = async (route, data) => {
    const response = await fetch(config.base + '/' + route +
      (data === undefined ? (route.includes('?') ? '&' : '?') + 'generation=' + config.generation : ''),
      data === undefined ? {signal: AbortSignal.timeout(5000)} : {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...data, generation: config.generation}), signal: AbortSignal.timeout(5000)
      }).catch(() => { throw new Disconnected('Conductor attachment disconnected', false); });
    if (!response.ok) throw new Disconnected('Conductor attachment disconnected', true);
    return response.json();
  };
  const serial = (sid, action) => {
    const next = (p.serial[sid] || Promise.resolve()).catch(() => {}).then(action);
    p.serial[sid] = next;
    return next;
  };
  const lease = async () => {
    if (p.stopped) throw new Disconnected('Conductor attachment replaced', true);
    await request('valid');
    if (p.stopped) throw new Disconnected('Conductor attachment replaced', true);
  };
  const originalPause = composer.setPaused;
  const originalSend = composer.sendMessage;
  const record = (sid, paused, id = crypto.randomUUID()) => request('pause', {sid, paused, request: id});
  // Write intent before the native action. Recovery uses this record, never a
  // sampled UI flag that could predate the user's stop/pause click.
  // While the daemon is away the user's own actions go straight through, and the
  // latest one per session is kept for the journal to learn on return.
  composer.setPaused = function (args) {
    if (!p.bindings[args.sessionId]) return originalPause.call(this, args);
    if (p.resting) { p.missed[args.sessionId] = args.paused; return originalPause.call(this, args); }
    return serial(args.sessionId, async () => {
      await record(args.sessionId, args.paused);
      await lease();
      return originalPause.call(this, args);
    });
  };
  composer.sendMessage = function (args) {
    if (!p.bindings[args.sessionId]) return originalSend.call(this, args);
    if (p.resting) { p.missed[args.sessionId] = false; return originalSend.call(this, args); }
    return serial(args.sessionId, async () => {
      await record(args.sessionId, false);
      await lease();
      return originalSend.call(this, args);
    });
  };
  const bind = async (wid, sid) => {
    try {
      const w = await getWorkspace(wid), s = await session.getSession(sid);
      const owner = s?.workspaceId?.localId ?? s?.localWorkspaceId;
      if (!w || !s || localWorkspaceId(w) !== wid || owner !== wid || w.state === 'archived') throw Error('Conductor target changed');
      p.bindings[sid] = wid;
      if (!p.watches[sid]) p.watches[sid] = await composer.watchSession({...workspaceTarget(wid), sessionId: sid});
      await composer.refresh({sessionId: sid});
      return s;
    } catch (error) {
      delete p.sessions[sid];
      p.watches[sid]?.cancel?.();
      delete p.watches[sid];
      throw error;
    }
  };
  const latestQuestion = async sid => {
    const rows = await session.dependencies.db.select(
      "SELECT content FROM session_messages WHERE session_id = ? AND role = 'assistant' ORDER BY rowid DESC LIMIT 100",
      [sid]
    );
    for (const row of rows) {
      let value;
      try { value = JSON.parse(row.content); } catch { continue; }
      if (value?.type !== 'assistant' || value.parent_tool_use_id) continue;
      for (const block of [...(value.message?.content || [])].reverse()) {
        if (block?.type === 'tool_use' && ['AskUserQuestion', 'ExitPlanMode'].includes(block.name)) {
          return {id: block.id, name: block.name};
        }
      }
    }
    return null;
  };
  async function execute(c) {
    if (c.op === 'detach') { p.detach(); return true; }
    if (c.op === 'repos') return workspace.dependencies.repo.getRepos();
    if (c.op === 'create') {
      let w;
      try { w = await getWorkspace(c.wid); } catch { /* factory not called yet */ }
      if (!w) {
        const {localWorkspaceId: ignoredLocalId, workspaceId: ignoredId, ...args} = c.args;
        const result = await workspace.createWorkspaceWithSetup({...args, ...workspaceTarget(c.wid)});
        learnWorkspaceId(result?.workspace);
        return result;
      }
      if (localWorkspaceId(w) !== c.wid) throw Error('Conductor target changed');
      return {workspace: w};
    }
    if (c.op === 'archive') {
      const w = await getWorkspace(c.wid);
      if (!w || localWorkspaceId(w) !== c.wid) throw Error('Conductor target changed');
      if (w.state === 'archived') return true;
      await bind(c.wid, c.sid);
      return workspace.archiveWorkspace({...workspaceTarget(c.wid), mode: 'safe', stopRunningAgents: true});
    }
    await bind(c.wid, c.sid);
    await lease();
    if (c.op === 'createdSession') return workspace.setActiveSessionId({...workspaceTarget(c.wid), activeSessionId: c.sid});
    if (c.op === 'configure') {
      if (c.configuration.agent === 'acp') {
        const changed = await session.setSessionAgentAndModel({
          sessionId: c.sid, agentType: 'acp', model: c.configuration.model
        });
        if (!changed) throw Error('Conductor model selection did not apply');
        return true;
      }
      return session.updateSessionModelConfiguration({sessionId: c.sid, configuration: c.configuration});
    }
    if (c.op === 'watch') return serial(c.sid, async () => {
      const intent = await request('pause?sid=' + encodeURIComponent(c.sid));
      // No observation means no authority to resume a pre-existing queue.
      await lease();
      if (intent) await originalPause.call(composer, {sessionId: c.sid, paused: !!intent.paused});
      return true;
    });
    if (c.op === 'send') return serial(c.sid, async () => {
      const intent = await record(c.sid, false, 'send:' + c.message_id);
      if (intent.paused) return {deferred: true};
      await lease();
      return originalSend.call(composer, {
        ...workspaceTarget(c.wid), sessionId: c.sid, messageId: c.message_id,
        mode: c.mode, message: c.prompt, attachmentIds: []
      });
    });
    if (c.op === 'reveal') return serial(c.sid, async () => {
      const url = 'conductor://workspace?id=' + encodeURIComponent(c.wid) +
        '&session=' + encodeURIComponent(c.sid);
      return {url};
    });
    if (c.op === 'questions') return serial(c.sid, async () => {
      const state = await session.getSession(c.sid);
      const pending = [];
      for (const [id, approval] of sidecar.pendingToolApprovals || []) {
        if (approval.sessionId !== c.sid) continue;
        const tool = session.getToolApproval(id);
        pending.push({question_id: id, kind: 'approval',
          prompt: tool?.toolName ? 'Approve ' + tool.toolName + ' tool use' : 'Approve tool use',
          choices: ['allow', 'deny']});
      }
      const codex = sidecar.pendingCodexPermissionApprovals?.get(c.sid);
      if (codex) pending.push({question_id: codex.interactionId, kind: 'codex_permission',
        prompt: 'Review Codex MCP permissions in Conductor', choices: ['grant', 'decline']});
      const mcp = sidecar.pendingCodexMcpElicitations?.get(c.sid);
      if (mcp) pending.push({question_id: mcp.interactionId, kind: 'mcp_elicitation',
        prompt: 'Complete MCP request in Conductor', choices: ['accept', 'decline']});
      if (state.status === 'needs_user_input' || state.status === 'needs_plan_response') {
        const latest = await latestQuestion(c.sid);
        if (latest?.name === 'AskUserQuestion') {
          const questions = session.store.getState().state.cloudStateBySessionId[c.sid]?.questions?.questions;
          if (Array.isArray(questions) && questions.length) pending.push({question_id: latest.id,
            kind: 'question', prompt: questions.map(q => q.question).join(' / '), questions});
        } else if (latest?.name === 'ExitPlanMode' && sidecar.pendingExitPlanModeApprovals?.has(c.sid)) {
          pending.push({question_id: latest.id, kind: 'plan', prompt: 'Review the plan in Conductor',
            choices: ['approve', 'reject']});
        }
      }
      return pending;
    });
    if (c.op === 'interrupt') return serial(c.sid, async () => {
      const current = await session.getCurrentMessageTurnId({sessionId: c.sid});
      if (!current || current !== c.turn_id) return {state: 'stale'};
      await record(c.sid, true, 'interrupt:' + c.turn_id);
      await lease();
      await originalPause.call(composer, {sessionId: c.sid, paused: true});
      const before = await session.getCurrentMessageTurnId({sessionId: c.sid});
      if (before !== c.turn_id) return {state: 'stale'};
      await control.cancelSession({sessionId: c.sid, turnId: c.turn_id});
      return {state: 'requested', turn_id: c.turn_id};
    });
    if (c.op === 'respond') return serial(c.sid, async () => {
      const {question_id: id, response} = c;
      if (response.kind === 'codex_permission' || response.kind === 'mcp_elicitation') {
        const permission = response.kind === 'codex_permission';
        const pending = permission ? sidecar.pendingCodexPermissionApprovals?.get(c.sid) :
          sidecar.pendingCodexMcpElicitations?.get(c.sid);
        if (!pending || pending.interactionId !== id) throw Error('Conductor interaction changed');
        const answer = permission ? response.action === 'grant' ? {
          action: 'grant', scope: response.scope, grantedPermissions: response.granted_permissions
        } : {action: 'decline'} : response.action === 'accept' ? {
          action: 'accept', ...(response.content === undefined ? {} : {content: response.content})
        } : {action: 'decline'};
        if (permission) await sidecar.resolveCodexPermissionApproval({sessionId: c.sid, response: answer});
        else await sidecar.resolveCodexMcpElicitation({sessionId: c.sid, response: answer});
        return {state: 'answered', question_id: id};
      }
      if (response.kind === 'approval') {
        const pending = sidecar.pendingToolApprovals?.get(id);
        if (!pending || pending.sessionId !== c.sid) throw Error('Conductor approval is no longer pending for this session');
        await sidecar.resolveToolApproval({toolUseId: id, decision: response.decision});
        return {state: 'answered', question_id: id};
      }
      const latest = await latestQuestion(c.sid);
      const expected = response.kind === 'plan' ? 'ExitPlanMode' : 'AskUserQuestion';
      if (latest?.id !== id || latest.name !== expected) throw Error('Conductor question changed');
      const current = await session.getSession(c.sid);
      const status = response.kind === 'plan' ? 'needs_plan_response' : 'needs_user_input';
      if (current.status !== status) throw Error('Conductor question is no longer pending');
      if (response.kind === 'plan') {
        if (!sidecar.pendingExitPlanModeApprovals?.has(c.sid)) throw Error('Conductor plan is no longer pending');
        await control.resolveExitPlanMode({sessionId: c.sid, approved: response.approved,
          feedback: response.feedback, isCodexSession: current.agentType === 'codex'});
      } else {
        const questions = session.store.getState().state.cloudStateBySessionId[c.sid]?.questions?.questions;
        if (!Array.isArray(questions) || !questions.length) throw Error('Conductor question is unavailable');
        if (response.answers.length !== questions.length) throw Error('Conductor answer count changed');
        await control.sendAskUserQuestionResponse(c.sid, response.answers, questions);
      }
      return {state: 'answered', question_id: id};
    });
    throw Error('Unsupported Conductor bridge operation');
  }
  p.detach = () => {
    if (p.stopped) return;
    p.stopped = true;
    clearInterval(p.heartbeatTimer); clearInterval(p.statusTimer);
    composer.setPaused = originalPause; composer.sendMessage = originalSend;
    for (const w of Object.values(p.watches)) w.cancel?.();
    if (window.__flotilla === p) delete window.__flotilla;
  };
  p.rest = () => { if (!p.stopped) p.resting = true; };
  const resume = () => {
    if (p.stopped || !p.resting) return;
    p.resting = false;
    for (const [sid, paused] of Object.entries(p.missed)) {
      delete p.missed[sid];
      serial(sid, () => record(sid, paused)).catch(() => { p.missed[sid] ??= paused; });
    }
  };
  p.statusTimer = setInterval(async () => {
    if (p.stopped || p.resting || p.reading) return;
    p.reading = true;
    try {
      await Promise.all(Object.keys(p.watches).map(async sid => {
        try {
          const live = await sidecar.getSessionStatus({id: sid});
          if (p.watches[sid]) p.sessions[sid] = {live, paused: composer.state.state.sessions[sid]?.paused, at: Date.now() / 1000};
        } catch { delete p.sessions[sid]; }
      }));
    } finally { p.reading = false; }
  }, 1000);
  // Heartbeats overlap under load, so an outage is measured from the last
  // acknowledgement rather than counted in failures. An outage past the release
  // window, or a daemon that says it is shutting down, rests this attachment: it
  // stops intercepting and keeps probing, so the daemon that answers next resumes
  // it without a bootstrap that would foreground Conductor.
  p.beatingSince = Date.now();
  p.idle = 0;
  p.heartbeatTimer = setInterval(() => {
    if (p.stopped || (p.resting && ++p.idle % 5)) return;
    request('heartbeat', {ready: true, sessions: p.sessions}).then(
      reply => {
        if (reply?.rest) return p.rest();
        p.beatingSince = Date.now();
        resume();
      },
      error => {
        if (error.superseded) p.detach();
        else if (Date.now() - p.beatingSince >= config.release) p.rest();
      }
    );
  }, 400);
  // The Inspector evaluation must finish once installed; returning the lifetime
  // promise can hold subsequent console evaluations behind this attachment.
  void (async () => {
  while (!p.stopped) {
    if (p.resting) { await new Promise(r => setTimeout(r, 500)); continue; }
    try {
      const c = await request('next');
      if (c) {
        let result;
        try {
          if (p.stopped) break;
          await request('valid');
          result = {value: await execute(c.body)};
        } catch (e) { result = e instanceof Disconnected ? {retry: true} : {error: String(e)}; }
        // Detach stops polling, but its completion still needs an acknowledgement.
        do {
          try { await request('result', {id: c.id, ...result}); break; }
          catch { if (!p.stopped) await new Promise(r => setTimeout(r, 500)); }
        } while (!p.stopped);
      }
    } catch { /* daemon reconnects with a new attachment lease */ }
    await new Promise(r => setTimeout(r, 100));
  }
  })();
})().catch(e => {
  console.error('Conductor Slack attachment failed:', String(e));
  return fetch(config.base + '/heartbeat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({generation: config.generation, ready: false, error: String(e)}),
    signal: AbortSignal.timeout(5000)
  }).catch(() => {});
}))(__CONFIG__);
