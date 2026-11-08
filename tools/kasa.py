#!/usr/bin/env python3
"""kasa: use Vault secrets without ever seeing or printing them.

  kasa.py run --env [NAME=]mount/path#field [--env ...] [--file [NAME=]mount/path#field] -- <command ...>
      (NAME defaults to the upper-cased field name; works with any command: psql, sqlcmd, mysql, gh, curl, ssh, dotnet)
  kasa.py check mount/path#field [...]     # prints "var (N karakter)" or "yok", never the value
  kasa.py fields mount/path                # prints field NAMES only
  kasa.py bul <keyword...> [--yenile] [--hepsi]   # FIND credentials: paths, field names, description (never values); all keywords must match
  kasa.py yaz mount/path#field [--pano | --stdin | --dosyadan PATH] [--pano-koru] [--gizli-degil | --kisa]   # writes a secret read from clipboard/stdin/file, never argv; --stdin/--dosyadan accept multi-line (PEM); --gizli-degil allows short non-secret values (ports) and marks the field non-secret; --kisa allows a short secret by design (PIN/OTP, >= 4 chars) that run still redacts
  kasa.py sil mount/path#field             # removes one field (other fields kept)
  kasa.py gizli-degil mount/path#field [--geri-al]   # declares an existing field non-secret (custom_metadata): run passes it on but does not redact it
  kasa.py aciklama-tasi [--uygula]         # one-time: copy description fields into custom_metadata (bul then reads no values)

Secrets reach the child only as environment variables (or as the path of a 0600 temp file that is
deleted afterwards). The child's stdout/stderr are streamed with every secret value redacted (exact,
URL-encoded, base64, JSON-escaped) and common token shapes masked. Exit code = child's exit code.
There is deliberately no subcommand that prints a value.

Auth (same path as the ekip-ajanlari operations-sm agent): VAULT_ADDR from the environment, token from VAULT_TOKEN or ~/.vault-token.
Reads KV v2 (`<mount>/data/<path>`), falling back to KV v1. The mount is the first path segment.
Writes (yaz/sil) use the same auth (vault_addr/vault_token), KV v2 only, merge with check-and-set.
"""
import argparse
import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # python -I drops the script dir
from gizli_yukle import gizli  # noqa: E402  (one hidden-launch helper)
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

MASK = b"***"
MIN_SECRET_LEN = 4  # shorter values would shred normal output
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REFUSED = {"print", "get", "show", "read", "cat", "export", "dump", "echo", "reveal", "value", "list-values"}
TOKEN_SHAPES = [
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"hv[sbr]\.[A-Za-z0-9_\-]{20,}"),
    re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(rb"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s&;\"',]+"),
]
SHAPE_WINDOW = 512  # bytes held back so a token shape split across chunks is still caught


class KasaError(Exception):
    pass


# ---------------------------------------------------------------- Vault access
def vault_addr():
    a = os.environ.get("VAULT_ADDR", "").rstrip("/")
    if not a:
        raise KasaError("VAULT_ADDR is not set")
    return a


def vault_token():
    t = os.environ.get("VAULT_TOKEN", "").strip()
    if t:
        return t
    p = os.path.join(os.path.expanduser("~"), ".vault-token")
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
    except OSError:
        t = ""
    if not t:
        raise KasaError("no Vault token (VAULT_TOKEN or ~/.vault-token)")
    return t


REQUESTS = {}  # Vault requests per kind (list/data/metadata/subkeys/other); printed by bul when KASA_ISTEK_SAYACI=1


def _count(url, method="GET"):
    u = urllib.parse.urlsplit(url)
    seg = (u.path.split("/") + ["", "", "", ""])[3]  # /v1/<mount>/<seg>/...
    kind = "list" if "list=true" in u.query else seg if seg in ("data", "metadata", "subkeys") else "other"
    kind = kind if method == "GET" else f"{method.lower()}-{kind}"
    REQUESTS[kind] = REQUESTS.get(kind, 0) + 1


