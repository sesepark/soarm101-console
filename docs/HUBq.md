# HUBq boundary

HUBq is a second process in this repository. It owns hardware exclusivity, scheduling, and job
lifetime; the console keeps motion logic, data handling, UI, and the public `/api/*` surface.
The console talks to HUBq only over its loopback HTTP API.

There is one deliberate import exception to that boundary: `hubq` may import
`soarm_console.owner_lock`, and no other `soarm_console` module. The lock implementation and its
`SOARM_OWNER_LOCK_FDS` inheritance format are already a cross-process contract used by console
workers. Copying either into HUBq would create two sources of truth.

The two services have separate processes, ports (`127.0.0.1:8094` for HUBq), and job-state
directories (`HUBQ_STATE_DIR=%h/.local/state/hubq`). They intentionally share exactly one rendezvous directory:
`SOARM_OWNER_LOCK_DIR=%t/soarm-console/owner-locks`. Both systemd units set it explicitly. HUBq also
runs through `sg dialout` because its later job children must open the same serial devices as console
children.

Stage 1 is observation-only. `GET /status` reads lock-file metadata and checks the corresponding
device/inode entries in `/proc/locks`; it never acquires a device lock. Metadata from an unlocked
file is historical and is therefore reported with `locked: false`, `owner: null`, and `pid: null`.
Later job stages must persist job state and reconcile it with live processes after HUBq restarts.
