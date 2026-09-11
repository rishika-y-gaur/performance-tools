import contextlib
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import benchmark


class WslBenchmarkTests(unittest.TestCase):
    def run_benchmark(self, release, benchmark_type='reg', device='CPU',
                      density=False, parser_script=None, wsl2=None):
        environment = {'LP_TAG': 'test-tag'}
        if wsl2 is not None:
            environment['WSL2'] = wsl2
        with tempfile.TemporaryDirectory() as results_dir:
            args = SimpleNamespace(
                target_fps=[15.0] if density else None,
                container_names=['gst0'] if density else None,
                results_dir=results_dir,
                compose_file=['workload.yaml'],
                benchmark_type=benchmark_type,
                target_device=device,
                retail_use_case_root='../..',
                density_increment=None,
                pipelines=1,
                init_duration=0,
                duration=0,
                docker_log=None,
                parser_script=parser_script or os.path.join(
                    os.path.dirname(__file__), 'parse_qmassa_metrics_to_json.py'),
                parser_args='-k device -k qmassa',
            )
            with (
                mock.patch.object(benchmark, 'parse_args', return_value=args),
                mock.patch.object(benchmark.os, 'uname', return_value=SimpleNamespace(release=release)) as uname,
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(benchmark, 'docker_compose_containers', return_value=(b'', b'', 0)) as compose,
                mock.patch.object(benchmark.time, 'sleep'),
                mock.patch.object(benchmark.subprocess, 'run') as parser,
                mock.patch.object(benchmark.stream_density, 'run_stream_density',
                                  return_value=[(15.0, 'gst0', 1, True)]) as stream_density,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                benchmark.main()
                if wsl2 is not None:
                    uname.assert_not_called()
                if density:
                    env_vars, compose_files = stream_density.call_args.args[:2]
                    compose.assert_not_called()
                else:
                    compose_files = compose.call_args_list[0].kwargs['compose_files']
                    env_vars = compose.call_args_list[0].kwargs['env_vars']
                    self.assertEqual([call.args[0] for call in compose.call_args_list], ['up', 'down'])
                return compose_files, env_vars, parser.call_count

    def test_wsl_cpu_and_gpu_benchmark_and_density(self):
        for device in ('CPU', 'GPU'):
            for density in (False, True):
                for benchmark_type in ('reg', 'default'):
                    with self.subTest(device=device, density=density, benchmark_type=benchmark_type):
                        compose_files, env_vars, parser_calls = self.run_benchmark(
                            '6.6.87.2-microsoft-standard-WSL2', benchmark_type, device, density,
                            wsl2='true')
                        expected_compose = 'docker-compose-wsl2-reg.yaml' if benchmark_type == 'reg' else 'docker-compose-wsl2.yaml'
                        self.assertEqual(os.path.basename(compose_files[-1]), expected_compose)
                        self.assertTrue(os.path.isfile(compose_files[-1]))
                        self.assertNotIn('BENCHMARK_IMAGE', env_vars)
                        self.assertEqual(env_vars['DEVICE'], device)
                        self.assertEqual(parser_calls, 0)

    def test_native_linux_keeps_collector_and_parser(self):
        for benchmark_type in ('reg', 'default'):
            with self.subTest(benchmark_type=benchmark_type):
                compose_files, env_vars, parser_calls = self.run_benchmark('6.8.0-generic', benchmark_type)
                expected_compose = 'docker-compose-reg.yaml' if benchmark_type == 'reg' else 'docker-compose.yaml'
                self.assertEqual(os.path.basename(compose_files[-1]), expected_compose)
                self.assertNotIn('BENCHMARK_IMAGE', env_vars)
                self.assertEqual(parser_calls, 1)

    def test_exported_wsl2_controls_collector(self):
        for density in (False, True):
            for wsl2, release, expected_compose, expected_parser_calls in (
                ('true', '6.8.0-generic', 'docker-compose-wsl2-reg.yaml', 0),
                ('false', '6.6.87.2-microsoft-standard-WSL2', 'docker-compose-reg.yaml', 1),
            ):
                with self.subTest(wsl2=wsl2, density=density):
                    compose_files, _, parser_calls = self.run_benchmark(
                        release, density=density, wsl2=wsl2)
                    self.assertEqual(os.path.basename(compose_files[-1]), expected_compose)
                    self.assertEqual(parser_calls, expected_parser_calls)

    def test_wsl_keeps_custom_parser(self):
        _, _, parser_calls = self.run_benchmark(
            '6.6.87.2-microsoft-standard-WSL2', parser_script='custom_parser.py',
            wsl2='true')
        self.assertEqual(parser_calls, 1)


if __name__ == '__main__':
    unittest.main()