def _http_get(url, token):
    _count(url)
    req = urllib.request.Request(url, headers={"X-Vault-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def read_record(path, cache=None):
    """Return the field dict of mount/path. Errors name the path, never values."""
    if cache is not None and path in cache:
        return cache[path]
    mount, _, rest = path.strip("/").partition("/")
    if not mount or not rest:
        raise KasaError(f"bad path '{path}' (expected mount/path)")
    base, token = vault_addr(), vault_token()
    q = urllib.parse.quote
    data = None
    for url, v2 in ((f"{base}/v1/{q(mount)}/data/{q(rest, safe='/')}", True), (f"{base}/v1/{q(mount)}/{q(rest, safe='/')}", False)):
        try:
            body = _http_get(url, token)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise KasaError(f"Vault HTTP {e.code} for {path}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")
        d = body.get("data") or {}
        if v2:
            data = d.get("data") if isinstance(d.get("data"), dict) else None
        else:
            data = d
        if data is not None:
            break
    if data is None:
        raise KasaError(f"record not found: {path}")
    if cache is not None:
        cache[path] = data
    return data


def parse_ref(ref):
    path, sep, field = ref.rpartition("#")
    if not sep or not path or not field:
        raise KasaError(f"bad reference '{ref}' (expected mount/path#field)")
    return path, field


def get_value(ref, cache=None):
    path, field = parse_ref(ref)
    rec = read_record(path, cache)
    if field not in rec:
        raise KasaError(f"field not found: {path}#{field}")
    v = rec[field]
    return v if isinstance(v, str) else json.dumps(v)


# ---------------------------------------------------------------- write (KV v2)
MIN_WRITE_LEN = 8


def _v2_url(path):
    mount, _, rest = path.strip("/").partition("/")
    if not mount or not rest:
        raise KasaError(f"bad path '{path}' (expected mount/path)")
    return f"{vault_addr()}/v1/{urllib.parse.quote(mount)}/data/{urllib.parse.quote(rest, safe='/')}"


def _read_v2(path):
    """Return (data dict, current version); ({}, 0) when the path does not exist yet."""
    try:
        body = _http_get(_v2_url(path), vault_token())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}, 0
        raise KasaError(f"Vault HTTP {e.code} for {path}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")
    d = body.get("data") or {}
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    return data, int((d.get("metadata") or {}).get("version") or 0)


def write_v2(path, data, cas):
    """POST merged data with check-and-set; returns the new version. Errors never contain values."""
    payload = json.dumps({"options": {"cas": cas}, "data": data}).encode("utf-8")
    req = urllib.request.Request(_v2_url(path), data=payload, method="POST",
                                 headers={"X-Vault-Token": vault_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 400:
            raise KasaError(f"write conflict or rejected for {path} (check-and-set); retry")
        raise KasaError(f"Vault HTTP {e.code} writing {path}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")
    return int(((body.get("data") or {}).get("version")) or 0)


def clipboard_read():
    if os.name != "nt":
        raise KasaError("clipboard is only supported on Windows; use --stdin")
    r = gizli.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                       capture_output=True)
    if r.returncode != 0:
        raise KasaError("cannot read the clipboard")
    return r.stdout.decode("utf-8", errors="replace")


def clipboard_clear():
    r = gizli.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value $null"],
                       capture_output=True)
    return r.returncode == 0


def clean_secret(raw, multiline=False, not_secret=False, short_secret=False):
    """not_secret: the caller declared a non-secret field (port, host, user name), so short values are allowed (never empty).
    short_secret: a short secret by design (6-digit PIN/OTP): allowed down to MIN_SECRET_LEN and still redacted by run."""
    v = (raw or "").strip()
    if multiline:
        v = v.replace("\r\n", "\n").replace("\r", "\n")
    elif "\n" in v or "\r" in v:
        raise KasaError("refused: value contains newlines")
    if short_secret and not not_secret:
        if len(v) < MIN_SECRET_LEN:
            raise KasaError(f"refused: --kisa value shorter than {MIN_SECRET_LEN} characters (run could not redact it)")
        return v
    if not v or (len(v) < MIN_WRITE_LEN and not not_secret):
        raise KasaError(f"refused: value empty or shorter than {MIN_WRITE_LEN} characters"
                        + ("" if not v else " (a non-secret field such as a port: add --gizli-degil)"))
    return v


# ---------------------------------------------------------------- redaction
def variants(value):
    """All byte forms of a secret that must be masked."""
    if len(value) < MIN_SECRET_LEN:
        return set()
    raw = value.encode("utf-8")
    forms = {value}
    forms.add(urllib.parse.quote(value, safe=""))
    forms.add(urllib.parse.quote_plus(value, safe=""))
    forms.add(urllib.parse.quote(value, safe="~"))
    for ea in (True, False):
        forms.add(json.dumps(value, ensure_ascii=ea)[1:-1])
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        s = enc(raw).decode()
        forms.update({s, s.rstrip("=")})
    if "\n" in value or "\r" in value:  # multi-line keys: mask each line too
        forms.update(l for l in re.split(r"[\r\n]+", value) if len(l) >= 8)
    return {f.encode("utf-8") for f in forms if len(f) >= MIN_SECRET_LEN}


class Redactor:
    """Streaming redactor: feed(bytes) -> safe bytes ready to emit; finish() flushes the held-back tail."""

    def __init__(self, values, shapes=True):
        forms = set()
        for v in values:
            forms |= variants(v)
        self.forms = sorted(forms, key=len, reverse=True)  # longest first
        self.shapes = shapes
        longest = max((len(f) for f in self.forms), default=0)
        self.keep = max(longest, SHAPE_WINDOW if shapes else 0)
        self.buf = b""

    def _mask(self, data):
        for f in self.forms:
            data = data.replace(f, MASK)
        if self.shapes:
            for rx in TOKEN_SHAPES:
                data = rx.sub(lambda m: (m.group(1) if rx.groups else b"") + MASK, data)
        return data

    def feed(self, chunk):
        self.buf = self._mask(self.buf + chunk)
        if self.keep == 0:
            out, self.buf = self.buf, b""
            return out
        cut = len(self.buf) - (self.keep - 1)
        if cut <= 0:
            return b""
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out

    def finish(self):
        out, self.buf = self._mask(self.buf), b""
        return out


# ---------------------------------------------------------------- commands
def _secure_file(path):
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        gizli.run(["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        os.chmod(path, 0o600)


def cmd_run(args):
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        raise KasaError("no command given after --")
    cache, env, values, tmpfiles, plain = {}, dict(os.environ), [], [], {}
    try:
        for kind, specs in (("env", args.env), ("file", args.file)):
            for spec in specs or []:
                name, sep, ref = spec.partition("=")
                if not sep or "/" in name:  # no NAME=: default to the field name
                    ref = spec
                    name = re.sub(r"[^A-Za-z0-9_]", "_", parse_ref(ref)[1]).upper()
                if not NAME_RE.match(name):
                    raise KasaError(f"bad --{kind} name '{name}' (expected [NAME=]mount/path#field)")
                val = get_value(ref, cache)
                rpath, rfield = parse_ref(ref)
                if rpath not in plain:
                    plain[rpath] = meta_not_secret(rpath)
                if rfield not in plain[rpath]:  # a field declared non-secret (gizli-degil) is passed on but not redacted
                    values.append(val)
                if kind == "env":
                    env[name] = val
                else:
                    fd, p = tempfile.mkstemp(prefix="kasa-")
                    tmpfiles.append(p)
                    os.close(fd)
                    _secure_file(p)
                    with open(p, "wb") as f:
                        f.write(val.encode("utf-8"))
                    env[name] = p
        exe = shutil.which(cmd[0], path=env.get("PATH")) or cmd[0]
        red_out, red_err = Redactor(values), Redactor(values)
        try:
            proc = gizli.popen([exe] + cmd[1:], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            raise KasaError(f"cannot start '{cmd[0]}' ({type(e).__name__})")
        lock = threading.Lock()

        def pump(src, dst, red):
            while True:
                chunk = os.read(src.fileno(), 4096)
                if not chunk:
                    break
                out = red.feed(chunk)
                if out:
                    with lock:
                        dst.write(out)
                        dst.flush()
            out = red.finish()
            if out:
                with lock:
                    dst.write(out)
                    dst.flush()

        ts = [threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer, red_out)),
              threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer, red_err))]
        for t in ts:
            t.start()
        rc = proc.wait()
        for t in ts:
            t.join()
        return rc
    finally:
        for p in tmpfiles:
            try:
                os.remove(p)
            except OSError:
                pass


