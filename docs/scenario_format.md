# Scenario Format

The scenario format is intentionally minimal and experimental. It is not a
fixed schema, and this document describes the current, deliberately loose
conventions rather than a contract the loader enforces field-by-field.

## What a scenario is

- **Scenario** defines what should be accomplished for the current task —
  returned by `get_execution_instructions()` as part of the tool result
  Claude reads at the start of a task.
- **Principles** (`lab/principles.yaml`) define scenario-independent
  operating rules that apply regardless of which scenario is active.
- **References** (`lab/references/`) provide reusable engineering knowledge
  that scenarios can draw on instead of repeating the same operational
  know-how in every scenario file.
- **Topology** defines where the task is performed — which devices and links
  are available to work with, returned separately by `get_active_topology()`.

`lab/scenarios/getting_started.yaml` is the default onboarding scenario for
a fresh installation (see below). It is not a template to copy literally for
every future use case.

## Scenario vs. Reference

- **Scenario**: task intent — *what* the user is trying to accomplish, *why*,
  and the expected outcome.
- **Reference**: reusable engineering knowledge — *how* to operate, measure,
  interpret, or troubleshoot. A reference supports multiple scenarios.

### Discovery, inspection, and proposals

`get_execution_instructions()` returns the active scenario and references in
full plus metadata-only catalogs (`available_scenarios`,
`available_references`: name and description). Optional `inspect_scenarios` /
`inspect_references` arguments return the full content of chosen stored
definitions read-only, without activating them. The AI can therefore discover
and reuse existing knowledge, and *propose* a refinement or a new
scenario/reference when nothing suitable exists. It cannot persist
definitions: a human reviews and commits them through the CLI. Definition
identity is the filename stem, as everywhere else.

### Platform guidance references

`lab/references/cisco_platform_guidance.yaml` is the stored reference that the
included sample configuration activates by default. It helps decide whether Cisco platform-specific knowledge applies
to a target device. It is discovered through `available_references` and read
with `inspect_references=["cisco_platform_guidance"]` like any other
reference. Activation comes only from the selection in `running-config`; no
vendor detection happens. It covers:

- the routing inputs (device type, operating system, product family, software
  release, feature domain, question type) and the principle that similar
  knowledge is not automatically applicable knowledge;
- the split between broadly reusable protocol concepts and
  implementation details that must be verified per platform;
- cross-platform safety rules, limited to device types in the `device.type`
  enum;
- documentation classes (Configuration Guides, Command References, Release
  Notes) and which question types each suits;
- YANG lookup (the YangModels Cisco repository, OS- and release-aware, with
  device-reported support preferred) and the rule that telemetry sensor paths
  are derived from YANG models, not guessed.

It is reasoning guidance only: it performs no retrieval and does not detect
the platform. The same pattern can host other vendors' guidance as separate
references.

A scenario may point at the references it needs but should not duplicate
their content. Knowledge evolves **reuse → refine → create**: reuse an
existing scenario/reference, refine it if incomplete, and create a new one
only when nothing suitable exists. Unverified assumptions are not promoted
into references.

A **Reference** (`lab/references/`) is reusable engineering knowledge:
operational procedures, measurement methods, interpretation rules,
troubleshooting procedures, protocol/device/tool knowledge, and reusable
safety guidance. It can support multiple investigations, validations,
operations, or scenarios. It is not a one-time task description, a
replacement for principles or onboarding, or automatically active. Suitable
content includes protocol/domain engineering guidance, troubleshooting and
validation knowledge, platform guidance, timing-network knowledge,
industrial/utility networking knowledge, and test/measurement equipment
procedures; none of these categories is required to exist as a file. Its
shape is as loose as a scenario's. The default fresh
reference is `cisco_platform_guidance`, the supporting Reference of the
included Cisco-oriented sample configuration. The Reference mechanism itself
is vendor-neutral.

## Validation

Only "valid YAML, root is a mapping" is enforced by
`lab.validate_scenario_data()` / `lab.validate_reference_data()`. No
detailed business schema (required fields, objective structure, etc.) is
checked — a scenario or reference file with any mapping content at all will
load successfully.

