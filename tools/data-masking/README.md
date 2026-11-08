# data-masking (opt-in)

## Not a guarantee

`kirp.py` is a **best-effort safety net**. Regex masking of untrusted free text can never be complete: new encodings, layouts
and key names will always exist (see "Known limits"). The control is the rule itself: **do not read raw personal data**
(production rows, logs with identifiers) - use counts, aggregates and masked samples, and mask at the source (the query,
the export, the service) before anything reaches a tool. The hook catches the common slips; it does not make raw reads safe.

`kirp.py` masks raw personal data in tool output before the model sees it, and truncates very large output.

Nothing is installed or enabled by default. Use the hook, the filter, or neither.

## What it masks

Matches are replaced by a stable token: `[TCKN:ab12cd]`, `[TEL:..]`, `[EPOSTA:..]`, `[IBAN:..]`, `[KART:..]`. The eight hex
characters (six in versions before this one; old six-hex tokens in stored text are still recognised as already masked) come from an HMAC-SHA256 over the normalised value with a local key, so the same value always gives the same
token (rows can still be correlated) and the value cannot be recovered from the token without the key.

| Token | Detects | Notes |
|---|---|---|
| `TCKN` | 11-digit national ID | checksum must hold; a number is skipped only when the FULL key (camelCase boundaries and `.`/`-` read as `_`, case-insensitive) is exactly one of an explicit CI allowlist: `run_id`, `job_id`, `check_run_id`, `check_suite_id`, `workflow_id`, `workflow_run_id`, `artifact_id`, `release_id`, `asset_id`, `comment_id`, `deployment_id`, `review_id`, `status_id`, `milestone_id`, `label_id`, `event_id`, `build_id`, `pipeline_id`, `issue_id`, `pull_request_id`, `installation_id`, `repository_id`, `step_id`, `task_id`, `github_run_id`, `ci_*_id` (so `runId`, `RUN_ID`, `github.run_id` work), or the number follows `/runs/`, `/jobs/`, `/comments/` ... in a URL path. Everything else is masked: a bare `id`, `caller_id`, `caller-id`, `callerId`, `x-customer-id`, `abone_no_id`, `identity-id`, `foo_id`, `paid`, `Said`. No list of person words is involved |
| `TEL` | Turkish mobile and landline numbers | `0532 123 45 67`, `+90 (532) ...`, `0090 ...`, `5321234567`, `(212) 123 45 67`; all formats of one number give one token; IPs, timestamps, versions, hashes are left alone |
| `MAIL` | e-mail addresses | lower-cased before hashing; file-name-like suffixes (`image@2x.png`, `x@1000.service`) and allowed domains are left alone |
| `IBAN` | Turkish IBANs | mod-97 check |
| `CARD` | payment cards, 13-19 digits | Luhn check and a plausible leading digit |

Digit boundaries: a number is never part of a longer digit run and never the fraction of a decimal (`1.<digits>` stays), but
letters, `_`, `.` and `,` around it do not protect it: `No.<TCKN>`, `T.C.<TCKN>`, `TC<TCKN>`, `<TCKN>.0`, `identity_<TCKN>.pdf`,
`<TCKN>_scan.pdf`, CSV `1,<TCKN>,x`, `Tel.0532...` are masked (also in Grep file lists). A TCKN in 3-3-3-2 groups (blank, dot,
dash; a dotted group that is a valid IPv4 address stays) is masked. Phone numbers with a `0` / `90` / `+90` prefix, IBANs and cards
also match when the groups are split by two blanks, NBSP, U+2011 or U+2013 (any dash). Every Unicode format/control/combining
character (Cf, Cc, Mn, Me; not newline or tab) between two digits is removed before matching (LRM, ZWSP, NUL, backspace, CR ...).
A bare `5XXXXXXXXX` (ten digits, no prefix, no separator) is a phone number only right after a phone-like key (`tel`, `telefon`,
`phone`, `gsm`, `cep`, `mobile`, `msisdn`, `number`, `caller`, `callee`, `arayan`, `aranan`, `src`, `dst` ..., also camelCase
`callerPhone`, and the dict key of a JSON leaf): without one it is a file size, `MemTotal`, `rx_bytes` or a row number far more often.
GitHub ids: `database_id` / `databaseId` and `/job/<id>` are in the allowlist; for a command that is exactly ONE `gh run list|view|watch`
or `gh api .../actions/runs|jobs` call (no `;`, `&&`, `|`, `$(`, backticks, newline; not `--log`) an 11-digit number in an id
position (the ID column of `gh run list`, `· <id>` / `(ID <id>)`, an `id` key in the JSON) stays readable; any other valid TCKN in
that output is masked. JSON numbers that are TCKN / phone / card shaped (`{"tckn": 12345678901}`) are masked (the number becomes the token string).
Percent, HTML-entity and `\uXXXX` forms (`email=ayse%40...`, `%2B90...`, `&#64;`, `&commat;`, `\u0040`, double encoding such as
`%2540`) are decoded in a copy of the line; when the copy holds an identifier, the decoded line is emitted, masked; other lines stay
byte for byte. Decoding never adds a line break or control character, and never unmasks a value the original line had.

