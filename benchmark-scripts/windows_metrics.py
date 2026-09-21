import contextlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
import uuid


MANIFEST = "windows_metrics.json"
UNAVAILABLE = {"gpu_power": "Unavailable via PDH GPU Engine counters",
               "memory_bandwidth": "Unavailable via psutil",
               "pcm": "Unavailable on WSL2", "npu": "No Windows NPU collector"}


def enabled(env=None):
    return (os.environ if env is None else env).get("WSL2", "false").lower() == "true"


def reporting_enabled(root):
    if "WSL2" in os.environ:
        return enabled()
    return (Path(root) / MANIFEST).is_file()


def collector_command():
    script = Path(__file__).with_name("collect_windows_metrics.py")
    windows_path = subprocess.check_output(
        ["wslpath", "-w", str(script)], text=True).strip()
    return ["python.exe", "-u", windows_path]


def preflight(env):
    subprocess.run(collector_command() + ["--check"], check=True, timeout=30)


def read_manifest(root):
    path = Path(root) / MANIFEST
    if path.exists():
        return json.loads(path.read_text())
    return {"source": "Windows host", "captures": [], "unavailable": UNAVAILABLE}


def write_manifest(root, manifest):
    destination = Path(root) / MANIFEST
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2))
    temporary.replace(destination)


def reset(root):
    write_manifest(root, {"source": "Windows host", "captures": [],
                          "unavailable": UNAVAILABLE})


def read_samples(path):
    samples = []
    with open(path) as source:
        for line in source:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("kind") == "sample":
                samples.append(entry)
    return samples


@contextlib.contextmanager
def measure(env, scenario="benchmark"):
    if not enabled(env):
        yield
        return
    root = Path(env["RESULTS_DIR"])
    directory = root / "windows-metrics"
    directory.mkdir(parents=True, exist_ok=True)
    relative = Path("windows-metrics") / (uuid.uuid4().hex + ".jsonl")
    capture = {"path": str(relative), "scenario": scenario,
               "pipeline_count": int(env.get("PIPELINE_COUNT", "1")),
               "selected": False, "status": "starting"}
    manifest = read_manifest(root)
    manifest["captures"].append(capture)
    write_manifest(root, manifest)
    with open(root / relative, "w") as output, open(
            root / "windows_metrics_error.log", "a") as errors:
        try:
            process = subprocess.Popen(collector_command(), stdin=subprocess.PIPE,
                                       stdout=output, stderr=errors, text=True)
        except (OSError, subprocess.SubprocessError):
            capture["status"] = "failed"
            write_manifest(root, manifest)
            raise
        completed = False
        try:
            deadline = time.monotonic() + 15
            while True:
                with open(root / relative) as source:
                    first_line = source.readline()
                if first_line.endswith("\n"):
                    ready = json.loads(first_line)
                    if ready.get("kind") != "ready":
                        raise RuntimeError("Windows collector did not report readiness")
                    if ready.get("gpu_error"):
                        print("WARNING: Windows GPU metrics unavailable: " + ready["gpu_error"])
                    break
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Windows collector failed to start; see windows_metrics_error.log")
                time.sleep(0.05)
            capture["status"] = "collecting"
            yield
            completed = True
        finally:
            try:
                process.communicate(input="stop\n", timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            samples = read_samples(root / relative)
            capture["sample_count"] = len(samples)
            capture["status"] = "complete" if completed and process.returncode == 0 and samples else "failed"
            capture["selected"] = capture["status"] == "complete"
            if capture["selected"]:
                for previous in manifest["captures"][:-1]:
                    if previous["scenario"] == scenario:
                        previous["selected"] = False
            write_manifest(root, manifest)
            if capture["status"] == "failed":
                print("WARNING: Windows metrics collection failed; see windows_metrics_error.log")


def select_iteration(env, scenario, pipeline_count):
    if not enabled(env):
        return
    manifest = read_manifest(env["RESULTS_DIR"])
    candidates = []
    for capture in manifest["captures"]:
        if capture["scenario"] == scenario:
            capture["selected"] = False
            if capture["pipeline_count"] == pipeline_count and capture["status"] == "complete":
                candidates.append(capture)
    if candidates:
        candidates[-1]["selected"] = True
    else:
        print(f"WARNING: No Windows metrics for selected pipeline count {pipeline_count}")
    write_manifest(env["RESULTS_DIR"], manifest)


def selected_samples(root):
    for capture in read_manifest(root)["captures"]:
        if capture.get("selected"):
            yield capture, read_samples(Path(root) / capture["path"])


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def series(samples):
    fields = {"CPU Utilization (%)": "cpu_percent", "Memory Usage (%)": "memory_percent",
              "Disk Read (MiB/s)": "disk_read_mib_s", "Disk Write (MiB/s)": "disk_write_mib_s"}
    values = {label: [sample.get(key) for sample in samples] for label, key in fields.items()}
    engines = sorted({(adapter, engine) for sample in samples
                      for adapter, counters in sample.get("gpu", {}).items() for engine in counters})
    for adapter, engine in engines:
        values[f"GPU {adapter} {engine} (%)"] = [
            sample.get("gpu", {}).get(adapter, {}).get(engine) for sample in samples]
    return values


def summary(root):
    result = {"CPU Utilization %": "NA", "NPU Utilization %": "NA",
              "Memory Utilization %": "NA", "Disk Read MB/s": "NA",
              "Disk Write MB/s": "NA", "S0 Memory Bandwidth Usage MB/s": "NA",
              "S0 Power Draw W": "NA"}
    selected = list(selected_samples(root))
    if len(selected) > 1:
        print("WARNING: metrics.csv uses the last selected Windows scenario; captures are not averaged together")
    samples = selected[-1][1] if selected else []
    fields = {"CPU Utilization %": ("cpu_percent", 1),
              "Memory Utilization %": ("memory_percent", 1),
              "Disk Read MB/s": ("disk_read_mib_s", 1048576 / 1000000),
              "Disk Write MB/s": ("disk_write_mib_s", 1048576 / 1000000)}
    for label, (field, multiplier) in fields.items():
        values = [sample[field] * multiplier for sample in samples if numeric(sample.get(field))]
        if values:
            result[label] = round(sum(values) / len(values), 2)
    adapters = sorted({adapter for sample in samples for adapter in sample.get("gpu", {})})
    gpu_fields = ("Compute[CCS] Utilization %", "Render/3D[RCS] Utilization %",
                  "Video[VCS] Utilization %", "VideoEnhance[VECS] Utilization %",
                  "Blitter Copy Engine %", "GPU Power (W)")
    for index in range(len(adapters)):
        for field in gpu_fields:
            result[f"GPU_{index} {field}"] = "NA"
    if not adapters:
        result["GPU Utilization %"] = "NA"
    return result