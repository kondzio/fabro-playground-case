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

## 4. Command Execution

The container runs `sleep infinity` as its entrypoint — all actual work happens via `docker exec` calls using the `bollard` Rust crate. Three distinct execution paths exist.

### Path 1 — Simple exec (`docker_exec`, `docker.rs:276`)

Used for **internal operations**: git clone, workspace mkdir, health check, `cat`/`test`/`find` file ops.

```
bollard.create_exec(cmd=["/bin/bash", "-c", command], attach_stdout, attach_stderr)
  → bollard.start_exec()
  → collect stdout/stderr strings
  → bollard.inspect_exec() to get exit code
```

Blocking — waits for full output before returning.

### Path 2 — Streaming exec (`docker_exec_shell_streaming`, `docker.rs:455`)

Used for **every agent node command** that needs live output. Every command is wrapped in `docker_controlled_shell_command` (`docker.rs:838`) before being sent to `docker exec`:

```bash
# Simplified structure of the generated wrapper:
stop_file=/tmp/fabro-exec-{pid}-{nonce}-{seq}.stop
pid_file=...

if [ -e "$stop_file" ]; then exit 143; fi        # pre-cancelled check

# Background watcher signals the process group when stop_file appears
( while [ ! -e "$stop_file" ]; do sleep 0.1; done
  kill -TERM -$child; sleep 0.2; kill -KILL -$child ) &

setsid /bin/bash -lc "$user_command" &            # new process group
echo $! > "$pid_file"
wait $child
```

Output is streamed chunk-by-chunk via `CommandOutputCallback` as `LogOutput::StdOut`/`LogOutput::StdErr` frames. `tokio::select!` races the output task against timeout/cancellation futures.

### Path 3 — Stdio process (`spawn_stdio_process`, `docker.rs:1564`)

Used for **MCP servers** (e.g. Jira MCP in `settings.toml`). Same stop-file wrapper as Path 2, but exec is started with `attach_stdin: true` and bidirectional pipes:

```
bollard.start_exec(attach_stdin=true, attach_stdout=true, attach_stderr=true, tty=false)
  → (input: AsyncWrite, output: Stream<LogOutput>)
```

A Tokio task fans out the output stream:
- `StdOut` bytes → `duplex` pipe (MCP client reads from this as its `stdout`)
- `StdErr` bytes → `StderrCollector` (ring-buffer, surfaced on error)

`StdioProcessHandle` wraps a `DockerStdioProcessControl` that can `terminate()` (via stop-file) or `wait()` (polls `inspect_exec` every second).

### Result type

```rust
ExecResult {
    stdout: String,
    stderr: String,
    exit_code: Option<i32>,         // None if timed out or cancelled
    termination: CommandTermination, // Exited | TimedOut | Cancelled
    duration_ms: u64,
}
```

Last **8,192 bytes (8 KB)** retained for error display (`DEFAULT_EXEC_OUTPUT_TAIL_BYTES`). Full output is NOT buffered in memory — streamed through and discarded unless collected by a callback.

### File I/O

| Operation | Mechanism |
|---|---|
| Read file | `docker exec cat <path>` (Path 1) |
| Write file to container | `docker.upload_to_container()` — tar stream via Docker API |
| Download file from container | `docker.download_from_container()` — tar stream via Docker API |
| Upload file from host | `docker.upload_to_container()` — tar stream |

Write/upload go through Docker's archive API, not `docker exec` — bypasses shell quoting and handles binary content correctly.

---

## 5. Error Handling

### Timeouts (`docker.rs:417-441`)
- `tokio::select!` races the command future against `time::sleep(timeout)`
- Returns `CommandTermination::TimedOut`
- Defaults: git clone = 300s, setup commands = 30s, general commands = 60s (all configurable per node)

### In-container cancellation via stop files (`docker.rs:838-879`)
Fabro cannot SIGTERM a `docker exec` from outside the container (Docker has no API for it). Instead, every streaming command (Paths 2 and 3 from Section 4) is wrapped in a shell that monitors a stop file inside the container:

1. Two temp files are created per exec: `/tmp/fabro-exec-{pid}-{nonce}-{seq}.stop` and `.pid` (sequence number from an `AtomicU64`, so concurrent execs never collide)
2. The user command runs via `setsid` — new process group
3. A bash watcher loop polls the stop file every 100ms (5ms in tests)
4. When stop file appears: `kill -TERM -{pgid}` → 200ms grace → `kill -KILL -{pgid}`
5. `setsid` ensures the kill signal covers the entire process group/tree

**How Fabro creates the stop file** (`request_docker_exec_stop`, `docker.rs:529`): a separate `docker exec` call runs `touch <stop_file>` inside the container. This is the only way to signal a running exec from outside.

Full timeout/cancel flow:
```
timeout fires or cancel_token cancelled
  → request_docker_exec_stop()
      → docker exec touch /tmp/fabro-exec-<id>.stop
          → watcher sends SIGTERM to process group
          → after 200ms grace, SIGKILL
  → output_task.await (drains remaining output)
  → returns ExecResult { termination: TimedOut | Cancelled, exit_code: None }
```

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