def cmd_check(args):
    cache, rc = {}, 0
    for ref in args.refs:
        try:
            path, field = parse_ref(ref)
            rec = read_record(path, cache)
            if field in rec:
                v = rec[field]
                n = len(v if isinstance(v, str) else json.dumps(v))
                print(f"{ref}: var ({n} karakter)")
            else:
                print(f"{ref}: yok")
                rc = 1
        except KasaError as e:
            print(f"{ref}: yok ({e})")
            rc = 1
    return rc


def cmd_fields(args):
    for name in sorted(read_record(args.path)):
        print(name)
    return 0


# ---------------------------------------------------------------- bul: find credentials by path / field name / description
# The index is built WITHOUT reading any secret value: recursive LIST for the paths, KV v2 `subkeys` (field names, values
# come back as null) for the field names, and KV v2 metadata `custom_metadata.aciklama` for the description (only for records that
# have a description-like field). Nothing is read from `<mount>/data/`. Records whose field names cannot be read that way (KV v1,
# no `subkeys` permission) are indexed by path only; their field names are read on demand for the few matches a query shows.
# Cache: ~/.cache/kasa/index.json (0600), refreshed after 24 h; a cache from an older index format is rebuilt and overwritten.
CACHE_TTL = 24 * 3600
INDEX_VERSION = 2  # 1 (no "v" key) = built from <mount>/data reads; 2 = names/metadata only
DESC_FIELDS = ("aciklama", "description", "desc", "not", "notes", "note", "comment", "amac", "kullanim")
NOT_SECRET_META = "gizli_degil"  # custom_metadata key: comma-separated field names `run` does not redact (set by gizli-degil / yaz --gizli-degil)
DESC_META = "aciklama"  # custom_metadata key that carries the description (written by yaz / aciklama-tasi)
DESC_MAX = 240
META_VALUE_MAX = 512  # Vault limit for one custom_metadata value (bytes)
BUL_WORKERS = 12
BUL_DEFAULT_LIMIT = 30
# Never walked, listed or read by bul / aciklama-tasi: config key KASA_BUL_HARIC (env), comma-separated `<mount>/<path>/` prefixes,
# default none. Use it for a tier where every request raises an alert (e.g. an offline backup tier); use the full path with
# fields / check / run there instead.
def bul_exclude():
    return tuple(e.strip().strip("/") + "/" for e in os.environ.get("KASA_BUL_HARIC", "").split(",") if e.strip().strip("/"))


