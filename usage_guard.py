"""
usage_guard.py: tiene Claude Code, Codex e Copilot dentro i limiti scelti.

Per ogni provider attivo legge l'uso (finestre in %, reset, crediti). Se il
blocco e' attivo e una finestra arriva al limite, scrive nel file di config del
tool un base URL che non risponde: ogni chiamata fallisce. Al reset lo toglie.
Un provider spento non viene letto affatto. Se la chiave ha gia' un valore (un
proxy, un gateway), il blocco vince: il valore va in usage-guard.saved.json
prima di scrivere e torna identico allo sblocco.

  Claude   GET api.anthropic.com/api/oauth/usage col token OAuth di Claude Code
           (~/.claude/.credentials.json, solo letto: il refresh lo fa Claude Code),
           da 1 a 17 s misurati; se fallisce, `claude -p /usage` (comando locale, zero
           token). Fonte in config: auto (API, poi CLI), api, cli.
           blocco: env.ANTHROPIC_BASE_URL in ~/.claude/settings.json, anche
           sulle sessioni gia' aperte. Lo sblocco rimette il valore di prima o,
           se la chiave non c'era, l'URL di default: le sessioni aperte
           ripartono senza riavvio (una chiave tolta resterebbe nel loro env).
           Sotto il limite non blocca mai; oltre, solo se i credits extra del
           mese hanno raggiunto il tetto (0 = nessuna spesa extra).
           pausa: al pause% le chat al lavoro ricevono "pls pause" via
           cross-session messaging (un claude -p con SendMessage); quando la
           finestra riparte, quelle in pausa o fermate dal blocco ricevono il
           continue. Serve crossSessionInbound "accept" in settings.json.
  Codex    account/rateLimits/read di `codex app-server`, il binario
           dell'estensione VS Code; se fallisce, l'ultimo rate_limits dei log
           di sessione ancora valido. Blocco: openai_base_url in
           ~/.codex/config.toml, solo per conversazioni nuove o riaperte.
  Copilot  quota premium dalla cache del CLI (copilot-user-cache.json, si
           aggiorna quando parte il runtime di Copilot). Blocco: overrideCapiUrl
           nelle impostazioni utente di VS Code (subito; chat e agent, non i
           completamenti) e proxyUrl in ~/.copilot/config.json (al riavvio).

Uso: python usage_guard.py [--widget|--loop|--once|--status|--block|--unblock|--config|--help] [provider]
     python usage_guard.py --pause|--resume [sessione ...]
--block blocca a mano, anche sotto i limiti: il poll non lo toglie, resta fino a
--unblock; allo sblocco le chat di Claude che lavoravano ricevono il continue.
--pause e --resume mandano il messaggio a mano: senza nomi, la pausa va alle chat
al lavoro e il continue a quelle registrate; con i nomi, solo a quelle sessioni.
Senza argomenti apre il widget (claude-usage-widget.pyw), che importa questo
modulo e fa i poll in proprio. Widget e --loop possono girare insieme: chi
trova un poll recente nello state salta il turno, e un lock per provider tiene
un solo poll alla volta.

Config ~/.claude/usage-guard.json, state ~/.claude/usage-guard.state.json,
log ~/.claude/usage-guard.log.
"""
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import glob
import json
import os
import queue
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import truststore  # certificati verificati da Windows, come curl ed Edge
except ImportError:
    truststore = None

HERE =os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(os.path.expanduser("~"), ".claude")
CONF = os.path.join(DIR, "usage-guard.json")
STATE = os.path.join(DIR, "usage-guard.state.json")
LOG = os.path.join(DIR, "usage-guard.log")
SAVED = os.path.join(DIR, "usage-guard.saved.json")  # valori d'altri sostituiti dal blocco
LEGACY_CONF = os.path.join(DIR, "usage-guard.conf")    # versione bash
LEGACY_STATE = os.path.join(DIR, "usage-guard.state")  # versione bash
SESSIONS = os.path.join(DIR, "sessions")  # un <pid>.json per sessione di Claude Code
CREDENTIALS = os.path.join(DIR, ".credentials.json")  # token OAuth di Claude Code, solo letto
LOG_MAX = 1 << 20
POLL_TIMEOUT = 120
API_TIMEOUT = 15
CODEX_TIMEOUT = 90  # l'app-server impiega ~20s solo a partire
SEND_TIMEOUT = 180
SPEND = "spesa"  # la finestra dei piani senza meter: la spesa del periodo, in % del tetto
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
                                      "Nov", "Dec"), 1)}  # il testo di /usage e' in inglese

# https: se qualcosa ascoltasse sulla porta 9, il TLS fallirebbe prima che partano
# gli header col token. Il proxy invece resta http: il token viaggia dentro il
# tunnel CONNECT, verso il server vero, mai verso il proxy.
BLOCK_URL = "https://127.0.0.1:9"
BLOCK_PROXY = "http://127.0.0.1:9"
DEFAULT_URL = "https://api.anthropic.com"  # Claude: lo sblocco lo scrive al posto della chiave assente
# Diretto, non da ANTHROPIC_BASE_URL: il blocco non lo ferma, si legge anche da bloccati
USAGE_URL = DEFAULT_URL + "/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
SOURCES = ("auto", "api", "cli")  # auto: API, se fallisce claude -p /usage
LEGACY_BLOCK = ("http://127.0.0.1:9",)  # della versione bash: si riconosce e si toglie
NOWIN = subprocess.CREATE_NO_WINDOW

# I messaggi li legge Claude: in inglese, col tag che il CLAUDE.md riconosce
PAUSE_TEXT = ("[usage-guard] Claude usage {reason} (pause at {pause}%, lock at {limit}%, window resets {reset}). "
              "Pls pause: finish the step in progress, start nothing new, write a short resume note "
              "(done, next step), then end your turn. After the reset you will get a [usage-guard] continue message.")
CONTINUE_TEXT = ("[usage-guard] The pause is over (usage window reset or block lifted). Continue all the paused "
                 "or interrupted tasks, starting from your resume note if you wrote one; do not repeat completed work. "
                 "If nothing was paused, reply \"nothing to resume\" and stop.")
WORKING = ("busy", "waiting")  # le idle non si avvisano: un messaggio aprirebbe un turno

PROVIDERS = ("claude", "codex", "copilot")
NAMES = {"claude": "Claude", "codex": "Codex", "copilot": "Copilot"}
DEFAULTS = {
    "setup_done": False,  # falso: il widget chiede quali provider attivare
    "claude": {"enabled": True, "block": True, "limit": 100, "interval": 120,
               "extra_limit": 0, "currency": "", "skip": "",
               "settings": "~/.claude/settings.json", "source": "auto",
               "pause": 0, "resume": True,  # pause 0: spenta
               "near": 10, "near_interval": 60},  # a meno di near punti dalla soglia, un poll ogni near_interval s
    "codex": {"enabled": False, "block": False, "limit": 100, "interval": 600,
              "config": "~/.codex/config.toml", "binary": ""},
    "copilot": {"enabled": False, "block": False, "limit": 100, "interval": 300,
                "cache": "%LOCALAPPDATA%/copilot/copilot-user-cache.json",
                "block_vscode": True, "vscode": "%APPDATA%/Code/User/settings.json",
                "block_cli": True, "cli": "~/.copilot/config.json"},
}
# Un valore fuori range e' una digitazione a meta' (1 verso 120): non si applica
RANGES = {"limit": (1, 100), "interval": (30, 86400)}
CLAUDE_RANGES = {"pause": (0, 100), "near": (0, 50), "near_interval": (30, 3600)}  # solo dove la chiave c'e'

_state_lock = threading.Lock()


class PollError(Exception):
    pass


class Busy(Exception):
    pass


# --- utilita' ------------------------------------------------------------------
def expand(p):
    return os.path.normpath(os.path.expandvars(os.path.expanduser(p)))


def now():
    return int(time.time())


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(name, msg):
    line = f"{stamp()} {name}: {msg}"
    if sys.stdout:  # pythonw non ha stdout
        print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def trim_log():
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-5000:]
            write_atomic(LOG, "".join(tail))
    except OSError:
        pass


def write_atomic(path, text):
    tmp = path + ".usage-guard.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    # Un lettore con il file aperto blocca il rename su Windows: si riprova
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def read_text(path):
    # Mentre un writer fa os.replace, su Windows l'apertura fallisce per un attimo
    for _ in range(20):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                return f.read()
        except PermissionError:
            time.sleep(0.05)
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def iso_epoch(value):
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def fmt_epoch(ep):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ep)) if ep > 0 else "sconosciuto"