## 10. MCP Server Support

Fabro supports three MCP transport types configured under `[run.agent.mcps.<name>]` in `settings.toml`. The type is set via the `type` field.

### Transport 1 — `stdio`

```toml
[run.agent.mcps.jira]
type = "stdio"
command = ["python3", "/opt/jira_mcp_server.py"]
startup_timeout = "15s"
tool_timeout = "90s"

[run.agent.mcps.jira.env]
JIRA_BASE_URL  = "vault:JIRA_BASE_URL"
JIRA_API_TOKEN = "vault:JIRA_API_TOKEN"
```

The MCP server process is launched **inside the sandbox container** via `spawn_stdio_process` (Section 4, Path 3): `docker exec` with `attach_stdin=true` creates a bidirectional pipe. The `rmcp` SDK's `TokioChildProcess` transport handles the MCP protocol over stdin/stdout.

This is the transport used by the Jira MCP server in the current deployment.

### Transport 2 — `http`

```toml
[run.agent.mcps.my_remote]
type = "http"
url = "https://my-mcp-server.example.com/mcp"

[run.agent.mcps.my_remote.headers]
Authorization = "Bearer {{ vars.TOKEN }}"
```

Connects to an **already-running remote MCP server** over HTTP. Fabro connects from the Fabro host process (not from inside the sandbox container). Two sub-protocols are supported via the optional `protocol` field:

| `protocol` | Default | Transport |
|---|---|---|
| `streamable_http` | yes | MCP Streamable HTTP (`StreamableHttpClientTransport`) |
| `sse` | no | MCP over Server-Sent Events (`SseClientTransport`) |

### Transport 3 — `sandbox`

```toml
[run.agent.mcps.my_http_server]
type = "sandbox"
command = ["node", "mcp-server.js"]
port = 8080
protocol = "streamable_http"   # or "sse"
```

Starts an HTTP MCP server **inside the sandbox container**, then connects to it over HTTP from Fabro. Useful when the MCP server needs access to the workspace filesystem or other sandbox resources. Startup flow (`session.rs:705`):

1. `setsid sh -c "<command> >/tmp/mcp_server_stdout.log 2>/tmp/mcp_server_stderr.log" &` — launched detached inside the container via `exec_command`
2. Polls `ss -tln | grep -q ':{port}'` inside the container every second, up to 30s
3. Resolves the sandbox's preview URL for that port (falls back to `http://localhost:{port}`)
4. **Rewrites config to `McpTransport::Http`** before passing to `McpClient` — the `Sandbox` variant is never connected directly (`client.rs:101` panics if it reaches connection without resolution)

> **Note**: `sandbox` transport is only meaningful when there is a preview/tunnel mechanism that makes the container port reachable from Fabro. For the Docker sandbox on EKS, `http://localhost:{port}` is used — the Fabro process and the container share the pod network namespace via DinD, so this works only if the MCP server inside the container is reachable from the Fabro container. In standard Docker networking, the container has its own IP; use `http://{container_ip}:{port}` or configure network accordingly.

### How MCP tools are exposed to the agent

All three transports converge at `McpConnectionManager` (`connection_manager.rs`). After startup, `list_tools()` is called on each server and each tool is registered with a qualified name:

```
mcp__{server_name}__{tool_name}
```

Special characters in server/tool names are replaced with `_`. The agent sees these as ordinary tools — no distinction between stdio, http, or sandbox at call time. `McpConnectionManager.call_tool()` routes by qualified name to the right client.

### MCP server lifecycle

| Phase | stdio | http | sandbox |
|---|---|---|---|
| Start | `docker exec` (stdin/stdout pipe) | HTTP connect (no start) | `docker exec setsid ...` + port poll |
| Ready check | MCP handshake timeout | MCP handshake timeout | `ss -tln` port check (30s) then MCP handshake |
| Shutdown | stop-file signal → SIGTERM/SIGKILL | HTTP disconnect | `kill {pid}` inside container |
| Failure | logged, other servers continue | logged, other servers continue | `McpServerFailed` event emitted |

Failed servers are logged but do **not** block other servers from starting or the agent from running.

---

## 11. Key Source Files

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
| `lib/crates/fabro-mcp/src/client.rs` | MCP client — stdio/http/sse transport setup and tool calls |
| `lib/crates/fabro-mcp/src/connection_manager.rs` | Multi-server manager, qualified tool names, routing |
| `lib/crates/fabro-mcp/src/sse_client.rs` | SSE transport implementation |
| `lib/crates/fabro-types/src/settings/run.rs` | `McpTransport` / `McpHttpProtocol` / `McpServerSettings` types |
| `lib/crates/fabro-agent/src/session.rs:705` | `resolve_sandbox_mcp_servers` — Sandbox→Http resolution |
| `lib/crates/fabro-agent/src/mcp_integration.rs` | Converts MCP tools into `RegisteredTool` for the agent |
| `docs/public/administration/sandboxing.mdx` | User-facing documentation |