Phone numbers also match with NBSP, thin spaces, tabs or dots (`0532.123.45.67`, dots only after a `0` / `90` / `+90` prefix) as separators. E-mail addresses may contain Unicode letters (`cagri@firma.com` written in Turkish, IDN domains); the TLDs `py rs md so sh zip` are real TLDs and no longer treated as file suffixes (`x@y.png`, `x@1000.service` still are). Non-ASCII decimal digits (fullwidth, Arabic-Indic ...) are read as ASCII digits (`TCKN`, `TEL`, `CARD`, `IBAN`); the text always comes back in that normalised form, so unrelated Arabic / Persian numerals are shown as ASCII digits.

The HMAC key itself is masked: its 64-hex literal (any case) becomes `[KEY:masked]` (`cat ~/.claude/.mask-key` through the hook or the filter shows no key; the key file is only read, never created, for this). The key split into hex groups is masked too: pairs or quads separated by up to three blanks, tabs, newlines, `,`, `:` or `-`, with an optional `0x` / `\x` per byte (`xxd`, `hexdump -C`, C arrays `{0x30, 0x31, ...}`, Python bytes `b'\x30...'`): any run holding 16 key characters, or 8 key bytes in hex. Not covered: base64, octal, decimal byte lists, line-wrapped text or any other re-encoding. **Required:** a deny rule for reading the key file (`Read(~/.claude/.mask-key)` and Bash `cat` / `xxd` / `base64` of it) in `settings.json` - that deny is the real control; masking its printouts is only a second line.

Masking runs before truncation and before the full output is stored, so the stored copy is masked too.
Idempotent: masking masked text changes nothing.

## Credentials

Credentials are masked like personal data, with the token `[SIR:xxxxxx]` (HMAC, same key; same value = same token).

- Key/value pairs whose key is (case-insensitive) `password`, `passwd`, `pwd`, `pass` (exactly), `secret`, `client_secret`, `token`,
  `access_token`, `refresh_token`, `api_key`, `apikey`, `api-key`, `authorization` (the value after `Bearer`/`Basic`), `private_key`,
  `sifre`, `şifre`, `password`, `key-sifre` (a prefix such as `DB_` is accepted, except for `pass`; camelCase / PascalCase keys are matched too: `ClientSecret`, `SmtpPassword`, `adminPassword`, `accessToken`, `apiKey`, `privateKey` - the keyword must be capitalised after a letter, so `mypassword`, `compass`, `bypass` still do not match; `cancellationToken` is deliberately ignored). Forms: JSON (`"password":"v"`,
  also backslash-escaped), YAML/INI/env (`password: v`, `Password = v`, `export TOKEN=v`), label lines (`Sifre : v`).
  Only the value is replaced, the key stays. The value must be 4+ characters; an unquoted value is one token.
