import {spawn, execFileSync} from 'node:child_process';

// Use the system-check runner's environment, including its installed PDF tools.
// Standalone invocations retain the demo's PEP 723 dependency provisioning.
export function fixturePython() {
  return process.env.PYTHON
    ? {command: process.env.PYTHON, prefix: []}
    : {command: 'uv', prefix: ['run', 'python']};
}

export function runFixturePython(args, options = {}) {
  const {command, prefix} = fixturePython();
  return execFileSync(command, [...prefix, ...args], options);
}

export async function startDemo({root, stateDir, scenario, timeout = 60000}) {
  const args = ['scripts/offline_system_demo.py', '--interactive', '--state-dir', stateDir, '--port', '0'];
  if (scenario) args.push('--scenario', scenario);
  const child = process.env.PYTHON
    ? spawn(process.env.PYTHON, ['-u', ...args], {cwd: root, stdio: ['ignore', 'pipe', 'pipe']})
    : spawn('uv', ['run', ...args], {cwd: root, stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env, PYTHONUNBUFFERED: '1'}});
  let stderr = '', stdout = '', exit = null;
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-64000); });
  child.on('exit', (code, signal) => { exit = {code, signal}; });
  const diagnostics = () => ({exit, stdout, stderr});
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill('SIGTERM'); reject(new Error(`Demo startup timed out: ${stderr}`)); }, timeout);
    const fail = message => {clearTimeout(timer); reject(new Error(message));};
    child.once('error', error => fail(`Cannot launch demo: ${error.message}`));
    child.once('exit', (code, signal) => fail(`Demo exited before readiness (${code}, ${signal}): ${stderr}`));
    child.stdout.on('data', chunk => {
      stdout = (stdout + chunk).slice(-64000);
      for (const line of stdout.split('\n')) {
        try {
          const info = JSON.parse(line);
          if (info.dashboard_url) { clearTimeout(timer); resolve(info); }
        } catch {}
      }
    });
  });
  return {process: child, url: ready.dashboard_url, ready, diagnostics};
}