def cache_file():
    return os.path.join(os.path.expanduser("~"), ".cache", "kasa", "index.json")


def fold_tr(text):
    """Case-insensitive, Turkish-aware form: I/İ/ı/i -> i and ş ğ ü ö ç -> s g u o c, so 'okuma', 'OKUMA' and 'açıklama' all match."""
    import unicodedata
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def _mask_shapes(text):
    b = text.encode("utf-8", "replace")
    for rx in TOKEN_SHAPES:
        b = rx.sub(lambda m: (m.group(1) if m.groups() else b"") + MASK, b)
    return b.decode("utf-8", "replace")


def clean_desc(text):
    """One-line, token-shape-masked description that fits DESC_MAX characters and one custom_metadata value."""
    d = _mask_shapes(" ".join(str(text).split()))[:DESC_MAX]
    while len(d.encode("utf-8")) > META_VALUE_MAX:
        d = d[:-1]
    return d


def is_desc_field(name):
    return fold_tr(name) in DESC_FIELDS


def excluded(path):
    p = path.strip("/") + "/"
    return any(p.startswith(e) for e in bul_exclude())


def kv_mounts():
    """[(mount, version)] of the KV mounts this token can see; falls back to sys/mounts, then to 'kv' (v2)."""
    base, token = vault_addr(), vault_token()
    for ep in ("sys/internal/ui/mounts", "sys/mounts"):
        try:
            d = _http_get(f"{base}/v1/{ep}", token).get("data") or {}
        except (urllib.error.URLError, OSError, ValueError):
            continue
        found = d.get("secret") if isinstance(d.get("secret"), dict) else {k: v for k, v in d.items() if isinstance(v, dict) and "type" in v}
        out = [(k.strip("/"), str((v.get("options") or {}).get("version") or "1")) for k, v in found.items()
               if v.get("type") in ("kv", "generic")]
        if out:
            return sorted(out)
    return [("kv", "2")]


def _list_keys(mount, version, path):
    base, token = vault_addr(), vault_token()
    q = urllib.parse.quote
    seg = f"{q(mount)}/metadata/{q(path, safe='/')}" if version == "2" else f"{q(mount)}/{q(path, safe='/')}"
    try:
        return (_http_get(f"{base}/v1/{seg}?list=true", token).get("data") or {}).get("keys") or []
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return []
        raise


def _v2_get(path, kind, query=""):
    """GET <mount>/<kind>/<rest> (kind: subkeys or metadata, never data). Returns the 'data' dict, or None on 404."""
    mount, _, rest = path.strip("/").partition("/")
    url = f"{vault_addr()}/v1/{urllib.parse.quote(mount)}/{kind}/{urllib.parse.quote(rest, safe='/')}{query}"
    try:
        return _http_get(url, vault_token()).get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def field_names(path):
    """Sorted field names of a KV v2 record from `subkeys` (values arrive as null, never read).
    None = record missing or deleted; raises HTTPError when not allowed (403) or not supported."""
    d = _v2_get(path, "subkeys", "?depth=1")
    if d is None:
        return None
    sk = d.get("subkeys")
    if not isinstance(sk, dict):
        return None  # latest version deleted / destroyed
    return sorted(sk)


def meta_desc(path):
    """Description from custom_metadata (no value read); '' when none or not allowed."""
    try:
        d = _v2_get(path, "metadata") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return ""
    cm = d.get("custom_metadata") or {}
    return clean_desc(cm[DESC_META]) if isinstance(cm.get(DESC_META), str) else ""


