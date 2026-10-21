# Security policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/Sahil-SS9/severian/security/advisories/new) rather than in a
public issue. Include the version, a description of the impact and steps to reproduce. You should get a reply within
a week.

## What counts

Severian's security boundary is the five-level scope (tenant, profile, collection, agent, session). Of particular
interest:

- any read that returns, counts or hints at a memory from another scope;
- any way to modify or delete canonical observations;
- path traversal or overwrite through export/import;
- secrets or personal data written to logs or derived memories without the collection's policy allowing it.

## Supported versions

Security fixes go into the latest release. The design and its assumptions are in
[docs/threat-model.md](docs/threat-model.md).
