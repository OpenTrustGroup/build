#!/usr/bin/env python3

from pathlib import Path
import tempfile
import unittest

from create_portable_manifest import make_portable_manifest


class PortableManifestTest(unittest.TestCase):
    def manifest(self, out_root, source_root):
        output = out_root / 'obj/libexample.a'
        header = source_root / 'include/example.h'
        output.parent.mkdir(parents=True, exist_ok=True)
        header.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'archive')
        header.write_bytes(b'header')
        return {
            'atoms': [
                {
                    'files': [
                        {'source': str(output)},
                        {'source': str(header)},
                    ],
                },
            ],
            'meta': {'target-arch': 'arm64'},
        }

    def test_manifest_is_independent_of_workspace_prefix(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        first = Path(temporary.name) / 'workspace/one'
        second = Path(temporary.name) / 'container/two'

        first_manifest = make_portable_manifest(
            self.manifest(first / 'out', first), first / 'out', first
        )
        second_manifest = make_portable_manifest(
            self.manifest(second / 'out', second), second / 'out', second
        )

        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(
            'out://obj/libexample.a',
            first_manifest['atoms'][0]['files'][0]['source'],
        )
        self.assertEqual(
            'source://include/example.h',
            first_manifest['atoms'][0]['files'][1]['source'],
        )
        self.assertEqual('1', first_manifest['meta']['source-locator-schema'])

    def test_rejects_source_outside_declared_roots(self):
        manifest = {
            'atoms': [{'files': [{'source': '/unowned/file'}]}],
            'meta': {},
        }

        with self.assertRaises(ValueError):
            make_portable_manifest(manifest, '/workspace/out', '/workspace')

    def test_rejects_relative_source(self):
        manifest = {
            'atoms': [{'files': [{'source': '../file'}]}],
            'meta': {},
        }

        with self.assertRaises(ValueError):
            make_portable_manifest(manifest, '/workspace/out', '/workspace')

    def test_rejects_missing_source(self):
        manifest = {
            'atoms': [
                {'files': [{'source': '/workspace/out/missing-file'}]},
            ],
            'meta': {},
        }

        with self.assertRaises(ValueError):
            make_portable_manifest(manifest, '/workspace/out', '/workspace')


if __name__ == '__main__':
    unittest.main()