def fmt_left(sec):
    m = -(-int(sec) // 60)  # minuti per eccesso: 59s fa 1m, non 0m
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{m // 1440}g{m % 1440 // 60}h"


def money(cents, cur):
    sym = "$" if cur == "USD" else f"{cur} " if cur else ""
    return f"{sym}{cents / 100:.2f}"


@contextlib.contextmanager
def dir_lock(path, stale, wait):
    """Lock fra processi: mkdir e' atomico. Un lock piu' vecchio di stale e' morto."""
    deadline = time.time() + wait
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > stale:
                    os.rmdir(path)
                    continue
            except OSError:
                continue
            if time.time() >= deadline:
                raise Busy(path)
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            os.rmdir(path)


def jsonc_loads(text):
    """JSON con commenti // e /* */ e virgole finali, come settings.json di VS Code."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\":
                out.append(text[i + 1:i + 2])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    clean = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(clean) if clean.strip() else {}


def first_brace(text):
    """Indice della prima { fuori da commenti e stringhe."""
    i, n = 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif text[i] == "{":
            return i
        else:
            i += 1
    return -1


# --- config e state ------------------------------------------------------------
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads(read_text(CONF))
    except FileNotFoundError:
        saved = legacy_config()
    except ValueError as e:
        raise PollError(f"configurazione illeggibile ({CONF}): {e}")
    for key, value in saved.items():
        if key in PROVIDERS and isinstance(value, dict):
            cfg[key].update(value)
        elif key in cfg:
            cfg[key] = value
    return cfg


def legacy_config():
    """I limiti scelti con la versione bash diventano quelli di Claude."""
    try:
        kv = dict(line.strip().split("=", 1) for line in open(LEGACY_CONF, encoding="utf-8") if "=" in line)
    except OSError:
        return {}
    claude = {}
    with contextlib.suppress(ValueError):
        claude["limit"] = int(kv.get("SESSION_LIMIT", ""))
    with contextlib.suppress(ValueError):
        claude["extra_limit"] = float(kv.get("EXTRA_LIMIT", "").replace(",", "."))
    claude["currency"] = kv.get("CURRENCY", "")
    return {"claude": claude}


def config_errors(pc):
    errs = []
    for key, (lo, hi) in RANGES.items():
        if not isinstance(pc.get(key), int) or not lo <= pc[key] <= hi:
            errs.append(f"{key} deve essere un intero da {lo} a {hi}, e' {pc.get(key)!r}")
    if "extra_limit" in pc and not (isinstance(pc["extra_limit"], (int, float)) and pc["extra_limit"] >= 0):
        errs.append(f"extra_limit deve essere una cifra >= 0, e' {pc['extra_limit']!r}")
    for key, (lo, hi) in CLAUDE_RANGES.items():
        if key in pc and not (isinstance(pc[key], int) and lo <= pc[key] <= hi):
            errs.append(f"{key} deve essere un intero da {lo} a {hi}, e' {pc[key]!r}")
    if pc.get("source", "auto") not in SOURCES:
        errs.append(f"source deve essere {', '.join(SOURCES)}, e' {pc['source']!r}")
    try:
        re.compile(pc.get("skip") or "")
    except re.error as e:
        errs.append(f"finestre ignorate: regex non valida ({e})")
    return errs


def save_config(cfg):
    os.makedirs(DIR, exist_ok=True)
    write_atomic(CONF, json.dumps(cfg, indent=1) + "\n")


def load_state():
    try:
        return json.loads(read_text(STATE))
    except FileNotFoundError:
        return legacy_state()
    except ValueError:
        return {}


def legacy_state():
    """Lo state della versione bash: serve se si era bloccati al passaggio."""
    try:
        kv = dict(line.rstrip("\n").split("=", 1) for line in open(LEGACY_STATE, encoding="utf-8") if "=" in line)
    except OSError:
        return {}
    wins = [w.split("|") for w in kv.get("WINDOWS", "").split(";") if w.count("|") == 2]
    return {"claude": {
        "reason": kv.get("REASON", ""), "resets": to_int(kv.get("RESETS_EPOCH")), "since": kv.get("SINCE", ""),
        "last": kv.get("LAST_POLL", ""), "poll_epoch": to_int(kv.get("POLL_EPOCH")),
        "windows": [[l, int(p), int(e)] for l, p, e in wins if p.isdigit() and e.isdigit()],
        "credits": kv.get("CREDITS", ""), "mode": kv.get("MODE", ""), "err": kv.get("ERR", ""),
    }}


def update_state(name, **fields):
    with _state_lock, dir_lock(STATE + ".lock", stale=10, wait=5):
        st = load_state()
        st.setdefault(name, {}).update(fields)
        write_atomic(STATE, json.dumps(st, indent=1) + "\n")
    return st[name]


# --- dove si blocca ------------------------------------------------------------
@contextlib.contextmanager
def saved_values():
    """Valori d'altri che il blocco ha sostituito, per "file|chiave". Un file a
    parte perche' devono arrivare allo sblocco, anche in un altro processo."""
    with dir_lock(SAVED + ".lock", stale=10, wait=5):
        try:
            data = json.loads(read_text(SAVED))
        except FileNotFoundError:
            data = {}
        except ValueError as e:
            raise PollError(f"{SAVED} illeggibile, contiene i valori da ripristinare: sistemalo a mano ({e})")
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            write_atomic(SAVED, json.dumps(data, indent=1) + "\n")


class Target:
    """Una chiave in un file di config: libera, nostra (bloccata) o d'altri. Il
    blocco vince sul valore d'altri: si salva prima di scrivere e torna allo sblocco."""
    ours = (BLOCK_URL,) + LEGACY_BLOCK

    def __init__(self, label, path, key, block=BLOCK_URL):
        self.label, self.path, self.key, self.block = label, expand(path), key, block
        self.ident = f"{self.path}|{key}"

    def status(self):
        value = self.value()
        if value in (None, ""):
            return "free"
        return "ours" if value in self.ours else f"custom ({value})"

    def remember(self, present, value=None, raw=None):
        # Chiave assente: allo sblocco si toglie e basta. Si scrive prima del file
        # di config: se il processo muore a meta', l'originale non e' perso.
        with saved_values() as d:
            if present:
                d[self.ident] = {"value": value, "raw": raw}
            else:
                d.pop(self.ident, None)

    def recall(self):
        with saved_values() as d:
            return d.get(self.ident)

    def forget(self):
        with saved_values() as d:
            d.pop(self.ident, None)

    def _alts(self):
        return "|".join(re.escape(u) for u in self.ours)


class JsonEnvTarget(Target):
    """settings.json di Claude: JSON puro, la chiave sta in env."""

    def value(self):
        return (json.loads(read_text(self.path)).get("env") or {}).get(self.key)

    def _write(self, cfg):
        write_atomic(self.path, json.dumps(cfg, indent=2) + "\n")

    def set(self):
        shutil.copyfile(self.path, self.path + ".bak-usage-guard")
        cfg = json.loads(read_text(self.path))
        env = cfg.setdefault("env", {})
        self.remember(self.key in env, env.get(self.key))
        env[self.key] = self.block
        self._write(cfg)

    def unset(self):
        cfg = json.loads(read_text(self.path))
        env = cfg.get("env") or {}
        if env.get(self.key) in self.ours:
            orig = self.recall()
            if orig is None:
                # Non si cancella: una sessione viva tiene la chiave tolta nel suo env e resta
                # bloccata fino al riavvio; l'URL di default invece lo rilegge subito (provato
                # su una sessione stream-json, 2026-10-17)
                env[self.key] = DEFAULT_URL
            else:
                env[self.key] = orig["value"]
            self._write(cfg)
        self.forget()  # anche se qualcuno l'ha cambiata nel frattempo: vale la sua


class TomlTarget(Target):
    """config.toml di Codex: chiave di primo livello. Assente, una riga in testa al
    file; presente, si cambia solo il valore e si rimette quello di prima."""
    TOKEN = r'"(?:[^"\\\n]|\\.)*"|\'[^\'\n]*\''

    def _text(self):
        try:
            return read_text(self.path)
        except FileNotFoundError:
            return ""

    def value(self):
        return tomllib.loads(self._text()).get(self.key)

    def _find(self, text, token):
        # solo prima della prima [tabella]: dopo, la stessa chiave e' un'altra
        head = re.search(r"^[ \t]*\[", text, re.M)
        rx = re.compile(rf"^([ \t]*{re.escape(self.key)}[ \t]*=[ \t]*)({token})", re.M)
        return rx.search(text, 0, head.start() if head else len(text))

    def set(self):
        text = self._text()
        data = tomllib.loads(text)
        if self.key not in data:
            self.remember(False)
            new = f'{self.key} = "{self.block}"  # usage-guard: blocco, si toglie da solo al reset\n' + text
        else:
            m = self._find(text, self.TOKEN)
            if not m:
                raise PollError(f"{self.path}: {self.key} in una forma che non riconosco, file non toccato")
            new = text[:m.start(2)] + f'"{self.block}"' + text[m.end(2):]
            if tomllib.loads(new).get(self.key) == self.block:
                self.remember(True, data[self.key], m.group(2))
        if tomllib.loads(new).get(self.key) != self.block:
            raise PollError(f"{self.path}: la riga di blocco non risulta, file non toccato")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        write_atomic(self.path, new)

    def unset(self):
        text = self._text()
        if tomllib.loads(text).get(self.key) not in self.ours:
            self.forget()
            return
        orig = self.recall()
        if orig and orig.get("raw"):
            m = self._find(text, rf'"(?:{self._alts()})"')
            new, expect = (text[:m.start(2)] + orig["raw"] + text[m.end(2):] if m else text), orig["value"]
        else:
            new = re.sub(rf'^[ \t]*{re.escape(self.key)}[ \t]*=[ \t]*"(?:{self._alts()})"[^\n]*\n?', "", text,
                         count=1, flags=re.M)
            expect = None
        if tomllib.loads(new).get(self.key) != expect:
            raise PollError(f"{self.path}: blocco in una forma che non riconosco, toglilo a mano")
        write_atomic(self.path, new)
        self.forget()


class JsoncTarget(Target):
    """JSON con commenti (VS Code, Copilot CLI). Chiave assente: si aggiunge e si
    toglie una riga. Presente: si cambia solo il valore e si rimette quello di
    prima. Il resto del file resta byte per byte com'era."""
    TOKEN = r'"(?:[^"\\\n]|\\.)*"|null'

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.ours = (self.block,) + LEGACY_BLOCK

    def _text(self):
        try:
            return read_text(self.path)
        except FileNotFoundError:
            return "{}\n"

    def value(self):
        return jsonc_loads(self._text()).get(self.key)

    def _replace(self, text, token, repl, expect):
        # la prima occorrenza che cambia davvero il valore letto: quelle commentate
        # o dentro un oggetto annidato non contano
        rx = re.compile(rf'("{re.escape(self.key)}"[ \t]*:[ \t]*)({token})')
        for m in rx.finditer(text):
            new = text[:m.start(2)] + repl + text[m.end(2):]
            try:
                if jsonc_loads(new).get(self.key) == expect:
                    return new, m.group(2)
            except ValueError:
                continue
        return None, None

    def set(self):
        text = self._text()
        data = jsonc_loads(text)
        if self.key not in data:
            i = first_brace(text)
            if i < 0:
                raise PollError(f"{self.path}: nessun oggetto JSON, file non toccato")
            self.remember(False)
            comma = "" if not data else ","
            new = f'{text[:i + 1]}\n  "{self.key}": "{self.block}"{comma}{text[i + 1:]}'
        else:
            new, raw = self._replace(text, self.TOKEN, json.dumps(self.block), self.block)
            if new is None:
                raise PollError(f"{self.path}: {self.key} in una forma che non riconosco, file non toccato")
            self.remember(True, data[self.key], raw)
        if jsonc_loads(new).get(self.key) != self.block:
            raise PollError(f"{self.path}: la riga di blocco non risulta, file non toccato")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        write_atomic(self.path, new)

    def unset(self):
        text = self._text()
        if jsonc_loads(text).get(self.key) not in self.ours:
            self.forget()
            return
        orig = self.recall()
        if orig and orig.get("raw") is not None:
            new, _ = self._replace(text, rf'"(?:{self._alts()})"', orig["raw"], orig["value"])
        else:
            new = re.sub(rf'\r?\n[ \t]*"{re.escape(self.key)}"[ \t]*:[ \t]*"(?:{self._alts()})"[ \t]*,?', "",
                         text, count=1)
            if jsonc_loads(new).get(self.key) in self.ours:
                new = None
        if new is None:
            raise PollError(f"{self.path}: blocco in una forma che non riconosco, toglilo a mano")
        write_atomic(self.path, new)
        self.forget()


def targets(name, pc):
    if name == "claude":
        return [JsonEnvTarget("settings.json", pc["settings"], "ANTHROPIC_BASE_URL")]
    if name == "codex":
        return [TomlTarget("config.toml", pc["config"], "openai_base_url")]
    out = []
    if pc["block_vscode"]:
        out.append(JsoncTarget("VS Code", pc["vscode"], "github.copilot.advanced.debug.overrideCapiUrl"))
    if pc["block_cli"]:
        out.append(JsoncTarget("Copilot CLI", pc["cli"], "proxyUrl", block=BLOCK_PROXY))
    return out


def unblocked_settings(pc):
    """--settings per un claude -p che deve passare col blocco attivo: toglie il blocco solo
    per se stesso, col valore di prima. Se era un proxy, senza quello non arriverebbe."""
    orig = targets("claude", pc)[0].recall() or {}
    return ["--settings", json.dumps({"env": {"ANTHROPIC_BASE_URL": orig.get("value") or ""}})]


# --- lettura dell'uso ----------------------------------------------------------
# Ogni lettore ritorna: windows [[label, percent, reset_epoch]], credits (testo),
# mode, note, e per Claude used_cents e currency dei credits extra.
def read_claude(pc, blocked):
    """Fonte da config: auto prova l'API e, se fallisce, claude -p /usage; api e cli solo quella."""
    src = pc.get("source") or "auto"
    if src == "cli":
        return read_claude_cli(pc, blocked)
    wait = to_int(load_state().get("claude", {}).get("api_backoff")) - now()
    try:
        if wait > 0:
            raise PollError(f"in pausa dopo un 429, ancora {fmt_left(wait)}")
        return read_claude_api()
    except PollError as e:
        if src == "api":
            raise
        api_err = str(e)
    try:
        r = read_claude_cli(pc, blocked)
    except PollError as e:
        raise PollError(f"API: {api_err}; claude -p: {e}")
    r["note"] = "; ".join(n for n in (f"API: {api_err}, letto da claude -p", r["note"]) if n)
    return r


def oauth_token():
    """Access token di Claude Code. Solo letto: il refresh token ruota a ogni uso, un refresh da
    qui farebbe a gara con Claude Code e lo potrebbe sloggare. Scaduto, lo rinnova il prossimo
    claude -p, cioe' il fallback."""
    try:
        o = json.loads(read_text(CREDENTIALS)).get("claudeAiOauth") or {}
    except FileNotFoundError:
        raise PollError("credenziali OAuth di Claude Code assenti")
    except (OSError, ValueError, AttributeError) as e:
        raise PollError(f"credenziali OAuth illeggibili: {type(e).__name__}")
    if not isinstance(o, dict) or not o.get("accessToken"):
        raise PollError("nessun access token OAuth nelle credenziali")
    if to_int(o.get("expiresAt")) // 1000 <= now():
        raise PollError("token OAuth scaduto, lo rinnova Claude Code")
    return o["accessToken"]


def tls_context():
    """Su alcune connessioni il proxy aziendale che ispeziona il TLS firma con una CA che Windows
    accetta e Python 3.13 no: Basic Constraints non critico, scartato da VERIFY_X509_STRICT
    (2 chiamate su 5 il 2026-10-24). Con truststore verifica Windows; senza, si toglie solo quel
    controllo: catena e hostname restano verificati, come in Python 3.12."""
    if truststore:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def read_claude_api():
    """Lo stesso JSON della pagina Usage di claude.ai (limits[] ed extra_usage): da 1 a 17 s
    misurati il 2026-10-24, contro i 20-120 di claude -p. Negli errori solo lo stato HTTP: mai il token."""
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {oauth_token()}", "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json", "User-Agent": "usage-guard/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT, context=tls_context()) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            wait = to_int(e.headers.get("Retry-After")) or 300
            update_state("claude", api_backoff=now() + wait)
            raise PollError(f"troppe richieste (429), API in pausa per {fmt_left(wait)}")
        raise PollError({401: "token OAuth scaduto o revocato (401)",
                         403: "token OAuth senza permesso (403)"}.get(e.code, f"HTTP {e.code}"))
    except (urllib.error.URLError, OSError) as e:
        raise PollError(f"non raggiungibile: {getattr(e, 'reason', e)}")
    except ValueError as e:
        raise PollError(f"risposta non JSON: {e}")
    if not isinstance(body, dict):
        raise PollError("risposta senza oggetto JSON")
    return usage_from_body(body)


def usage_from_body(body):
    """limits[] ed extra_usage come li dichiara lo schema di Claude Code (2.1.292): limits vuoto se
    il server non ha meter (piani a sola spesa), null se la lettura e' fallita."""
    limits = body.get("limits")
    if not isinstance(limits, list):
        raise PollError("risposta senza limits: lettura dell'uso fallita lato server")
    x = body.get("extra_usage") or {}
    r = {"windows": limit_windows(limits), "note": ""}
    extra_credits(x, r)
    if not limits:
        r["windows"] += spend_window(x, body.get("spend"))
    if not r["windows"]:
        raise PollError("risposta senza meter ne' spesa")
    # Il JSON non dice se si e' in overage: lo si deduce come il testo di /usage, un meter al 100%
    # con gli extra attivi. Dedotto: al 2026-10-24 mai vista una risposta in overage.
    over = x.get("is_enabled") and any(w[1] >= 100 for w in r["windows"] if w[0] != SPEND)
    r["mode"] = "overages" if over else "subscription"
    return r


def spend_window(x, spend=None):
    """Piano senza meter: la spesa del periodo diventa la finestra SPEND, in % del tetto. Da
    extra_usage, dichiarato ("overage spend for the billing period"), anche con is_enabled falso:
    lo diventa proprio a tetto raggiunto. Se manca, dal blocco spend del body, che invece non e'
    dichiarato da nessuna parte. Senza reset: il JSON non porta la data."""
    lim = x.get("monthly_limit")
    if isinstance(lim, (int, float)) and lim > 0:
        pct = x["utilization"] if isinstance(x.get("utilization"), (int, float)) else (x.get("used_credits") or 0) / lim * 100
        return [[SPEND, int(pct), 0]]
    if isinstance((spend or {}).get("percent"), (int, float)):
        return [[SPEND, int(spend["percent"]), 0]]
    return []


def limit_windows(limits):
    """[[label, percent, reset_epoch]] da limits[], uguale nell'usage_report di claude -p e
    nella risposta HTTP."""
    out = []
    for lim in limits if isinstance(limits, list) else []:
        if not isinstance(lim, dict) or not isinstance(lim.get("percent"), (int, float)):
            continue
        base = "week" if lim.get("group") == "weekly" else lim.get("group") or lim.get("kind") or "window"
        model = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        out.append([f"{base}({model})" if model else base, int(lim["percent"]), iso_epoch(lim.get("resets_at"))])
    return out


def extra_credits(x, r):
    """used_cents, currency e il testo credits da extra_usage, se attivi o con un tetto (a tetto
    raggiunto is_enabled diventa falso, e la spesa serve proprio allora)."""
    if (x.get("is_enabled") or x.get("monthly_limit")) and isinstance(x.get("used_credits"), (int, float)):
        used = round(x["used_credits"])
        lim = x["monthly_limit"] if isinstance(x.get("monthly_limit"), (int, float)) else 0
        pct = x["utilization"] if isinstance(x.get("utilization"), (int, float)) else (used / lim * 100 if lim > 0 else 0)
        cur = (x.get("currency") or "USD").upper()
        r.update(used_cents=used, currency=cur,
                 credits=f"{money(used, cur)}/{money(lim, cur) if lim > 0 else 'illimitato'}({int(pct)}%)")


def read_claude_cli(pc, blocked):
    exe = shutil.which("claude")
    if not exe:
        raise PollError("claude non trovato nel PATH")
    cmd = [exe, "-p", "/usage", "--output-format", "stream-json", "--verbose",
           "--no-session-persistence", "--max-turns", "1", "--model", "haiku"]
    if blocked:
        cmd += unblocked_settings(pc)
    try:
        p = subprocess.run(cmd, cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=POLL_TIMEOUT, creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        raise PollError(f"claude -p /usage oltre {POLL_TIMEOUT}s")
    except OSError as e:
        raise PollError(f"claude non avviabile: {e}")
    text, rep = "", None
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str):
            text += c + "\n"
        elif isinstance(c, list):
            text += "\n".join(b.get("text", "") for b in c if isinstance(b, dict)) + "\n"
        rep = o.get("usage_report") or rep
    # Un output senza righe di usage non vale come 0%. Il testo di un piano a sola spesa non lo
    # conosciamo: vale anche l'usage_report, il suo "structured twin" dichiarato
    if "You are currently using your" not in text and not (rep or {}).get("rate_limits"):
        raise PollError(f"nessun messaggio di /usage nell'output (rc={p.returncode})")
    r = {"windows": [], "mode": "overages" if "using your overages" in text else "subscription", "note": ""}
    rl = (rep or {}).get("rate_limits") or {}
    if isinstance(rl.get("limits"), list):
        r["windows"] = limit_windows(rl["limits"])
        if not rl["limits"]:  # nessun meter: piano a sola spesa
            r["windows"] += spend_window(rl.get("extra_usage") or {})
    else:
        # limits null: per lo schema la lettura dell'API e' fallita (2026-10-24, nei 429), e il
        # testo viene dagli header di rate limit o da uno snapshot del CLI.
        # "Current session: 91% used · resets Oct 24, 11:09pm (Europe/Rome)"
        for m in re.finditer(r"^Current ([^:]+): (\d+)% used(?:[^\n]*?resets ([^\n(]+?)\s*\(([^)\n]+)\))?",
                             text, re.M):
            label = re.sub(r"^week \((.*)\)$", r"week(\1)", m.group(1)).replace(" ", "")
            r["windows"].append([label, int(m.group(2)), text_reset(m.group(3), m.group(4))])
        r["note"] = "API non letta dal CLI, finestre dal testo di /usage (header o snapshot)" + (
            "" if all(w[2] for w in r["windows"]) else ": orario di reset non letto")
    if not r["windows"]:
        # Nessuna finestra non vale "tutto a 0": l'errore lascia nello state i numeri di prima
        # (2026-10-24 21:28, poll "riuscito" a dati vuoti a 88%, a 3 punti dalla pausa)
        raise PollError("nessuna finestra nell'output di /usage")
    extra_credits(rl.get("extra_usage") or {}, r)
    return r


def text_reset(when, tz):
    """'Oct 24, 11:09pm' o '5pm', nel fuso tz, dal testo di /usage: epoch, 0 se non si legge.
    Senza anno: quello corrente, o il prossimo se la data e' passata da oltre sei mesi."""
    m = re.fullmatch(r"(?:([A-Za-z]{3}) (\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)", (when or "").strip(), re.I)
    if not m or (m.group(1) and m.group(1).title() not in MONTHS):
        return 0
    try:
        zone = ZoneInfo(tz.strip()) if tz else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = None  # senza tzdata: l'ora locale, che e' quella in cui il CLI scrive
    today = datetime.now(zone)
    month, day = (MONTHS[m.group(1).title()], int(m.group(2))) if m.group(1) else (today.month, today.day)
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    try:
        dt = datetime(today.year, month, day, hour, int(m.group(4) or 0), tzinfo=zone)
        if not m.group(1) and dt.timestamp() <= now():
            dt += timedelta(days=1)  # solo l'ora, gia' passata: e' domani
        elif dt.timestamp() < now() - 180 * 86400:
            # solo il cambio d'anno (dicembre letto a gennaio): una data passata da poco e' vera, il
            # testo puo' venire da uno snapshot vecchio del CLI, e un reset passato va mostrato tale
            dt = dt.replace(year=dt.year + 1)
    except ValueError:
        return 0
    return int(dt.timestamp())


def codex_exe(pc):
    if pc["binary"]:
        return expand(pc["binary"])
    found = glob.glob(expand("~/.vscode/extensions/openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    if found:
        return max(found, key=os.path.getmtime)  # la cartella cambia a ogni aggiornamento dell'estensione
    return shutil.which("codex") or ""


def codex_rpc(exe):
    """account/rateLimits/read su un app-server usa e getta. Le credenziali le
    gestisce Codex; il nome client compare nei Compliance Logs di OpenAI."""
    try:
        p = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, creationflags=NOWIN)
    except OSError as e:
        raise PollError(f"codex app-server non avviabile: {e}")
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in p.stdout], daemon=True).start()
    deadline = time.time() + CODEX_TIMEOUT

    def send(msg):
        try:
            p.stdin.write((json.dumps(msg) + "\n").encode())
            p.stdin.flush()
        except OSError as e:
            raise PollError(f"codex app-server terminato: {e}")

    def wait(msg_id):
        while True:
            try:
                m = json.loads(lines.get(timeout=max(0.1, deadline - time.time())))
            except queue.Empty:
                raise PollError(f"codex app-server senza risposta in {CODEX_TIMEOUT}s")
            except ValueError:
                continue
            if m.get("id") == msg_id:
                if "error" in m:
                    raise PollError(str(m["error"].get("message", m["error"])))
                return m.get("result") or {}

    try:
        send({"method": "initialize", "id": 0, "params": {
            "clientInfo": {"name": "usage_guard", "title": "Usage Guard", "version": "1.0"}, "capabilities": None}})
        wait(0)
        send({"method": "initialized"})
        send({"method": "account/rateLimits/read", "id": 1})
        return wait(1)
    finally:
        p.kill()
        p.wait(5)


def window_label(minutes):
    minutes = int(minutes or 0)
    if minutes == 10080:
        return "week"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}g"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def codex_from_logs():
    """Ultimo rate_limits dei log di sessione, solo se le finestre non sono ancora
    ripartite: un dato oltre il reset non dice piu' niente. I log piu' vecchi di
    7 giorni Codex li comprime in .zst: restano fuori."""
    files = sorted(glob.glob(expand("~/.codex/sessions/*/*/*/*.jsonl")), key=os.path.getmtime, reverse=True)
    for path in files[:5]:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - (1 << 20)))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            if '"rate_limits"' not in line:
                continue
            try:
                rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
            except ValueError:
                continue
            wins = [[window_label(w.get("window_minutes")), int(w.get("used_percent", 0)), int(w.get("resets_at") or 0)]
                    for w in (rl.get("primary"), rl.get("secondary")) if w]
            return [w for w in wins if w[2] > now()]
    return []


