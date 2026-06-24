# Fabro Docker Sandbox — Technical Report

## 1. Container Lifecycle

**One Docker container per workflow run** (not per node). Container named `fabro-run-{run_id}`.

### Creation (INITIALIZE phase — `initialize.rs:357-364`)
1. Pull image if not cached locally (controlled by `auto_pull=true`, default true)
2. `docker.create_container()` with name `fabro-run-{run_id}`
3. `docker.start_container()`
4. Health check via `docker exec`, verifies exit code 0
5. Git clone into `/workspace` (unless `skip_clone=true`)

Container entrypoint: `/bin/bash -lc "mkdir -p /workspace && sleep infinity"` — stays alive for the entire run duration.

### Destruction — DEFERRED pattern
- At FINALIZE phase: container is **stopped** (not deleted) if `stop_on_terminal=true` (`finalize.rs:505-518`)
- Actual **deletion** happens via a `cleanup_guard` spawned at the START of the **next** run (`initialize.rs:366-374`)
- Deletion uses `RemoveContainerOptions { force: true }` — force-removes even running containers

> **Gotcha**: orphaned containers from crashed or last runs persist until a new run initializes. A watchdog job is needed for production.

### Container reuse between runs — NO (except checkpoint resume)

**Each run always gets a brand-new container.** Container names include the `run_id` (`fabro-run-{run_id}`), which is unique per run, so no two runs share a name or container.

**Exact sequence when run N+1 starts:**
1. `cleanup_guard` fires → `docker rm -f fabro-run-{run_N}` (deletes the previous stopped container)
2. `ensure_name_available()` inspects the new name `fabro-run-{run_N+1}` — if a container with that name already exists it throws a hard error (`docker.rs:735-736`):
   > `"Docker container name '...' already exists for run ... Remove the stale container manually before retrying."`
3. Fresh container is created and initialized from scratch

**Exception — checkpoint resume** (`initialize.rs:296`):

`attach_existing = options.checkpoint.is_some()`

When a run is resumed from a checkpoint:
- `attach_existing = true`
- The existing container is **reconnected** via `reconnect_for_run_with_callback`, not recreated
- The `cleanup_guard` is **skipped** (`(!attach_existing).then(...)` at line 366)
- `sandbox.start()` is called instead of `sandbox.initialize()`

This is the only case where a container survives into a subsequent run.

**Cold start cost per run** (image already cached locally):
`docker create` → `docker start` → health check exec → git clone

Every normal workflow execution pays this full cold start. There is no warm container pool.

---

## 2. Parallel Nodes — Same Container, Git Worktrees

Parallel branches share the **single run container** — no extra containers are created.

Each branch gets an isolated git worktree inside the container:

```
Main container: /workspace  (main git branch)
├─ Branch A: /workspace/.fabro/scratch/{run_id}/parallel/{node_id}/branch_a
├─ Branch B: /workspace/.fabro/scratch/{run_id}/parallel/{node_id}/branch_b
└─ Branch C: /workspace/.fabro/scratch/{run_id}/parallel/{node_id}/branch_c
```

**Execution flow** (`parallel.rs:241-450`):
1. Git checkpoint created as baseline before fan-out
2. Each branch gets a `WorktreeSandbox` wrapper (`worktree.rs`)
3. `git worktree add` creates isolated working directory per branch
4. Commands run via `docker exec -w {worktree_path}`
5. Branches execute concurrently via Tokio tasks
6. **Default max parallelism: 4** (configurable via `max_parallel` attribute)
7. Concurrency capped by a Tokio `Semaphore`
8. Cleanup: `git worktree remove` after each branch
9. Join policy: `wait_all` or `first_success`

> **AWS implication**: All parallel branches compete for the same container's CPU quota. 4 CPU-heavy branches on a 1-core quota = contention.

---

## 3. Resource Configuration

Defaults are **unlimited** — must be set explicitly for production.

### Config fields (`docker.rs:61-91`)

| Field | Type | Default | Notes |
|---|---|---|---|
| `image` | `String` | `"buildpack-deps:noble"` | Ubuntu 24.04 LTS |
| `memory_limit` | `Option<i64>` | `None` (unlimited) | bytes |
| `cpu_quota` | `Option<i64>` | `None` (unlimited) | microseconds per 100ms |
| `network_mode` | `Option<String>` | `Some("bridge")` | outbound open |
| `auto_pull` | `bool` | `true` | pull image if not local |
| `env_vars` | `Vec<String>` | `[]` | `KEY=VALUE` strings |
| `skip_clone` | `bool` | `false` | empty workspace if true |

