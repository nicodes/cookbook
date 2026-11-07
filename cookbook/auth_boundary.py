"""Compose generic authentication checks with the canonical Cookbook Preview contract."""
import argparse
import json
import os
from pathlib import Path
import subprocess

from configuration import load, read_source_data, relative
import dependencies
import preview_workflows
import sdk_environment

SCRIPT = r'''
const [modulePath, policyText, contractText] = process.argv.slice(1);
const {checkAuthBoundaryCore, previewNamesItsOwnOrigin, sourceFiles, report} = await import(modulePath);
const fs = await import('node:fs'); const path = await import('node:path');
const root = process.cwd(); const declared = JSON.parse(policyText); const contract = JSON.parse(contractText);
const findings = checkAuthBoundaryCore(root, declared);
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
(failed?console.error:console.log)(text);process.exitCode=failed?1:0;
'''


def check(root, policy, environment=None):
    root = Path(root).resolve()
    declared = read_source_data(relative(root, policy, exists=True), maximum=65536)
    if (not isinstance(declared, dict) or set(declared) != {'api', 'provider'}
            or not isinstance(declared['provider'], str) or type(declared['api']) is not bool):
        raise ValueError('auth policy must explicitly select provider and API presence')
    contract = preview_workflows.auth_contract(root, load(root))
    module = (dependencies.tools_root() / 'helpers/auth-boundary.mjs').as_uri()
    command = ['bun', '--eval', SCRIPT, module, json.dumps(declared), json.dumps(contract)]
    values = sdk_environment.command_environment(command, root, environment or dict(os.environ))
    subprocess.run(command, cwd=root, env=values, check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    args = parser.parse_args()
    check(Path.cwd(), args.policy)
