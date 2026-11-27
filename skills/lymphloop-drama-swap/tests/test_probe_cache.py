"""Completed full decodes are reusable; changed files and failed probes are not."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('cached_media', Path(__file__).resolve().parents[1]/'scripts/media_pipeline.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)


class ProbeCacheTests(unittest.TestCase):
    def test_cache_invalidates_even_for_same_size_rewrite_with_restored_mtime(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); source = root/'movie.mp4'; source.write_bytes(b'old')
            decoder = root/'ffprobe'; decoder.write_bytes(b'decoder')
            with patch.dict(os.environ, {'LYMPHLOOP_PROBE_CACHE':str(root/'cache')}), patch.object(m, 'binary', return_value=str(decoder)), patch.object(m, 'run', return_value=json.dumps({'frames':[1,2]})) as run:
                flags = ['-show_frames']; a = m._probe_json(source, flags)
                self.assertEqual(m._probe_json(source, flags), a); self.assertEqual(run.call_count, 1)
                stat = source.stat(); source.write_bytes(b'new'); os.utime(source, ns=(stat.st_atime_ns,stat.st_mtime_ns))
                m._probe_json(source, flags); self.assertEqual(run.call_count, 2)
                decoder.write_bytes(b'updated'); m._probe_json(source, flags); self.assertEqual(run.call_count, 3)

    def test_failed_or_mid_write_probes_do_not_cache(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); source = root/'movie.mp4'; source.write_bytes(b'old'); decoder = root/'ffprobe'; decoder.write_bytes(b'tool')
            with patch.dict(os.environ, {'LYMPHLOOP_PROBE_CACHE':str(root/'cache')}), patch.object(m,'binary',return_value=str(decoder)):
                with patch.object(m,'run',side_effect=RuntimeError('decode failed')):
                    with self.assertRaises(RuntimeError):m._probe_json(source, ['-show_frames'])
                self.assertFalse(list((root/'cache').glob('*.json')))
                def mutate(_): source.write_bytes(b'changed'); return '{}'
                with patch.object(m,'run',side_effect=mutate):m._probe_json(source,['-show_frames'])
                self.assertFalse(list((root/'cache').glob('*.json')))


if __name__ == '__main__':unittest.main()
