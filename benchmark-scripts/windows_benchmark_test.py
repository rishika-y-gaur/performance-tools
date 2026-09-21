import contextlib
import os
import tempfile
import unittest
from unittest.mock import patch

import benchmark


class WindowsBenchmarkTests(unittest.TestCase):
    def run_benchmark(self, root, wsl, interrupt=False, density=False):
        argv = ["benchmark.py", "--compose_file", "application.yaml", "--results_dir", root,
                "--duration", "2", "--init_duration", "0"]
        if density:
            argv += ["--target_fps", "15", "--container_names", "gst0"]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"WSL2": str(wsl).lower(), "TARGET_DEVICE": "CPU"}))
            stack.enter_context(patch("sys.argv", argv))
            compose = stack.enter_context(patch.object(benchmark, "docker_compose_containers", return_value=(b"", b"", 0)))
            preflight = stack.enter_context(patch.object(benchmark.windows_metrics, "preflight"))
            reset = stack.enter_context(patch.object(benchmark.windows_metrics, "reset"))
            measure = stack.enter_context(patch.object(benchmark.windows_metrics, "measure", return_value=contextlib.nullcontext()))
            stack.enter_context(patch.object(benchmark.stream_density, "clean_up_pipeline_logs"))
            density_run = stack.enter_context(patch.object(benchmark.stream_density, "run_stream_density",
                                                           return_value=[(15, "gst0", 2, True)]))
            sleep = stack.enter_context(patch.object(benchmark.time, "sleep"))
            commands = stack.enter_context(patch.object(benchmark.subprocess, "run"))
            if interrupt:
                sleep.side_effect = [None, KeyboardInterrupt()]
                with self.assertRaises(KeyboardInterrupt):
                    benchmark.main()
            else:
                benchmark.main()
            if wsl:
                preflight.assert_called_once()
                reset.assert_called_once_with(root)
            else:
                preflight.assert_not_called()
                reset.assert_not_called()
            if density:
                files = density_run.call_args.args[1]
                measure.assert_not_called()
            else:
                files = compose.call_args_list[0].kwargs["compose_files"]
                self.assertEqual(compose.call_args_list[-1].args[0], "down")
                measure.assert_called_once()
            self.assertEqual(len(files), 1 if wsl else 2)
            if not interrupt:
                self.assertEqual(commands.call_count, 0 if wsl else 1)

    def test_wsl_benchmark(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_benchmark(root, True)

    def test_native_benchmark(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_benchmark(root, False)

    def test_wsl_interruption_stops_containers(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_benchmark(root, True, interrupt=True)

    def test_wsl_density_uses_host_collection(self):
        with tempfile.TemporaryDirectory() as root:
            self.run_benchmark(root, True, density=True)


if __name__ == "__main__":
    unittest.main()