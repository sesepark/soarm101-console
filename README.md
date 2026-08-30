# SO-ARM101 Console

> A local-first operator console for an SO-ARM101 leader–follower pair: inspect hardware, calibrate arms, teleoperate safely, and record LeRobot-compatible demonstrations from one browser UI.

Built as a portfolio project around the practical problems that appear between a working robot arm and a usable operator workflow: stable device identity, exclusive serial ownership, calibration gates, camera lifecycle, and an intentional motion-start flow.

## Highlights

- **Browser-based operations** — a FastAPI console for hardware status, camera preview, teleoperation, and dataset recording.
- **Stable hardware identity** — serial devices are addressed by `/dev/serial/by-id`; cameras by `/dev/v4l/by-path`, rather than volatile `ttyACM*` or `video*` indices.
- **Motion is gated** — teleoperation requires valid leader/follower calibration, an enabled motion flag, an explicit physical-workspace acknowledgement, and the `START SOARM101` confirmation phrase.
- **Exclusive hardware ownership** — a single active mode owns the serial bus and cameras; observation, teleop, and recording do not contend for device handles.
- **Dataset-oriented** — records local, reviewable LeRobot datasets with episode success/retry controls and no automatic Hub upload.
- **Operational documentation** — runbook, failure-mode guide, safety notes, architecture, and an ADR capture the decisions behind the implementation.

## System overview

```text
Browser
  │  loopback HTTP
  ▼
FastAPI console
  ├── read-only hardware doctor
  ├── leader → follower teleoperation (30 FPS, 2° relative-target limit)
  ├── camera preview workers
  └── LeRobot dataset recorder
          │
          ▼
SO-ARM101 leader / follower + scene / wrist cameras
```

The console binds to `127.0.0.1` by default. Remote use should go through an SSH tunnel rather than exposing an unauthenticated motion-control API on a LAN.

## Tech stack

| Area | Choice |
| --- | --- |
| Robot runtime | Python 3.12, [LeRobot](https://github.com/huggingface/lerobot) 0.6.1, Feetech SDK |
| Web console | FastAPI, Uvicorn, vanilla HTML/CSS/JavaScript |
| Video and data | OpenCV camera input, PyAV / MP4, LeRobot dataset format |
| Deployment | `uv`, systemd user service, udev device aliases |
| Validation | pytest hardware-free tests + read-only bus doctor |

## Quick start

### 1. Install

```bash
git clone https://github.com/<your-account>/so-arm101-console.git
cd so-arm101-console
uv sync --all-groups
cp config/soarm.env.example config/soarm.env
```

Set the leader, follower, and camera paths in `config/soarm.env` for your own hardware. Do not commit that file.

### 2. Verify without motion

```bash
./scripts/doctor.sh
.venv/bin/pytest -q
```

The doctor reads motor ID, model, firmware, position, voltage, and torque state without issuing a motion command.

### 3. Calibrate the arms

With the work area clear, a local observer present, and a power-cut method ready:

```bash
./scripts/calibrate_follower.sh
./scripts/calibrate_leader.sh
```

For each arm, place it near the middle of its safe range, press Enter, then sweep the requested joints one at a time through their safe usable ranges. Full instructions are in the [runbook](RUNBOOK.md).

### 4. Start the console

```bash
./scripts/run_web.sh
```

Open <http://127.0.0.1:8088>. To enable teleoperation, set `SOARM_ENABLE_MOTION=1` in the local configuration and restart the service. The browser still requires an explicit safety acknowledgement and `START SOARM101` before it starts a motion session.

## Operator workflow

| Step | Operator action | Guardrail |
| --- | --- | --- |
| 1 | Run **Environment Doctor** | Read-only; blocked while teleop or recording owns hardware |
| 2 | Confirm calibration files | Both roles must have valid motor IDs and ranges |
| 3 | Start **Teleoperation** | Motion flag + physical confirmation + typed phrase |
| 4 | Stop the current mode | Closes the active hardware owner before another mode starts |
| 5 | Record demonstrations | Requires confirmed camera roles as an additional gate |

## Repository layout

```text
src/soarm_console/        FastAPI app, teleop, recording, diagnostics, camera workers
scripts/                  Calibration, doctor, web, recording, service installation
config/                   Local runtime configuration template
deploy/                   systemd user service and udev rule
tests/                    Hardware-free API and safety-gate tests
ADR/                      Architecture decisions
RUNBOOK.md                Field procedure for calibration, teleop, and recording
SAFETY.md                 Safety invariants and current limitations
ARCHITECTURE.md           Ownership model and future extensibility
```

## Safety and scope

This is an experimental robotics project, **not** a safety-certified control system. The repository does not claim that software stop behavior replaces a physical E-stop or power cutoff. Start with small motions, keep a local observer present, and review [SAFETY.md](SAFETY.md) and [FAILURE_MODES.md](FAILURE_MODES.md) before operating hardware.

## Validation performed

- Hardware-free test suite: `12 passed`
- Read-only bus checks for both arms: motor IDs 1–6, voltage, firmware, position, and torque state
- Calibration JSON validation for leader and follower
- Browser preflight confirms device paths and calibration gates before enabling teleoperation

## Documentation

- [Runbook](RUNBOOK.md) — calibration, teleoperation, recording, and recovery sequence
- [Hardware notes](hardware.md) — USB/camera identity and the verified role mapping
- [Safety model](SAFETY.md) — what the system does and does not guarantee
- [Architecture](ARCHITECTURE.md) — exclusive hardware ownership and future policy/VLA integration
- [Protocol](PROTOCOL.md) — planned observation/action contract
- [Failure modes](FAILURE_MODES.md) — expected faults and operator response

## Acknowledgements

The arm runtime is built on [Hugging Face LeRobot](https://github.com/huggingface/lerobot) and its [SO-101 workflow](https://github.com/huggingface/lerobot/blob/main/docs/source/so101.mdx). This repository adds a local operations layer around that hardware workflow; it is not affiliated with or endorsed by Hugging Face.