**CPU math**: 1 core = 100,000 quota microseconds.
- `cpu = 2` → `cpu_quota = 200_000`
- `cpu = 4` → `cpu_quota = 400_000`

### settings.toml syntax (`from_environment.rs:69-104`)
```toml
[environments.production.resources]
memory = "4GB"   # parsed to bytes → memory_limit
cpu = 2          # cores × 100_000 → cpu_quota

[environments.production.network]
mode = "block"              # network_mode = "none" (full isolation)
# or:
mode = "cidr_allow_list"
allow = ["10.0.0.0/8"]
```

### Docker HostConfig (`docker.rs:1120-1128`)
```rust
HostConfig {
    binds: None,        // NO host bind mounts — hardcoded
    network_mode: ...,
    memory: ...,        // bytes
    cpu_quota: ...,
    ..Default::default()  // no seccomp/AppArmor overrides
}
```

Resources are released immediately when the container is force-removed (kernel cgroups).

---

## 4. Output Collection

Commands run via `docker exec` with attached stdout/stderr streams.

### Three execution modes
| Mode | Behavior | Used for |
|---|---|---|
| `docker_exec()` | blocks, collects full output | internal setup |
| `docker_exec_streaming()` | streams via async callback | live output |
| `docker_exec_shell_streaming()` | streaming + timeout + cancellation | actual node execution |

### ExecResult structure
```rust
ExecResult {
    stdout: String,
    stderr: String,
    exit_code: Option<i32>,
    termination: CommandTermination,  // Normal | TimedOut | Cancelled
    duration_ms: u64,
}
```

### Log tail
Last **8,192 bytes (8 KB)** retained for error display (`DEFAULT_EXEC_OUTPUT_TAIL_BYTES`). Full output is NOT buffered in memory — it is streamed through and discarded unless collected by a callback.

### File transfers
Files pulled from container: `docker.download_from_container()` via tar archive.

---

## 5. Error Handling

### Timeouts (`docker.rs:417-441`)
- `tokio::select!` races the command future against `time::sleep(timeout)`
- Returns `CommandTermination::TimedOut`
- Defaults: git clone = 300s, setup commands = 30s, general commands = 60s (all configurable per node)

### In-container cancellation via stop files (`docker.rs:838-879`)
Fabro cannot SIGTERM a `docker exec` from outside the container, so it uses a stop-file mechanism inside:

1. Each exec creates `/tmp/fabro-exec-{pid}-{nonce}-{seq}.stop` and `.pid` files
2. A bash watcher loop polls for the stop file (every 100ms prod, 5ms tests)
3. When stop file appears: `kill -TERM -{pgid}` → 200ms grace → `kill -KILL -{pgid}`
4. Uses `setsid` so the kill covers the entire process group/tree

### Container identity verification (`docker.rs:1153-1173`)
Before **every** operation, Fabro verifies labels:
- `sh.fabro.managed=true` — must be present
- `sh.fabro.run_id={run_id}` — must match current run

Prevents accidental operations on non-Fabro containers.

### Error categories (`error.rs:433-452`)
| Type | Retryable |
|---|---|
| Handler, Engine, IO | Yes |
| Parse, Validation, Cancelled | No |

### Parallel branch failures
Individual branch failures do NOT kill sibling branches. Policy per node: `wait_all` or `first_success`.

### Post-failure state
Container remains alive after failure — deferred cleanup still applies. The container can be inspected manually before the next run deletes it.

---

## 6. Security Model

| Aspect | Implementation |
|---|---|
| Host filesystem | `binds: None` — hardcoded, zero host bind mounts |
| Docker socket | NOT mounted in containers |
| Network default | Bridge (outbound open, inbound blocked) |
| Network block mode | `mode = "block"` → `network_mode = "none"` |
| User inside container | **root** (Docker default, no user namespace remapping) |
| Seccomp / AppArmor | NOT configured — Docker daemon defaults only |
| Container verification | Label-based check on every single operation |
| Credentials | GitHub tokens injected as env vars, redacted from logs via `redact_auth_url` |

### What containers CAN access
- `/workspace` — working directory, repo clone
- `/repos` — repository cache (if `clone_origin_url` set)
- Outbound network (unless blocked via config)

### What containers CANNOT access
- Host filesystem (no bind mounts)
- Docker socket
- Other containers
- Host process namespace