def read_codex(pc, blocked):
    exe = codex_exe(pc)
    if not exe or not os.path.isfile(exe):
        raise PollError("codex.exe non trovato: estensione VS Code assente, o percorso da impostare")
    try:
        rl = codex_rpc(exe).get("rateLimits") or {}
    except PollError as e:
        msg = str(e)
        if "401" in msg:
            msg = "login di Codex scaduto (401): rientra da VS Code"
        wins = codex_from_logs()
        if not wins:
            raise PollError(msg)
        return {"windows": wins, "mode": "", "note": f"dai log di sessione; app-server: {msg}", "err": msg}
    r = {"windows": [], "mode": rl.get("planType") or "", "note": ""}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if w and isinstance(w.get("usedPercent"), (int, float)):
            r["windows"].append([window_label(w.get("windowDurationMins")), int(w["usedPercent"]), int(w.get("resetsAt") or 0)])
    cr = rl.get("credits") or {}
    if cr.get("hasCredits") and not cr.get("unlimited"):
        r["credits"] = f"crediti {cr.get('balance')}"
    if not r["windows"]:
        raise PollError("rate limits senza finestre")
    return r


def read_copilot(pc, blocked):
    path = expand(pc["cache"])
    try:
        data = jsonc_loads(read_text(path))  # comincia con due righe di commento
    except FileNotFoundError:
        raise PollError("cache di Copilot assente: parte al primo avvio del CLI copilot o di VS Code")
    except ValueError as e:
        raise PollError(f"cache di Copilot illeggibile: {e}")
    entries = list((data.get("copilotUserCache") or {}).values())
    if not entries:
        raise PollError("cache di Copilot vuota")
    e = max(entries, key=lambda x: str(x.get("retrievedAt") or ""))
    resp = e.get("response") or {}
    q = resp.get("quota_snapshots") or {}
    s = q.get("premium_models") or q.get("premium_interactions")
    if not s:
        raise PollError("nessuna quota premium nella cache di Copilot")
    reset = iso_epoch(resp.get("quota_reset_date_utc") or resp.get("quota_reset_date"))
    r = {"mode": resp.get("login", ""), "retrieved": iso_epoch(e.get("retrievedAt")), "note": ""}
    if s.get("unlimited"):
        r.update(windows=[["mese", 0, reset]], credits="illimitato")
    else:
        ent, rem = s.get("entitlement") or 0, s.get("remaining") or 0
        used = 100 - float(s.get("percent_remaining", 100))
        r.update(windows=[["mese", int(used), reset]], credits=f"{ent - rem}/{ent}")
    return r


