import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import collect_wsl_disk as collector


class DiskCollectorTests(unittest.TestCase):
    def test_rates_use_elapsed_time(self):
        rates = collector.disk_rates({'sda': (100, 200)}, {'sda': (300, 800)}, 2)
        self.assertEqual(rates['sda'], {'read_bytes_per_second': 100, 'write_bytes_per_second': 300})

    def test_invalid_intervals_and_counters_are_not_zero_usage(self):
        for current, elapsed in [({'sda': (0, 0)}, 1), ({'sdb': (100, 200)}, 1), ({'sda': (100, 200)}, 0)]:
            with self.subTest(current=current, elapsed=elapsed), self.assertRaises(ValueError):
                collector.disk_rates({'sda': (100, 200)}, current, elapsed)

    def test_proc_fallback_excludes_partitions_and_stacked_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ['sda', 'loop0', 'dm-0']:
                (root / 'block' / name / 'slaves').mkdir(parents=True)
            (root / 'block/dm-0/slaves/sda').touch()
            diskstats = root / 'diskstats'
            diskstats.write_text('8 0 sda 1 0 10 0 1 0 20 0\n8 1 sda1 1 0 10 0 1 0 20 0\n7 0 loop0 1 0 10 0 1 0 20 0\n253 0 dm-0 1 0 10 0 1 0 20 0\n')
            with patch.object(collector, 'psutil', None):
                self.assertEqual(collector.read_counters(root / 'block', diskstats), {'sda': (5120, 10240)})

    def test_collection_writes_real_sample_and_unavailable_status(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(collector, 'read_counters', side_effect=[{'sda': (0, 0)}, {'sda': (200, 400)}]), patch.object(collector.time, 'monotonic', side_effect=[10, 12]):
                collector.collect(root, 1, stop)
            records = [json.loads(line) for line in (root / 'disk_bandwidth.log').read_text().splitlines()]
            self.assertEqual(records[0]['format'], 'wsl_disk_io_v1')
            self.assertEqual(records[1]['read_bytes_per_second'], 100)
            status = json.loads((root / 'collection_status.json').read_text())
            self.assertEqual(status['disk_bandwidth.log']['status'], 'collected')
            self.assertEqual(status['pcm.csv']['status'], 'unavailable')
            self.assertFalse((root / 'pcm.csv').exists())

    def test_unavailable_disk_does_not_stop_other_collectors(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(collector, 'read_counters', side_effect=OSError('not exposed')):
                collector.collect(root, 1, stop)
            self.assertEqual(len((root / 'disk_bandwidth.log').read_text().splitlines()), 1)
            status = json.loads((root / 'collection_status.json').read_text())
            self.assertEqual(status['disk_bandwidth.log']['status'], 'unavailable')
            stop.wait.assert_called_once_with(1)


if __name__ == '__main__':
    unittest.main()