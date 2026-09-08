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

Stage 1 was observation-only. `GET /status` reads lock-file metadata and checks the corresponding
device/inode entries in `/proc/locks`; it never acquires a device lock. Metadata from an unlocked
file is historical and is therefore reported with `locked: false`, `owner: null`, and `pid: null`.
Later job stages must persist job state and reconcile it with live processes after HUBq restarts.

Stage 2 added `POST /claim {kind, devices}`. HUBq returns a structured 409 with
the console's established English `detail` text so existing Mac and phone translation tables keep
working. Unknown kinds and an unreadable scheduler fail closed rather than starting a hardware job.
The console retains checks that are not ownership decisions: whether a requested model is the active
policy's model, whether a recording/calibration preview exists, and the ordered stop sequence.

Stage 3 moves the five command-line modes into HUBq. The five files under `src/hubq/kinds/` fix each
command, accepted owner name, required device roles, motion classification, and stop policy. The
console managers still validate requests, assemble `SOARM_*` overrides and interpret each worker's
runtime status; they now use the internal job API instead of acquiring locks or calling `Popen`.

`POST /jobs` acquires one `DeviceLockSet`, writes the job record atomically, launches a new process
session with `pass_fds` and `SOARM_OWNER_LOCK_FDS`, and writes stdout directly to the fixed runtime
log declared by its kind. The remote-policy SSH tunnel is a declared policy sidecar owned by the same job, so a
console restart does not tear the transport out from under the rollout. `GET /jobs`,
`GET /jobs/{id}`, and `POST /jobs/{id}/stop` are loopback-only internal APIs. Virtual leader remains
a console thread and uses `/registrations/virtual-leader` only to register/release its ownership.

Every job record contains PID plus Linux process start ticks, preventing PID reuse from being
mistaken for recovery. HUBq runs `reconcile()` at startup: a matching live process becomes a
reconciled running job; a missing or mismatched process becomes `lost`. The worker inherited the
lock descriptors, so stopping HUBq closes only HUBq's copies and leaves both the worker and its
kernel locks alive. The unit therefore uses `KillMode=process`; `ExecStop` asks uvicorn to stop itself
instead of killing the whole cgroup. Job logs go to files rather than daemon-owned pipes for the
same reason.

Teleoperation and open-ended recording add an abandoned-session deadline. Their kind files declare
`limit_seconds: 3600`; the other three self-terminating kinds have no deadline. A dedicated console
heartbeat renews every running limited job to `now + limit_seconds`, independently of whether a Mac
or phone currently has the status screen open. There is deliberately no absolute extension cap:
this is an owner-liveness guard, not a usage quota. If the console heartbeat disappears while HUBq
stays alive, HUBq sends the kind's existing graceful `stop_signal` when the last renewed deadline
passes and retains its existing `stop_timeout`/`kill_after_timeout` policy. The deadline and expiry
request time live in the durable job record and survive HUBq reconciliation.

The console shutdown hook no longer stops these five jobs. A newly started console discovers active
jobs by kind and restores manager metadata from HUBq. Stop calls go through HUBq, with a direct
process-group signal only as the safety escape hatch when HUBq cannot be reached; stopping an arm
must never be gated on scheduler availability.

There is still no automatic motion queue in this stage. Every motion kind also requires the console
to set the per-start `confirmed` gate after its existing human confirmation, so no arm-moving kind
can start from a queue without that fresh confirmation. Adding a queue later must preserve that rule
(or explicitly require the existing fresh onsite deadman lease).