- More forms: URL userinfo (`postgresql://app:pw@host/db`, only the password), XML (`<password>v</password>`, `<add key="Password" value="v"/>`,
  `<entry key="password">v</entry>`, `<setting name="Password"><value>v</value></setting>`), `=>` and `:=` assignments (`'password' => 'v'`), JSON keys written with
  unicode escapes (decoded before matching, so a masked key shows its decoded spelling), a YAML value on the line after `key:`
  (not a nested key, a comment or a list item; only the first value line), and an unquoted head shorter than 4 characters runs on over commas
  (`password=Ab1,xyz9Q`; a comma after 4+ characters still ends the value).
- Key suffixes and all-caps keys: after the keyword a suffix may follow when it starts with `_`, `.`, `-`, a digit or an upper-case
  letter (`aws_secret_access_key`, `SECRET_KEY`, `"SecretAccessKey"`, `secretKey`, `accessKey`, `Password1`, `passwordConfirm`,
  `DB_PASSWORD_PROD`), at most 40 characters; an all-caps keyword glued to a prefix counts (`PGPASSWORD`, `VAULTTOKEN`).
  `tokens`, `max_tokens`, `secretary`, `tokenizer` are not keys; a key whose last segment names metadata (`_min_length`, `_expiry`,
  `_type`, `-name`, `SecretRef`, `_url`, `_file`, `_path` ...) is left alone, and so are `PWD` / `OLDPWD` / `pwd` holding a directory.
  More keywords: `passphrase`, `access_key`, `signing_key`, `auth_key`, `encryption_key`, `master_key`.
  A purely numeric value shorter than 6 digits is masked only under a strong key (`password`, `secret`, `*key`, `sifre`, `password`,
  `passphrase`): `pass=1234`, `token=4096`, `Pass: 12 Failed: 0` stay, `pass=123456` and `password=1234` are masked.
- Name / value structures: k8s `- name: DB_PASSWORD` + `value: x`, JSON `{"name": "DB_PASSWORD", "value": "x"}` and
  `{"key": "password", "value": ...}` in either order, `vault kv get` tables (`password    x`), psql expanded rows (`password | x`),
  psql and mysql result tables (every cell under a credential column, e.g. `password_hash`), `.netrc` (`machine h login u password x`).
- HTTP / CLI auth: `Cookie:` (every value) and `Set-Cookie:` (the first pair; `Path`, `HttpOnly` ... stay), `curl -u user:pw` /
  `--user` / `--proxy-user` (a short number such as `docker run -u 1000:1000` stays), `Authorization:` / `Proxy-Authorization:` with
  any scheme (`Token`, `Digest ...`, `AWS4-HMAC-SHA256 Credential=..., Signature=...`: everything after the scheme), `https://TOKEN@host`
  (userinfo without a colon, 12+ characters with letters and digits), and query parameters `key`, `sig`, `signature`, `token`,
  `secret`, `password`, `apikey`, `api_key`, `access_key`, `auth`, `X-Amz-Signature`, `X-Amz-Security-Token`, `X-Amz-Credential`,
  `X-Goog-Signature`, `X-Goog-Credential`.
- Well-known shapes anywhere: Vault `hvs.`/`hvb.`, GitHub `ghp_`/`gho_`/`ghs_`/`ghu_`/`ghr_`/`github_pat_`, Cloudflare `cfut_`,
  AWS `AKIA...` / `ASIA...`, Slack `xox[abprs]-` and webhook URLs (`hooks.slack.com/services/...`), JWT (`eyJ...`.`...`.`...`),
  Anthropic `sk-ant-`, OpenAI `sk-proj-`, GitLab `glpat-`, Google `AIza...`, Stripe `sk_live_` / `rk_live_` (and `_test_`), npm `npm_`,
  the private part of a PuTTY `.ppk` file (`Private-Lines:` to `Private-MAC:`), and whole `-----BEGIN ... PRIVATE KEY-----` blocks
  (also `PGP PRIVATE KEY BLOCK`, four dashes, lower case; a block with no END line is masked to the end).
