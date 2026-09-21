import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import windows_metrics


class WindowsReportingTests(unittest.TestCase):
    def test_csv_default_formatting(self):
        from consolidate_multiple_run_of_metrics import format_metric_value
        for value in (None, "NA", "N/A", "-", "", 0, 0.0, float("nan"), float("inf")):
            self.assertEqual(format_metric_value(value), "0.00")
        for value in (12.96, 121.509, 2, True, False, "timestamp"):
            self.assertEqual(format_metric_value(value), value)

    def test_native_defaults_and_measured_values(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root, "metrics.csv")
            script = Path(__file__).with_name("consolidate_multiple_run_of_metrics.py")
            command = [sys.executable, str(script), "--root_directory", root, "--output", str(output)]
            env = dict(os.environ, WSL2="false")
            subprocess.run(command, env=env, check=True, capture_output=True)
            with output.open() as source:
                metrics = dict(csv.reader(source))
            self.assertEqual(set(metrics), {
                "CPU Utilization %", "NPU Utilization %", "Memory Utilization %",
                "Disk Read MB/s", "Disk Write MB/s", "S0 Memory Bandwidth Usage MB/s",
                "S0 Power Draw W", "Overall Latency (ms)"})
            self.assertEqual(set(metrics.values()), {"0.00"})
            Path(root, "cpu_usage.log").touch()
            Path(root, "pcm.csv").touch()
            Path(root, "npu_usage.csv").write_text("percent_usage\n12.5\n")
            Path(root, "qmassa1-parsed.json").write_text('[{"RCS %": 10.59}]')
            subprocess.run(command, env=env, check=True, capture_output=True)
            with output.open() as source:
                metrics = dict(csv.reader(source))
            self.assertEqual(metrics["CPU Utilization %"], "0.00")
            self.assertEqual(metrics["Power Draw W"], "0.00")
            self.assertEqual(metrics["Memory Bandwidth Usage MB/s"], "0.00")
            self.assertEqual(metrics["NPU Utilization %"], "12.5")
            self.assertEqual(metrics["GPU_1 Render/3D[RCS] Utilization %"], "10.59")
            self.assertEqual(metrics["GPU_1 Compute[CCS] Utilization %"], "0.00")

    def test_consolidation_and_plot_cli(self):
        with tempfile.TemporaryDirectory() as root:
            samples = [{"kind": "sample", "timestamp": 100 + index,
                        "cpu_percent": value, "memory_percent": 25,
                        "disk_read_mib_s": index, "disk_write_mib_s": None,
                        "gpu": {"adapter0": {"Compute/engine0": value}}}
                       for index, value in enumerate((0, 50, 100))]
            Path(root, "capture.jsonl").write_text("".join(json.dumps(sample) + "\n" for sample in samples))
            windows_metrics.write_manifest(root, {
                "source": "Windows host", "captures": [{"path": "capture.jsonl",
                "scenario": "benchmark", "pipeline_count": 2, "selected": True, "status": "complete"}]})
            Path(root, "qmassa0-stale-parsed.json").write_text('[{"CCS %": 99}]')
            env = dict(os.environ, WSL2="true", MPLBACKEND="Agg")
            scripts = Path(__file__).parent
            subprocess.run([sys.executable, str(scripts / "consolidate_multiple_run_of_metrics.py"),
                            "--root_directory", root, "--output", str(Path(root, "metrics.csv"))],
                           env=env, check=True, capture_output=True)
            with open(Path(root, "metrics.csv")) as source:
                metrics = dict(csv.reader(source))
            self.assertEqual(metrics, {
                "CPU Utilization %": "50.0", "NPU Utilization %": "0.00",
                "Memory Utilization %": "25.0", "Disk Read MB/s": "1.05",
                "Disk Write MB/s": "0.00", "S0 Memory Bandwidth Usage MB/s": "0.00",
                "S0 Power Draw W": "0.00", "GPU_0 Compute[CCS] Utilization %": "0.00",
                "GPU_0 Render/3D[RCS] Utilization %": "0.00", "GPU_0 Video[VCS] Utilization %": "0.00",
                "GPU_0 VideoEnhance[VECS] Utilization %": "0.00", "GPU_0 Blitter Copy Engine %": "0.00",
                "GPU_0 GPU Power (W)": "0.00", "Overall Latency (ms)": "0.00"})
            import usage_graph_plot
            with patch.dict(os.environ, env), patch.object(sys, "argv", ["usage_graph_plot.py", "--dir", root]), \
                    patch.object(usage_graph_plot, "open_plot") as viewer:
                usage_graph_plot.main()
                viewer.assert_called_once_with(str(Path(root, "plot_metrics.png")))
            from PIL import Image, ImageStat
            with Image.open(Path(root, "plot_metrics.png")) as image:
                self.assertGreater(image.width, 100)
                self.assertGreater(sum(ImageStat.Stat(image.convert("RGB")).var), 0)

    def test_plot_without_counters(self):
        from usage_graph_plot import plot_windows_metrics
        with tempfile.TemporaryDirectory() as root:
            windows_metrics.reset(root)
            image = plot_windows_metrics(root)
            self.assertGreater(Path(image).stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()