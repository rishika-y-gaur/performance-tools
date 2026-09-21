import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from collect_windows_metrics import GPUCounters, aggregate_gpu
import windows_metrics


class WindowsMetricsTests(unittest.TestCase):
    def test_wsl_gate(self):
        self.assertFalse(windows_metrics.enabled({}))
        self.assertFalse(windows_metrics.enabled({"WSL2": "false"}))
        self.assertTrue(windows_metrics.enabled({"WSL2": "true"}))

    def test_gpu_groups_processes_but_not_engines_or_adapters(self):
        counters = [
            ("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 30),
            ("pid_2_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 40),
            ("pid_1_luid_0x0_0x1_phys_0_eng_1_engtype_Copy", 90),
            ("pid_1_luid_0x0_0x2_phys_0_eng_0_engtype_3D", 0)]
        values = aggregate_gpu(counters)
        self.assertEqual(values["0x0_0x1-phys0"]["3D/engine0"], 70)
        self.assertEqual(values["0x0_0x1-phys0"]["Copy/engine1"], 90)
        self.assertEqual(values["0x0_0x2-phys0"]["3D/engine0"], 0)

    def test_summary_and_iteration_selection(self):
        with tempfile.TemporaryDirectory() as root:
            windows_metrics.reset(root)
            manifest = windows_metrics.read_manifest(root)
            for count in (1, 2):
                path = f"capture{count}.jsonl"
                samples = [{"kind": "sample", "timestamp": index,
                            "cpu_percent": value, "gpu": {}}
                           for index, value in enumerate((0, None, 60))]
                Path(root, path).write_text("".join(json.dumps(sample) + "\n" for sample in samples))
                manifest["captures"].append({"path": path, "scenario": "density",
                                              "pipeline_count": count, "status": "complete", "selected": True})
            windows_metrics.write_manifest(root, manifest)
            windows_metrics.select_iteration({"WSL2": "true", "RESULTS_DIR": root}, "density", 1)
            result = windows_metrics.summary(root)
            self.assertEqual(result["CPU Utilization %"], 30)
            self.assertEqual(result["Disk Read MB/s"], "NA")
            self.assertEqual(result["GPU Utilization %"], "NA")
            self.assertEqual([capture["pipeline_count"] for capture, _ in windows_metrics.selected_samples(root)], [1])
            self.assertFalse(any(key.startswith("Windows") for key in result))

    def test_native_measure_is_noop(self):
        with windows_metrics.measure({"WSL2": "false"}):
            pass

    def test_summary_preserves_zero_and_does_not_mix_scenarios(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = {"captures": []}
            for scenario, value in (("first", 90), ("second", 0)):
                path = f"{scenario}.jsonl"
                Path(root, path).write_text(json.dumps({"kind": "sample", "cpu_percent": value}) + "\n")
                manifest["captures"].append({"path": path, "scenario": scenario, "selected": True})
            windows_metrics.write_manifest(root, manifest)
            with patch("builtins.print") as warning:
                summary = windows_metrics.summary(root)
            self.assertEqual(summary["CPU Utilization %"], 0)
            self.assertEqual(summary["Memory Utilization %"], "NA")
            warning.assert_called_once()

    def test_empty_summary_has_legacy_keys(self):
        with tempfile.TemporaryDirectory() as root:
            result = windows_metrics.summary(root)
            self.assertEqual(set(result), {"CPU Utilization %", "NPU Utilization %",
                             "Memory Utilization %", "Disk Read MB/s", "Disk Write MB/s",
                             "S0 Memory Bandwidth Usage MB/s", "S0 Power Draw W", "GPU Utilization %"})
            self.assertTrue(all(value == "NA" for value in result.values()))

    def test_pdh_primes_new_instances_and_closes_query(self):
        first = "pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D"
        second = "pid_2_luid_0x0_0x1_phys_0_eng_0_engtype_3D"
        pdh = Mock(spec=["OpenQuery", "AddEnglishCounter", "CollectQueryData",
                         "GetFormattedCounterArray", "CloseQuery", "PDH_FMT_DOUBLE"])
        pdh.GetFormattedCounterArray.side_effect = [{first: 0}, {first: 30, second: 90}, {second: 20}]
        gpu = GPUCounters(pdh)
        self.assertEqual(gpu.sample()["0x0_0x1-phys0"]["3D/engine0"], 30)
        self.assertEqual(gpu.sample()["0x0_0x1-phys0"]["3D/engine0"], 20)
        gpu.close()
        pdh.CloseQuery.assert_called_once_with(pdh.OpenQuery.return_value)

    def test_collector_cleanup_on_interruption(self):
        class FakeProcess:
            returncode = 0

            def __init__(self, *args, **kwargs):
                self.stopped = False
                kwargs["stdout"].write(json.dumps({"kind": "ready"}) + "\n")
                kwargs["stdout"].write(json.dumps({"kind": "sample", "timestamp": 1}) + "\n")
                kwargs["stdout"].flush()

            def communicate(self, input=None, timeout=None):
                self.stopped = input == "stop\n"

        with tempfile.TemporaryDirectory() as root:
            env = {"WSL2": "true", "RESULTS_DIR": root}
            processes = []

            def create_process(*args, **kwargs):
                process = FakeProcess(*args, **kwargs)
                processes.append(process)
                return process

            with patch.object(windows_metrics, "collector_command", return_value=["python.exe"]), \
                    patch.object(windows_metrics.subprocess, "Popen", side_effect=create_process):
                with self.assertRaises(KeyboardInterrupt):
                    with windows_metrics.measure(env):
                        raise KeyboardInterrupt()
                self.assertTrue(processes[0].stopped)
                capture = windows_metrics.read_manifest(root)["captures"][0]
                self.assertEqual(capture["status"], "failed")
                self.assertFalse(capture["selected"])
                with windows_metrics.measure(env):
                    pass
                self.assertTrue(processes[1].stopped)
                self.assertEqual(windows_metrics.read_manifest(root)["captures"][-1]["status"], "complete")

    def test_collector_start_failure(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(windows_metrics, "collector_command", return_value=["python.exe"]), \
                    patch.object(windows_metrics.subprocess, "Popen", side_effect=FileNotFoundError):
                with self.assertRaises(FileNotFoundError):
                    with windows_metrics.measure({"WSL2": "true", "RESULTS_DIR": root}):
                        pass
            self.assertEqual(windows_metrics.read_manifest(root)["captures"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()