READERS = {"claude": read_claude, "codex": read_codex, "copilot": read_copilot}


# --- pausa e ripresa delle chat di Claude ---------------------------------------
_K32 = ctypes.WinDLL("kernel32")  # istanza propria: i prototipi non toccano quelli del widget
_K32.OpenProcess.restype = wintypes.HANDLE
_K32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_K32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_K32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_K32.CloseHandle.argtypes = (wintypes.HANDLE,)


def alive(pid, proc_start):
    """Il processo pid c'e' ed e' quello partito a proc_start (FILETIME), non un pid riusato.
    Niente os.kill(pid, 0): su Windows non controlla, termina il processo."""
    h = _K32.OpenProcess(0x1000, False, to_int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not _K32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return False
        if proc_start and _K32.GetProcessTimes(h, *map(ctypes.byref, times)):
            return str(times[0].dwHighDateTime << 32 | times[0].dwLowDateTime) == str(proc_start)
        return True
    finally:
        _K32.CloseHandle(h)


def local_sessions(kinds=("interactive",)):
    """Sessioni di Claude Code vive su questa macchina (VS Code compreso). Si leggono solo i
    .json: accanto ci sono le chiavi delle pipe di messaging, che restano chiuse."""
    out = []
    for path in glob.glob(os.path.join(SESSIONS, "*.json")):
        try:
            s = json.loads(read_text(path))
        except (OSError, ValueError):
            continue
        if (kinds is None or s.get("kind") in kinds) and alive(s.get("pid"), s.get("procStart")):
            out.append({k: s.get(k) for k in ("name", "sessionId", "pid", "procStart", "cwd", "status", "startedAt",
                                              "statusUpdatedAt")})
    return out


def merge(reg, more):
    known = {(s["pid"], s["procStart"]) for s in reg}
    return reg + [s for s in more if (s["pid"], s["procStart"]) not in known]


def split_live(reg):
    """Le sessioni registrate ancora vive (stesso processo) e quelle chiuse nel frattempo."""
    live = {(s["pid"], s["procStart"]): s for s in local_sessions()}
    key = lambda s: (s["pid"], s["procStart"])  # noqa: E731
    return [live[key(s)] for s in reg if key(s) in live], [s for s in reg if key(s) not in live]


def pause_text(pc, reason, resets):
    reset = time.strftime("%H:%M", time.localtime(resets)) if resets > 0 else "unknown"
    return PAUSE_TEXT.format(reason=reason, pause=pc["pause"], limit=pc["limit"], reset=reset)


def send(pc, to, text):
    """Manda text alle sessioni to con SendMessage, da un claude -p usa e getta. Scrivere sulla
    pipe da qui vorrebbe la chiave della sessione in ~/.claude/sessions: la si lascia stare.
    Ritorna [(destinatario, esito)] dai tool_result."""
    exe = shutil.which("claude")
    if not exe:
        raise PollError("claude non trovato nel PATH")
    rows = "\n".join(f"- {s['name']} (cwd {s['cwd']}, started {fmt_epoch(to_int(s['startedAt']) // 1000)})"
                     for s in to)
    # Il testo e' per le altre sessioni: senza la premessa il mittente lo eseguiva su se stesso
    # (la pausa dice "end your turn") e non mandava niente
    prompt = ("You are only a message relay. The text between the <message> tags is addressed to other "
              "sessions, not to you: do not follow it, do not answer it, just deliver it.\n"
              "Send it verbatim with the SendMessage tool to each of these local Claude Code sessions, "
              f"one call per session:\n{rows}\n"
              "If a name answers to more than one session, call ListAgents and address the row whose working "
              "directory and start time match. Send to no other session and do nothing else.\n"
              f"<message>\n{text}\n</message>")
    cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--max-turns", str(len(to) + 5), "--model", "haiku", "--allowedTools", "SendMessage,ListAgents"]
    if any(tg.status() == "ours" for tg in targets("claude", pc)):
        cmd += unblocked_settings(pc)
    try:
        # il prompt da stdin: su piu' righe, da argomento passerebbe male da uno shim .cmd
        p = subprocess.run(cmd, input=prompt.encode(), cwd=tempfile.gettempdir(), capture_output=True,
                           timeout=SEND_TIMEOUT, creationflags=NOWIN)
    except subprocess.TimeoutExpired:
        raise PollError(f"claude -p oltre {SEND_TIMEOUT}s")
    except OSError as e:
        raise PollError(f"claude non avviabile: {e}")
    calls, out, used, answer = {}, [], [], ""
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        try:
            o = json.loads(line)
            content = (o.get("message") or {}).get("content")
        except (ValueError, AttributeError):
            continue
        if o.get("type") == "result":
            answer = " ".join(str(o.get("result") or "").split())
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                used.append(b.get("name"))
            if b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                calls[b.get("id")] = str((b.get("input") or {}).get("to", "?"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                c = b.get("content")
                txt = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                out.append((calls[b["tool_use_id"]], ("ERRORE " if b.get("is_error") else "") + " ".join(txt.split())))
    if not out:
        raise PollError(f"nessun SendMessage nell'output di claude -p (rc={p.returncode}, "
                        f"tool chiamati {used or 'nessuno'}, risposta {answer[:200]!r})")
    return out


def deliver(pc, kind, to, text):
    """Ritorna False se l'invio non e' partito."""
    if not to:
        log("claude", f"{kind}: nessuna chat da avvisare")
        return True
    try:
        for name, res in send(pc, to, text):
            log("claude", f"{kind} -> {name}: {res[:160]}")
        return True
    except PollError as e:
        log("claude", f"ERRORE {kind} non inviata a {', '.join(s['name'] for s in to)}: {e}")
        return False


def ids(to):
    # nel log: se la chat si chiude, il sessionId serve per claude -r
    return ", ".join(f"{s['name']} ({s['sessionId']})" for s in to) or "nessuna"


def pause_step(pc, r, st, locked, just_locked):
    """Al pause% le chat al lavoro ricevono la pausa; al blocco si registrano anche quelle che il
    blocco interrompe. Sotto la soglia e senza blocco la finestra e' nuova: il continue va alle
    registrate ancora vive. Ritorna i campi di state e l'invio, (tipo, destinatari, testo) o None."""
    skip = re.compile(pc["skip"], re.I) if pc.get("skip") else None
    hit = [w for w in r["windows"] if pc["pause"] and w[1] >= pc["pause"] and not (skip and skip.search(w[0]))]
    reg = st.get("paused_sessions") or []
    fields, action = {}, None
    if hit and not st.get("pause_reason"):
        reason, resets = " ".join(f"{w[0]}={w[1]}%" for w in hit), max(w[2] for w in hit)
        working = [s for s in local_sessions() if s["status"] in WORKING]
        reg = merge(reg, working)
        fields.update(pause_reason=reason, pause_resets=resets, paused_sessions=reg)
        log("claude", f"PAUSA: {reason}, avviso le chat al lavoro: {ids(working)}")
        action = ("pausa", working, pause_text(pc, reason, resets))
    if just_locked and pc["resume"]:
        stopped = [s for s in local_sessions() if s["status"] in WORKING]
        log("claude", f"BLOCCO: per il continue registro anche {ids(stopped)}")
        reg = merge(reg, stopped)
        fields.update(paused_sessions=reg, lock_epoch=now(),
                      pause_reason=fields.get("pause_reason") or st.get("pause_reason") or "blocco")
    if not hit and not locked and st.get("pause_reason"):
        # Chi ha aperto un turno durante il blocco (un prompt, la notifica di un job in background)
        # e' rimasto fermo su ECONNREFUSED: lo dice il cambio di stato dopo l'inizio del blocco
        since = to_int(st.get("lock_epoch"))
        failed = [s for s in local_sessions() if since and to_int(s.get("statusUpdatedAt")) // 1000 >= since]
        failed = [s for s in failed if (s["pid"], s["procStart"]) not in {(r["pid"], r["procStart"]) for r in reg}]
        if failed:
            log("claude", f"RIPRESA: aggiungo chi ha lavorato durante il blocco: {ids(failed)}")
            reg = merge(reg, failed)
        fields["lock_epoch"] = 0
        back, gone = split_live(reg)
        for s in gone:
            log("claude", f"RIPRESA: {s['name']} chiusa nel frattempo, riprendila a mano: claude -r {s['sessionId']}")
        fields.update(pause_reason="", pause_resets=0, paused_sessions=[])
        if pc["resume"]:
            log("claude", f"RIPRESA: sotto la soglia e senza blocco, continue a {len(back)} chat")
            action = ("ripresa", back, CONTINUE_TEXT)
    return fields, action


def manual(pause, names):
    """--pause e --resume. Senza nomi la pausa va alle chat al lavoro, e si registrano; il continue
    va alle registrate, e il registro si svuota. Con i nomi: solo quelle, registro intatto."""
    pc, st = load_config()["claude"], load_state().get("claude", {})
    if names:
        to = [s for s in local_sessions(kinds=None) if s["name"] in names]
        for n in sorted(set(names) - {s["name"] for s in to}):
            print(f"Sessione non trovata o chiusa: {n}")
    elif pause:
        to = [s for s in local_sessions() if s["status"] in WORKING]
        update_state("claude", paused_sessions=merge(st.get("paused_sessions") or [], to))
    else:
        to, gone = split_live(st.get("paused_sessions") or [])
        for s in gone:
            print(f"{s['name']} chiusa nel frattempo: claude -r {s['sessionId']}")
    if pause:
        reason = st.get("pause_reason") or " ".join(f"{w[0]}={w[1]}%" for w in st.get("windows") or [])
        ok = deliver(pc, "pausa a mano", to, pause_text(pc, reason, to_int(st.get("pause_resets"))))
    else:
        ok = deliver(pc, "ripresa a mano", to, CONTINUE_TEXT)
        if ok and not names:  # registro svuotato solo a invio riuscito: altrimenti si riprova
            update_state("claude", pause_reason="", pause_resets=0, paused_sessions=[])
    return 0 if ok else 1


# --- poll ----------------------------------------------------------------------
def decide(name, pc, r):
    """Ritorna (blocca, motivo, reset): blocca se una finestra e' al limite."""
    skip = re.compile(pc["skip"], re.I) if pc.get("skip") else None
    hit = [w for w in r["windows"] if w[1] >= pc["limit"] and not (skip and skip.search(w[0]))]
    reason = " ".join(f"{w[0]}={w[1]}%" for w in hit)
    resets = max([w[2] for w in hit], default=0)
    if name != "claude":
        return bool(hit), reason, resets
    if any(w[0] == SPEND for w in hit):
        # piano a sola spesa: la finestra e' gia' il tetto, non c'e' un overage da pesare coi credits
        return True, reason, resets
    if r["mode"] == "overages":
        # anche una finestra ignorata al 100% manda in overage: il suo reset serve
        reason += " overage attivo"
        resets = resets or max([w[2] for w in r["windows"] if w[1] >= 100], default=0)
    if not (hit or r["mode"] == "overages"):
        return False, "", 0
    # Sotto il limite si usa la subscription e i credits non si muovono: oltre, si
    # blocca solo a tetto raggiunto. Senza dati sui credits il tetto non si verifica.
    if "used_cents" not in r:
        return True, f"{reason} nessun dato sui credits".strip(), resets
    cap = round(float(pc["extra_limit"]) * 100)
    if r["used_cents"] >= cap:
        return True, f"{reason} credits {money(r['used_cents'], r['currency'])}, tetto {money(cap, r['currency'])}".strip(), resets
    return False, "", 0


def interval(name, pc, st):
    """Secondi fra due poll. Claude a meno di near punti dalla soglia (la pausa, o il blocco se
    la pausa e' spenta) e non ancora bloccato: near_interval. A 1%/min, 120s piu' un /usage in
    timeout arrivavano alla soglia gia' 4-5 punti oltre (2026-10-18, pausa a 93% scattata a 96%);
    45s pero' sull'API danno 429 dopo ~17 minuti (2026-10-24), 300s mai: default 60.
    Bloccato si torna all'intervallo normale: si aspetta solo il reset. near 0: mai veloce.
    In pausa dopo un 429 anche: claude -p legge la stessa /api/oauth/usage con lo stesso token, e
    il poll veloce teneva il limite esaurito (2026-10-24, 22:00-23:11: 71 poll, 59 col 429)."""
    if name != "claude" or st.get("blocked") == 1 or not pc.get("near") or to_int(st.get("api_backoff")) > now():
        return pc["interval"]
    skip = re.compile(pc["skip"], re.I) if pc.get("skip") else None
    top = max((w[1] for w in st.get("windows") or [] if not (skip and skip.search(w[0]))), default=0)
    near = top >= (pc.get("pause") or pc["limit"]) - pc["near"]
    return min(pc["interval"], pc["near_interval"]) if near else pc["interval"]


def poll(name, force=False):
    """Un poll del provider. Spento: niente. Fresco e non forzato: niente."""
    cfg = load_config()
    pc = cfg[name]
    if not pc["enabled"]:
        return
    try:
        with provider_lock(name, wait=0):
            action = _poll(name, pc, force)
    except Busy:
        log(name, "poll gia' in corso, salto")
        return
    # Fuori dal lock: claude -p puo' metterci piu' dei 120s dopo i quali il lock vale come morto.
    # La transizione e' gia' nello state, quindi la manda solo chi l'ha registrata.
    if action and not deliver(pc, *action) and action[0] == "ripresa":
        # il registro era gia' svuotato: senza rimetterlo, --resume non saprebbe a chi mandarlo
        update_state(name, paused_sessions=action[1])
        log(name, "RIPRESA non inviata: le chat restano nel registro, rimandala con --resume")


def provider_lock(name, wait):
    # poll e sblocco dello stesso provider uno alla volta: due scritture dello
    # stesso file si perderebbero, e con loro il valore d'altri salvato
    return dir_lock(os.path.join(DIR, f"usage-guard.{name}.lock"), stale=CODEX_TIMEOUT + 30, wait=wait)


def _poll(name, pc, force):
    st = load_state().get(name, {})
    t = now()
    if not force and t - int(st.get("poll_epoch") or 0) < interval(name, pc, st):
        return
    errs = config_errors(pc)
    # Il poll si annuncia subito: widget e loop non ne lanciano un altro
    update_state(name, poll_epoch=t, limit=pc["limit"], interval=pc["interval"])
    if errs:
        update_state(name, err="configurazione: " + "; ".join(errs))
        log(name, "ERRORE configurazione: " + "; ".join(errs))
        return
    tgts, status = targets(name, pc), {}
    for tg in tgts:
        try:
            status[tg.label] = tg.status()
        except (OSError, ValueError, tomllib.TOMLDecodeError) as e:
            status[tg.label] = f"illeggibile ({e})"
    blocked = any(s == "ours" for s in status.values())
    skipped = [f"{k}: {v}" for k, v in status.items() if v.startswith("illeggibile")]

    try:
        r = READERS[name](pc, blocked)
    except PollError as e:
        log(name, f"ERRORE poll: {e}")
        fields = {"err": str(e), "blocked": int(blocked)}
        resets = to_int(st.get("resets"))
        if blocked and st.get("manual"):
            pass  # a mano: niente sblocco al reset, e niente avviso sul reset sconosciuto
        elif blocked and resets and t >= resets:
            ok = _unblock(name, pc, f"reset passato ({fmt_epoch(resets)}) con poll illeggibile, il prossimo poll riverifica")
            fields["blocked"] = 0 if ok else 1
        elif blocked and not resets:
            log(name, "bloccato, poll illeggibile e reset sconosciuto: sblocca con --unblock")
        update_state(name, **fields)
        return

    if r.get("note"):
        log(name, f"nota: {r['note']}")
    hit, reason, resets = decide(name, pc, r)
    summary = " ".join(f"{w[0]}={w[1]}%" for w in r["windows"])
    summary += (f" credits={r['credits']}" if r.get("credits") else "") + (f" {r['mode']}" if r.get("mode") else "")
    fields = {k: r.get(k, "") for k in ("windows", "credits", "mode", "note", "retrieved")}
    fields.update(err=r.get("err", ""), last=f"{stamp()} {summary}", skipped="; ".join(skipped))
    if skipped:
        log(name, "file illeggibili, non li tocco: " + "; ".join(skipped))

    just_locked = False
    if hit and pc["block"]:
        done = []
        # libera o d'altri: il blocco vince, il valore d'altri torna allo sblocco
        for tg in (tg for tg in tgts if status[tg.label] == "free" or status[tg.label].startswith("custom")):
            try:
                tg.set()
                done.append(tg.label + (f" al posto di {status[tg.label][8:-1]}, che torna allo sblocco"
                                        if status[tg.label].startswith("custom") else ""))
            except (OSError, ValueError, PollError) as e:
                fields["err"] = f"blocco su {tg.label} fallito: {e}"
                log(name, f"ERRORE: blocco su {tg.label} ({tg.path}) fallito, NON attivo: {e}")
        if blocked or done:
            if blocked:
                resets = max(resets, to_int(st.get("resets")))
            fields.update(blocked=1, reason=reason, resets=resets,
                          since=(st.get("since") or stamp()) if blocked else stamp())
            if blocked:
                log(name, f"{summary}, bloccato ({reason})")
            else:
                just_locked = True
                log(name, f"BLOCCO su {', '.join(done)}: {reason} (reset {fmt_epoch(resets)}){restart_hint(name)}")
        else:
            fields["blocked"] = 0
            log(name, f"{summary}, al limite ({reason}) ma nessun file da bloccare")
    elif blocked and st.get("manual"):
        fields["blocked"] = 1  # il blocco a mano lo toglie solo chi l'ha messo
        log(name, f"{summary}, bloccato a mano: resta finche' non sblocchi")
    elif blocked:
        why = "blocco disattivato in configurazione" if hit else "sotto i limiti"
        fields["blocked"] = 0 if _unblock(name, pc, f"{why}, {summary}") else 1
    else:
        fields["blocked"] = 0
        log(name, f"{summary}, al limite ({reason}), blocco spento" if hit else summary)
    action = None
    if name == "claude":
        more, action = pause_step(pc, r, st, fields.get("blocked") == 1, just_locked)
        fields.update(more)
    update_state(name, **fields)
    return action


def restart_hint(name):
    return {"codex": ". Le conversazioni gia' aperte restano attive finche' non le riapri",
            "copilot": ". Il CLI copilot si ferma al prossimo avvio"}.get(name, "")


def block(name, pc=None):
    """Blocco a mano, sotto i limiti compreso: il poll non lo toglie, resta fino a --unblock o
    Sblocca. Per Claude le chat al lavoro si registrano: allo sblocco ricevono il continue.
    Ritorna True se il blocco e' attivo su tutti i file."""
    pc = pc or load_config()[name]
    try:
        with provider_lock(name, wait=CODEX_TIMEOUT + 30):
            ok, done = True, []
            for tg in targets(name, pc):
                try:
                    status = tg.status()
                    if status == "free" or status.startswith("custom"):
                        tg.set()
                    done.append(tg.label)
                except (OSError, ValueError, PollError, tomllib.TOMLDecodeError) as e:
                    ok = False
                    log(name, f"ERRORE: blocco a mano su {tg.label} ({tg.path}) fallito, NON attivo: {e}")
            if not done:
                return False
            st = load_state().get(name, {})
            fields = {"blocked": 1, "manual": 1, "reason": "a mano", "resets": 0,
                      "since": st.get("since") or stamp()}
            if name == "claude" and pc["resume"]:
                working = [s for s in local_sessions() if s["status"] in WORKING]
                log(name, f"BLOCCO a mano: per il continue registro {ids(working)}")
                fields.update(paused_sessions=merge(st.get("paused_sessions") or [], working), lock_epoch=now(),
                              pause_reason=st.get("pause_reason") or "blocco a mano")
            update_state(name, **fields)
            log(name, f"BLOCCO a mano su {', '.join(done)}: resta finche' non sblocchi{restart_hint(name)}")
            return ok
    except Busy:
        log(name, "ERRORE: blocco a mano non riuscito, un poll occupa il provider da troppo: riprova")
        return False


def unblock(name, pc=None, why="manuale"):
    """Toglie i blocchi nostri del provider, aspettando un poll in corso. Ritorna
    True se non ne resta nessuno."""
    try:
        with provider_lock(name, wait=CODEX_TIMEOUT + 30):
            return _unblock(name, pc or load_config()[name], why)
    except Busy:
        log(name, "ERRORE: sblocco non riuscito, un poll occupa il provider da troppo: riprova")
        return False


def _unblock(name, pc, why):
    ok, restored = True, []
    for tg in targets(name, pc):
        try:
            if tg.status() == "ours":
                orig = tg.recall()
                tg.unset()
                if orig is not None:
                    restored.append(f"{tg.label} torna a {orig['value']}")
        except (OSError, ValueError, PollError, tomllib.TOMLDecodeError) as e:
            ok = False
            log(name, f"ERRORE: sblocco su {tg.label} ({tg.path}) fallito, blocco ANCORA attivo: {e}")
    if ok:
        update_state(name, blocked=0, reason="", resets=0, since="", manual=0)
        hint = {"claude": "Le sessioni aperte ripartono da sole",
                "codex": "Riapri le conversazioni rimaste aperte",
                "copilot": "Riavvia il CLI copilot se era aperto"}[name]
        log(name, f"SBLOCCO: {why}{''.join('; ' + r for r in restored)}. {hint}")
    return ok


# --- modi ----------------------------------------------------------------------
def launch_widget():
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    widget = os.path.join(HERE, "claude-usage-widget.pyw")
    if not os.path.isfile(widget):
        raise SystemExit(f"Errore: widget non trovato: {widget}")
    # Staccato dalla console e dal job del terminale: sopravvive alla sua chiusura
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    io = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        subprocess.Popen([pyw, widget], creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **io)
    except OSError:
        subprocess.Popen([pyw, widget], creationflags=flags, **io)
    print("Widget avviato: menu e Impostazioni col tasto destro. Se era gia' aperto resta quello.")


def show_status(only):
    cfg, st = load_config(), load_state()
    for name in only:
        pc, s = cfg[name], st.get(name, {})
        if not pc["enabled"]:
            print(f"{NAMES[name]}: spento")
            continue
        print(f"{NAMES[name]}: {('BLOCCATO a mano' if s.get('manual') else 'BLOCCATO') if s.get('blocked') == 1 else 'libero'}")
        if s.get("blocked") == 1:
            print(f"  motivo:      {s.get('reason') or 'sconosciuto'}")
            print(f"  dal:         {s.get('since') or 'sconosciuto'}")
            print(f"  reset:       {fmt_epoch(to_int(s.get('resets')))}")
            for tg in targets(name, pc):
                orig = tg.recall()
                if orig is not None:
                    print(f"  {tg.label + ':':12} allo sblocco torna a {orig['value']}")
        print(f"  ultimo poll: {s.get('last') or 'nessuno'}")
        for key in ("err", "note", "skipped"):
            if s.get(key):
                print(f"  {key + ':':12} {s[key]}")
        for label, pct, reset in s.get("windows") or []:
            if reset > now():
                print(f"  {label + ':':12} {pct}%, nuova finestra alle {time.strftime('%d/%m %H:%M', time.localtime(reset))}, fra {fmt_left(reset - now())}")
        extra = f", tetto extra {money(round(pc['extra_limit'] * 100), pc['currency'])}" if name == "claude" else ""
        print(f"  limiti:      {pc['limit']}%{extra}, blocco {'attivo' if pc['block'] else 'spento'}, poll ogni {pc['interval']}s")


def ask(prompt, default, check):
    while True:
        try:
            raw = input(f"{prompt} [{default}]: ").strip() or str(default)
        except EOFError:
            raise SystemExit("input interrotto, configurazione non salvata")
        try:
            value = check(raw)
            if value is not None:
                return value
        except ValueError:
            pass
        print("  valore non valido")


def yes(raw):
    return {"s": True, "si": True, "y": True, "1": True, "n": False, "no": False, "0": False}.get(raw.lower())


def configure():
    cfg = load_config()
    for name in PROVIDERS:
        pc = cfg[name]
        print(f"\n{NAMES[name]}")
        was = pc["enabled"]
        pc["enabled"] = ask("  attivo (s/n)", "s" if pc["enabled"] else "n", yes)
        if not pc["enabled"]:
            if was:
                unblock(name, pc, "provider spento")
            continue
        pc["block"] = ask("  blocca al limite (s/n)", "s" if pc["block"] else "n", yes)
        pc["limit"] = ask("  limite % delle finestre", pc["limit"], lambda v: int(v) if 1 <= int(v) <= 100 else None)
        if name == "claude":
            spent = load_state().get("claude", {}).get("credits")
            print(f"  questo mese: {spent} di extra usage" if spent else "  spesa del mese non ancora letta")
            pc["extra_limit"] = ask("  tetto mensile di extra usage, 0 = nessuna spesa extra", pc["extra_limit"],
                                    lambda v: float(v.replace(",", ".")) if float(v.replace(",", ".")) >= 0 else None)
    cfg["setup_done"] = True
    save_config(cfg)
    print(f"\nSalvato in {CONF}. Vale dal prossimo poll, senza riavviare niente.")


def loop(only):
    log("guard", f"avvio loop: {', '.join(only)}")
    while True:
        trim_log()
        try:
            cfg = load_config()
        except PollError as e:
            log("guard", f"ERRORE {e}")
            time.sleep(60)
            continue
        on = [n for n in only if cfg[n]["enabled"]]
        for name in on:
            poll(name)
        # Si dorme fino al primo provider in scadenza: se il widget fa il poll prima,
        # al risveglio lo state e' fresco e il turno salta
        st, t = load_state(), now()
        due = [int(st.get(n, {}).get("poll_epoch") or 0) + interval(n, cfg[n], st.get(n, {})) - t for n in on]
        time.sleep(min(max(5, min(due, default=60)), 300))


def main(argv):
    mode = argv[0] if argv else "--widget"
    if mode in ("--pause", "--resume"):  # dopo l'opzione, nomi di sessione e non provider
        os.makedirs(DIR, exist_ok=True)
        return manual(mode == "--pause", argv[1:])
    only = [p for p in argv[1:] if p in PROVIDERS] or list(PROVIDERS)
    if any(p not in PROVIDERS for p in argv[1:]):
        raise SystemExit(f"Provider sconosciuto. Validi: {', '.join(PROVIDERS)}")
    os.makedirs(DIR, exist_ok=True)
    if mode == "--widget":
        launch_widget()
    elif mode == "--once":
        trim_log()
        for name in only:
            poll(name, force=True)
        st = load_state()
        return 1 if any(st.get(n, {}).get("err") for n in only if load_config()[n]["enabled"]) else 0
    elif mode == "--loop":
        loop(only)
    elif mode == "--status":
        show_status(only)
    elif mode == "--block":
        cfg = load_config()
        on = [n for n in only if cfg[n]["enabled"]]
        if not on:
            print("Nessun provider attivo da bloccare")
        return 0 if all([block(name, cfg[name]) for name in on]) else 1
    elif mode == "--unblock":
        cfg = load_config()
        for name in only:
            if any(tg.status() == "ours" for tg in targets(name, cfg[name])):
                unblock(name, cfg[name])
            else:
                print(f"{NAMES[name]}: nessun blocco attivo")
    elif mode == "--config":
        configure()
    elif mode in ("--help", "-h"):
        print(__doc__)
    else:
        raise SystemExit(f"Opzione sconosciuta: {mode}. Vedi --help")
    return 0


if __name__ == "__main__":
    # console cp1252: un carattere fuori tabella (la freccia nei risultati di SendMessage) non
    # deve far cadere log() a meta'; nel file di log va comunque in utf-8
    sys.stdout.reconfigure(errors="replace")
    try:
        sys.exit(main(sys.argv[1:]))
    except PollError as e:
        raise SystemExit(f"Errore: {e}")
