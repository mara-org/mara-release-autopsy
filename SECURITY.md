# Security

Mara Release Autopsy processes unusually sensitive data. Its design starts with the
assumption that both health exports and repositories may be hostile inputs.

## Privacy model

- Analysis is local. The application contains no network client, telemetry, account, or API key.
- Apple Health XML is streamed from the export; the ZIP is never extracted.
- Git is queried only for tag names and creator timestamps. Source files are not opened.
- Reports contain aggregates. They can still reveal private health and project information, so
  output files are created with user-only permissions where the platform supports them.

## Input hardening

- ZIP member paths, ambiguity, entry count, and declared/uncompressed size are bounded.
- XML is parsed incrementally and elements are cleared after use.
- Git runs with prompts, hooks, optional locks, network protocols, and filesystem monitoring
  disabled, plus hard time and result limits.
- CLI errors do not print raw XML, repository configuration, or health records.

Do not run an older release against an untrusted export or repository after a security fix has
been published.

## Reporting a vulnerability

Please open a private security advisory in the public repository. Include the affected version,
reproduction steps, and impact. Do not attach real Apple Health exports or private repositories;
use a minimal synthetic reproducer.

## Safety boundary

This project provides observational comparisons, not diagnoses, clinical guidance, or proof that
a release caused a health change. A security report is not the correct place for disputes about
the interpretation of an observational result.
