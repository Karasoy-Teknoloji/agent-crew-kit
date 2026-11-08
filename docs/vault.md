# vault.py: use Vault secrets without seeing them

`tools/vault.py` (Python 3, stdlib only, Windows + Linux). Auth: `VAULT_ADDR` from the environment, token from `VAULT_TOKEN` or `~/.vault-token`. Reference syntax: `mount/path#field` (the mount is the first path segment).

```
vault.py run --env [NAME=]mount/path#field [--file [NAME=]mount/path#field] -- <command ...>
vault.py check mount/path#field ...    # "var (N karakter)" or "missing", never the value
vault.py fields mount/path             # field names only
vault.py find <keyword...> [--refresh] [--all] [--limit N]   # FIND a credential: path / field-name / description search, never values
vault.py write mount/path#field [--clipboard | --stdin | --from-file PATH] [--keep-clipboard] [--not-secret]   # write a secret, other fields kept
vault.py delete mount/path#field          # remove one field, other fields kept
vault.py migrate-descriptions [--apply]      # one-time: copy description fields into custom_metadata (dry run without --apply)
```

## Finding a credential (`find`)

Before you report "no access / 401 / 403 / no token" or ask a person for a credential or permission, search the Vault: `vault.py find <system> [<scope>]`, for example `vault.py find panel okuma` (finds `.../panel/read-token`, the read-only token that a 403 on the publish token wrongly turned into "we have no read token"). Every keyword must occur in the record's path, field names or description (`description`, `description`, `not`, ...); matching is case-insensitive and Turkish-aware (`LİSANS`, `license`, `açıklama` = `description`). Output per match: the path, the field names and the description text; never a value. Then use the match: `vault.py run --env NAME=<path>#<field> -- <command>`, and try every plausible match (read vs publish vs admin scope) before concluding anything. Report the searches and the credentials tried. The Stop/SubagentStop hook `tools/evidence_hook.py` blocks a final message that claims missing access without a `vault.py find` run in the same turn (README, "Evidence hook").

How it works: the index is built without reading any secret value. It lists every KV mount the token can see (recursive LIST, 12 parallel requests), takes each record's field names from KV v2 `<mount>/subkeys/<path>?depth=1` (values come back as null) and, only for records that have a description-like field, the description from `custom_metadata.description` of `<mount>/metadata/<path>`. Nothing is read from `<mount>/data/`. It keeps path, field names and that description (capped at 240 characters, token shapes masked) in `~/.cache/vault/index.json` (directory 0700, file 0600, keyed by `VAULT_ADDR` and index format `v`; a cache of the old format, built from value reads, is never used and is overwritten). Records whose field names cannot be read that way (KV v1, no `subkeys` right) are indexed by path only; for the matches a query shows, their field names are read once on demand and kept in the cache (output `fields: ? (not readable with this token)` when that fails too). Prefixes in the config key `VAULT_FIND_EXCLUDING` (env, comma-separated `<mount>/<path>/`, default none; e.g. an offline backup tier where every request raises an alert) are never listed or read by `find`; use their full path with `fields` / `check` / `run`. With `VAULT_REQUEST_COUNTER=1` `find` prints the Vault requests it made per kind (`list`, `subkeys`, `metadata`, `data`) to stderr. The first run takes about 15-60 s (about 500 records); later runs read the cache (under 0.1 s) until it is 24 h old or you pass `--refresh`. Without Vault access it uses a stale cache with a warning, or exits 3 with "this is NOT evidence that no credential exists". Concurrent runs on a cold cache build once: the builder holds an OS lock on `~/.cache/vault/index.json.lock` (released when its process exits, so a crash leaves no stale lock), the others wait up to 120 s and read the finished cache, then build themselves on timeout. Exit 0 = matches, 1 = none, 3 = error.

Descriptions: `write` and `delete` on a description field (`description`, `description`, `not`, ...) also mirror it into `custom_metadata.description`, so `find` shows it (a description is readable by anyone with metadata read on that path, so keep secrets out of it). `vault.py migrate-descriptions` lists the records that have a description field but no metadata description; `--apply` reads only those records once and writes the metadata (run once by a coordinator after upgrading; it skips the `VAULT_FIND_EXCLUDING` prefixes).

## Writing a secret without it passing through chat (`write`)

`write` reads the value from the Windows clipboard (`--clipboard`, default on Windows) stdin (`--stdin`) or a file (`--from-file PATH`), never from argv. `--stdin` and `--from-file` accept multi-line values such as PEM keys (CRLF normalised to LF, surrounding whitespace trimmed); the clipboard path stays single-line. It reads the current KV v2 data, merges the one field and writes with check-and-set on the current version (creates the path if missing; other fields are kept). On success the clipboard is cleared (keep it with `--keep-clipboard`) and only `yazildi: <path>#<field> (<N> karakter, sha256 <8 hex>, surum <M>)` is printed. Surrounding whitespace is trimmed; empty or shorter than 8 characters (and multi-line from the clipboard) are refused. A field that is not a secret (a port, host or user name) can be shorter: add `--not-secret` (`printf 993 | python tools/vault.py write <path>#imap_port --stdin --not-secret`); empty values stay refused. Errors never contain the value. `delete` removes a single field the same way.

Example: copy the GitHub PAT (`read:packages`), then run

```
python tools/vault.py write kv/<team>/github.com/ci/packages-read#token --clipboard
```

(path layout `kv/<team>/<system>/...` is only an example; use your own Vault layout).

`run` passes secrets to the child only as environment variables (or as the path of a 0600 temp file, deleted afterwards), streams stdout/stderr with every value redacted (exact, URL-encoded, base64, JSON-escaped; also ghp_/github_pat_/hvs./JWT shapes and `password=` pairs) and returns the child's exit code. `NAME` defaults to the upper-cased field name. There is no subcommand that prints a value, by design.

Examples (`V=kv/<team>/db`):

```
vault.py run --env PGPASSWORD=$V#password -- psql -h host -U svc -d app -c "select count(*) from t"
vault.py run --env SQLCMDPASSWORD=$V#password -- sqlcmd -S host -U svc -Q "select 1"
vault.py run --env MYSQL_PWD=$V#password -- mysql -h host -u svc -e "select 1"
vault.py run --env GH_TOKEN=kv/<team>/github#token -- gh api user
vault.py run --env TOKEN=kv/<team>/api#token -- sh -c 'curl -s -H "Authorization: Bearer $TOKEN" https://api.example/health'
ssh host uptime        # key based, no secret needed: no change
```

On Windows PowerShell use `$env:TOKEN` instead of `$TOKEN` inside a `powershell -c` child. Tests: `python -m unittest discover -s tools/tests` (fake Vault, no real secrets).
