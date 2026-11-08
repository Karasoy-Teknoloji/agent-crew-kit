# Contributing

Thanks for taking a look.

- **Issues are welcome**: bugs, unclear docs, ideas. Say what you ran, what you expected and what happened (OS, Python and Claude Code versions help).
- **Pull requests are reviewed before merge.** Keep a PR to one topic, add or update the tests next to the code you change (`python -m unittest discover -s <tests folder>`, see the README), and keep the "single code path" rule: extend the existing mechanism instead of adding a second one.
- **Never put secrets in issues, PRs or test data**: no tokens, passwords, keys, connection strings, internal host names or IP addresses, and no personal data. Mask them (`***`) or use placeholders such as `kv/<team>/<system>`, `example.com`, `192.0.2.10`. If you posted one by mistake, rotate it first, then tell us so we can remove the text.
- Security problems go through [SECURITY.md](SECURITY.md), not through a public issue.
- Code comments and some messages are Turkish; English is fine for issues, PRs and new comments.

By contributing you agree that your contribution is licensed under the MIT licence of this repository.