def walk_paths(ex):
    """[(mount/path, kv version)] of every record the token can LIST, KASA_BUL_HARIC prefixes pruned before they are listed; plus skipped count."""
    paths, skipped = [], 0
    for mount, ver in kv_mounts():
        level = [""]
        while level:
            nxt = []
            for prefix, keys in zip(level, ex.map(lambda p: _safe(_list_keys, mount, ver, p), level)):
                if keys is None:
                    skipped += 1
                    continue
                for k in keys:
                    full = f"{mount}/{prefix}{k}"
                    if excluded(full):
                        continue
                    if k.endswith("/"):
                        nxt.append(prefix + k)
                    else:
                        paths.append((full, ver))
            level = nxt
    return paths, skipped


def build_index():
    """Walk every KV mount (parallel recursive LIST), then per record: field names from `subkeys` and, when the record has a
    description-like field, the description from custom_metadata. No `<mount>/data/` read, so no secret value is fetched.
    Returns {"v", "records": [{"p", "f", "d"}], "skipped": n}; "f" is None when field names are unknown (read on demand)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(BUL_WORKERS) as ex:
        paths, skipped = walk_paths(ex)

        def meta(item):
            path, ver = item
            names = None
            if ver == "2":
                try:
                    names = field_names(path)
                    if names is None:
                        return None  # gone or deleted: skipped, like before
                except (urllib.error.URLError, OSError, ValueError):
                    names = None  # no subkeys right: path only, names on demand
            desc = meta_desc(path) if ver == "2" and names and any(is_desc_field(n) for n in names) else ""
            return {"p": path, "f": names, "d": desc}

        recs = [r for r in ex.map(meta, paths) if r]
    return {"v": INDEX_VERSION, "records": sorted(recs, key=lambda r: r["p"]), "skipped": skipped + len(paths) - len(recs)}


def _safe(fn, *a):
    try:
        return fn(*a)
    except (urllib.error.URLError, OSError, ValueError):
        return None


def save_index(idx):
    path = cache_file()
    try:
        d = os.path.dirname(path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written only costs a rebuild next time


LOCK_TIMEOUT = 120  # seconds a process waits for another process's index build before it builds itself


def lock_file():
    return cache_file() + ".lock"


class BuildLock:
    """Per-user exclusive lock on <cache>.lock (flock on POSIX, msvcrt.locking on Windows). The OS drops it when its holder
    exits or dies, so a crashed builder never leaves a stale lock; the file itself is never deleted. `acquired` is False when
    the wait timed out or the lock cannot be used (the caller then builds without it)."""

    def __init__(self, timeout=None):
        self.timeout = LOCK_TIMEOUT if timeout is None else timeout
        self.fd, self.acquired = None, False

    def _try(self):
        if os.name == "nt":
            import msvcrt
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def __enter__(self):
        import time
        try:
            path = lock_file()
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return self
        deadline = time.time() + self.timeout
        while True:
            try:
                self._try()
                self.acquired = True
                return self
            except OSError:
                if time.time() >= deadline:
                    return self
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                if self.acquired:  # closing the descriptor releases the lock on both platforms
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            os.close(self.fd)
        self.fd, self.acquired = None, False


def _read_cache(path, addr):
    """The cache when it exists and matches the current address and index format, else None."""
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if (not isinstance(cached, dict) or cached.get("v") != INDEX_VERSION or cached.get("addr") != addr
                or not isinstance(cached.get("records"), list)):
            return None
        return cached
    except (OSError, ValueError):
        return None


def load_index(refresh=False):
    """(index dict, note). Uses the cache when fresh (same VAULT_ADDR and index format, < 24 h); on a failed refresh falls back
    to a stale cache of the current format. A cache of an older format (built from value reads) is never used and is overwritten.
    Only one process builds at a time (BuildLock): the others wait, then use the cache the builder wrote (for --yenile: a cache
    built after they started); after LOCK_TIMEOUT s a waiter builds on its own."""
    import time
    started = time.time()
    path, addr = cache_file(), vault_addr()
    cached = _read_cache(path, addr)
    if cached and not refresh and started - float(cached.get("built") or 0) < CACHE_TTL:
        return cached, ""
    with BuildLock() as lock:
        if lock.acquired:  # another process may have built while we waited
            again = _read_cache(path, addr)
            if again:
                cached = again
                built = float(again.get("built") or 0)
                if (built >= started) if refresh else (time.time() - built < CACHE_TTL):
                    return again, ""
        return _build_and_save(cached, addr)


def _build_and_save(cached, addr):
    import time
    try:
        idx = build_index()
        if not idx["records"]:
            raise KasaError("no readable records")
    except (KasaError, urllib.error.URLError, OSError, ValueError) as e:
        if cached:
            return cached, f"kasa: refresh failed ({e}); using the cache from {_age(cached)} ago"
        raise KasaError(f"cannot read Vault ({e}) and there is no cache; this is NOT evidence that no credential exists")
    idx.update({"addr": addr, "built": time.time()})
    if cached and len(idx["records"]) < len(cached["records"]) / 2:
        return idx, "kasa: refresh looks partial (many records unreadable); cache not overwritten"
    save_index(idx)
    return idx, ""


def _age(idx):
    import time
    h = (time.time() - float(idx.get("built") or 0)) / 3600
    return f"{h:.0f} h" if h >= 1 else "<1 h"


def search_index(idx, words):
    """Records where EVERY word occurs (folded substring) in path + field names + description, best first:
    path hits weigh most, then field names, then description."""
    ws = [fold_tr(w) for w in words if w.strip()]
    hits = []
    for r in idx["records"]:
        p, f, d = fold_tr(r["p"]), fold_tr(" ".join(r["f"] or [])), fold_tr(r.get("d") or "")
        score = 0
        for w in ws:
            s = 3 * (w in p) + 2 * (w in f) + (w in d)
            if not s:
                break
            score += s
        else:
            hits.append((-score, r["p"], r))
    return [r for _, _, r in sorted(hits, key=lambda h: (h[0], h[1]))]


def fill_names(records):
    """On demand, for the shown records whose field names are unknown: read the record once and keep only the names.
    Returns True when any record changed (the cache is then saved)."""
    changed = False
    for r in records:
        if r.get("f") is None:
            try:
                r["f"] = sorted(read_record(r["p"]))
                changed = True
            except KasaError:
                pass
    return changed


def cmd_bul(args):
    idx, note = load_index(args.yenile)
    if note:
        print(note, file=sys.stderr)
    hits = search_index(idx, args.words)
    shown = hits if args.hepsi else hits[:args.limit]
    if fill_names(shown):
        save_index(idx)
    for r in shown:
        print(r["p"])
        f = r.get("f")
        print(f"    fields: {', '.join(f) or '-'}" if f is not None else "    fields: ? (not readable with this token)")
        if r.get("d"):
            print(f"    {r['d']}")
    if any(len(w) >= 3 and fold_tr(w) in fold_tr(e) for e in bul_exclude() for w in args.words):
        print(f"kasa bul: {', '.join(bul_exclude())} is never indexed (KASA_BUL_HARIC); use its full path with fields/check/run.",
              file=sys.stderr)
    if os.environ.get("KASA_ISTEK_SAYACI") == "1":
        print("kasa: Vault requests: " + (", ".join(f"{k}={v}" for k, v in sorted(REQUESTS.items())) or "none (cache)"), file=sys.stderr)
    if not hits:
        print(f"kasa bul: no record matches all of: {' '.join(args.words)} ({len(idx['records'])} records indexed, cache {_age(idx)} old). "
              "Try fewer or different keywords (system name, host, 'token', 'pat', 'okuma'), or --yenile.")
        return 1
    more = len(hits) - len(shown)
    print(f"-- {len(hits)} match(es){f', {more} more hidden (add a keyword or --hepsi)' if more > 0 else ''}. "
          "Use one: kasa.py run --env NAME=<path>#<field> -- <command>   (values are never printed)")
    return 0


# ---------------------------------------------------------------- description in custom_metadata (so bul never reads values)
META_KEEP = ("max_versions", "cas_required", "delete_version_after")  # sent back unchanged with every metadata POST


def _write_meta(path, custom, cur):
    """POST <mount>/metadata/<rest> with the full custom_metadata map. The record's version settings (META_KEEP) are copied
    from the metadata just read, so they never fall back to defaults (PATCH would avoid that but needs the 'patch' right)."""
    mount, _, rest = path.strip("/").partition("/")
    url = f"{vault_addr()}/v1/{urllib.parse.quote(mount)}/metadata/{urllib.parse.quote(rest, safe='/')}"
    _count(url, "POST")
    body = {k: cur[k] for k in META_KEEP if k in (cur or {})}
    body["custom_metadata"] = custom
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"X-Vault-Token": vault_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except urllib.error.HTTPError as e:
        raise KasaError(f"Vault HTTP {e.code} writing metadata of {path}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")


def _set_meta(path, key, value):
    """Set custom_metadata[key] ('' removes it), keeping the other keys. Returns True when written."""
    try:
        cur = _v2_get(path, "metadata")
    except urllib.error.HTTPError as e:
        raise KasaError(f"Vault HTTP {e.code} reading metadata of {path}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")
    custom = dict((cur or {}).get("custom_metadata") or {})
    if custom.get(key, "") == value:
        return False
    if value:
        custom[key] = value
    else:
        custom.pop(key, None)
    _write_meta(path, custom, cur)
    return True


def set_meta_desc(path, desc):
    """Mirror the description into custom_metadata[DESC_META] ('' removes it). Returns True when written."""
    return _set_meta(path, DESC_META, clean_desc(desc) if desc else "")


def meta_not_secret(path):
    """Field names the record declares non-secret (custom_metadata[NOT_SECRET_META], comma-separated). Empty when none or
    when the metadata cannot be read: an unreadable declaration means 'secret' (fail safe)."""
    try:
        cm = (_v2_get(path, "metadata") or {}).get("custom_metadata") or {}
    except (urllib.error.URLError, OSError, ValueError):
        return set()
    v = cm.get(NOT_SECRET_META)
    return {f.strip() for f in v.split(",") if f.strip()} if isinstance(v, str) else set()


def set_not_secret(path, field, declared=True):
    """Declare (or withdraw) one field of the record as non-secret. Returns True when written."""
    fields = meta_not_secret(path)
    fields = fields | {field} if declared else fields - {field}
    return _set_meta(path, NOT_SECRET_META, ",".join(sorted(fields)))


def _desc_of(data):
    return next((data[n] for n in sorted(data) if is_desc_field(n) and isinstance(data[n], str)), "")


def cmd_aciklama_tasi(args):
    """One-time migration: copy each record's description field into custom_metadata so bul shows it without reading values.
    Reads data only of records whose field names (subkeys) contain a description field and whose metadata has none yet."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(BUL_WORKERS) as ex:
        paths, _ = walk_paths(ex)

        def todo(item):
            path, ver = item
            if ver != "2":
                return None
            try:
                names = field_names(path)
            except (urllib.error.URLError, OSError, ValueError):
                return None
            if names and any(is_desc_field(n) for n in names) and not meta_desc(path):
                return path
            return None

        pending = sorted(p for p in ex.map(todo, paths) if p)
    done = 0
    for path in pending:
        if not args.uygula:
            print(f"tasinacak: {path}")
            continue
        try:
            desc = _desc_of(read_record(path))
            if desc and set_meta_desc(path, desc):
                done += 1
                print(f"tasindi: {path}")
        except KasaError as e:
            print(f"atlandi: {path} ({e})", file=sys.stderr)
    print(f"-- {len(pending)} record(s) without a metadata description" + (f", {done} written" if args.uygula else "; run with --uygula to write"))
    return 0