- Code and config shapes (round 4): a name and a value passed as two string literals right after `(` or in an indexer
  (`headers.Add("X-API-Key", "v")`, `AddWithValue("@password", "v")`, `new AuthenticationHeaderValue("Bearer", "v")`,
  `headers["X-Api-Key"] = "v"`, `new NetworkCredential("user", "v")`, also `$"..."` / `@"..."` literals; a scheme word such as `Bearer ` stays,
  an interpolated `{token}` or a non-literal value is left alone); `Pass` as a PascalCase suffix (`SmtpPass`); ASP.NET
  `validationKey` / `decryptionKey` (`<machineKey>`); verbatim `Password=""v"";` and escaped `Password=\'v\'` in connection strings;
  a bare `Bearer <opaque token>` (20+ characters with a digit); an opening quote with no closing one on the line (a cut or partly masked line);
  and a tail after the first blank: `SmtpPassword=Ab9 xY 12` / `password: a b c` at the start of a line (no blank before `=` / `:`, a strong key such as
  password/secret/sifre/parola/key) masks the whole rest of the line. `Pass: 1234 Failed: 0`, `password = expr` and values that stop at `;` or a quote keep the short rule.
- Not touched: prose ("the password is..."), keys without values, values shorter than 4 characters, and values that are
  **entirely** a placeholder: `null`/`true`/`false`/`none`/`nil`/`undefined`/`required`/`optional`/`string`/`empty`/`redacted`/`hidden`/`masked`, runs of `*` / `x` / `.` / `#` / `-` / `_`, an already masked token (`[SIR:xxxxxx]`, `[TEL:xxxxxx]`),
  `${PRESENT}` / `${secrets.pw}`, `$DB_PASS` and `$(DB_PASS)` (upper-case names; each `_`-separated segment is letters, the first one may end in at most two digits: `$S3_KEY`, `$OAUTH2_SECRET`),
  `<your-password>` / `<YOUR_PASSWORD>` / `<db password>` (all-lower or all-upper words, digits only after a separator), `%DB_PASS%` / `%appdata%`, `{{ secrets.pw }}`.
  Placeholders are recognised by structure, not by looking like a name: `$ADMIN2026`, `$PASS12345`, `<Secret123>`, `<password1>`, `%Pass9%`, `%Pass%` are masked.
  The allowlist file has no effect on credentials (it lists domains and phone numbers only).
- The skip rule looks at the WHOLE value, never at its first character or its shape: real passwords start with `$`, `!`, `%`, `<`
  or look like code, so `$ecretPass1`, `$ADMIN2026`, `<Secret123>`, `!Bang2026`, `%Pass9`, `<Pass#1x`, `Admin(2026)`, `a.b.c.d` and `args.password` are masked.
  A code literal such as `password=os.environ['X']` is therefore masked too (a false positive is preferred to a leaked secret).
  A closing bracket at the end of an unquoted value belongs to the value only when it closes an opener inside it
  (`Admin(2026)`), otherwise it stays outside (`(password=abc)`).
- Terminal colour codes (`ESC[32m` ...) and zero-width characters between digits are ignored while matching, so coloured output cannot hide a value or a
  key; the text that comes back is always the normalised, masked text (no colour codes, no OSC titles/hyperlink payloads, zero-width characters between digits removed): what the normaliser removes was never scanned, so it is never returned.
- Round 4 limits: values shorter than 4 characters are still not masked; a key named only `Credentials` / `Pw`; an unquoted value that contains `;`, a quote or (in the middle of a line) a blank; a value written as an expression (`"Bearer " + token`). The pair rule also fires on a string list `["password", "x"]` (false positive preferred). 59 lines of 316,000 (git log -p and source files of 6 repos plus 12 MB of .cs/.json/.config/.sh/.py) changed against the previous version: credential-like literals and machineKey lines; run time +5 %.
- Limits: output of a Bash command that exits non-zero and Read of repo files are not rewritten (see Known limits); CLI flags such as
  `--password v`, `-p v` or `sshpass -p v` are not covered (too ambiguous); a password written in a free sentence is not recognised; a secret
  whose key is not in the list (e.g. `db_pass`, `credentials`, a bare `key=` outside a URL query) is only caught if it has a well-known shape.
  Code lines are masked too (`self.password = os.environ[...]`, `passwordHash = Hash(password)`): a false positive is preferred.
  False-positive risk: a descriptive line such as `password: must be long` masks its first word.

