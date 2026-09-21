import argparse
import json
import math
import re
import sys
import threading
import time


def aggregate_gpu(counters):
    engines = {}
    for instance, value in counters:
        match = re.search(r"luid_(.+?)_phys_(\d+)_eng_(\d+)_engtype_(.+)$", instance)
        if not match or not math.isfinite(value) or value < 0:
            continue
        luid, physical, engine, engine_type = match.groups()
        adapter = f"{luid}-phys{physical}"
        key = (adapter, f"{engine_type}/engine{engine}")
        engines[key] = engines.get(key, 0.0) + value
    result = {}
    for (adapter, engine), value in engines.items():
        result.setdefault(adapter, {})[engine] = min(100.0, value)
    return result


class GPUCounters:
    def __init__(self, pdh):
        self.pdh = pdh
        self.query = pdh.OpenQuery()
        try:
            self.counter = pdh.AddEnglishCounter(self.query, r"\GPU Engine(*)\Utilization Percentage")
            pdh.CollectQueryData(self.query)
            self.instances = set(pdh.GetFormattedCounterArray(self.counter, pdh.PDH_FMT_DOUBLE))
        except Exception:
            self.close()
            raise

    def sample(self):
        self.pdh.CollectQueryData(self.query)
        counters = self.pdh.GetFormattedCounterArray(self.counter, self.pdh.PDH_FMT_DOUBLE)
        values = [(instance, value) for instance, value in counters.items() if instance in self.instances]
        self.instances = set(counters)
        return aggregate_gpu(values)

    def close(self):
        self.pdh.CloseQuery(self.query)


def emit(value):
    print(json.dumps(value, allow_nan=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run this collector with Windows Python from WSL")
    import psutil
    import win32pdh

    gpu = None
    gpu_error = None
    try:
        gpu = GPUCounters(win32pdh)
    except Exception as error:
        gpu_error = str(error)
        print(f"GPU counters unavailable: {error}", file=sys.stderr)
    if args.check:
        if gpu:
            gpu.close()
        print("Windows psutil and pywin32 available")
        return
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), stop.set()), daemon=True).start()
    psutil.cpu_percent(interval=None)
    previous_disk = psutil.disk_io_counters()
    previous_time = time.monotonic()
    emit({"kind": "ready", "source": "Windows host", "gpu_error": gpu_error})
    try:
        while not stop.wait(1):
            now = time.monotonic()
            disk = psutil.disk_io_counters()
            sample = {"kind": "sample", "timestamp": time.time(),
                      "cpu_percent": psutil.cpu_percent(interval=None),
                      "memory_percent": psutil.virtual_memory().percent,
                      "disk_read_mib_s": None, "disk_write_mib_s": None,
                      "gpu": {}, "gpu_error": gpu_error}
            if disk is not None and previous_disk is not None:
                for direction in ("read", "write"):
                    delta = getattr(disk, direction + "_bytes") - getattr(previous_disk, direction + "_bytes")
                    if delta >= 0:
                        sample[f"disk_{direction}_mib_s"] = delta / (now - previous_time) / 1048576
            if gpu:
                try:
                    sample["gpu"] = gpu.sample()
                except Exception as error:
                    sample["gpu_error"] = str(error)
            else:
                try:
                    gpu = GPUCounters(win32pdh)
                    gpu_error = None
                except Exception as error:
                    gpu_error = str(error)
            emit(sample)
            previous_disk, previous_time = disk, now
    finally:
        if gpu:
            gpu.close()


if __name__ == "__main__":
    main()