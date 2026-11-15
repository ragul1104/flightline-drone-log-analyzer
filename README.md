# Flightline — Universal Drone Flight Log Analyzer

A local-first, desktop-friendly web app for inspecting decoded flight telemetry. Uploads are read by the app running on your computer; the log file is not sent to a third-party service. The chart libraries are loaded from public CDNs, so graph rendering needs an internet connection on first use. The rest of the interface is served locally.

## Start on Windows

Python 3.10 or newer is recommended. From this folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. Keep the terminal open while using the app. Stop it with Ctrl+C. The server binds to loopback only and does not expose the analyzer to the network.

Uploads stream directly to a temporary local file and the default size limit is 1 GB. To set a different limit in PowerShell before starting the server, for example 2 GB:

```powershell
$env:FLIGHTLINE_MAX_UPLOAD_MB = "2048"
python -m uvicorn main:app --host 127.0.0.1 --port 8765
```

The practical limit also depends on available RAM and the parser used by the log format. Large Blackbox logs are read into memory by the parser, so keep the configured limit within the computer’s capacity.

## Implemented in this milestone

- File selection and drag-and-drop with content/signature detection for PX4 ULog, ArduPilot DataFlash BIN, and Blackbox logs (`.bbl`, `.bfl`, `.txt`). Blackbox firmware is identified as Betaflight or iNav only when the log headers establish that; otherwise it is reported as unknown Blackbox.
- Separate parser adapters: PX4's `pyulog`, ArduPilot's `pymavlink` DataFlash reader, and `orangebox` for the Cleanflight/Betaflight Blackbox format family.
- Timestamped normalized series, channel coverage percentages, missing-channel warnings, log-span summary, a field selector with synchronized time axis, event timeline, and 3D position path when latitude and longitude are recorded.
- Conservative findings for firmware status fields and explicit unavailability statements for GPS and RPM when those channels are absent.
- JSON report, CSV series, and print-to-PDF via the browser's print dialog.
- Beginner and Pro views. Pro view includes a preview of decoded rows.
- Original-file streaming with upload percentage, background parsing, and a responsive page while analysis runs. Timestamp normalization and channel-coverage counting are linear-time. Parser adapters retain exact full-log counts, duration, and field coverage while keeping at most 5,000 evenly spaced rows for the charts and CSV preview.

## Coverage and limitations

This is an analyzer foundation, not a certified causal-failure detector. The adapters expose fields decoded by the libraries; a field name by itself does not prove its units or meaning. The generic channel list preserves source field names. It does not currently map every firmware/version's equivalent channels into a single semantic vocabulary. Large series are reduced to at most 5,000 evenly spaced points for the graph and CSV; this display sample does not alter decoded count, time span, or channel coverage. Transient findings may be missed in the sample, and sampled voltage ranges are labeled as estimates. Very large logs still require a full local decode and can take time depending on hardware and file format, but the page remains responsive and no longer creates a base64 copy of the entire file.

- **Betaflight / iNav Blackbox:** Orangebox supports the Cleanflight/Betaflight Blackbox family, and iNav logs that conform to that family may decode. Differences across iNav versions, binary layouts, compressed/container variants, and unsupported Blackbox fields can fail decoding. The official `blackbox_decode` tools remain the reference fallback for logs Orangebox cannot decode. This app does not bundle that separate executable. Orangebox is pinned to 0.4.0 because 0.5.0 currently has invalid console-script metadata that makes pip fail installation.
- **ArduPilot:** pymavlink reads binary DataFlash logs; the app currently extracts timestamped numeric message fields and keeps their message prefixes. It does not yet interpret every message's scaling, units, firmware-specific event/state, or text-log variant.
- **PX4:** pyulog reads ULog topics and arrays. The app preserves topic-qualified field names. Its first-milestone view does not yet perform complete topic resampling/interpolation, parameter-aware unit conversion, or all PX4 event interpretation.
- **Time and flight phases:** displayed duration is the first-to-last decoded timestamp span. It is not independently established airborne time. Takeoff, landing, and failure points are not inferred unless explicit validated state rules are added in a later milestone.
- **Diagnostics:** this milestone avoids thresholds that require configuration or sensor context. Vibration, battery sag/remaining capacity, GPS health, ESC failure, motor RPM, and root-cause diagnosis need firmware-specific interpretation and corroborating evidence. Motor commands are not measured RPM. Coordinates and altitude are not synthesized when absent.
- **3D view:** only plotted when decoded latitude and longitude fields are present. The viewer uses source values and labels axes accordingly; it is not a terrain map or reconstruction of absent position data.

## Planned, not yet implemented

Version-aware channel/unit mappings for each firmware; parameter-aware thresholds and validated failsafe/GPS/vibration/battery/ESC/control rule packs; robust takeoff/landing/failure phase detection; diagnostic residuals with causal confidence; timestamp-aligned cross-topic interpolation; altitude unit normalization and attitude-driven 3D replay; and real sample-log compatibility coverage across firmware releases. Values and features in this list must not be inferred from the current UI when they are absent from a report.

## Validation

Run the local unit checks with:

```powershell
python -m unittest discover -s tests -v
```

The included tests cover normalized timestamps, missing-channel reporting, safe RPM/GPS unavailability, and downsampling boundaries on known synthetic records. They do not claim a full compatibility certification against every firmware version or real-world flight fixture. For format-level validation, add representative logs from each supported firmware/version, with sensitive coordinates removed where appropriate.

## Upstream parsers

- PX4 ULog: [PX4/pyulog](https://github.com/PX4/pyulog)
- ArduPilot DataFlash: [ArduPilot/pymavlink](https://github.com/ArduPilot/pymavlink)
- Cleanflight/Betaflight Blackbox: [orangebox on PyPI](https://pypi.org/project/orangebox/) and [Betaflight blackbox-tools](https://github.com/betaflight/blackbox-tools)
- iNav Blackbox logging and decoder guidance: [iNav documentation](https://github.com/iNavFlight/inav/blob/master/docs/Blackbox.md)

Orangebox is GPL-3.0; review its license and distribution requirements before redistributing a packaged copy of this app. The app installs it as a dependency rather than bundling its source.