### Security gaps to address for production AWS
1. **Root inside container** — no `--user` flag set. Consider enabling Docker user namespace remapping at the daemon level, or adding `user: "1000:1000"` to HostConfig.
2. **No explicit seccomp profile** — Docker applies its default profile (~40 blocked syscalls), but this may not meet compliance requirements.
3. **Bridge networking open by default** — containers can reach the internet unless you configure `mode = "block"` or a CIDR allowlist.
4. **Deferred container deletion** — orphaned containers from crashed runs persist indefinitely without a watchdog.

---

## 7. Image Management

- **Default image**: `buildpack-deps:noble` (Ubuntu 24.04 LTS, ~650 MB compressed)
- **Included**: gcc, git, curl, python, common build tools, bash
- **Requirements**: must have `/bin/bash` AND `git` (verified on container start — hard failure if missing)
- **Pull strategy**: auto-pull on first use, then cached by Docker daemon
- **Custom images**: any image with bash + git; configure via `settings.image.docker` in environment config

---

## 8. AWS Deployment — Boundaries and Sizing

### Docker access
Fabro connects via `Docker::connect_with_local_defaults()` — needs one of:
- `/var/run/docker.sock` mounted (DinD or privileged ECS/EKS pod)
- `DOCKER_HOST` env var pointing to a remote Docker daemon

### Compute sizing (per concurrent run)

| Component | Minimum | Recommended |
|---|---|---|
| Fabro process | 100 MB RAM | 200 MB RAM |
| Per container | 512 MB RAM, 0.5 CPU | 2–4 GB RAM, 1–2 CPU |
| Parallel (4 branches) | 4× the per-container CPU | Size container quota for peak |

### Disk (on Docker host)
- Image cache: ~650 MB for `buildpack-deps:noble`
- Scratch space: container overlay2 in Docker's storage driver (ephemeral, released on container deletion)
- No persistent volumes needed for typical use

### Recommended production environment config
```toml
[environments.production.resources]
memory = "4GB"
cpu = 2

[environments.production.network]
mode = "cidr_allow_list"
allow = ["10.0.0.0/8", "172.16.0.0/12"]  # internal VPC only
```

### Orphaned container watchdog
Run periodically via EventBridge + Lambda or cron:
```bash
docker ps -q --filter label=sh.fabro.managed=true --filter status=running \
  | xargs -r docker inspect --format '{{.Name}} {{.State.StartedAt}}'
# compare StartedAt against threshold; force-remove stale ones:
docker rm -f $(docker ps -q --filter label=sh.fabro.managed=true)
```

---

## 9. Execution Flow Summary

```
RUN START
  │
  ▼ INITIALIZE PHASE
  ├─ Pull image (if not cached)
  ├─ Create container: fabro-run-{run_id}
  ├─ Start container (entrypoint: sleep infinity)
  ├─ Health check (docker exec)
  └─ Git clone → /workspace
  │
  ▼ EXECUTE PHASE
  ├─ Sequential node:
  │   └─ docker exec {command}  →  ExecResult
  └─ Parallel node:
      ├─ git checkpoint (baseline)
      ├─ For each branch (concurrent, max 4):
      │   ├─ git worktree add → /workspace/.fabro/scratch/.../branch_x
      │   ├─ docker exec -w {worktree} {command}
      │   └─ git worktree remove
      └─ Merge results (wait_all / first_success)
  │
  ▼ FINALIZE PHASE
  └─ Stop container (NOT deleted)
  │
  ▼ NEXT RUN START
  └─ cleanup_guard fires → docker rm -f fabro-run-{prev_run_id}
```

---

## 10. Key Source Files

| File | Purpose |
|---|---|
| `lib/crates/fabro-sandbox/src/docker.rs` | Main Docker sandbox implementation (~2300 lines) |
| `lib/crates/fabro-sandbox/src/worktree.rs` | WorktreeSandbox — parallel branch isolation |
| `lib/crates/fabro-sandbox/src/from_environment.rs` | Config mapping from settings.toml |
| `lib/crates/fabro-sandbox/src/managed_labels.rs` | Label constants (`sh.fabro.managed`, `sh.fabro.run_id`) |
| `lib/crates/fabro-sandbox/src/sandbox.rs` | Sandbox trait definition |
| `lib/crates/fabro-workflow/src/pipeline/initialize.rs` | Container creation (lines 357–386) |
| `lib/crates/fabro-workflow/src/pipeline/finalize.rs` | Container stop (lines 505–518) |
| `lib/crates/fabro-workflow/src/handler/parallel.rs` | Parallel execution (lines 241–450) |
| `lib/crates/fabro-workflow/src/error.rs` | Error categories and retry logic |
| `docs/public/administration/sandboxing.mdx` | User-facing documentation |