def cmd_yaz(args):
    path, field = parse_ref(args.ref)
    dosya = getattr(args, "dosyadan", None)
    use_pano = args.pano or (not args.stdin and not dosya and os.name == "nt")
    if not use_pano and not args.stdin and not dosya:
        raise KasaError("choose --stdin or --dosyadan PATH (clipboard default is Windows only)")
    if dosya:
        try:
            with open(dosya, "rb") as fh:
                raw = fh.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise KasaError(f"cannot read file '{dosya}' ({type(e).__name__})")
    else:
        raw = clipboard_read() if use_pano else sys.stdin.read()
    value = clean_secret(raw, multiline=not use_pano, not_secret=args.gizli_degil,
                         short_secret=args.kisa)
    data, ver = _read_v2(path)
    data = dict(data)
    data[field] = value
    newver = write_v2(path, data, ver)
    if use_pano and not args.pano_koru and not clipboard_clear():
        print("kasa: warning: clipboard could not be cleared", file=sys.stderr)
    sha = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    if len(value) < MIN_WRITE_LEN:  # a hash prefix of a short secret (6-digit code) is trivially brute-forced
        sha = "gizli (kisa deger)"
    print(f"yazildi: {path}#{field} ({len(value)} karakter, sha256 {sha}, surum {newver})")
    if args.gizli_degil:
        _declare_not_secret(path, field)
    if is_desc_field(field):
        _mirror_desc(path, value)
    return 0


