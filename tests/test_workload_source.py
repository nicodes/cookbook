"""Validate authored workloads without reading caller secret files or interpolation."""
from source_fixture import SDK_ROOT, PREPARED_ROOT
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import deployment
import komizo_core

SOURCE='''kind="komizo.application"
version=1
application="demo"
[workload]
jobs={}
networks={private={internal=true}}
volumes={}
routes=[{host="app.example.test",path="/",target={kind="asset",name="web"}}]
readiness=[{url="https://app.example.test/",status=200}]
[workload.assets.web]
artifact="web"
serving={headers={"X-Content-Type-Options"="nosniff"}}
[workload.services.api]
artifact="api"
user="10001:10001"
environment_files=["runtime"]
environment={PUBLIC_ORIGIN="${PUBLIC_ORIGIN}"}
networks=["private"]
limits={memory_bytes=67108864,memory_swap_bytes=67108864,milli_cpus=500,pids=32}
'''


class WorkloadSource(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core=komizo_core.binary(PREPARED_ROOT,dict(os.environ))

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='native workload source ');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);(self.root/'deploy').mkdir();self.path=self.root/'deploy/application.toml'

    def validate(self,source):
        self.path.write_text(source)
        return subprocess.run([str(self.core),'application','validate',str(self.path)],capture_output=True,text=True,timeout=30)

    def test_secret_aliases_are_logical_and_literal_environment_never_reads_dotenv(self):
        for name in ['.env','deploy/runtime','deploy/secrets.env']:(self.root/name).write_text("PRIVATE_SENTINEL='unterminated\n")
        before={path.relative_to(self.root).as_posix():path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(self.validate(SOURCE).returncode,0)
        model=deployment.declaration(self.root)
        self.assertEqual(model['workload']['services']['api']['environment_files'],['runtime'])
        self.assertEqual(model['workload']['services']['api']['environment'],{'PUBLIC_ORIGIN':'${PUBLIC_ORIGIN}'})
        self.assertNotIn('PRIVATE_SENTINEL',str(model))
        after={path.relative_to(self.root).as_posix():path.read_bytes() for path in self.root.rglob('*') if path.is_file() and path!=self.path}
        self.assertEqual(before,after)

    def test_paths_dynamic_aliases_unknown_fields_and_graph_drift_fail_closed(self):
        for alias in ['/private/runtime.env','../runtime','${PRIVATE_FILE}','https://example.test/env','runtime.env']:
            with self.subTest(alias=alias):self.assertNotEqual(self.validate(SOURCE.replace('["runtime"]','["'+alias+'"]')).returncode,0)
        for source in [SOURCE.replace('networks=["private"]','networks=["missing"]'),
                       SOURCE+'privileged=true\n',SOURCE.replace('artifact="api"','artifact="../api"')]:
            with self.subTest(source=source):self.assertNotEqual(self.validate(source).returncode,0)