## Install the hook (Claude Code)

Add to `~/.claude/settings.json` (user scope; never inside a repo). Replace the path with your clone:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "/usr/bin/python3",
            "args": ["-I", "/path/to/claude-shared/tools/data-masking/kirp.py", "--hook"], "timeout": 30 }
        ]
      },
      {
        "matcher": "mcp__grafana.*|mcp__plugin_grafana-mcp_grafana.*|mcp__plugin_slack_slack__.*|mcp__claude-in-chrome__(get_page_text|read_page|find|javascript_tool|read_network_requests|read_console_messages)|Read|Grep",
        "hooks": [
          { "type": "command", "command": "/usr/bin/python3",
            "args": ["-I", "/path/to/claude-shared/tools/data-masking/kirp.py", "--hook"], "timeout": 30 }
        ]
      }
    ]
  }
}
```

- The second matcher is an example for Grafana, Slack and Chrome text readers plus `Read` and `Grep`; list the tools your team
  actually uses. Every tool name other than `Bash` and `Read` that the matcher routes to the hook is masked as a generic
  tool response tree (strings and dict keys), so adding `Grep` (or any other tool) to the matcher is all that is needed. Every tool whose result can carry personal data needs an entry, otherwise it is not masked.
- `Read` only gets masked for files under `/tmp/`, `~/.claude/tool-output/`, `~/.claude/projects/` and `~/Downloads/`
  (saved query results, downloads, stored tool output). Other paths pass through untouched.
- The script needs only the Python 3 standard library. Tested on Linux only.
- The hook fails closed (see "Time budget and failure behaviour"): text that could not be masked is replaced by a notice and an
  error counter is logged; it is never passed through raw.

## Filter mode (for production reads)

The hook does not fire for a failed Bash command, so pipe production reads through the filter. It masks even when the
command fails:

```
<command> 2>&1 | python3 -I /path/to/claude-shared/tools/data-masking/kirp.py
```

Use `set -o pipefail` in the shell if you need the exit code. Python callers can `import kirp` and use
`kirp.mask_out(text) -> (masked_text, Counter)`; it never raises and never returns text it could not mask.

## Files and settings

Everything is created at first use with mode `0600`; the repo ships no key and no data.

| What | Default | Override |
|---|---|---|
| HMAC key (32 random bytes, hex; never printed) | `~/.claude/.mask-key` | `TRIM_KEY_FILE` |
| Count log (counts per run, never values) | `~/.claude/mask-counter.log` | `TRIM_COUNT_LOG` |
| Allowlist | `~/.claude/mask-allowed.txt` | `TRIM_ALLOWLIST` |
| Stored full output of truncated results (7 days) | `~/.claude/tool-output/` | - |

Keep the key per person. Do not commit it or copy it to shared machines. If the key file cannot be created, a temporary
per-process key is used (masking still works, tokens just differ between runs). Delete the key to rotate it; all tokens change.

Environment variables (set them in the `env` block of `settings.json`, i.e. in the environment of the hook process, not
in a command an agent writes):

| Variable | Effect |
|---|---|
| `TRIM_MASK_CLOSED=1` | masking off |
| `TRIM_CLOSED=1` | truncation off (masking stays) |
| `TRIM_THRESHOLD_LINE`, `TRIM_THRESHOLD_BYTES` | truncation thresholds (default 250 lines / 30000 bytes) |
| `TRIM_MASK_DURATION` | masking time budget in seconds for the whole process (default 7, capped at 20); see below |

Truncation keeps the first 60 and last 60 lines plus up to 40 error lines (`error|fail|exception|traceback|fatal`) and writes
the full masked output to `~/.claude/tool-output/`. It is skipped for commands that must stay whole (`git diff/show/log -p`,
`diff`, `head`, `tail`, `sed -n`) and when the command contains the comment `# full-output`; masking still applies there.

