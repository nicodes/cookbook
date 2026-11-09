"""Standalone host bytes retain exact native source and matrix acceptance."""
from source_fixture import SDK_ROOT
from pathlib import Path
import copy
import json
import sys
import tempfile
import unittest

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import native_components
import native_extras as extras
import native_ci


class NativeCompanions(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name)
        self.config={'recipe':'rust-cli','components':{'rust':[{'name':'cli'}],'native':[
            {'name':'host','component':'cli','role':'host','binary':'host','output':'.artifacts/native-host/host_{platform}_{arch}'},
            {'name':'cli','component':'cli','role':'cli','binary':'cli','output':'.artifacts/native-cli/cli_{platform}_{arch}'}]},
            'package':{'extra_files':['host_Linux_x86_64','host_Linux_arm64'],
                'extra_file_sources':{name:{'component':'host','platform':platform} for name,platform in [
                    ('host_Linux_x86_64','Linux/x86_64'),('host_Linux_arm64','Linux/arm64')]}}}
        self.env={'COMMIT':'a'*40,'VERSION':'1.2.3','DATE':'today'}
        self.output=self.root/'dist';self.output.mkdir()
        for component,platform in extras.records(self.config).values():
            path=native_components.output(self.root,component,platform);path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(('exact reviewed host '+platform).encode())
            native_components.payload_helper().record(path,self.env['COMMIT'],self.env['VERSION'],'host',platform)

    def test_both_platform_binaries_and_raw_receipts_are_published_without_embedding(self):
        extras.materialize(self.root,self.config,self.env,self.output)
        self.assertEqual(set(path.name for path in self.output.iterdir()),set(extras.names(self.config)))
        extras.verify(self.root,self.config,self.env,self.output)
        for name,(component,platform) in extras.records(self.config).items():
            source=native_components.output(self.root,component,platform)
            self.assertEqual((self.output/name).read_bytes(),source.read_bytes())
            self.assertEqual((self.output/(name+'.json')).read_bytes(),source.with_name(source.name+'.json').read_bytes())
        self.assertEqual(native_ci.phase_names(self.config),(['host'],['cli']))
        self.assertFalse(self.config['components']['native'][1].get('needs'))

    def test_native_matrix_packet_requires_every_companion_input_and_release_member(self):
        (self.root/'Cargo.toml').write_text('[package]\nname="fixture"\nversion="1.2.3"\n')
        self.config['package'].update(binary='fixture',files=[],version_arguments=['--version'],
            version_template='{binary} {version}',native_targets=[{'platform':'Linux','arch':'x86_64','extension':'tar.gz'}])
        required,_allowed=native_ci.build_payloads(self.root,self.config,{'components':['cli'],'platform':'Linux','arch':'x86_64','extension':'tar.gz'})
        self.assertTrue(set(extras.input_paths(self.root,self.config))<=required)
        self.assertTrue({'dist/'+name for name in extras.names(self.config)}<=required)

    def test_source_version_role_platform_digest_and_dependency_substitutions_refuse_before_copy(self):
        component,platform=next(iter(extras.records(self.config).values()))
        source=native_components.output(self.root,component,platform);receipt=source.with_name(source.name+'.json');original=receipt.read_bytes()
        for change in [{'source':'b'*40},{'version':'other'},{'kind':'cli'},{'platform':'Linux/arm64'},
                       {'sha256':'f'*64},{'dependencies':{'unreviewed-payload':'c'*64}}]:
            receipt.write_text(json.dumps(json.loads(original)|change))
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'mismatch'):
                extras.materialize(self.root,self.config,self.env,self.output)
            self.assertEqual(list(self.output.iterdir()),[])
        receipt.write_bytes(original)
        source.write_bytes(b'substituted native binary')
        with self.assertRaisesRegex(ValueError,'mismatch'):extras.materialize(self.root,self.config,self.env,self.output)
        self.assertEqual(list(self.output.iterdir()),[])

    def test_collection_and_publication_recheck_both_output_and_receipt_bytes(self):
        extras.materialize(self.root,self.config,self.env,self.output)
        for name in extras.names(self.config):
            path=self.output/name;original=path.read_bytes();path.write_bytes(original+b'changed')
            with self.subTest(name=name),self.assertRaises(ValueError):extras.verify(self.root,self.config,self.env,self.output)
            path.write_bytes(original)
        self.assertEqual(len(extras.input_paths(self.root,self.config)),4)

    def test_fresh_publisher_uses_independent_matrix_receipts_without_build_staging(self):
        extras.materialize(self.root,self.config,self.env,self.output)
        proofs=[]
        for component,platform in extras.records(self.config).values():
            path=native_components.output(self.root,component,platform)
            source=path.relative_to(self.root).as_posix();system,arch=platform.split('/')
            proofs.append({'entry':{'components':['host'],'platform':system,'arch':arch},'build':{'artifacts':{
                source:extras.hashlib.sha256(path.read_bytes()).hexdigest(),
                source+'.json':extras.hashlib.sha256(path.with_name(path.name+'.json').read_bytes()).hexdigest()}}})
            path.unlink();path.with_name(path.name+'.json').unlink()
        proof={'native_matrix':{'proofs':proofs}}
        extras.verify_publication(self.root,self.config,proof,self.env['COMMIT'],self.env['VERSION'],self.output)
        for change in ['missing','duplicate','wrong-bytes','wrong-receipt']:
            bad=copy.deepcopy(proof)
            if change=='missing':bad['native_matrix']['proofs'].pop()
            elif change=='duplicate':bad['native_matrix']['proofs'].append(copy.deepcopy(proofs[0]))
            else:
                paths=bad['native_matrix']['proofs'][0]['build']['artifacts'];key=next(key for key in paths if key.endswith('.json')==(change=='wrong-receipt'));paths[key]='f'*64
            with self.subTest(change=change),self.assertRaises(ValueError):
                extras.verify_publication(self.root,self.config,bad,self.env['COMMIT'],self.env['VERSION'],self.output)
        with self.assertRaises(ValueError):extras.verify_publication(self.root,self.config,proof,'f'*40,self.env['VERSION'],self.output)

    def test_unknown_component_platform_source_fields_and_name_collisions_fail_closed(self):
        for change in [{'component':'other','platform':'Linux/x86_64'},
                       {'component':'host','platform':'other'},
                       {'component':'host','platform':'Linux/x86_64','path':'/unreviewed/binary'}]:
            config=copy.deepcopy(self.config);config['package']['extra_file_sources']['host_Linux_x86_64']=change
            with self.subTest(change=change),self.assertRaises(ValueError):extras.records(config)
        config=copy.deepcopy(self.config);config['package']['extra_files'].append('host_Linux_x86_64.json')
        with self.assertRaises(ValueError):extras.records(config)
        config=copy.deepcopy(self.config);config['package']['extra_files'][0]='../host'
        with self.assertRaises(ValueError):extras.records(config)

    def test_symlink_substitution_is_refused_for_both_native_inputs_and_release_outputs(self):
        extras.materialize(self.root,self.config,self.env,self.output)
        output=self.output/'host_Linux_x86_64';output.unlink();output.symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):extras.verify(self.root,self.config,self.env,self.output)

    def test_proxy_receipt_joins_the_same_closed_native_publication_chain(self):
        import native_proxy
        from test_native_proxy import elf
        name='caddy_Linux_x86_64';platform='Linux/x86_64'
        self.config['package']['extra_files'].append(name)
        self.config['package']['extra_file_sources'][name]={'provider':'caddy','platform':platform}
        proxy=native_proxy.output(self.root,platform);proxy.parent.mkdir(parents=True,exist_ok=True)
        body=elf(platform);proxy.write_bytes(body);proxy.chmod(0o755)
        raw=native_proxy.canonical(native_proxy.expected(platform)|{'binary_sha256':'sha256:'+extras.hashlib.sha256(body).hexdigest()})
        native_proxy.receipt_path(proxy).write_bytes(raw)
        extras.materialize(self.root,self.config,self.env,self.output)
        self.assertEqual((self.output/name).read_bytes(),body)
        self.assertEqual((self.output/(name+'.json')).read_bytes(),raw)
        self.assertEqual(extras.producer_paths(self.root,self.config,platform,['host']),
            [proxy.relative_to(self.root).as_posix(),proxy.relative_to(self.root).as_posix()+'.json'])
        self.assertEqual(extras.producer_paths(self.root,self.config,platform,['cli']),[])
        proofs=[]
        for component,target in extras.records(self.config).values():
            source=extras.source_path(self.root,component,target);relative=source.relative_to(self.root).as_posix();system,arch=target.split('/')
            match=next((value for value in proofs if value['entry']['arch']==arch),None)
            if match is None:
                match={'entry':{'components':['host'],'platform':system,'arch':arch},'build':{'artifacts':{}}};proofs.append(match)
            match['build']['artifacts'].update({relative:extras.hashlib.sha256(source.read_bytes()).hexdigest(),
                relative+'.json':extras.hashlib.sha256(source.with_name(source.name+'.json').read_bytes()).hexdigest()})
        proof={'native_matrix':{'proofs':proofs}}
        extras.verify_publication(self.root,self.config,proof,self.env['COMMIT'],self.env['VERSION'],self.output)
        changed=json.loads(raw);changed['tools_revision']='f'*40
        (self.output/(name+'.json')).write_bytes(native_proxy.canonical(changed))
        with self.assertRaises(ValueError):extras.verify_publication(self.root,self.config,proof,self.env['COMMIT'],self.env['VERSION'],self.output)
