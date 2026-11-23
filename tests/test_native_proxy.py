"""Native proxy pinned-release, byte, and architecture admission controls."""
import sys
sys.dont_write_bytecode=True
from source_fixture import SDK_ROOT
from pathlib import Path
import copy
import io
import hashlib
import json
import os
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(SDK_ROOT/'cookbook'))
import native_proxy as proxy

REAL=dict(proxy.PLATFORMS)


def elf(platform):
    body=bytearray(120);body[:6]=b'\x7fELF\x02\x01'
    struct.pack_into('<HH',body,16,2,proxy.PLATFORMS[platform][1])
    struct.pack_into('<Q',body,32,64);struct.pack_into('<HH',body,54,56,1)
    struct.pack_into('<I',body,64,1)
    return bytes(body)+b'exact independently reviewed companion'


def archive(body,members=None):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as bundle:
        for name,data in (members if members is not None else [('LICENSE',b'license'),('README.md',b'readme'),('caddy',body)]):
            info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o755;bundle.addfile(info,io.BytesIO(data))
    return stream.getvalue()


def pins(platform,body=None,packed=None):
    """A fake pin table entry for a fake release, so tests never need the network."""
    body=elf(platform) if body is None else body;packed=archive(body) if packed is None else packed
    arch,machine,_,_=proxy.PLATFORMS[platform]
    return {platform:(arch,machine,hashlib.sha256(packed).hexdigest(),hashlib.sha256(body).hexdigest())},body,packed


