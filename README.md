# Network Lab MCP

**An AI Network Engineer Workspace for real network labs.**

Network Lab MCP gives an MCP-capable AI client a safe, observable way to investigate real network lab devices while keeping the engineer in control.

The **AI client performs the reasoning**: it decides what to inspect, which MCP tool to call, how to interpret the evidence, and what to do next. Network Lab MCP provides the structured engineering context and controlled terminal access that make that investigation possible, without exposing device credentials to the AI.

<p align="center">
  <img src="docs/images/network-lab-mcp-concept.png"
       alt="Network Lab MCP concept: Human Engineer to AI Clients to MCP Tools Interface to Network Lab MCP Workspace to Lab Environment"
       width="100%">
</p>

**Human provides intent. AI investigates and proposes. Human reviews and commits.**

The architecture is vendor-neutral. The included public sample and platform guidance are Cisco-oriented, with registered device types for Cisco IOS XR, IOS XE, classic IOS, and NX-OS.

**Jump to:** [Demo](#demo) · [Use cases](#use-cases) · [Quick Start](#quick-start) · [Architecture](docs/architecture.md) · [MCP tools](docs/mcp_tools.md) · [CLI reference](docs/cli_reference.md)

## Demo

### Traffic Loss Validation with Network Lab MCP

This short demo shows an AI-assisted validation workflow: live terminal monitoring, scenario execution, traffic measurement, result analysis, and final report generation.

[▶ Watch the demo on YouTube](https://youtu.be/ILkW7yuU1Y4)

## Why I built it

As a network engineer, I often want to validate something in a lab but do not have enough time to perform every investigation manually. Customer work, troubleshooting, meetings, documentation, and other responsibilities usually come first.

That led to a simple question:

> What if I could ask AI to carry out the time-consuming parts of network lab investigation for me?

Not just run a command, but understand the topology, connect to the right devices, inspect the current state, compare evidence, investigate problems, and report back.

At the same time, I do not want the AI to become a black box. I still want to see what it is doing and make the final engineering decisions.

Network Lab MCP is the result.

## What Network Lab MCP does

Network Lab MCP is **not** a fixed test-automation framework and it is **not** a network-engineering reasoning engine.

The AI client performs the reasoning. Network Lab MCP provides two complementary capabilities:

1. **Structured engineering context** — Principles, Topology, Scenario, and References give the AI the rules, environment, task intent, and reusable engineering knowledge needed for the current investigation.
2. **Controlled execution** — managed terminal sessions provide real, general-purpose SSH/Telnet access to lab devices without exposing private connection information to the AI.

The AI decides what command to run next, how to interpret the output, and when the investigation is complete.

The repository also includes an independent IOS XR-style human CLI (`./run_cli.sh`) for selecting the active lab context, editing lab definitions, discovering topology, using candidate/commit configuration semantics, and monitoring AI terminal activity live.

## Use cases

Network Lab MCP is designed for AI-assisted engineering tasks where the investigation path cannot be reduced to one fixed script.

Typical use cases include:

- **Topology discovery and validation** — discover LLDP/CDP relationships and review the candidate topology before committing it.
- **Troubleshooting and evidence collection** — inspect multiple devices, compare state, follow symptoms, and gather supporting command output.
- **Failure and resiliency validation** — observe network behavior before, during, and after a planned lab fault.
- **Traffic and service validation** — combine device state with external test/measurement results and produce an engineering conclusion.
- **Repeated lab investigation** — reuse Scenarios and References instead of rediscovering the same engineering knowledge for every task.
- **Human-observable AI operations** — watch the exact terminal activity used by the AI while the investigation is in progress.

The included sample configuration is Cisco-oriented. Topology discovery currently supports Cisco IOS XR, IOS XE, and classic IOS as described in [Supported device types](#supported-device-types).

## What makes it different

- **AI is the reasoning layer.** Network Lab MCP does not try to replace the AI client with a fixed reasoning engine.
- **Exactly seven controlled MCP tools.** The interface stays deliberately small and auditable.
- **Credentials never cross the MCP boundary.** The AI works with logical device names; private access information is resolved internally.
- **Real terminals remain observable.** Engineers can monitor the same managed terminal sessions used by the AI.
- **Engineering context is explicit.** Principles, Topology, Scenario, and References separate rules, environment, task intent, and reusable knowledge.
- **Human authority is preserved.** Definition and selection changes use candidate/commit semantics; the AI proposes, while the engineer reviews and commits.

## How Network Lab MCP organizes engineering context

You do not need to author YAML first. Start with your intent:

> Tell me what you want to build, investigate, or validate.

Network Lab MCP organizes work with four running-config concepts:

| Concept | Meaning |
|---|---|
| `access-info` | how to connect |
| `topology` | what network/environment exists |
| `scenario` | what the AI should accomplish |
| `reference` | reusable engineering knowledge |

**Scenario** answers "what should be accomplished for the current task?"
**Reference** answers "what reusable engineering knowledge can help accomplish
it?" A Reference can support multiple investigations, validations,
operations, or Scenarios. It is not a one-time task description, a
replacement for the always-applied principles or for onboarding, and it is
not active unless selected.

For example, the Scenario "validate EVPN traffic forwarding while a link is
disconnected" could be supported by References holding EVPN engineering
knowledge, Cisco platform applicability guidance, and operating knowledge for
the test/measurement equipment in use.

Examples of reusable engineering knowledge:

- SR-MPLS design and validation knowledge
- EVPN behavior and troubleshooting knowledge
- time synchronization and timing-network knowledge, such as PTP and SyncE
- industrial or utility network engineering knowledge
- operating procedures for test and measurement equipment

These are illustrations of suitable content, not bundled files or
implemented features.

Scenario and reference knowledge evolves by **reuse → refine → create**: the
AI discovers stored scenarios and references (a catalog is returned with the
execution instructions, and relevant non-active ones can be inspected
read-only), reuses existing knowledge, and when it is incomplete or missing
*proposes* a refined or new Scenario/Reference. Definition changes are
reviewed and committed through the CLI; the AI never writes them directly.

A fresh installation (`cp lab/settings.example.yaml lab/settings.yaml`) starts with:

- access-info: `sample_lab` — how the system connects to the fictional public sample lab
- topology: `sample_lab` — what network/environment that sample lab contains
- scenario: `getting_started` — what the AI should accomplish; the default onboarding Scenario
- reference: `cisco_platform_guidance` — reusable engineering knowledge; the default supporting Reference

The architecture is vendor-neutral; the included sample configuration is
Cisco-oriented and ships with a Cisco supporting Reference. This is not a
vendor requirement: other environments can select other References.

These are fresh-install defaults only. Existing `lab/settings.yaml` files are
never rewritten or migrated: your current selections and descriptions stay as
they are. See [docs/scenario_format.md](docs/scenario_format.md) for the
Scenario / Reference boundary.

## Core design

The configuration model deliberately separates five kinds of information:

| Concept | Purpose | Exposed to the AI? |
|---|---|---|
| `running-config` | Selects which topology, access-info, scenario, and references are active | Indirectly |
| `access-info` | Private connection information: address, transport, port, username, password | **Never** |
| `topology` | Logical devices and connectivity | Yes |
| `scenario` | What should be accomplished for the current task | Yes |
| `reference` | Reusable engineering knowledge and operational guidance | Yes |

In short:

```text
access-info    = HOW TO ACCESS DEVICES     = private
topology       = WHAT EXISTS / CONNECTIVITY
scenario       = WHAT TO DO
reference      = REUSABLE KNOWLEDGE
running-config = WHICH DEFINITIONS ARE ACTIVE
```

`running-config` is a **selection**, not a definition. Editing a topology, scenario, or reference does not make it active. The active selection changes only after an explicit commit.

See [docs/architecture.md](docs/architecture.md) for the complete model.

## Key design principles

- **Exactly seven MCP tools.** `get_active_topology`, `get_execution_instructions`, `terminal_open`, `terminal_send`, `terminal_read`, `terminal_list`, and `terminal_close`.
- **Real, human-observable terminals.** Sessions run in a dedicated tmux environment.
- **Credentials never cross the MCP boundary.** The AI opens a terminal by logical device name; private access information is resolved internally.
- **Candidate / commit configuration.** Human CLI changes remain candidate state until an explicit `commit`.
- **Discovery is candidate-first.** `discover topology` never auto-commits and never auto-selects the discovered topology. It rebuilds the candidate from the current LLDP/CDP results only (no merge with older links), is L2-only by default (`discover topology l3` adds L3 interface data), and refuses to run over uncommitted manual edits.
- **Fail closed instead of guessing.** Missing access information, ambiguous resolution, type mismatches, and invalid state produce explicit errors.
- **Human judgment stays authoritative.** AI can investigate and report, but the engineer remains responsible for the final technical decision.

## Quick Start

### Prerequisites

- Linux
- Python 3.10 or newer
- `tmux`
- OpenSSH client
- `telnet` client only if Telnet devices are used
- an MCP-capable client such as Claude Code or Codex CLI

macOS is likely compatible because the project relies on Python, tmux, OpenSSH, and a POSIX shell, but formal macOS validation has not yet been completed. Native Windows is not supported; WSL2 may work but is not formally validated.

### Install

```bash
git clone https://github.com/eiuemura/network-lab-mcp.git
cd network-lab-mcp

python3 -m venv .venv
source .venv/bin/activate

pip install -e .
```

Network Lab MCP currently supports a **local editable checkout**. A normal wheel or package-index installation is not supported because the repository-local `lab/` directory is part of the operational model.

### Create your local running-config

```bash
cp lab/settings.example.yaml lab/settings.yaml
```

### Register Network Lab MCP with an AI client

Network Lab MCP is a local stdio MCP server. Register the executable from this repository's virtual environment with the AI client you use.

Replace `/path/to/network-lab-mcp` in the examples below with the **absolute path** to your local checkout.

Using the absolute path to `.venv/bin/network-lab-mcp` ensures that the AI client uses the intended Network Lab MCP installation regardless of the directory from which the client is started.

#### Claude Code

Register Network Lab MCP at **user scope**:

```bash
claude mcp add network-lab -s user -- \
  /path/to/network-lab-mcp/.venv/bin/network-lab-mcp
```

Verify the registration:

```bash
claude mcp list
```

You should see Network Lab MCP reported as connected:

```text
network-lab: /path/to/network-lab-mcp/.venv/bin/network-lab-mcp - ✔ Connected
```

#### Codex CLI

Register the local stdio MCP server:

```bash
codex mcp add network-lab -- \
  /path/to/network-lab-mcp/.venv/bin/network-lab-mcp
```

Verify the registration:

```bash
codex mcp list
```

You should see an enabled `network-lab` MCP server:

```text
Name         Command                                               Args  Env  Cwd  Status   Auth
network-lab  /path/to/network-lab-mcp/.venv/bin/network-lab-mcp    -     -    -    enabled  Unsupported
```

`Auth: Unsupported` is expected for this local stdio MCP server. It refers to MCP-level authentication between Codex and the MCP server; device authentication is handled internally by Network Lab MCP through the private `access-info` configuration.

##### Codex approval setting

In the tested Codex CLI setup, registering the server alone was not sufficient for the terminal workflow to proceed to lab-device access.

Edit:

```bash
vi ~/.codex/config.toml
```

and configure the Network Lab MCP server as follows:

```toml
[mcp_servers.network-lab]
command = "/path/to/network-lab-mcp/.venv/bin/network-lab-mcp"
default_tools_approval_mode = "approve"
```

Then restart Codex and verify the MCP registration again:

```bash
codex mcp list
```

The `default_tools_approval_mode = "approve"` setting above reflects the configuration verified with Network Lab MCP in the tested Codex CLI environment.

### Start the human CLI

```bash
./run_cli.sh
```

Useful commands:

```text
help
help claude
show version
show running-config
configure
```

### Start an AI client from any task workspace

The task workspace does not need to be inside the Network Lab MCP repository.

#### Claude Code

```bash
cd ~/work/my-lab-task
claude
```

> **Advanced / isolated-lab-only:** `claude --permission-mode bypassPermissions` skips Claude Code's own approval prompts. Use it only in an isolated lab environment you fully control.

#### Codex CLI

```bash
cd ~/work/my-lab-task
codex
```

> **Advanced / isolated-lab-only:** The following commands disable Codex approval prompts. Use them only in an isolated lab or task environment that you fully control.
>
> ```bash
> codex --sandbox workspace-write --ask-for-approval never
> ```
>
> Or, to keep the normal terminal screen and scrollback available:
>
> ```bash
> codex --sandbox workspace-write --ask-for-approval never --no-alt-screen
> ```

`--sandbox workspace-write` lets Codex work inside the task workspace while keeping the session sandboxed.

`--ask-for-approval never` disables Codex approval prompts.

`--no-alt-screen` is optional and keeps the normal terminal screen and scrollback available.

## Example workflow

The tracked sample files use documentation-only addresses and cannot connect to real devices.

Create your own private access-info definition:

```bash
cp lab/access-info/sample_lab.yaml lab/access-info/my_lab.yaml
```

A typical topology-discovery flow looks like this:

```text
network-lab# configure
network-lab(config)# discover topology
Discovering topology from access-info 'my_lab'...
Discovery complete.

  Access-info:          my_lab
  IOS XR targets:       2
  IOS XE targets:       0
  IOS targets:          0
  Connected:            2
  LLDP observations:    4
  CDP observations:     0
  Managed links:        2
  Unresolved neighbors: 0
  L3 enrichment:        Disabled
  Previous L3 data:     Not carried over
  Topology candidate:   my_lab

network-lab(config-topology-my_lab)# show configuration
...
network-lab(config-topology-my_lab)# commit
Commit complete.

network-lab(config-topology-my_lab)# root
network-lab(config)# running-config
network-lab(config-running)# topology my_lab
network-lab(config-running)# commit
Commit complete.
```

Discovery behavior in brief:

- `discover topology` collects LLDP/CDP only; `discover topology l3` also
  collects IPv4/VRF interface data. L2 runs never send L3-only commands and
  never carry earlier L3 data into the new candidate.
- The candidate is **rebuilt** from this run alone: devices and links not
  observed (or no longer in the active access-info) are dropped from the
  candidate, and the removed devices/links are listed before the candidate is
  replaced. The committed definition is untouched until `commit`; `clear`
  restores it. Topology name, description and unknown top-level metadata are
  kept.
- Discovery fails closed (the candidate is left exactly as it was): login,
  command or timeout failure, unrecognized LLDP/CDP output, a device with no
  usable neighbor protocol (e.g. LLDP and CDP both disabled), conflicting
  neighbor evidence (including one physical interface in two links), and, for
  `l3`, any L3 collection or parse failure. A disabled or unsupported protocol
  beside a working one is just listed under `Neighbor discovery protocol
  status:` (not a warning).
- It refuses to run when the target topology (or another definition) has
  uncommitted manual edits; `clear` first. Re-running over a candidate that
  only Discovery produced is allowed.


Discovery commits only the topology **definition**. Selecting that topology as active remains a separate explicit running-config operation.

Once the active topology and instructions are selected, a typical AI workflow is:

```text
get_active_topology()
get_execution_instructions()
terminal_open(...)
terminal_send(...)
terminal_read(...)
...
```

## Human-observable terminals

An engineer can watch the exact terminal activity used by the AI:

```text
network-lab# terminal monitor R1
RP/0/RP0/CPU0:R1#show version
...
RP/0/RP0/CPU0:R1#

--------------------------------------------------------------------------------
Monitoring terminal R1 | Read-only | Source: managed | Status: active | q: quit
--------------------------------------------------------------------------------
```

`terminal monitor <device-id>` is EXEC-only, strictly read-only, independent from the AI session, and able to follow normal managed sessions and Discovery sessions.

Terminal activity is also persisted under:

```text
logs/terminal/<device-id>/*.log
```

See [docs/cli_reference.md](docs/cli_reference.md#terminal-monitor).

## Platform-aware knowledge

Network Lab MCP can use reusable References to decide whether technical
knowledge applies to the target device. Protocol concepts may be reusable
across platforms, but configuration syntax, operational behavior, feature
support, limitations, YANG models, telemetry paths, and release-specific
behavior must be verified for the applicable platform.

The included `cisco_platform_guidance` Reference is one concrete example. It
provides reusable guidance for determining whether Cisco-specific knowledge
applies to the target platform, software release, feature, and question type,
for the Cisco device types registered in the `device.type` enum (`iosxr`,
`iosxe`, `ios`, `nxos`). It is the default supporting Reference of the
included Cisco-oriented sample configuration, selected by the sample settings
only; it is not universal, system-wide, or mandatory. The AI can also discover
and inspect it like any stored Reference. It is guidance only: nothing is
searched, fetched, or detected automatically.

```text
User intent
    ↓
Target device
    ↓
Platform / operating system
    ↓
Software release when relevant
    ↓
Feature domain
    ↓
Question type
    ↓
Applicable knowledge / source
    ↓
Lab validation when appropriate
```

For YANG and model-driven telemetry, the guidance points to the
[YangModels Cisco YANG repository](https://github.com/YangModels/yang/tree/main/vendor/cisco)
(the repository Cisco documentation refers users to), says telemetry paths
should be derived from the applicable model hierarchy rather than guessed,
and notes that platform and release matter and that the models a device
actually supports should be verified when practical.

## Security model

See [SECURITY.md](SECURITY.md) for the full trust model, risks, and vulnerability-reporting guidance.

### Credentials stay private from the AI

Real `access-info` definitions may contain management addresses, transport/port, usernames, passwords, and optional jump-host information.

No MCP tool returns access-info.

`terminal_open(device)` receives only a logical device name. Network Lab MCP resolves the selected private access-info internally and performs the connection.

Passwords are not intentionally returned in MCP tool results, terminal-send responses, or generated errors, and are not placed on subprocess command lines.

This is a credential boundary, not output sanitization. Terminal logs are raw transcripts: Network Lab MCP does not redact device output, and anything the remote side prints or echoes can appear in them. Treat terminal output and logs as potentially sensitive; Network Lab MCP does not guarantee they are free of secrets. See [SECURITY.md](SECURITY.md) for details.

### Important local CLI behavior

When a human explicitly displays access-info configuration locally with commands such as `show running-config` or `show configuration`, passwords are shown in clear text. Treat that terminal output as sensitive.

### SSH and Telnet authentication

Managed sessions support SSH key/agent authentication, SSH password authentication, and classic IOS-style Telnet username/password authentication.

Telnet remains unencrypted and should be used only in an isolated lab.

### SSH jump host support

A device may reference a single OpenSSH ProxyJump host from the same access-info definition. Multi-hop jump-host chains are not supported.

## Supported device types

| Type | Meaning | Topology Discovery |
|---|---|---|
| `iosxr` | Cisco IOS XR | LLDP + CDP |
| `iosxe` | Cisco IOS XE | LLDP + CDP |
| `ios` | Classic Cisco IOS | CDP |
| `nxos` | Cisco NX-OS | Not currently supported |
| `host` | Generic host / endpoint | Skipped |

Discovery is read-only. It does not enable LLDP/CDP, change device configuration, auto-commit, or auto-select the result.

## External YAML editing

Topology, scenario, and reference definitions can be edited through an external editor selected in this order:

1. `$VISUAL`
2. `$EDITOR`
3. Vim fallback

When a confirmed Vim executable is used, Network Lab MCP enables Vim paste mode automatically for the editing session.

Access-info remains structured-CLI editing only.

## Real-lab tests

Real-lab tests are deliberately opt-in:

```bash
NETWORK_LAB_REAL_TESTS=1 pytest tests/test_real_lab_iosxr.py -v --tb=line
```

Avoid `--showlocals` when running tests against real credentials.

## Repository layout

```text
network-lab-mcp/
├── pyproject.toml
├── README.md
├── LICENSE
├── run_cli.sh
├── src/
│   └── network_lab_mcp/
│       ├── __init__.py
│       ├── mcp_server.py
│       ├── lab.py
│       ├── terminal.py
│       ├── discovery.py
│       └── cli/
│           ├── main.py
│           ├── config.py
│           ├── grammar.py
│           └── editor.py
├── lab/
│   ├── settings.example.yaml
│   ├── principles.yaml
│   ├── access-info/
│   ├── topologies/
│   ├── scenarios/
│   └── references/
└── docs/
    ├── images/
    │   └── network-lab-mcp-concept.png
    ├── architecture.md
    ├── mcp_tools.md
    ├── cli_reference.md
    └── scenario_format.md
```

## Public-repository safety

Real lab files can contain sensitive information.

The repository's `.gitignore` deliberately excludes local operational files by default while preserving tracked fictional sample definitions.

This reduces the risk of accidentally committing real lab data, but it is **not** a complete security boundary. Always review changes before pushing to a public repository.

## Current limitations

- Topology discovery supports IOS XR, IOS XE, and classic IOS, but not NX-OS discovery.
- No SNMP, NETCONF, RESTCONF, or generic discovery plugin framework.
- No automatic stale-link pruning after Discovery.
- L3 enrichment records only directly observed IPv4 address + VRF.
- Non-editable / wheel installation is not supported.
- Access-info does not currently support external-editor editing.
- SSH ProxyJump is single-hop only.
- Scenario/reference schema remains intentionally flexible.
- Some identifier classes do not yet have case-only collision protection.

## Documentation

- [Architecture](docs/architecture.md)
- [MCP tool contract](docs/mcp_tools.md)
- [CLI reference](docs/cli_reference.md)
- [Scenario / reference format](docs/scenario_format.md)

## Issues and suggestions

Issues, bug reports, and improvement suggestions are welcome through [GitHub Issues](https://github.com/eiuemura/network-lab-mcp/issues).

## Version and license

Run `show version` in the human CLI to display the running version, release date, Git revision, branch, platform, MCP SDK version, and license.

Network Lab MCP is licensed under the **GNU General Public License v3.0**. See [LICENSE](LICENSE).

