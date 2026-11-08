from pathlib import Path
import hashlib
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cookbook'))
import artifact_packet as packet


class ArtifactPacketTests(unittest.TestCase):
    def test_extraction_checks_complete_compressed_stream_and_removes_failed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'public';source.mkdir()
            (source/'index.html').write_bytes(b'owned document')
            packet.pack_static(source,root/'valid.tar.gz',max_bytes=1024,max_file_bytes=512)
            valid=(root/'valid.tar.gz').read_bytes()
            for body in [valid[:-4],valid[:-8]+b'\xff'*8,
                         gzip.compress(gzip.decompress(valid)+b'hidden payload',mtime=0)]:
                (root/'bad.tar.gz').write_bytes(body)
                with self.assertRaises((ValueError,OSError,EOFError)):
                    packet.unpack_static(root/'bad.tar.gz',root/'output',max_bytes=1024,max_file_bytes=512)
                self.assertFalse((root/'output').exists())
            self.assertEqual(packet.unpack_static(root/'valid.tar.gz',root/'output',max_bytes=1024,max_file_bytes=512),{'index.html'})
            self.assertEqual((root/'output/index.html').read_bytes(),b'owned document')

    def test_static_archive_is_deterministic_and_preserves_all_payload_bytes(self):
        with tempfile.TemporaryDirectory(prefix='static packet ') as directory:
            root = Path(directory)
            source = root / 'public'
            (source / '_expo').mkdir(parents=True)
            (source / 'index.html').write_bytes(b'<!doctype html><main>application</main>')
            (source / '_expo/.routes.json').write_bytes(b'{"routes":[]}')
            (source / 'app.js.gz').write_bytes(b'previously compressed payload')
            expected = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob('*') if path.is_file()}
            first = packet.pack_static(source, root / 'one.tar.gz', max_bytes=4096, max_file_bytes=1024)
            second = packet.pack_static(source, root / 'two.tar.gz', max_bytes=4096, max_file_bytes=1024)
            self.assertEqual((root / 'one.tar.gz').read_bytes(), (root / 'two.tar.gz').read_bytes())
            self.assertEqual(first, second)
            with tarfile.open(root / 'one.tar.gz', 'r:gz') as archive:
                self.assertEqual({entry.name: archive.extractfile(entry).read() for entry in archive}, expected)
                for entry in archive.getmembers():
                    self.assertEqual((entry.uid, entry.gid, entry.mtime, entry.uname, entry.gname), (0, 0, 0, '', ''))
            self.assertEqual(first['files'], {name: hashlib.sha256(body).hexdigest() for name, body in expected.items()})

    def test_static_source_links_and_over_budget_content_fail_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'public'
            source.mkdir()
            (source / 'index.html').write_bytes(b'12345')
            for limits in [(4, 4), (8, 4)]:
                with self.subTest(limits=limits), self.assertRaises(ValueError):
                    packet.pack_static(source, root / 'archive.tar.gz', max_bytes=limits[0], max_file_bytes=limits[1])
                self.assertFalse((root / 'archive.tar.gz').exists())
            (source / 'index.html').unlink()
            (root / 'private').write_bytes(b'not public')
            (source / 'index.html').symlink_to(root / 'private')
            with self.assertRaisesRegex(ValueError, 'links'):
                packet.pack_static(source, root / 'archive.tar.gz', max_bytes=1024, max_file_bytes=512)
            self.assertFalse((root / 'archive.tar.gz').exists())

    def test_static_source_membership_change_does_not_publish_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'public'
            source.mkdir()
            (source / 'index.html').write_bytes(b'original')
            original = packet.read_regular
            def mutate(path, maximum):
                body = original(path, maximum)
                (source / 'new.js').write_bytes(b'concurrent addition')
                return body
            with patch.object(packet, 'read_regular', side_effect=mutate):
                with self.assertRaisesRegex(ValueError, 'membership'):
                    packet.pack_static(source, root / 'archive.tar.gz', max_bytes=1024, max_file_bytes=512)
            self.assertFalse((root / 'archive.tar.gz').exists())

    def test_zero_image_packet_has_closed_digest_bound_static_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'static').mkdir()
            (root / 'release.json').write_text('{"kind":"komizo.release","images":{}}\n')
            (root / 'static/app.tar.gz').write_bytes(b'static-only accepted bytes')
            inventory = packet.write_inventory(root, {'release.json': 'release', 'static/app.tar.gz': 'static'})
            self.assertEqual(set(inventory['files']), {'release.json', 'static/app.tar.gz'})
            self.assertEqual(packet.verify_inventory(root), inventory)
            (root / 'static/app.tar.gz').write_bytes(b'changed bytes')
            with self.assertRaisesRegex(ValueError, 'accepted member bytes'):
                packet.verify_inventory(root)

    def test_undeclared_paths_links_and_duplicate_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'release.json').write_bytes(b'{}')
            packet.write_inventory(root, {'release.json': 'release'})
            (root / 'private.env').write_bytes(b'PRIVATE=value')
            with self.assertRaisesRegex(ValueError, 'undeclared'):
                packet.verify_inventory(root)
            (root / 'private.env').unlink()
            (root / 'surprise').symlink_to(root / 'release.json')
            with self.assertRaisesRegex(ValueError, 'links'):
                packet.verify_inventory(root)
            (root / 'surprise').unlink()
            (root / 'inventory.json').write_text('{"version":1,"version":1,"files":{}}')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                packet.verify_inventory(root)


if __name__ == '__main__':
    unittest.main()
