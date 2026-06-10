# fabro-playground-case

Welcome to the Fabro Playground Case repository! This repository contains a collection of Fabro workflows and an automated test suite.

---

## Directory Structure

Workflows are defined in directories under `.fabro/workflows/`. Each workflow directory typically contains:
- **`<workflow_name>.fabro`**: A graph definition file utilizing a DOT-like digraph syntax that defines nodes (steps, prompts, approvals) and directed edges (flow, conditions).
- **`workflow.toml`**: The runner configuration file defining the graph path, environments, inputs, and other execution settings.

---

## Listing Workflows

To view all workflows available in this project:

```bash
fabro workflow list
```

---

## Running Workflows

Fabro workflows can be run interactively or in the background using the `fabro run` command.

### 1. Standard Run
Launch a workflow by passing the path to its `workflow.toml` file:
```bash
fabro run .fabro/workflows/hello/workflow.toml
```

### 2. Local Simulation / Dry Run
To test and validate workflow execution flow locally without calling external LLMs (avoiding API charges and rate limits), use the `--dry-run` flag:
```bash
fabro run .fabro/workflows/hello/workflow.toml --dry-run
```

### 3. Running in the Background (Detached)
To run a workflow in the background and immediately return the unique Run ID (ULID):
```bash
fabro run .fabro/workflows/hello/workflow.toml --detach
# Or shorthand:
fabro run .fabro/workflows/hello/workflow.toml -d
```

### 4. Overriding Goals and Inputs
- **Override Goal**: Customize the main prompt/goal of the workflow:
  ```bash
  fabro run .fabro/workflows/hello/workflow.toml --goal "Say hello in German"
  ```
- **Override Inputs**: Override key-value inputs defined in the TOML configuration:
  ```bash
  fabro run .fabro/workflows/hello/workflow.toml -I key=value
  ```

---

## Testing and Validating Workflows

To ensure workflows are structurally sound and configured correctly, Fabro provides validation and preflight utilities.

### 1. Structural Validation
Verify that the `.fabro` graph file has valid digraph syntax, exactly one start node, valid conditions, and correct shapes/labels:
```bash
fabro validate .fabro/workflows/hello/workflow.fabro
```

### 2. Preflight Configuration Check
Verify the runner environment, repository setup, sandbox readiness, and perform a quick probe to test LLM model accessibility without executing the full workflow:
```bash
fabro preflight .fabro/workflows/hello/workflow.toml
```

### 3. Automated Test Suite
To run validation and preflight checks on all workflows automatically, use the provided test suite runner script:
```bash
./run_tests.sh
```
This script finds all workflow directories, runs validation and preflight, and generates a neat summary table showing passing, failing, and warning states.

---

## Human-in-the-Loop & Approvals

Some workflows define **Human Approval Gates** to pause execution and await user confirmation or rejection. These are typically represented by hexagon nodes in the `.fabro` file (e.g., `shape=hexagon`).

### How It Works:
1. When a run reaches a hexagon node (e.g., `approval_gate`), execution halts and the run transitions to a pending state.
2. The user inspects the output generated up to that point.
3. The user either approves or denies the pending run to determine which branch it proceeds down.

### Commands for Approval:
- **Approve a run**: Approve the run and allow it to continue along the approved path:
  ```bash
  fabro approve <RUN_ID>
  ```
- **Deny/Reject a run**: Deny the run and direct it down the rejected/revising path:
  ```bash
  fabro deny <RUN_ID>
  ```
- **Auto-Approve**: To bypass human gates and automatically approve all approval prompts when initiating a run:
  ```bash
  fabro run .fabro/workflows/mine/workflow.toml --auto-approve
  ```

---

## Monitoring and Visualizing Runs

You can monitor, inspect, and visualize active or completed workflow runs using the following commands:

- **Inspect Run Details**: Retrieve complete JSON state, configuration, and node-by-node outcomes:
  ```bash
  fabro inspect <RUN_ID>
  ```
- **Live Event Feed**: Watch step-by-step state changes and events of a run:
  ```bash
  fabro events <RUN_ID>
  ```
- **Detailed Logs**: View raw worker-tracing logs for deep debugging:
  ```bash
  fabro logs <RUN_ID>
  ```
- **Visualize the Workflow Graph**: Generate an SVG image representing the graph architecture:
  ```bash
  fabro graph .fabro/workflows/hello/workflow.fabro -o hello-graph.svg
  ```

---

## Jira MCP Server Setup

The Jira MCP server (`tools/jira_mcp_server.py`) exposes 15 Jira tools to all Fabro workflows.

### 1. Install dependencies

```bash
pip install -r tools/requirements.txt
```

### 2. Configure user-level Fabro settings

Add the following to `~/.fabro/settings.toml` (create the file if it doesn't exist), replacing the placeholder values with your actual Jira credentials and the absolute path to this repository:

```toml
[run.agent.mcps.jira]
type = "stdio"
command = ["python3", "/absolute/path/to/fabro-playground-case/tools/jira_mcp_server.py"]
startup_timeout = "15s"
tool_timeout = "30s"

[run.agent.mcps.jira.env]
JIRA_BASE_URL = "https://your-org.atlassian.net"
JIRA_EMAIL = "your@email.com"
JIRA_API_TOKEN = "your-api-token"
```

Get a Jira API token from: https://id.atlassian.com/manage-profile/security/api-tokens

### 3. Run the demo workflow

Edit `.fabro/workflows/jira-demo/workflow.toml` to set your `project_key`, then:

```bash
fabro run --workflow jira-demo
```
