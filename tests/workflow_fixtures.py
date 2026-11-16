"""Read generated workflows with the prepared, locked source parser."""
import json
import subprocess
from source_fixture import parser_root
import source_policy

def read_yaml(body):
    parser = source_policy.interpreter(parser_root())
    return json.loads(subprocess.check_output([parser, '-I', '-c',
        'import json,sys,yaml;print(json.dumps(yaml.safe_load(sys.stdin.read())))'],
        input=body, text=True, timeout=30))
