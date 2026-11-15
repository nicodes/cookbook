"""Compose owned native auth findings with generic checks and canonical Preview."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

import artifact_packet
from configuration import load, read_source_data, relative
import dependencies
import preview_workflows
import sdk_environment

SCRIPT = r'''
const [modulePath, policyText, contractText, checkerText, outputFormat] = process.argv.slice(1);
const {checkAuthBoundaryCore, previewNamesItsOwnOrigin, sourceFiles, report} = await import(modulePath);
const fs = await import('node:fs'); const path = await import('node:path');
const {spawnSync} = await import('node:child_process');
const {createHash} = await import('node:crypto');
const root = process.cwd(); const declared = JSON.parse(policyText); const contract = JSON.parse(contractText);
const checker = JSON.parse(checkerText);
let native;
if (checker !== null) {
  if (!checker || typeof checker !== 'object' || Array.isArray(checker) ||
      Object.keys(checker).sort().join(',') !== 'path,sha256' || typeof checker.path !== 'string' ||
      typeof checker.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(checker.sha256)) {
    throw new Error('native auth checker identity is invalid');
  }
  const relative = path.relative(root, checker.path);
  if (path.isAbsolute(relative) || relative.startsWith('..' + path.sep) || relative === '..') {
    throw new Error('native auth checker must remain in its owning checkout');
  }
  const stat = fs.lstatSync(checker.path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 1048576 ||
      createHash('sha256').update(fs.readFileSync(checker.path)).digest('hex') !== checker.sha256) {
    throw new Error('native auth checker differs from its admitted source');
  }
  const child = spawnSync(process.execPath, [checker.path, '--native'], {
    cwd: root, env: process.env, encoding: 'utf8', maxBuffer: 1048576, timeout: 30000, killSignal: 'SIGKILL',
  });
  if (child.error || child.signal || ![0, 1].includes(child.status) || child.stderr !== '' ||
      typeof child.stdout !== 'string') throw new Error('native auth checker did not complete with bounded JSON');
  let values;
  try { values = JSON.parse(child.stdout); }
  catch { throw new Error('native auth checker returned invalid JSON'); }
  if (!Array.isArray(values) || JSON.stringify(values) !== child.stdout.trim() ||
      child.status !== Number(values.some(value => value?.level === 'fail'))) {
    throw new Error('native auth checker status or canonical JSON differs from its findings');
  }
  native = {nativeFindings: values};
}
const findings = checkAuthBoundaryCore(root, declared, native);
if (declared.provider === 'clerk') {
  if (contract.present === false) {
    const files = sourceFiles(root, {within:['api','app','src','scripts','golang_api','web','e2e'],
      extensions:['.go','.ts','.tsx','.js','.mjs','.cjs','.svelte','.gd']});
    findings.push(...previewNamesItsOwnOrigin(root, files, declared));
  } else if (contract.present !== true || contract.origin_layout !== 'subdomain' || !Array.isArray(contract.origin_variables)) {
    findings.push({level:'fail',text:'the cookbook Preview must retain the explicit development-tenant and PR-origin contract'});
  } else if (declared.api !== true) {
    findings.push({level:'fail',text:'this native Preview declaration requires its real authenticated API boundary'});
  } else {
    const server = sourceFiles(root, {within:['api'],extensions:['.rs','.go']})
      .map(file=>fs.readFileSync(path.join(root,file),'utf8')).join('\n');
    const missing = contract.origin_variables.filter(name=>typeof name !== 'string'||!server.includes(name));
    const preview = missing.map(name=>({level:'fail',text:'the declared Preview origin carrier does not reach native API configuration: '+name}));
    if (!/\bazp\b|AuthorizedPartyHandler|authorizedPart/i.test(server)) preview.push({level:'fail',text:'the native Preview API must retain its token authorized-party decision'});
    if (!preview.length) preview.push({level:'info',text:'the canonical trusted-main Preview caller retains readonly full validation, a development tenant and host-resolved PR origins'});
    findings.push(...preview);
  }
}
const {text,failed}=report(process.env.GITHUB_REPOSITORY??'local checkout',findings,true);
if (outputFormat === 'json') console.log(JSON.stringify(findings));
else (failed?console.error:console.log)(text);
process.exitCode=failed?1:0;
'''


def check(root, policy, environment=None, *, output_format='text'):
    if output_format not in {'text', 'json'}:
        raise ValueError('auth output format must be text or json')
    root = Path(root).resolve()
    declared = read_source_data(relative(root, policy, exists=True), maximum=65536)
    if (not isinstance(declared, dict) or not isinstance(declared.get('provider'), str)
            or type(declared.get('api')) is not bool):
        raise ValueError('auth policy must explicitly select provider and API presence')
    required = declared['provider'] == 'clerk' and declared['api']
    expected = {'api', 'provider', 'native_checker'} if required else {'api', 'provider'}
    if set(declared) != expected:
        raise ValueError('Clerk API auth policy requires its explicit native_checker; other policies select only provider and API presence')
    contract = preview_workflows.auth_contract(root, load(root))
    native = native_checker(root, declared['native_checker']) if required else None
    module = (dependencies.tools_root() / 'helpers/auth-boundary.mjs').as_uri()
    command = ['bun', '--eval', SCRIPT, module, json.dumps(declared), json.dumps(contract), json.dumps(native)]
    if output_format == 'json':
        command.append('json')
    values = sdk_environment.command_environment(command, root, environment or dict(os.environ))
    subprocess.run(command, cwd=root, env=values, check=True)


def native_checker(root, name):
    if (not isinstance(name, str) or not name.startswith('tests/') or not name.endswith('.mjs')
            or Path(name).as_posix() != name):
        raise ValueError('native auth checker must name an owned tests/ JavaScript module')
    source = relative(root, name, exists=True)
    tracked = subprocess.run(['git', '--literal-pathspecs', '-c', 'core.fsmonitor=false', 'ls-files', '--error-unmatch', '--', name],
        cwd=root, capture_output=True, timeout=15)
    if tracked.returncode:
        raise ValueError('native auth checker must be tracked source')
    body = artifact_packet.read_regular(source, 1 << 20)
    return {'path': str(source), 'sha256': hashlib.sha256(body).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args()
    check(Path.cwd(), args.policy, output_format=args.format)
