import json
from pathlib import Path
import tempfile
import unittest

from consolidate_multiple_run_of_metrics import DiskBandwidthExtractor, WslCollectionStatusExtractor


class WslDiskMetricsTests(unittest.TestCase):
    def extract_disk(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'disk_bandwidth.log'
            path.write_text(text)
            return DiskBandwidthExtractor().extract_data(path)

    def test_wsl_samples_are_time_weighted(self):
        records = [
            {'format': 'wsl_disk_io_v1'},
            {'elapsed_seconds': 1, 'read_bytes_per_second': 1000000, 'write_bytes_per_second': 2000000},
            {'elapsed_seconds': 3, 'read_bytes_per_second': 3000000, 'write_bytes_per_second': 6000000},
        ]
        text = '\n'.join(json.dumps(record) for record in records) + '\n{"partial":'
        self.assertEqual(self.extract_disk(text), {'Disk Read MB/s': 2.5, 'Disk Write MB/s': 5.0})

    def test_missing_samples_are_na(self):
        for text in ('', '{"format": "wsl_disk_io_v1"}\n'):
            with self.subTest(text=text):
                self.assertEqual(self.extract_disk(text), {'Disk Read MB/s': 'NA', 'Disk Write MB/s': 'NA'})

    def test_native_iotop_format_still_works(self):
        text = 'Total DISK READ:      1000000.00 B/s | Total DISK WRITE:      2000000.00 B/s\n'
        self.assertEqual(self.extract_disk(text), {'Disk Read MB/s': 1.0, 'Disk Write MB/s': 2.0})

    def test_unsupported_hardware_is_reported_as_na(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'collection_status.json'
            path.write_text(json.dumps({
                'scope': 'WSL2 Linux-visible metrics, not Windows host metrics',
                'pcm.csv': {'status': 'unavailable'},
                'npu_usage.csv': {'status': 'unavailable'},
                'qmassa': {'status': 'unavailable'},
            }))
            metrics = WslCollectionStatusExtractor().extract_data(path)
            self.assertEqual(metrics['Power Draw W'], 'NA')
            self.assertEqual(metrics['Memory Bandwidth Usage MB/s'], 'NA')
            self.assertEqual(metrics['GPU Utilization %'], 'NA')
            self.assertEqual(metrics['NPU Utilization %'], 'NA')


if __name__ == '__main__':
    unittest.main()