// private-board-only SDK entrypoint. Invoked inside the existing MC outer guest, never host root.
import { randomBytes } from 'node:crypto';
import { lstatSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
// Use the same managed release selection as the installed Pi launcher, never a fallback.
const version = readFileSync(`${homedir()}/.pi/agent/install/current-version`, 'utf8').trim();
if (!version || ['.', '..'].includes(version) || !/^[0-9A-Za-z._+-]+$/.test(version)) {
  throw new Error('private-board managed Pi version refused');
}
const {
  createAgentSession, createBashTool, createExtensionRuntime,
  ModelRuntime, SessionManager, SettingsManager,
} = await import(`${homedir()}/.pi/agent/install/releases/${version}/node_modules/@earendil-works/pi-coding-agent/dist/index.js`);

process.umask(0o077); // private session/transcript/cache files; no global settings writes

const quote = s => "'" + s.replaceAll("'", "'\\''") + "'";
const inside = (path, root) => path === root || path.startsWith(root + '/');

export async function createPrivateSession(spec, options = {}) {
  const cwd = resolve(spec.cwd), privateDir = resolve(spec.private_dir);
  const agentDir = resolve(spec.agent_dir);
  if (typeof spec.write_worktree !== 'boolean' || privateDir === '/tmp'
      || inside(privateDir, cwd) || inside(agentDir, cwd)) {
    throw new Error('private-board profile scope refused');
  }
  // /tmp is ONLY the outer guest's owned evidence projection. Mask private
  // child mounts AFTER binding it; otherwise recursive binding can expose them.
  const argv = ['/usr/bin/bwrap', '--die-with-parent', '--unshare-all',
    '--cap-drop', 'ALL', '--ro-bind', '/', '/', '--bind', '/tmp', '/tmp',
    '--tmpfs', '/proc', '--tmpfs', `${homedir()}/.pi`, '--tmpfs', privateDir,
    spec.write_worktree ? '--bind' : '--ro-bind', cwd, cwd,
    '--tmpfs', agentDir, '--dev', '/dev'];
  // Exact controller-produced disclosure-safe review inputs, not a private-dir
  // exception. Keep legacy prompt paths; only these regular bounded files reopen.
  for (const name of ['qa-evidence.json', 'qa-changes.diff', 'acceptance-evidence.json']) {
    const path = privateDir + '/' + name;
    let stat;
    try { stat = lstatSync(path); } catch (error) {
      if (error.code === 'ENOENT') continue;
      throw error;
    }
    if (!stat.isFile() || stat.size > 2 * 1024 * 1024) throw new Error('private-board review input refused');
    argv.push('--ro-bind', path, path);
  }
  argv.push('--clearenv');
  const env = { PATH: '/usr/bin:/bin', LANG: 'C.UTF-8', TMPDIR: '/tmp',
    PYTHONDONTWRITEBYTECODE: '1', XDG_CACHE_HOME: '/tmp/.cache',
    AUTH_MODE: 'local', LOCAL_AUTH_TOKEN: randomBytes(32).toString('hex'),
    BASE_URL: 'http://localhost:8000', // synthetic app-import token; no deployment accepts it
    // /proc is masked, so a trimmed Go binary cannot locate its own root; builds stay offline.
    GOROOT: '/usr/lib/go', GOPROXY: 'off', GOFLAGS: '-mod=mod', GOTOOLCHAIN: 'local',
    GOCACHE: '/tmp/.cache/go-build' };
  for (const [key, value] of Object.entries(env)) argv.push('--setenv', key, value);
  argv.push('--chdir', cwd, '--', '/bin/bash', '--noprofile', '--norc', '-c');
  const bash = createBashTool(cwd, {
    spawnHook: ({ command, cwd, env }) => ({
      command: [...argv, command].map(quote).join(' '), cwd, env,
    }),
  });
  const resources = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => 'Execute the authorized disclosure-safe task. Bash is isolated; no network or provider credentials. Record permitted findings, plan, implementation and checks.',
    getSystemPromptSource: () => undefined, getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [], extendResources: () => {}, reload: async () => {},
  };
  const sessionManager = SessionManager.create(cwd, spec.session_dir, { id: spec.session_id });
  const { session } = await createAgentSession({ ...options, cwd, agentDir,
    sessionManager, resourceLoader: resources, noTools: true,
    customTools: [bash] });
  try {
    if (JSON.stringify(session.getActiveToolNames()) !== '["bash"]') {
      throw new Error('private-board required tool missing');
    }
    const command = `test ! -e /proc/self/status && test ! -e ${quote(agentDir + '/auth.json')} && test ! -e ${quote(privateDir + '/out.raw')} && printf PRIVATE_NAMESPACE_READY`;
    const result = await session.agent.state.tools[0].execute('private-startup', { command, timeout: 10 });
    const text = result.content.filter(x => x.type === 'text').map(x => x.text).join('');
    if (result.isError || text !== 'PRIVATE_NAMESPACE_READY') throw new Error('private-board namespace unavailable');
    return session;
  } catch (error) {
    session.dispose();
    throw error; // never prompt or retry an ordinary tool on startup failure
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let session;
  try {
    const spec = JSON.parse(process.argv[2]);
    const modelRuntime = await ModelRuntime.create({
      authPath: spec.agent_dir + '/auth.json', modelsPath: spec.agent_dir + '/models.json',
      refreshOnCreate: false, allowModelNetwork: false,
    });
    const model = modelRuntime.getModel(spec.provider, spec.model);
    if (modelRuntime.getError() || !model) throw new Error('private-board selected model unavailable');
    // Only the already prepared trusted runtime settings, not project/global discovery.
    const settings = spec.settings;
    session = await createPrivateSession(spec, { modelRuntime, model,
      thinkingLevel: spec.thinking, settingsManager: SettingsManager.inMemory({ ...settings, cacheWarming: 'off' }) });
    console.log(JSON.stringify(session.sessionManager.getHeader()));
    session.subscribe(event => {
      // Completed messages retain usage; omit growing snapshots/partial tool output.
      if (!['message_update', 'tool_execution_update'].includes(event.type)) console.log(JSON.stringify(event));
    });
    // argv[3] only for a runner started before prompt_file existed.
    await session.prompt(spec.prompt_file ? readFileSync(spec.prompt_file, 'utf8') : process.argv[3]);
  } catch {
    console.error('private-board tool-profile failed closed');
    process.exitCode = 78;
  } finally { session?.dispose(); }
}