## Conventional fields

There is no fixed schema, but the getting_started scenario and the CLI's `edit`
workflow follow this loose convention:

```yaml
name: sample

description: >
  Inspect the selected lab topology and summarize the current
  state of the available devices.

objectives:
  - obtain the active topology
  - access available devices when appropriate
  - inspect their current state
  - summarize the findings
```

- `name`: a human-readable label (not necessarily the same as the file's own
  definition name used for selection).
- `description`: free text describing what the scenario is.
- `message` (optional): a concise starting message tied to the scenario.
  Existing scenarios without `message` remain valid and behave unchanged.
  It is preserved as ordinary scenario content and delivered to the AI in
  `get_execution_instructions()`; nothing prints it automatically.
- `objectives`: an ordered list of what should be accomplished.

### Structured guidance (getting_started)

`getting_started.yaml` additionally carries machine-readable guidance for the
AI, all optional and all returned in the scenario `content`:

- `message`: the short intent-first message ("Tell me what you want to build,
  investigate, or validate. You do not need to know the YAML format.").
- `workflow`: ordered task-level steps.
- `scenario_guidance` / `reference_guidance`: `purpose`, `create_when`,
  `refine_when`, `include`, `avoid` for each kind of knowledge.
- `decision_rules`: rules for choosing between reuse, refine and create, and
  for keeping scenario and reference content separate.
- `knowledge_lifecycle`: the ordered lifecycle from user intent to captured
  reusable knowledge.

These are definition content. They are never shown by the CLI's
`show running-config` / `show configuration`, which only show the active
selection and running-entry descriptions.

A reference file follows the same loose shape (`name`, `description`, plus
whatever guidance content makes sense for that reference):

```yaml
name: example_timing_notes

description: >
  Fictional example of reusable engineering knowledge.

guidance:
  - This reference is intentionally simple; a real reference file can hold
    whatever structured or free-form operational knowledge is useful.
```

Nothing about these fields is enforced by the loader — a scenario or
reference author is free to add, omit, or restructure fields as long as the
document's root is a YAML mapping.

## Prohibited actions and operating principles

`lab/principles.yaml` (not editable through the CLI) is the place for
scenario-independent operating rules — the things that should always (or
never) happen regardless of which scenario is active, such as a list of
prohibited actions. `get_execution_instructions()` returns its content
alongside the active scenario and active references, combined into one
structured result:

```json
{
  "principles": { "workspace_principles": [...], "general_operating_principles": [...], "prohibited_actions": [...] },
  "scenario": { "name": "getting_started", "content": { "...": "..." } },
  "references": [ { "name": "cisco_platform_guidance", "content": { "...": "..." } } ]
}
```

## Authoring and selecting scenarios/references

The human CLI (`./run_cli.sh`) separates *authoring* a scenario/reference
from *selecting* which one MCP currently uses — see
[cli_reference.md](cli_reference.md) for the full command reference:

- **Authoring**: global mode's `scenario <name>` / `reference <name>`
  create or edit a scenario/reference *definition* — an existing name loads
  it as a candidate, a new name starts a fresh minimal one. This enters
  `network-lab(config-scenario-<name>)#` / `network-lab(config-reference-<name>)#`,
  where `edit` opens the candidate in an external YAML editor
  (`$VISUAL`/`$EDITOR`/`vim`); there is no structured field-by-field editor,
  consistent with the format not being fixed.
- **Selection**: `running-config` mode's `scenario <name>` / `reference
  <name>` / `no reference <name>` change which scenario/references MCP
  uses, validating that the name exists as an exact, case-sensitive match
  against a file under `lab/scenarios/`/`lab/references/`, or is the
  definition currently being authored in the same configure session.

## Why the schema is not fixed

Scenarios are expected to represent very different kinds of work — for
example environment builds, network configuration, troubleshooting,
failover or feature validation, migration validation, design creation,
configuration review, or documentation creation. Committing to a rigid
schema before enough of those shapes have been used in practice would risk
over-constraining scenario authors. A firmer scenario schema may be designed
once real usage patterns are clearer.
