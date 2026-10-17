# Thermal-aware mobile inference measurements

Version **1.1.0** — [archived data and code](https://doi.org/10.5281/zenodo.23437172) · [source repository](https://github.com/ictu-se/thermal-aware-mobile-inference). This single complete package contains the observations supporting the revised study; no earlier package or split-archive assembly is required.

This package contains reproducibility code and numerical observations from one HONOR Magic V5 (MBH-N49, SM8750), Android 16, MagicOS 10.0.0.166C636E5R4P2, application 0.2.0, LiteRT-LM 0.18.0. It contains no manuscript, response letter, private device dump, model weights, vendor libraries, APK, or system-wide trace. Physical conditions were user-reported: unfolded, no case, on a table, air-conditioner setpoint 26 °C; actual ambient temperature was not measured. Measurements used external power.

## Install and reproduce numerical results

Python 3.9 or later is required. Run from the package root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 scripts/analyze.py studies/unified_experiments_20261016
python3 scripts/generate_results.py
python3 scripts/reconcile_history.py
python3 -m unittest discover -s acquisition_source/scripts -p 'test_*.py'
```

`analyze.py` reconstructs request-level points, paired comparisons, sensor summaries and repeated-use outcomes from retained numeric observations and the controller event log. Original primary comparisons contain 119 main observations, 59 complete main pairs and 38 complete monitoring pairs. All twelve long sessions stop at severe status and contribute 892 dependent requests. Three earlier premeasurement attempts admitted no inference observation: the first stopped at severity one, the second lost USB during calibration, and the third was interrupted by Mac clamshell sleep and exhausted its initial connection-wait bound. All three records are retained under `premeasurement_failures/`. After verifying an open Mac lid, host AC power, and renewed ADB authorization, one new bounded acquisition used fresh identifiers, a new idle reference, home-screen rest with the display kept awake on external power, and an initial recovery bound of 1800 seconds. This campaign permits one calibration attempt and one inference attempt per scheduled unit. The supplementary cohort has new identifiers and an independent idle reference; it never replaces primary exclusions or increases primary sample size. Do not concatenate cohorts. Each configured unit has one attempt; a severe stop is a measured outcome.

`generate_results.py` regenerates numerical tables as CSV, empirical figures as PNG, and a structured metric report under `generated/`. Fixed work, monitoring, and repeated-use figures use retained measurements. The original three absent partners stay absent. Block intervals use 10,000 whole segment-by-block resamples with seed 20261016; they describe one handset, not a population of devices. Minimal monitoring retains safety/timing instrumentation and is not a zero-overhead reference.

`reconcile_history.py` verifies the earlier 167-container snapshot, 147 six-component containers, 37 later additions, and 155 ledger entries representing 146 identifiers. The published historical evidence is a component-presence inventory and ledger identifiers. It supports accounting reconstruction, not reanalysis of older performance claims; those older measurements do not enter the new estimates.

## Rebuild the Android application

Use JDK 17 and Android SDK platform 36. The Gradle wrapper pins Gradle 9.5.1; the project pins Android Gradle Plugin 9.2.1, Kotlin 2.4.0 and LiteRT-LM 0.18.0. Before building, obtain the matching Qualcomm/HTP dispatch libraries from the official Google AI Edge Gallery release 1.0.20 (<https://github.com/google-ai-edge/gallery/releases/tag/1.0.20>), subject to their upstream terms. Extract the five arm64 shared libraries named in `android/native-library-hashes.json` into `android/app/src/main/jniLibs/arm64-v8a/` and verify every SHA-256 against that manifest. Libraries are not redistributed here.

```sh
cd android
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The measured APK digest is in each study's `intake/build_identity.json`. A fresh local debug signing key produces a different APK digest. Record the rebuilt digest as a new build identity before fresh acquisition; never alter the recorded study identity. Model download URLs, pinned upstream revisions, filenames and digests are in `models/manifest.json` under each study. Obtain the model files from those official upstream locations under their licenses, verify hashes, and copy them into the application's external model directory.

## Run a new acquisition

Do not run a controller inside a retained completed study. Use the helper to create an empty sibling workspace containing reusable acquisition code and frozen protocol templates:

```sh
python3 scripts/new_campaign.py --output ../new-thermal-study --adb adb --serial YOUR_SERIAL
python3 ../new-thermal-study/scripts/preflight.py --adb adb --serial YOUR_SERIAL
python3 ../new-thermal-study/scripts/freeze_inputs.py
python3 ../new-thermal-study/scripts/campaign.py --adb adb --serial YOUR_SERIAL
```

The helper records the current installed APK and firmware identity, but does not run the workload. Preflight verifies model hashes and collects new pilots excluded from main estimates. Review protocol/environment values in the new workspace before preflight; do not copy old pilot acceptance. The controller requires a fresh 600-second idle reference after the near-full-charge/stable-state gate. Later launches require a complete 60-second severity-zero window and the prespecified multi-sensor bounds. Recovery is bounded at 1800 seconds; pair retries are prohibited. Place a file named `PAUSE` in the new workspace to request stopping before the next operation. A device lock prevents overlapping controllers. Do not resume an interrupted partial pair without an explicit accounting review; the source checks refuse an unaccounted partial pair.

## Independent trace scope

The retained studies supply Perfetto configurations, per-capture outcomes and query exports for phase slices, attributed application CPU, frequency counters and positive loss counters. Original fixed Gemma attribution was missing. Added process-lifecycle/process-polling diagnostics are reported separately. A missing annotation is not zero CPU work. Neither trace configuration establishes NPU operator placement. System-wide raw traces stay private because they include unrelated device activity; the supplied query exports reproduce the reported trace-coverage table. To collect new traces, use the supplied capture source after a new completed campaign and an official Perfetto trace processor; the supplied Apache-licensed launcher fetches the official processor from <https://get.perfetto.dev/trace_processor>. After the new campaign completes, run `python3 ../new-thermal-study/profiling/capture.py --adb adb --serial YOUR_SERIAL`. The helper creates the needed trace directories and copies the diagnostic configuration.

## Provenance and boundaries

Numeric observations, timestamps, failed dispositions and original exclusions are retained; private serials/host paths are redacted. `source-provenance.json` records hashes of the source used during collection; `source-adaptations.json` documents the portable analysis adapter and the stricter supplementary initial-recovery bound. The shared source prepares fresh acquisitions with the original primary protocol; the supplementary protocol records its own stricter initial bound. `inventory.json` records the exported files and their digests. No energy, quality-equivalence, universal model preference, or complete NPU-offload claim follows from these data. The version-specific DOI identifies this exact release. The GitHub repository provides the corresponding reusable source and numerical observations.

## Additional retained cohorts

The explicitly requested later acquisition and six-condition requested-backend grid are kept separate from the primary estimates and all preceding failures. Regenerate their numerical outputs with:

```bash
python3 scripts/additional_results.py --studies studies --output generated/additional
```

Create a fresh diagnostic workspace only after a new acquisition has an admitted idle baseline:

```bash
python3 scripts/new_backend_grid.py --baseline-study /path/to/completed-acquisition --workspace /path/to/new-empty-grid
python3 /path/to/new-empty-grid/scripts/run_backend_diagnostics.py --adb adb --serial DEVICE_SERIAL
```

The grid tests requested labels on the same pinned packages. Within-capture calls are dependent; library loading and CPU/application slices do not prove NPU operator placement. System-wide traces remain private; their numerical query exports and complete failed-condition dispositions are retained.

Run the additional transport-loss and missing-data checks with `python3 -m unittest discover -s scripts -p 'test_*.py'`. These use synthetic fixtures and never connect to a phone.

Use `scripts/new_campaign.py --template completion --campaign-id FRESH_ID` with the documented output/ADB/device arguments to prepare the three-pair supplementary protocol. The default template remains the primary protocol. Both create fresh identities and require the documented preflight and input freeze. For independent fixed and streaming traces, the fresh capture entry point accepts `--modes fixed natural`. For results from fresh workspace locations, `additional_results.py` accepts `--retry-study` and `--backend-study` in addition to its published-data defaults.

Use `new_backend_grid.py --template hardware` with the same baseline/workspace arguments for the separate two-model kernel-event/power-counter capability protocol. Unsupported vendor patterns and absent rails are missing coverage, not evidence of zero NPU work or energy. `additional_results.py` also accepts `--hardware-study` and `--metadata-study` for fresh locations.

The optional metadata module reads declarations through the installed target application's pinned SDK, without replacing the target APK or executing inference:

```bash
cd android
./gradlew :metadata-probe:assembleDebug
cd ..
python3 scripts/metadata_probe.py --baseline-study /path/to/completed-acquisition --study-root /path/to/new-metadata-output --apk android/metadata-probe/build/outputs/apk/debug/metadata-probe-debug.apk --aapt "$ANDROID_HOME/build-tools/36.0.0/aapt2" --adb adb --serial DEVICE_SERIAL
```

Only the dedicated instrumentation APK is installed, its target APK digest is verified before and after reading, and the test package is removed. The result is metadata evidence, not independent execution placement, realized sampler verification, or a maximum-context stress test. Model weights, vendor binaries, APKs, build caches and manuscripts remain excluded.

## Recorded outcomes and interpretation

The primary dataset contains 119 valid fixed-work requests (59 complete model pairs), 78 valid monitoring requests (38 complete full/minimal pairs), and 12 repeated-use trajectories with 892 dependent requests. Every repeated-use trajectory ends at the prescribed severe-status stop; none reaches the 900-second target. Original exclusions remain retained.

A separate further acquisition contains six valid requests in three complete pairs plus two diagnostic captures. The earlier incomplete supplementary acquisition remains separate and contributes one valid standalone request, no pair, three gate timeouts, two unlaunched partners, and two failed captures.

The requested-backend grid prescribes 18 calls across six model/backend conditions. It starts 10 attempts: six NPU-requested calls succeed, four CPU/GPU attempts fail during engine creation with a missing input tensor, and eight remaining calls are unlaunched after those errors. Those initialization failures describe these exact packages and runtime; they are not CPU/GPU speed measurements.

Two separate kernel/power capability probes contribute six valid NPU-requested calls. Observed battery tracks are capacity, charge, and current; usable vendor-operator and power-rail evidence is unavailable. No independent NPU allocation or calibrated energy-per-request result is claimed. Read-only metadata declares context limits of 1280 and 4224 tokens, and CPU/GPU/NPU backend labels, without validating all execution paths or maximum-context performance.

Numerical query exports reproduce the trace-coverage summaries. System-wide traces remain private because they include unrelated device activity. Model weights and vendor libraries are obtained separately using the pinned upstream identities and hashes.

## Verify the release

```sh
python3 scripts/verify_release.py
python3 -m unittest discover -s acquisition_source/scripts -p 'test_*.py'
python3 -m unittest discover -s scripts -p 'test_*.py'
```

The inventory covers distributed source and observations. Generated figures can contain writer timestamps when regenerated; scientific numerical outputs are verified separately. The optional Gradle wrapper downloads the pinned toolchain. No network service or phone is needed for the 48 portable tests.

## Citation and license

Cite Nguyen The-Vinh (2026), *Thermal-aware measurement and reproducibility of on-device generative AI: Code and experimental data*, version 1.1.0, DOI: 10.5281/zenodo.23437172. Author-provided code is licensed under Apache-2.0; upstream notices and dependency licenses continue to apply. Measurements and numerical exports are supplied under the same release license.