class ProxyCompanions(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix='native-proxy-control-');self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.platform='Linux/x86_64'
        self.table,self.body,self.packed=pins(self.platform)
        patcher=patch.dict(proxy.PLATFORMS,self.table);patcher.start();self.addCleanup(patcher.stop)

    def download(self,packed=None):
        opened=[]
        def urlopen(url,timeout):opened.append(url);return io.BytesIO(self.packed if packed is None else packed)
        return patch.object(proxy.artifact_packet.urllib.request,'urlopen',side_effect=urlopen),opened

    def fetch(self,platform=None,packed=None):
        patcher,opened=self.download(packed)
        with patcher:result=proxy.fetch(self.root,platform or self.platform)
        return result,opened

    def test_release_pins_are_exact_official_asset_names_urls_and_hashes(self):
        self.assertEqual(proxy.CADDY_VERSION,'2.11.6')
        for platform,arch in [('Linux/x86_64','amd64'),('Linux/arm64','arm64')]:
            current=REAL[platform]
            self.assertEqual(proxy.asset(platform),'caddy_2.11.6_linux_'+arch+'.tar.gz')
            self.assertEqual(proxy.expected(platform)['source_url'],
                'https://github.com/caddyserver/caddy/releases/download/v2.11.6/caddy_2.11.6_linux_'+arch+'.tar.gz')
            self.assertEqual(proxy.expected(platform)['platform'],'linux/'+arch)
            for digest in current[2:]:self.assertRegex(digest,'^[a-f0-9]{64}$')
        self.assertEqual(REAL['Linux/x86_64'][2:],('22c84f8d2d4e4e0e2d422f8049fdd0fc1ed8d5665d0fe166f506c7fd863b4555',
            '9855a8971f1d3cd705b6d606962271f4acec82a349e88d7d280d92d055e7f11d'))
        self.assertEqual(REAL['Linux/arm64'][2:],('6a59a88914db644d410c4773286784096a97b59ea74152311442c19e988071ac',
            '38717b9d12fb46e5c7ac2270968567ba4bf0f5c561c144c857b852e33d67e649'))

    def test_a_download_with_the_pinned_checksums_is_admitted_with_the_build_mode_and_location(self):
        (body,raw),opened=self.fetch()
        path=proxy.output(self.root,self.platform)
        self.assertEqual(body,self.body);self.assertEqual(path.read_bytes(),self.body)
        self.assertEqual(opened,[proxy.RELEASE+'caddy_2.11.6_linux_amd64.tar.gz'])
        self.assertEqual(path.relative_to(self.root).as_posix(),'.artifacts/native-proxy/caddy_Linux_x86_64')
        self.assertEqual(path.stat().st_mode&0o777,0o755);self.assertEqual(proxy.receipt_path(path).stat().st_mode&0o777,0o644)
        receipt=json.loads(raw)
        self.assertEqual(set(receipt),{'kind','version','platform','caddy_version','source_url','archive_sha256','binary_sha256'})
        self.assertEqual(receipt['archive_sha256'],'sha256:'+hashlib.sha256(self.packed).hexdigest())
        self.assertEqual(receipt['binary_sha256'],'sha256:'+hashlib.sha256(self.body).hexdigest())
        self.assertEqual(sorted(child.name for child in path.parent.iterdir()),[path.name,path.name+'.json'])

    def test_receipt_round_trips_through_verify_and_an_admitted_copy_is_not_downloaded_again(self):
        (_,raw),_=self.fetch()
        self.assertEqual(proxy.verify(self.root,self.platform),(self.body,raw))
        patcher,opened=self.download()
        with patcher:proxy.fetch(self.root,self.platform)
        self.assertEqual(opened,[])

    def test_changed_or_missing_bytes_are_downloaded_and_verified_again(self):
        self.fetch();path=proxy.output(self.root,self.platform);path.write_bytes(b'changed');path.chmod(0o755)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        _,opened=self.fetch();self.assertEqual(len(opened),1);self.assertEqual(path.read_bytes(),self.body)

    def test_a_wrong_archive_checksum_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaisesRegex(ValueError,'checksum mismatch'):self.fetch(packed=self.packed+b'tampered')
        directory=proxy.output(self.root,self.platform).parent
        self.assertEqual(list(directory.iterdir()),[])

    def test_a_wrong_binary_checksum_is_refused_and_keeps_the_previous_files(self):
        other=elf(self.platform)+b'different binary';packed=archive(other)
        table={self.platform:self.table[self.platform][:2]+(hashlib.sha256(packed).hexdigest(),self.table[self.platform][3])}
        with patch.dict(proxy.PLATFORMS,table),self.assertRaisesRegex(ValueError,'binary checksum mismatch'):
            self.fetch(packed=packed)
        self.assertFalse(proxy.output(self.root,self.platform).exists())
        self.assertFalse(proxy.receipt_path(proxy.output(self.root,self.platform)).exists())

    def test_unsupported_platforms_are_refused_before_any_download(self):
        patcher,opened=self.download()
        for platform in ['Linux/riscv64','Windows/x86_64','macOS/arm64','Linux/../arm64']:
            with self.subTest(platform=platform),patcher,self.assertRaises(ValueError):proxy.fetch(self.root,platform)
        self.assertEqual(opened,[])

    def test_the_archive_must_hold_exactly_one_ordinary_caddy_executable(self):
        for members in [[('LICENSE',b'license')],[('caddy',self.body),('caddy',self.body)],[('bin/caddy',self.body)]]:
            packed=archive(self.body,members);table,_,_=pins(self.platform,self.body,packed)
            with self.subTest(members=[name for name,_ in members]),patch.dict(proxy.PLATFORMS,table),self.assertRaises(ValueError):
                self.fetch(packed=packed)
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w:gz') as bundle:
            info=tarfile.TarInfo('caddy');info.type=tarfile.SYMTYPE;info.linkname='/etc/passwd';bundle.addfile(info)
        with self.assertRaises(ValueError):proxy.extract(stream.getvalue())

    def test_a_pinned_binary_of_the_wrong_architecture_is_refused(self):
        body=elf('Linux/arm64');table,_,packed=pins(self.platform,body)
        with patch.dict(proxy.PLATFORMS,table),self.assertRaisesRegex(ValueError,'ELF architecture'):self.fetch(packed=packed)

    def test_only_explicit_linux_proxy_companions_are_enabled(self):
        config={'package':{'extra_files':['caddy_Linux_arm64'],'extra_file_sources':{
            'caddy_Linux_arm64':{'provider':'caddy','platform':'Linux/arm64'}}}}
        self.assertTrue(proxy.enabled(config));self.assertFalse(proxy.enabled({}))
        for source in [{'provider':'caddy','platform':'Windows/x86_64'},
            {'provider':'caddy','platform':'Linux/arm64','path':'/unreviewed'},
            {'provider':'caddy','platform':'Linux/x86_64'}]:
            bad=copy.deepcopy(config);bad['package']['extra_file_sources']['caddy_Linux_arm64']=source
            with self.subTest(source=source),self.assertRaises(ValueError):proxy.enabled(bad)
        with self.assertRaises(ValueError):proxy.output(self.root,'Linux/../arm64')

    def test_receipt_role_version_platform_url_and_raw_substitution_refuse(self):
        (_,raw),_=self.fetch();path=proxy.output(self.root,self.platform);receipt=proxy.receipt_path(path)
        for change in [{'kind':'komizo.proxy-build'},{'version':2},{'platform':'linux/arm64'},{'caddy_version':'2.11.5'},
            {'source_url':'https://example.invalid/caddy.tar.gz'},{'archive_sha256':'sha256:'+'f'*64},
            {'binary_sha256':'sha256:'+'f'*64},{'extra':'unreviewed'}]:
            receipt.write_bytes(proxy.canonical(json.loads(raw)|change))
            with self.subTest(change=change),self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        for malformed in [raw+b'\n',b'{"version":1,"version":1}',raw.replace(b'"version":1',b'"version":true')]:
            receipt.write_bytes(malformed)
            with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        receipt.write_bytes(raw);path.write_bytes(self.body+b'changed')
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)

    def test_wrong_architecture_dynamic_interpreter_and_malformed_program_table_refuse(self):
        for body in [elf('Linux/arm64'),b'#!unreviewed',elf(self.platform)[:80],
            elf(self.platform)[:64]+struct.pack('<I',3)+elf(self.platform)[68:]]:
            with self.assertRaises(ValueError):proxy.elf(body,self.platform)
        body=bytearray(elf(self.platform));struct.pack_into('<H',body,56,65535)
        with self.assertRaises(ValueError):proxy.elf(body,self.platform)

    def test_binary_links_writable_executable_and_receipt_links_are_refused(self):
        (_,raw),_=self.fetch();path=proxy.output(self.root,self.platform);path.chmod(0o777)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        path.chmod(0o755);other=self.root/'other';os.link(path,other)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        other.unlink();receipt=proxy.receipt_path(path);saved=self.root/'saved';receipt.rename(saved);receipt.symlink_to(saved)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)
        receipt.unlink();receipt.write_bytes(raw);path.unlink();path.symlink_to(saved)
        with self.assertRaises(ValueError):proxy.verify(self.root,self.platform)

if __name__=='__main__':unittest.main()
