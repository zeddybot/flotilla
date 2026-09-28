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
  // Probe initialized services, not just exported method names.
  await workspace.getWorkspaces();
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
      const w = await workspace.getWorkspace(wid), s = await session.getSession(sid);
      if (!w || !s || s.localWorkspaceId !== wid || w.state === 'archived') throw Error('Conductor target changed');
      p.bindings[sid] = wid;
      if (!p.watches[sid]) p.watches[sid] = await composer.watchSession({localWorkspaceId: wid, sessionId: sid});
      await composer.refresh({sessionId: sid});
      return s;
    } catch (error) {
      delete p.sessions[sid];
      p.watches[sid]?.cancel?.();
      delete p.watches[sid];
      throw error;
    }
  };
  async function execute(c) {
    if (c.op === 'detach') { p.detach(); return true; }
    if (c.op === 'repos') return workspace.dependencies.repo.getRepos();
    if (c.op === 'create') {
      let w;
      try { w = await workspace.getWorkspace(c.wid); } catch { /* factory not called yet */ }
      if (!w) return workspace.createWorkspaceWithSetup(c.args);
      return {workspace: w};
    }
    if (c.op === 'archive') {
      const w = await workspace.getWorkspace(c.wid);
      if (w.state === 'archived') return true;
      await bind(c.wid, c.sid);
      return workspace.archiveWorkspace({localWorkspaceId: c.wid, mode: 'safe', stopRunningAgents: true});
    }
    await bind(c.wid, c.sid);
    await lease();
    if (c.op === 'createdSession') return workspace.setActiveSessionId({localWorkspaceId: c.wid, activeSessionId: c.sid});
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
        localWorkspaceId: c.wid, sessionId: c.sid, messageId: c.message_id,
        mode: c.mode, message: c.prompt, attachmentIds: []
      });
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
    if (p.stopped || (p.resting && ++p.idle % 12)) return;
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