### Allowlist file format

One entry per line, `#` starts a comment. An entry is either

- an e-mail domain (`example.org`, optionally `@example.org`): that domain and its subdomains are not masked, or
- a phone number (digits, spaces, `+`, `()`, `-`): that number is not masked in any format (compared on its last 10 digits).

```
# technical / own addresses that must stay readable
ci.example.org
@mail.example.org
0850 000 00 00   # public switchboard
```

Built in: `github.com`, `users.noreply.github.com`, `anthropic.com`, `example.com/.org/.net`, `localhost`, `invalid`, `test`,
`example.com`, `example.org`, `example.net`. Add your own company domains to the file if you want them readable.

## Time budget and failure behaviour

Tool output is untrusted (web pages, logs), and the harness shows the output **unmasked** when the hook is killed or times out.
So masking is built to finish early and to fail closed:

- Every pattern is linear-time (no nested or ambiguous quantifiers; the credential key is matched by itself, not through a
  repeated prefix group). `test_trim.py` (`Robustness`) feeds 100 KB and 1 MB adversarial inputs (`a_a_a_...`, `password` repeated,
  digit runs, `-----BEGIN` without END, long e-mail-like strings ...) and asserts each finishes in under 2 s.
- Text is masked in pieces of about 128000 characters under one time budget per process (`TRIM_MASK_DURATION`, default 7 s,
  capped at 20 s). The budget is shared by stdout, stderr and the stored copy.
- A match can never straddle a cut: pieces end only after a newline, a line longer than the piece size is masked whole (the
  patterns are linear-time), and a piece that would end inside a `-----BEGIN` block is extended past its `-----END` line (or to
  the end of the text). A single line or block over 8 MB is withheld with `[mask: line too long, N lines withheld]`.
  `test_trim.py` (`Boundary`) places every class of match exactly across the old cut and checks that masking in small pieces
  equals masking the text at once.
- The budget starts when the input has been read completely: a slow producer (`(sleep 8; cmd) | kirp.py`) does not use it up.
- Second line of defence: on POSIX a `SIGALRM` interrupts a piece that is still running when the budget ends (CPython's regex
  engine checks signals), so even a future catastrophic pattern cannot hold the hook past the budget.
- Huge trees: the walk over an MCP / Grep response checks the budget at every node (by the clock every 1024 nodes, so a tree of
  numbers stops too). Once it is spent the walk stops: the fixed envelope keys keep their shape (value withheld), everything
  else is dropped and ONE notice (`[mask: time budget exceeded, the rest of the tool output is withheld]`, a text block in a
  content list) stands for it - no per-leaf notices, so the output never grows. Short texts are masked without piece splitting
  and cached (keys repeat millions of times). Measured on Linux: 3 million short keys (50 MB) 7.7 s, 2 million content
  blocks (134 MB) 7.5 s, a 150 MB persisted Bash output 7.5 s - all answered, none raw, output smaller than the input.
