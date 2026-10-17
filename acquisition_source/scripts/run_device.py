#!/usr/bin/env python3
"""Run a single Android measurement; collect raw HAL traces without mixing sensors."""
import argparse, datetime, hashlib, json, math, os, pathlib, re, shutil, subprocess, threading, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = "vn.ictu.thermalbench"
DEFAULT_ADB = (
    os.environ.get("THERMALBENCH_ADB")
    or shutil.which("adb")
    or (
        str(
            pathlib.Path(
                os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT"))
            )
            / "platform-tools/adb"
        )
        if os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT"))
        else "adb"
    )
)


def call(adb, serial, *args, timeout=30, check=True):
    p = subprocess.run(
        [adb, "-s", serial, *args], text=True, capture_output=True, timeout=timeout
    )
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout


def sensors(raw):
    match = re.search(
        r"Current temperatures from HAL:(.*?)Current cooling devices from HAL:",
        raw,
        re.S,
    )
    block = match.group(1) if match else ""
    return [
        dict(value=float(v), type=int(t), name=n, severity=int(s))
        for v, t, n, s in re.findall(
            r"Temperature\{mValue=([-0-9.]+), mType=(\d+), mName=([^,}]+), mStatus=(\d+)\}",
            block,
        )
    ]


def validate(directory, mode, prefill, decode):
    points, issues = [], []
    result_path = directory / "results.jsonl"
    for line in result_path.read_text().splitlines() if result_path.exists() else []:
        try:
            point = json.loads(line)
            if not isinstance(point, dict):
                raise ValueError("Result is not an object")
            points.append(point)
        except (ValueError, TypeError):
            issues.append("malformed_result_row")
    if not points:
        issues.append("no_result_rows")

    def positive(value):
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        )

    for point in points:
        if point.get("status") != "ok":
            issues.append("non_ok_rows")
        if mode == "idle":
            continue
        if mode == "selftest":
            elapsed = point.get("observed_elapsed_ns")
            if not positive(elapsed) or not 80_000_000 <= elapsed <= 500_000_000:
                issues.append("clock_selftest_out_of_range")
            continue
        counts_ok = all(
            type(point.get(key)) is int and point[key] > 0
            for key in ["actual_prefill_tokens", "actual_decode_tokens"]
        )
        if not counts_ok:
            issues.append("invalid_actual_token_counts")
        metrics_ok = all(
            positive(point.get(key))
            for key in [
                "native_ttft_s",
                "native_prefill_tokens_per_s",
                "native_decode_tokens_per_s",
            ]
        )
        if not metrics_ok:
            issues.append("missing_nonfinite_or_nonpositive_metric")
        if counts_ok and metrics_ok:
            prefill_s = (
                point["actual_prefill_tokens"] / point["native_prefill_tokens_per_s"]
            )
            decode_s = (
                point["actual_decode_tokens"] / point["native_decode_tokens_per_s"]
            )
            proxy = prefill_s + 1 / point["native_decode_tokens_per_s"]
            if not math.isclose(
                proxy, point["native_ttft_s"], rel_tol=1e-5, abs_tol=1e-7
            ):
                issues.append("native_ttft_formula_mismatch")
            elapsed = point.get(
                "app_call_elapsed_ns"
                if mode == "fixed"
                else "app_generation_elapsed_ns"
            )
            if not positive(elapsed):
                issues.append("invalid_app_duration")
            elif prefill_s + decode_s > elapsed / 1e9 + 0.05:
                issues.append("native_work_duration_exceeds_app_wall")
        if mode == "fixed" and (
            point.get("actual_prefill_tokens") != prefill
            or point.get("actual_decode_tokens") != decode
        ):
            issues.append("fixed_work_mismatch")
        if mode == "natural":
            first = point.get("observed_first_nonempty_chunk_ns")
            elapsed = point.get("app_generation_elapsed_ns")
            if not positive(first) or not positive(elapsed) or first > elapsed:
                issues.append("invalid_first_chunk_boundary")
            if not point.get("response_text"):
                issues.append("empty_text_response")
    report = {
        "passed": not issues,
        "issues": sorted(set(issues)),
        "rows": len(points),
        "phase_rows": {
            phase: sum(p.get("phase") == phase for p in points)
            for phase in ["warmup", "measure"]
        },
        "backend_independently_verified": False,
    }
    (directory / "validation.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", required=True)
    ap.add_argument("--adb", default=os.environ.get("THERMALBENCH_ADB", DEFAULT_ADB))
    ap.add_argument(
        "--mode", choices=["selftest", "idle", "fixed", "natural"], default="selftest"
    )
    ap.add_argument("--model", default="")
    ap.add_argument("--monitoring", choices=["full", "minimal"], default="full")
    ap.add_argument("--backend", choices=["CPU", "GPU", "NPU"], default="NPU")
    ap.add_argument("--prefill", type=int, default=128)
    ap.add_argument("--decode", type=int, default=64)
    ap.add_argument("--duration", type=int, default=0)
    ap.add_argument("--iterations", type=int, default=1)
    ap.add_argument("--warmup", type=int, default=0)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument(
        "--prompt", default="Explain edge AI on a mobile phone in two short sentences."
    )
    a = ap.parse_args()
    rid = (
        a.run_id
        or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        + "-"
        + a.mode
        + "-"
        + a.backend.lower()
    )
    if not re.fullmatch("[A-Za-z0-9_-]+", rid):
        raise ValueError("invalid run id")
    directory = ROOT / "runs" / rid
    directory.mkdir(parents=True, exist_ok=False)
    remote = f"/sdcard/Android/data/{PACKAGE}/files/runs/{rid}"
    context = {
        "host_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_serial": a.serial,
        "mode": a.mode,
        "backend_requested": a.backend,
        "ambient_c": None,
        "environment": json.loads((ROOT / "intake/environment.json").read_text()),
        "monitoring": a.monitoring,
        "case_state": "not_observed",
        "independent_backend_profile": "not_yet_available",
    }
    for key, args in {
        "fingerprint": ["getprop", "ro.build.fingerprint"],
        "battery": ["dumpsys", "battery"],
        "thermal": ["dumpsys", "thermalservice"],
        "screen_brightness": ["settings", "get", "system", "screen_brightness"],
        "screen_brightness_mode": [
            "settings",
            "get",
            "system",
            "screen_brightness_mode",
        ],
        "power_mode": ["settings", "get", "global", "low_power"],
        "device_uptime": ["cat", "/proc/uptime"],
    }.items():
        context[key] = call(a.adb, a.serial, "shell", *args)
    context["power_class"] = (
        "externally_powered_wired_ADB"
        if re.search(r"(?:AC|USB|Wireless|Dock) powered:\s*true", context["battery"])
        else "unplugged_observed_at_start"
    )
    (directory / "host_context.json").write_text(json.dumps(context, indent=2))
    stop = threading.Event()

    def collect():
        with (directory / "host_thermal.jsonl").open("w") as f:
            while not stop.is_set():
                start = time.monotonic_ns()
                try:
                    raw = call(a.adb, a.serial, "shell", "dumpsys", "thermalservice")
                    uptime = call(a.adb, a.serial, "shell", "cat", "/proc/uptime")
                    sample = {
                        "host_before_ns": start,
                        "host_after_ns": time.monotonic_ns(),
                        "host_utc": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                        "device_uptime": uptime.strip(),
                        "raw": raw,
                        "hal_sensors": sensors(raw),
                    }
                except Exception as e:
                    sample = {"error": str(e), "host_before_ns": start}
                f.write(json.dumps(sample) + "\n")
                f.flush()
                stop.wait(max(0, 1 - (time.monotonic_ns() - start) / 1e9))

    collector = threading.Thread(target=collect)
    if a.monitoring == "full":
        collector.start()
    logproc = None
    try:
        command = [
            "shell",
            "am",
            "start",
            "-S",
            "-n",
            PACKAGE + "/.BenchmarkActivity",
            "--es",
            "run_id",
            rid,
            "--es",
            "monitoring",
            a.monitoring,
            "--es",
            "mode",
            a.mode,
            "--es",
            "backend",
            a.backend,
            "--es",
            "model_path",
            "'" + a.model.replace("'", "'\\''") + "'",
            "--ei",
            "prefill_tokens",
            str(a.prefill),
            "--ei",
            "decode_tokens",
            str(a.decode),
            "--ei",
            "duration_seconds",
            str(a.duration),
            "--ei",
            "iterations",
            str(a.iterations),
            "--ei",
            "warmup",
            str(a.warmup),
            "--es",
            "prompt",
            "'" + a.prompt.replace("'", "'\\''") + "'",
        ]
        (directory / "launch.txt").write_text(call(a.adb, a.serial, *command))
        pid = call(a.adb, a.serial, "shell", "pidof", PACKAGE, check=False).strip()
        uid_raw = call(
            a.adb,
            a.serial,
            "shell",
            "cmd",
            "package",
            "list",
            "packages",
            "-U",
            PACKAGE,
            check=False,
        )
        uid_match = re.search(r"uid:(\d+)", uid_raw)
        if uid_match:
            (directory / "logcat_initial.txt").write_text(
                call(
                    a.adb,
                    a.serial,
                    "logcat",
                    "-d",
                    "--uid=" + uid_match.group(1),
                    "-v",
                    "threadtime",
                    check=False,
                )
            )
        if uid_match:
            logf = (directory / "logcat.txt").open("w")
            logproc = subprocess.Popen(
                [
                    a.adb,
                    "-s",
                    a.serial,
                    "logcat",
                    "--uid=" + uid_match.group(1),
                    "-T",
                    "1",
                    "-v",
                    "threadtime",
                ],
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + a.timeout
        while time.monotonic() < deadline:
            exists = (
                subprocess.run(
                    [
                        a.adb,
                        "-s",
                        a.serial,
                        "shell",
                        "test",
                        "-f",
                        remote + "/done.json",
                    ],
                    capture_output=True,
                ).returncode
                == 0
            )
            if exists:
                break
            stop.wait(1)
        else:
            call(a.adb, a.serial, "shell", "am", "force-stop", PACKAGE)
            raise TimeoutError("measurement deadline exceeded; app stopped")
        if uid_match:
            (directory / "logcat_final.txt").write_text(
                call(
                    a.adb,
                    a.serial,
                    "logcat",
                    "-d",
                    "--uid=" + uid_match.group(1),
                    "-v",
                    "threadtime",
                    check=False,
                )
            )
        pull = call(a.adb, a.serial, "pull", remote + "/.", str(directory))
        (directory / "pull.txt").write_text(pull)
        print(
            json.dumps(
                {
                    "run": rid,
                    "validation": validate(directory, a.mode, a.prefill, a.decode),
                },
                indent=2,
            )
        )
    except Exception as error:
        (directory / "runner_error.json").write_text(
            json.dumps({"error": str(error), "run": rid}, indent=2)
        )
        call(a.adb, a.serial, "shell", "am", "force-stop", PACKAGE, check=False)
        try:
            partial = call(
                a.adb, a.serial, "pull", remote + "/.", str(directory), check=False
            )
            (directory / "partial_pull.txt").write_text(partial)
        except Exception as recovery_error:
            (directory / "partial_pull_error.txt").write_text(str(recovery_error))
        raise
    finally:
        stop.set()
        if collector.is_alive():
            collector.join(timeout=40)
        if logproc:
            logproc.terminate()
            logproc.wait(timeout=10)
            logf.close()
        (directory / "battery_after.txt").write_text(
            call(a.adb, a.serial, "shell", "dumpsys", "battery", check=False)
        )


if __name__ == "__main__":
    main()
