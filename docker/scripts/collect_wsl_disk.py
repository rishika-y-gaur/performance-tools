import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None


def read_counters(sys_block=Path('/sys/block'), diskstats=Path('/proc/diskstats')):
    devices = {
        device.name for device in sys_block.iterdir()
        if not device.name.startswith(('loop', 'ram', 'zram'))
        and not any((device / 'slaves').iterdir())
    }
    if psutil is not None:
        counters = {
            name: (counter.read_bytes, counter.write_bytes)
            for name, counter in (psutil.disk_io_counters(perdisk=True, nowrap=False) or {}).items()
            if name in devices
        }
    else:
        counters = {}
        for line in diskstats.read_text().splitlines():
            fields = line.split()
            if len(fields) >= 10 and fields[2] in devices:
                counters[fields[2]] = (int(fields[5]) * 512, int(fields[9]) * 512)
    if not counters:
        raise OSError('No supported Linux-visible block-device counters found')
    return counters


def disk_rates(previous, current, elapsed):
    if elapsed <= 0 or previous.keys() != current.keys():
        raise ValueError('Disk set or sampling interval changed; collecting a new baseline')
    rates = {}
    for name, (read_bytes, write_bytes) in current.items():
        previous_read, previous_write = previous[name]
        if read_bytes < previous_read or write_bytes < previous_write:
            raise ValueError('Disk counters reset; collecting a new baseline')
        rates[name] = {
            'read_bytes_per_second': (read_bytes - previous_read) / elapsed,
            'write_bytes_per_second': (write_bytes - previous_write) / elapsed,
        }
    return rates


def collect(results_dir, interval, stop):
    results_dir.mkdir(parents=True, exist_ok=True)
    status_path = results_dir / 'collection_status.json'
    status = {
        'scope': 'WSL2 Linux-visible metrics, not Windows host metrics',
        'cpu_usage.log': {'status': 'delegated', 'collector': 'sar'},
        'memory_usage.log': {'status': 'delegated', 'collector': 'free'},
        'disk_bandwidth.log': {'status': 'initializing'},
        'pcm.csv': {'status': 'unavailable', 'reason': 'PCM hardware counters are not collected under WSL2'},
        'npu_usage.csv': {'status': 'unavailable', 'reason': 'NPU telemetry is not collected under WSL2'},
        'qmassa': {
            'status': 'unavailable',
            'reason': 'Linux DRM GPU telemetry is not collected under WSL2',
            'outputs': ['qmassa*-tool-generated.json', 'qmassa*-parsed.json', 'qmassa_error.log'],
        },
    }

    def update_status(disk_status):
        status['disk_bandwidth.log'] = disk_status
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(status, indent=2) + '\n')
        temporary.replace(status_path)

    update_status({'status': 'initializing'})
    previous = None
    previous_time = None
    with (results_dir / 'disk_bandwidth.log').open('w', buffering=1) as output:
        output.write(json.dumps({
            'format': 'wsl_disk_io_v1',
            'scope': status['scope'],
            'collector': 'psutil' if psutil is not None else '/proc/diskstats',
            'aggregation': 'whole leaf block devices; excludes partitions, loop and RAM disks',
        }) + '\n')
        while not stop.is_set():
            try:
                current = read_counters()
                current_time = time.monotonic()
                if previous is not None:
                    try:
                        rates = disk_rates(previous, current, current_time - previous_time)
                    except ValueError as error:
                        update_status({'status': 'unavailable', 'reason': str(error)})
                    else:
                        output.write(json.dumps({
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'elapsed_seconds': current_time - previous_time,
                            'devices': rates,
                            'read_bytes_per_second': sum(value['read_bytes_per_second'] for value in rates.values()),
                            'write_bytes_per_second': sum(value['write_bytes_per_second'] for value in rates.values()),
                        }) + '\n')
                        update_status({'status': 'collected', 'devices': sorted(rates)})
                previous, previous_time = current, current_time
            except (OSError, ValueError, RuntimeError) as error:
                update_status({'status': 'unavailable', 'reason': str(error)})
                previous = None
            stop.wait(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--results-dir', type=Path, default=Path('/tmp/results'))
    parser.add_argument('--interval', type=float, default=1.0)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error('--interval must be positive')
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    collect(args.results_dir, args.interval, stop)


if __name__ == '__main__':
    main()