def _declare_not_secret(path, field):
    try:
        set_not_secret(path, field)
    except KasaError as e:
        print(f"kasa: warning: field not marked non-secret ({e}); run will keep masking it", file=sys.stderr)


def cmd_gizli_degil(args):
    """Declare an existing field non-secret (or withdraw it with --geri-al). Reads field names only, never values."""
    path, field = parse_ref(args.ref)
    try:
        names = field_names(path)
    except urllib.error.HTTPError as e:
        raise KasaError(f"Vault HTTP {e.code} reading field names of {path}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise KasaError(f"Vault unreachable for {path} ({type(e).__name__})")
    if names is None or field not in names:
        raise KasaError(f"field not found: {path}#{field}")
    changed = set_not_secret(path, field, declared=not args.geri_al)
    print(f"{'secret olarak geri alindi' if args.geri_al else 'gizli degil olarak isaretlendi'}: {path}#{field}"
          + ("" if changed else " (zaten oyleydi)"))
    return 0


def _mirror_desc(path, desc):
    """Keep custom_metadata[DESC_META] equal to the record's description field, so bul shows it without reading values."""
    try:
        set_meta_desc(path, desc)
    except KasaError as e:
        print(f"kasa: warning: description not mirrored to metadata ({e}); bul will not show it", file=sys.stderr)


def cmd_sil(args):
    path, field = parse_ref(args.ref)
    data, ver = _read_v2(path)
    if field not in data:
        raise KasaError(f"field not found: {path}#{field}")
    rest = {k: v for k, v in data.items() if k != field}
    newver = write_v2(path, rest, ver)
    print(f"silindi: {path}#{field} (surum {newver})")
    if is_desc_field(field):
        _mirror_desc(path, _desc_of(rest))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0].lower() in REFUSED:
        print("kasa: refused; there is no subcommand that prints secret values (use run/check/fields)", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="kasa.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a command with secrets in its environment, output redacted")
    r.add_argument("--env", action="append", metavar="[NAME=]mount/path#field")
    r.add_argument("--file", action="append", metavar="[NAME=]mount/path#field")
    r.add_argument("command", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)
    bl = sub.add_parser("bul", help="find credentials by keywords in path / field names / description (never values)")
    bl.add_argument("words", nargs="+", metavar="keyword")
    bl.add_argument("--yenile", action="store_true", help="rebuild the local index now (default: when older than 24 h)")
    bl.add_argument("--hepsi", action="store_true", help="show every match (default: first %d)" % BUL_DEFAULT_LIMIT)
    bl.add_argument("--limit", type=int, default=BUL_DEFAULT_LIMIT, metavar="N")
    bl.set_defaults(fn=cmd_bul)
    c = sub.add_parser("check", help="print only var (N karakter) / yok per field")
    c.add_argument("refs", nargs="+", metavar="mount/path#field")
    c.set_defaults(fn=cmd_check)
    f = sub.add_parser("fields", help="list field names of a record")
    f.add_argument("path", metavar="mount/path")
    f.set_defaults(fn=cmd_fields)
    y = sub.add_parser("yaz", help="write a secret field from clipboard/stdin/file (never argv), other fields kept")
    y.add_argument("ref", metavar="mount/path#field")
    g = y.add_mutually_exclusive_group()
    g.add_argument("--pano", action="store_true", help="read from the Windows clipboard (default on Windows)")
    g.add_argument("--stdin", action="store_true", help="read from stdin (multi-line allowed)")
    g.add_argument("--dosyadan", metavar="PATH", help="read from a file (multi-line allowed, e.g. PEM); never pass the value itself")
    y.add_argument("--pano-koru", action="store_true", help="keep the clipboard content after writing")
    y.add_argument("--gizli-degil", action="store_true",
                   help="the field is not a secret (port, host, user name): allow values shorter than %d characters and "
                        "do not redact it in run (same as the gizli-degil subcommand)" % MIN_WRITE_LEN)
    y.add_argument("--kisa", action="store_true",
                   help="a secret that is short by design (PIN, fixed OTP): allow %d-%d characters; run still redacts it"
                        % (MIN_SECRET_LEN, MIN_WRITE_LEN - 1))
    y.set_defaults(fn=cmd_yaz)
    gd = sub.add_parser("gizli-degil", help="mark an existing field as non-secret: run passes it on but no longer redacts it")
    gd.add_argument("ref", metavar="mount/path#field")
    gd.add_argument("--geri-al", action="store_true", help="withdraw the mark (the field is redacted again)")
    gd.set_defaults(fn=cmd_gizli_degil)
    d = sub.add_parser("sil", help="remove one field, other fields kept")
    d.add_argument("ref", metavar="mount/path#field")
    d.set_defaults(fn=cmd_sil)
    at = sub.add_parser("aciklama-tasi", help="one-time: copy description fields into custom_metadata so bul never reads values")
    at.add_argument("--uygula", action="store_true", help="write (default: only list the records that would be migrated)")
    at.set_defaults(fn=cmd_aciklama_tasi)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KasaError as e:
        print(f"kasa: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
