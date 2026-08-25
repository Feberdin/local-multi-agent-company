<!--
Purpose: Explain private vulnerability reporting and secret-handling rules.
Input/Output: A reporter supplies a minimal private report; maintainers return triage and remediation steps.
Invariants: Never place credentials, personal data, or exploitable details in a public issue.
Debugging: If GitHub private reporting is unavailable, contact the maintainer through an established private channel.
-->

# Security Policy

## Reporting a vulnerability

Do not open a public issue for an unpatched vulnerability or exposed credential.
Use GitHub's private vulnerability-reporting function when it is available. If
the repository does not offer that function, contact the maintainer through an
existing trusted private channel and include only the minimum necessary details.

Include the affected version or commit, impact, reproducible steps, and a
suggested mitigation. Do not include real tokens, passwords, private keys,
cookies, personal records, production exports, or unredacted logs.

## Secret handling

- Store runtime credentials outside Git and reference only documented variable names.
- Keep local environment and secret files ignored.
- Treat every committed credential as compromised: revoke or rotate it first,
  then remove it from the current tree and assess Git-history cleanup.
- Secret-scanning findings must be resolved, not silenced without a documented
  false-positive reason tied to an exact non-secret fingerprint.

## Supported versions

Security fixes target the default branch unless a release-specific policy is
documented in this repository. Dependency and secret-scan pull requests must
pass the repository's CI checks before merge.
