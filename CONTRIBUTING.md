# How to Contribute

Thanks for your interest in contributing to Network Lab MCP! Here are a few general guidelines on contributing and
reporting bugs that we ask you to review. Following these guidelines helps to communicate that you respect the time of
the contributors managing and developing this open source project. In return, they should reciprocate that respect in
addressing your issue, assessing changes, and helping you finalize your pull requests. In that spirit of mutual respect,
we aim to review incoming issues and pull requests on a best-effort basis. This project is maintained without a
fixed review schedule, and lingering issues or pull requests that become inactive may be closed.

Please note that all of your interactions in the project are subject to our [Code of Conduct](/CODE_OF_CONDUCT.md). This
includes creation of issues or pull requests, commenting on issues or pull requests, and extends to all interactions in
any real-time space e.g., Slack, Discord, etc.

## Reporting Issues

Before reporting a new issue, please ensure that the issue was not already reported or fixed by searching through our
[issues list](https://github.com/CiscoDevNet/network-lab-mcp-community/issues).

When creating a new issue, please be sure to include a **title and clear description**, as much relevant information as
possible, and, if possible, a test case.

**If you discover a security bug, please do not report it through GitHub. Instead, please see security procedures in
[SECURITY.md](/SECURITY.md).**

## Sending Pull Requests

Before sending a new pull request, take a look at existing pull requests and issues to see if the proposed change or fix
has been discussed in the past, or if the change was already implemented but not yet released.

We expect new pull requests to include tests for any affected behavior. This project is still pre-1.0 and its behavior
may change between releases, so we may reserve breaking changes for a later release.

## Development and Testing

Network Lab MCP is a Python package (`requires-python >=3.10`) that drives real network lab devices through `tmux`,
`ssh`, and `telnet`. Install it from a local checkout in a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Run the default test suite, which never touches real hardware:

```bash
python -m pytest
```

Tests that need real lab devices are **opt-in** and are skipped unless `NETWORK_LAB_REAL_TESTS=1` is set. Run them only
against lab infrastructure that you are authorized to use:

```bash
NETWORK_LAB_REAL_TESTS=1 python -m pytest tests/test_real_lab_iosxr.py -v --tb=line
```

Please do not use `--showlocals` when tests could access real credentials, and never include credentials, private
`access-info`, device logs, or customer data in an issue, a pull request, or a test fixture. See
[SECURITY.md](/SECURITY.md) for the project's security boundaries.

When changing behavior, keep the MCP tool contract ([docs/mcp_tools.md](docs/mcp_tools.md)) and the human CLI reference
([docs/cli_reference.md](docs/cli_reference.md)) consistent with the code, and add or update tests for the affected
behavior.

## License

Network Lab MCP is licensed under the Apache License, Version 2.0 (see [LICENSE](/LICENSE)). As described in Section 5
of that license, unless you explicitly state otherwise, any contribution you intentionally submit for inclusion in the
project is submitted under the same terms. Source files carry an `SPDX-License-Identifier: Apache-2.0` header.

## Other Ways to Contribute

We welcome anyone that wants to contribute to Network Lab MCP to triage and reply to open issues to help troubleshoot
and fix existing bugs. Here is what you can do:

- Help ensure that existing issues follows the recommendations from the _[Reporting Issues](#reporting-issues)_ section,
  providing feedback to the issue's author on what might be missing.
- Review and update the documentation in [`docs/`](docs/) and the [README](README.md) so that they match the current
  behavior.
- Review existing pull requests, and testing patches in your own lab.
- Write a test, or add a missing test case to an existing test.

Thanks again for your interest on contributing to Network Lab MCP!

:heart:
