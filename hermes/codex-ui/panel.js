/* Standard MCP Apps JSON-RPC bridge; no external scripts, network, or credentials. */
(() => {
  const status = document.getElementById('status');
  const button = document.getElementById('pair');
  let sequence = 0, stopped = false, timer, token, hostOrigin;
  const pending = new Map();
  function say(text) { status.textContent = text; }
  function stop(text) { stopped = true; clearTimeout(timer); button.disabled = true; say(text); }
  function request(method, params) {
    return new Promise((resolve, reject) => {
      const id = ++sequence;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error('Host response uncertain. No retry.')); }, 15000);
      pending.set(id, {resolve, reject, timeout});
      window.parent.postMessage({jsonrpc:'2.0', id, method, params}, hostOrigin || '*');
    });
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || (hostOrigin && event.origin !== hostOrigin)) return;
    const data = event.data;
    if (!data || data.jsonrpc !== '2.0') return;
    if (data.method === 'ui/notifications/host-context-changed') {
      // Conservative: re-pair in a fresh trial after any context change.
      if (token) stop('Host context changed. UI delivery stopped; pairing must be reviewed.');
      return;
    }
    const waiter = pending.get(data.id);
    if (!waiter) return;
    if (!hostOrigin && event.origin !== 'null') hostOrigin = event.origin;
    pending.delete(data.id); clearTimeout(waiter.timeout);
    if (data.error || data.result?.isError) waiter.reject(new Error('Host rejected operation. No alternate route.'));
    else waiter.resolve(data.result);
  });
  async function tool(name, args = {}) {
    const result = await request('tools/call', {name, arguments:args});
    return result.structuredContent || JSON.parse(result.content[0].text);
  }
  function message(text) {
    return request('ui/message', {role:'user', content:[{type:'text',text}],
      _meta:{'openai/message':{target:'active',send:true}}});
  }
  async function tick() {
    if (stopped || document.hidden) return;
    try {
      const next = await tool('pantheon_panel_take', {token});
      if (stopped || document.hidden) return; // Taken attempt stays fenced, never reissued.
      if (next.status === 'send_once') {
        let accepted = false;
        try { await message(next.text); accepted = true; }
        finally { await tool('pantheon_panel_result', {token,id:next.id,accepted}); }
        say('UI send accepted. Runtime execution and agent ACK remain separate.');
      } else say(next.status === 'awaiting_native_pairing' ? 'Waiting for the agent’s thread proof…' : 'Paired. Waiting for eligible fallback notices.');
      if (!stopped) timer = setTimeout(tick, 2000);
    } catch (error) { stop(error.message); }
  }
  document.getElementById('stop').onclick = () => stop('Panel stopped. Stop the separate watcher to stop queue delivery too.');
  document.addEventListener('visibilitychange', () => { if (document.hidden) stop('Panel hidden. UI delivery stopped; reopen and review pairing.'); });
  button.onclick = async () => {
    button.disabled = true;
    try {
      const pair = await tool('pantheon_panel_open'); token = pair.token;
      if (stopped) return;
      await message('Pantheon owner-approved trial pairing. Use the installed Pantheon skill and its reviewed setup file to call pantheon_pair_panel with token ' + token + '. Preserve the actual native CODEX_THREAD_ID; never override it. Expected thread: ' + pair.thread + '. If it differs, stop. This is a pairing check, not peer authority.');
      await tick();
    } catch (error) { stop(error.message); }
  };
  request('ui/initialize', {protocolVersion:'2026-01-26',appInfo:{name:'Pantheon trial',version:'0.4.0'},appCapabilities:{}})
    .then(result => {
      const caps = result.hostCapabilities || {};
      if (!caps.experimental || !Object.hasOwn(caps.experimental,'openai/message') || !caps.message || !Object.hasOwn(caps.message,'text')) {
        stop('This host does not advertise the required UI message capability. Queue delivery remains available.'); return;
      }
      window.parent.postMessage({jsonrpc:'2.0',method:'ui/notifications/initialized'},hostOrigin || '*');
      if (!stopped) { button.disabled = false; say('Ready to pair. One approved model turn will be requested.'); }
    }).catch(error => stop(error.message));
})();