- Input the hook cannot parse (not JSON, nested deeper than Python's recursion limit, not an object) is withheld with a fixed notice
  (a leading BOM is stripped and trailing data after the object is ignored first). For Bash every field of the response is masked
  (also extra fields, a `stdout` that is a list or dict, a response that is not a dict, `isImage` with text); an image block keeps
  only its base64 `data` unmasked.
- When the budget ends (or a piece raises), the unprocessed part is replaced by ONE line, for example
  `[mask: time budget exceeded, 1200 lines withheld]` or `[mask: masking error, 3 lines withheld]`, and the count log gets
  `ATLANDI`. The withheld text is never shown. An unexpected error inside the hook replaces every string of that tool's
  response (dict keys are masked too) by `[mask: internal error, output withheld]`; if even that cannot be written, a fixed ASCII
  payload is emitted, so the hook never ends silently for a tool it handles. The hook output is pure ASCII JSON (`\uXXXX` escapes), a lone surrogate in
  a tool response cannot break it. Dict keys of MCP/other responses are masked like values (a key can carry data). Every string key goes through the same masking (no pre-filter); a key the masking leaves alone stays as it is. When the budget is spent, a key becomes `[mask: key N]` except the fixed envelope names (`type`, `text`, `content`, `stdout`, `stderr`, `isError`, `structuredContent`, `file`, `filePath`).
- Relation to the hook timeout: the hook `timeout` in `settings.json` is 30 s. Masking is capped at 20 s, the usual budget is 7 s;
  truncation, writing the stored copy and the JSON output come on top and take well under a second to a few seconds even for
  very large output. Do not set the hook timeout below 30 s, and do not raise `TRIM_MASK_DURATION` beyond what the timeout allows.

## Known limits

- A Bash command that exits non-zero does not trigger `PostToolUse`; its raw output reaches the model. Use the filter for
  production reads.
- `Read` of repo files is not masked (only the temporary locations listed above), and images or screenshots are not masked.
  Output written to a file and read by other means is not covered either.
- Only the patterns above are detected, with their checksums. Names, addresses, free text about a person, SMS content and
  national IDs of other countries are not detected; a mistyped number that fails its checksum is not masked.
- A killed hook process (timeout, OOM) emits nothing and the harness shows the output unmasked; the budget keeps the hook far
  below the timeout, but cannot help when the machine itself stalls.
- Large outputs: a persisted Bash output over 200 MB is not read (only Claude Code's 30000-character preview is masked); in
  any output, what is left when the budget runs out is withheld, not masked.
- Masking works on each piece separately for context checks that look back (`id:` before a number): a number on the line after an
  `id:` key is masked even though the whole-text run would skip it (the safe direction).
- Not handled (documented, not chased: exotic encodings and layouts): YAML anchors / aliases / tags (`&pw`, `*pw`, `!!binary`),
  YAML block scalars (`password: |`, `>-`) beyond their first line, TOML multi-line strings (three single or double quotes),
  XML CDATA sections, HCL heredocs, JSON keys split or put in arrays, double-escaped JSON, a key split by zero-width / soft hyphen /
  homoglyph characters or written with fullwidth `=` / `:`, a value on the line after `password=`, `.pgpass` / `htpasswd` /
  `/etc/shadow` lines, docker `config.json` base64 `auth`, a TCKN split at an arbitrary position (not 3-3-3-2) or across lines,
  a TCKN glued to other digits (also through decoding: `%30<TCKN>`), base64-encoded identifiers, e-mail with `(at)` / fullwidth `@` /
  quoted local part / IP-literal domain / a file-like TLD (`.json`, `.service`), phone numbers with mixed dot separators or without
  a prefix in dotted / slash form, cards split by dots, a psql / mysql table whose rows continue in the next 128000-character piece,
  a bare `5XXXXXXXXX` in a table column without a phone-like key, the HMAC key in any encoding other than the hex groups above.
- Not handled: `--password v` / `db_pass`, a dot-separated phone without a prefix (`532.123.45.67`), an e-mail whose domain is followed by `.digit` (`x@y.so.2` is masked up to `.so`), block scalars (`password: |`) beyond their first line, a YAML value more than one line below its key, the key written as base64 / split into pieces, a key in a nested JSON string with doubly escaped quotes. Without SIGALRM (library use from a thread) the budget is only checked between pieces (16000 characters; a single line over 1 MB is withheld).
- Tokens are not data: never write a token into a file, a query or a commit message.
- Safety net, not a boundary: keep production reads to aggregates (`COUNT`/`GROUP BY`, catalog metadata, LogQL metric
  queries) as `rules/WORKING-RULES.md` asks.

## Tests

```
python3 tools/data-masking/test_trim.py -v
```

Synthetic data only. The tests use temporary key, log and allowlist files and a temporary `HOME`; the real `~/.claude`
is not touched.
