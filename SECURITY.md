# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Report it privately through GitHub: the repository's **Security** tab → **Report a vulnerability** (private vulnerability reporting). Include what is affected (file or tool), how to reproduce it, and the impact you see.

We acknowledge a report within 5 working days and keep you posted until it is fixed. Please give us a reasonable time to release a fix before you disclose it.

## Scope

The code in this repository: the installers, hooks, status line scripts and tools (for example `tools/vault_cli.py`, `tools/evidence_hook.py`, `tools/gh_limit.py`, `tools/data-masking/trim.py`). Problems in Claude Code itself go to Anthropic; problems in Vault, GitHub or other services go to their vendors.

## Secrets

This kit is built so secret values never reach output (`vault_cli.py` redacts them). If you find a way to make a value appear in output, logs or the model context, that is a vulnerability: report it as above.
