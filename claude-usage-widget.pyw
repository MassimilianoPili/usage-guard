"""
claude-usage-widget.pyw: widget floating di usage_guard.py.

Una riga per provider attivo (Claude, Codex, Copilot), in primo piano anche
sopra la barra delle applicazioni: finestre di usage, tempo al reset, crediti,
blocco. Fa anche da loop: lancia i poll di usage_guard dentro il proprio
processo, ciascuno al suo intervallo; un provider spento non viene letto. Se un
--loop da terminale ha fatto il poll da poco, il widget non lo ripete.

Si avvia con claude-usage-guard.sh o `python usage_guard.py`; rilanciato col
widget aperto, lo porta in alto a destra del monitor del mouse. Tasto sinistro
trascina, tasto destro apre il menu. Al primo avvio chiede quali provider
controllare. Preferenze del widget in ~/.claude/usage-guard.widget, dei
provider in ~/.claude/usage-guard.json.
"""
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import usage_guard as ug  # noqa: E402

PREFS = os.path.join(ug.DIR, "usage-guard.widget")
TICK_MS = 2000

BG, BG_BLOCK = "#1e1e1e", "#6b1414"
# Colore chiave della finestra del testo: diventa trasparente. Vicino a BG, cosi'
# l'antialiasing del testo fa un bordo scuro che si legge su ogni sfondo.
KEY = "#1e1e1f"
FG, DIM, OK, WARN, BAD = "#d4d4d4", "#8a8a8a", "#89d185", "#e5c07b", "#f14c4c"

LABELS = {"S, W": ("S", "W"), "Session, W": ("Session", "W"), "Session, Week": ("Session", "Week")}
DEFAULTS = {
    "x": None, "y": None,
    "show_session": True, "show_reset": True, "show_credits": True, "show_overage": True,
    "hidden": [],  # "provider:finestra" nascoste: una finestra nuova si vede
    "labels": "Session, W",
    "transp": 0, "font": 9, "topmost": True, "warn": 80,
}
# Un valore fuori range e' una digitazione a meta' (1 verso 120): non si applica
# font da 1: in Tk 0 e' il font di default e un negativo e' in pixel
RANGES = {"transp": (0, 100), "font": (1, 20), "warn": (1, 100)}


def num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_prefs():
    prefs = dict(DEFAULTS)
    try:
        with open(PREFS, encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return prefs
    try:
        prefs.update(json.loads(raw))
    except ValueError:  # formato precedente: "x y trasparenza"
        prefs.update(zip(("x", "y", "transp"), (num(v) for v in raw.split())))
    prefs.pop("interval", None)  # ora per provider, in usage-guard.json
    # finestre nascoste della versione solo Claude: senza prefisso del provider
    prefs["hidden"] = [h if ":" in h else f"claude:{h}" for h in prefs.get("hidden") or []]
    if prefs["labels"] not in LABELS:
        prefs["labels"] = DEFAULTS["labels"]
    for key, (lo, hi) in RANGES.items():
        if not lo <= num(prefs[key], -1) <= hi:
            prefs[key] = DEFAULTS[key]
    return prefs


def short(label, names):
    session, week = names
    if label == "session":
        return session
    m = re.fullmatch(r"week(?:\((.*)\))?", label)
    if m:
        return f"{week} {m.group(1)}" if m.group(1) else week
    return label


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


U32 = ctypes.windll.user32
U32.MonitorFromPoint.restype = ctypes.c_void_p
U32.MonitorFromPoint.argtypes = [wintypes.POINT, ctypes.c_uint]
U32.MonitorFromRect.restype = ctypes.c_void_p
U32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT), ctypes.c_uint]
U32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
U32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
U32.GetParent.restype = ctypes.c_void_p
U32.GetParent.argtypes = [ctypes.c_void_p]
U32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
U32.GetForegroundWindow.restype = ctypes.c_void_p
U32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
U32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
U32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
U32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + [ctypes.c_int] * 4 + [ctypes.c_uint]
TASKBARS = ("Shell_TrayWnd", "Shell_SecondaryTrayWnd")
K32 = ctypes.WinDLL("kernel32", use_last_error=True)
K32.CreateMutexW.restype = K32.CreateEventW.restype = ctypes.c_void_p
K32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
K32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
K32.SetEvent.argtypes = [ctypes.c_void_p]
K32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]


def monitor_rect(x, y):
    """Rettangolo intero del monitor piu' vicino a (x, y), barra delle applicazioni
    compresa: un monitor staccato da quando e' stata salvata la posizione non vale."""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    mon = U32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
    if mon and U32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        r = mi.rcMonitor
        return r.left, r.top, r.right, r.bottom
    return 0, 0, U32.GetSystemMetrics(0), U32.GetSystemMetrics(1)


def attached(x, y, w, h):
    """Vero se almeno un pezzo del widget sta su un monitor attaccato."""
    rect = wintypes.RECT(x, y, x + w, y + h)
    return bool(U32.MonitorFromRect(ctypes.byref(rect), 0))  # MONITOR_DEFAULTTONULL


def mouse_corner():
    """Alto a destra del monitor col mouse, stesso offset del primo avvio."""
    pt = wintypes.POINT()
    U32.GetCursorPos(ctypes.byref(pt))
    _l, t, r, _b = monitor_rect(pt.x, pt.y)
    return r - 420, t + 8


def foreground_blocks_raise(own):
    """Vero se in primo piano c'e' una finestra topmost che non va coperta: Start,
    calendario, impostazioni rapide, un altro programma sempre in primo piano, o la
    finestra Impostazioni. La barra e il widget stesso (own) invece no."""
    fg = U32.GetForegroundWindow()
    if not fg or fg in own:
        return False
    pid = wintypes.DWORD()
    U32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
    if pid.value == os.getpid():
        return True
    cls = ctypes.create_unicode_buffer(64)
    U32.GetClassNameW(fg, cls, 64)
    if cls.value in TASKBARS:
        return False
    return bool(U32.GetWindowLongW(fg, -20) & 0x8)  # GWL_EXSTYLE, WS_EX_TOPMOST


class Widget:
    # Due finestre sovrapposte, perche' -alpha vale per tutta la finestra: sotto lo
    # sfondo con l'alpha scelto, sopra il testo opaco su KEY trasparente.
    def __init__(self, show):
        self.show = show  # evento segnalato da un secondo avvio
        self.state, self.state_mtime, self.cfg_mtime = {}, None, None
        self.cfg = json.loads(json.dumps(ug.DEFAULTS))  # copia: se la config non si legge
        self.cfg_err, self.shown, self.running, self.last_run, self.errs = "", None, set(), {}, {}
        self.p, self.win, self.win_vars, self.save_job, self.cfg_job = load_prefs(), None, [], None, None
        self.geom, self.menu_open, self.moved = None, False, False
        root = self.root = tk.Tk()
        top = self.top = tk.Toplevel(root)
        for win in (root, top):
            win.overrideredirect(True)
            self.bind(win)  # sulla toplevel vale anche per frame e label (bindtags)
        top.configure(bg=KEY)
        top.attributes("-transparentcolor", KEY)
        root.report_callback_exception = self.crash
        self.frame = tk.Frame(top, bg=KEY, padx=5, pady=2)
        self.frame.pack()
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Poll ora", command=self.poll_now)
        self.menu.add_command(label="Sblocca", command=self.unblock_all)
        self.menu.add_command(label="Blocca", command=self.block_all)
        self.menu.add_command(label="Impostazioni...", command=self.settings)
        self.menu.add_command(label="Apri log", command=self.open_log)
        self.autostart = tk.BooleanVar(value=os.path.isfile(STARTUP_LNK))
        self.menu.add_checkbutton(label="Avvia con Windows", variable=self.autostart, command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="Chiudi", command=root.destroy)
        self.reload()
        self.apply()
        self.anchor = self.start_pos()
        self.place()
        root.update()
        # sfondo prima, testo dopo: nello z-order il testo resta sopra
        self.hwnds = (U32.GetParent(root.winfo_id()), U32.GetParent(top.winfo_id()))
        # Il testo ha per owner lo sfondo: Windows lo tiene sempre sopra, anche
        # quando un clic attiva lo sfondo. Tk non lo fa per le overrideredirect.
        U32.SetWindowLongPtrW(self.hwnds[1], -8, self.hwnds[0])  # GWLP_HWNDPARENT
        if not self.cfg.get("setup_done"):
            root.after(300, lambda: self.settings(first=True))
        self.tick()
        self.keep_on_top()

    # --- finestra -------------------------------------------------------------
    def bind(self, w):
        w.bind("<ButtonPress-1>", self.press)
        w.bind("<B1-Motion>", self.drag)
        w.bind("<ButtonRelease-1>", self.release)
        w.bind("<Button-3>", self.popup)

    def start_pos(self):
        x, y = self.p["x"], self.p["y"]
        if isinstance(x, int) and isinstance(y, int):
            return x, y  # ci pensa place() a tenerlo dentro il monitor
        return self.root.winfo_screenwidth() - 420, 8

    def place(self):
        # anchor e' dove l'hai messo; qui si sposta solo quel che serve per stare
        # nel monitor. Una riga larga per poco non lo trascina via per sempre.
        w, h = self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()
        x, y = self.anchor
        if not attached(x, y, w, h):
            # monitor staccato: il clamp lo lascerebbe nell'angolo piu' vicino. Si salva,
            # cosi' il monitor che torna non lo riporta via.
            x, y = mouse_corner()
            if (x, y) != self.anchor:  # senza nessun monitor non riscrive a ogni tick
                self.anchor = x, y
                self.save_prefs()
        l, t, r, b = monitor_rect(x + w // 2, y + h // 2)
        x, y = min(max(x, l), r - w), min(max(y, t), b - h)
        if (x, y, w, h) == self.geom:
            return
        self.geom = (x, y, w, h)
        for win in (self.root, self.top):
            win.geometry(f"{w}x{h}+{x}+{y}")

    def keep_on_top(self):
        # La barra delle applicazioni e' topmost anche lei: cliccata, passa sopra
        # al widget appoggiato su di essa. SWP_NOACTIVATE: niente furto di focus.
        # Col menu aperto no: il widget gli passerebbe sopra.
        try:
            if self.p["topmost"] and not self.menu_open and not foreground_blocks_raise(self.hwnds):
                for hwnd in self.hwnds:
                    U32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x13)  # HWND_TOPMOST; NOSIZE|NOMOVE|NOACTIVATE
        finally:
            self.root.after(500, self.keep_on_top)

    def press(self, e):
        self.dx, self.dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()
        self.moved = False

    def drag(self, e):
        self.anchor, self.moved = (e.x_root - self.dx, e.y_root - self.dy), True
        self.place()

    def release(self, _e):
        if self.moved:
            self.anchor = self.geom[:2]  # trascinato oltre il bordo: resta dove lo vedi
            self.save_prefs()

    def popup(self, e):
        on = [n for n in ug.PROVIDERS if self.cfg[n]["enabled"]]
        self.menu.entryconfigure(0, state="normal" if any(n not in self.running for n in on) else "disabled")
        blocked = [self.state.get(n, {}).get("blocked") == 1 for n in on]
        self.menu.entryconfigure(1, state="normal" if any(blocked) else "disabled")
        self.menu.entryconfigure(2, state="normal" if not all(blocked) else "disabled")
        self.autostart.set(os.path.isfile(STARTUP_LNK))  # il collegamento si puo' togliere anche a mano
        self.menu_open = True  # su Windows tk_popup torna a menu chiuso
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu_open = False

    def toggle_autostart(self):
        try:
            set_autostart(self.autostart.get())
        except (OSError, RuntimeError) as e:
            self.cfg_err = f"avvio con Windows non cambiato: {e}"
            ug.log("widget", f"ERRORE {self.cfg_err}")
        self.autostart.set(os.path.isfile(STARTUP_LNK))

    def open_log(self):
        try:
            os.startfile(ug.LOG)
        except OSError as e:
            self.cfg_err = f"log non apribile: {e}"

    def crash(self, *exc):
        # pythonw non ha console: un'eccezione non mostrata sparirebbe. Nel log una
        # volta sola per messaggio, non a ogni tick.
        msg = f"errore widget: {exc[1]!r}"
        if msg != self.cfg_err:
            ug.log("widget", "ERRORE " + "".join(traceback.format_exception(*exc)).replace("\n", " | "))
        self.cfg_err = msg

    # --- preferenze del widget ------------------------------------------------
    def apply(self):
        p = self.p
        # Ad alpha 0 i clic passano attraverso: fra una parola e l'altra il widget
        # non si potrebbe piu' trascinare. 0.01 e' invisibile ma prende il clic.
        self.root.attributes("-alpha", max(0.01, 1 - p["transp"] / 100))
        for win in (self.root, self.top):
            win.attributes("-topmost", bool(p["topmost"]))
        self.shown = None

    def save_prefs(self):
        self.save_job = None
        self.p["x"], self.p["y"] = self.anchor
        try:
            with open(PREFS, "w", encoding="utf-8") as f:
                json.dump(self.p, f, indent=1)
        except OSError as e:
            self.cfg_err = f"preferenze non salvate: {e}"

    def set_pref(self, key, value):
        if key in RANGES and not RANGES[key][0] <= value <= RANGES[key][1]:
            return
        self.p[key] = value
        self.apply()
        # lo slider manda decine di valori al secondo: si salva a mano ferma
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self.save_prefs)

    # --- configurazione dei provider ------------------------------------------
    def set_cfg(self, name, key, value):
        pc = self.cfg[name]
        if key in ug.RANGES and not ug.RANGES[key][0] <= value <= ug.RANGES[key][1]:
            return
        if key == "extra_limit" and value < 0 or key == "pause" and not 0 <= value <= 100:
            return
        was = pc.get(key)
        pc[key] = value
        if key == "enabled" and was and not value:
            # spento: niente piu' poll, quindi nessuno toglierebbe un blocco rimasto
            threading.Thread(target=ug.unblock, args=(name, dict(pc), "provider spento"), daemon=True).start()
        self.shown = None
        if self.cfg_job:
            self.root.after_cancel(self.cfg_job)
        self.cfg_job = self.root.after(500, self.save_cfg)

    def save_cfg(self):
        self.cfg_job = None
        try:
            ug.save_config(self.cfg)
            self.cfg_mtime = None
        except OSError as e:
            self.cfg_err = f"configurazione non salvata: {e}"

    def settings(self, first=False):
        if self.win and self.win.winfo_exists():
            self.win.lift()
            return
        w = self.win = tk.Toplevel(self.root)
        w.withdraw()  # si mostra dopo, gia' al suo posto
        w.title("usage guard")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        self.win_vars = []  # una variabile Tk senza riferimento viene raccolta e la checkbox si svuota
        if first:
            tk.Label(w, text="Primo avvio: attiva i provider da controllare, poi chiudi.",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.widget_tab(nb)
        for name in ug.PROVIDERS:
            self.provider_tab(nb, name)
        if first:
            nb.select(1)

        def close():
            if first:
                self.cfg["setup_done"] = True
                self.save_cfg()
            w.destroy()
        w.protocol("WM_DELETE_WINDOW", close)

        # Sotto il widget se c'e' posto, altrimenti sopra: col widget sulla barra
        # delle applicazioni, sotto sarebbe fuori schermo.
        w.update_idletasks()
        x, y, ww, wh = self.geom
        l, t, r, b = monitor_rect(x + ww // 2, y + wh // 2)
        sw, sh = w.winfo_reqwidth(), w.winfo_reqheight() + 45  # 45: barra del titolo
        sy = y + wh + 8 if y + wh + 8 + sh <= b else y - 8 - sh
        w.geometry(f"+{min(max(x, l), r - sw)}+{max(sy, t)}")
        w.deiconify()

    def bound(self, kind, value, setter):
        v = kind(value=value)

        def changed(*_):
            try:
                setter(v.get())
            except (tk.TclError, ValueError):  # campo vuoto o non numerico mentre si scrive
                pass
        v.trace_add("write", changed)
        self.win_vars.append(v)
        return v

    def widget_tab(self, nb):
        tab = tk.Frame(nb, padx=6, pady=4)
        nb.add(tab, text="Widget")
        p = self.p

        def pref(kind, key):
            return self.bound(kind, p[key], lambda v, key=key: self.set_pref(key, v))

        def section(row, title):
            box = tk.LabelFrame(tab, text=title, padx=8, pady=4)
            box.grid(row=row, column=0, sticky="ew", padx=2, pady=3)
            return box

        box = section(0, "Elementi")
        for key, text in (("show_session", "Claude: Session %"), ("show_reset", "Tempo al reset della prima finestra"),
                          ("show_credits", "Crediti"), ("show_overage", "Claude: overage")):
            tk.Checkbutton(box, text=text, variable=pref(tk.BooleanVar, key)).pack(anchor="w")
        for name in ug.PROVIDERS:
            for label, _pct, _reset in self.state.get(name, {}).get("windows") or []:
                if name == "claude" and label == "session":
                    continue
                key = f"{name}:{label}"

                def toggle(show, key=key):
                    self.set_pref("hidden", [h for h in p["hidden"] if h != key] + ([] if show else [key]))
                tk.Checkbutton(box, text=f"{ug.NAMES[name]}: {short(label, LABELS['Session, Week'])}",
                               variable=self.bound(tk.BooleanVar, key not in p["hidden"], toggle)).pack(anchor="w")
        tk.Label(box, text="Gli errori si vedono sempre.", fg="gray40").pack(anchor="w")

        box = section(1, "Etichette")
        labels = pref(tk.StringVar, "labels")
        for name in LABELS:
            tk.Radiobutton(box, text=name, value=name, variable=labels).pack(anchor="w")

        box = section(2, "Aspetto")
        tk.Label(box, text="Trasparenza sfondo %").grid(row=0, column=0, sticky="w")
        tk.Scale(box, from_=0, to=100, orient="horizontal", length=180,
                 variable=pref(tk.IntVar, "transp")).grid(row=0, column=1, sticky="w")
        tk.Label(box, text="Dimensione testo").grid(row=1, column=0, sticky="w")
        tk.Spinbox(box, from_=1, to=20, width=5, textvariable=pref(tk.IntVar, "font")).grid(row=1, column=1, sticky="w")
        tk.Checkbutton(box, text="Sempre in primo piano",
                       variable=pref(tk.BooleanVar, "topmost")).grid(row=2, column=0, columnspan=2, sticky="w")
        tk.Label(box, text="Giallo dal % del limite").grid(row=3, column=0, sticky="w")
        tk.Spinbox(box, from_=1, to=100, width=5, textvariable=pref(tk.IntVar, "warn")).grid(row=3, column=1, sticky="w")
        tk.Label(box, text="Rosso al 100% del limite di ciascun provider.",
                 fg="gray40").grid(row=4, column=0, columnspan=2, sticky="w")

    def provider_tab(self, nb, name):
        tab = tk.Frame(nb, padx=10, pady=6)
        nb.add(tab, text=ug.NAMES[name])
        pc = self.cfg[name]
        row = [0]

        def cfg(kind, key):
            return self.bound(kind, pc[key], lambda v, key=key: self.set_cfg(name, key, v))

        def check(text, key):
            tk.Checkbutton(tab, text=text, variable=cfg(tk.BooleanVar, key)).grid(
                row=row[0], column=0, columnspan=2, sticky="w")
            row[0] += 1

        def spin(text, key, lo, hi, inc=1):
            tk.Label(tab, text=text).grid(row=row[0], column=0, sticky="w")
            tk.Spinbox(tab, from_=lo, to=hi, increment=inc, width=7,
                       textvariable=cfg(tk.IntVar, key)).grid(row=row[0], column=1, sticky="w")
            row[0] += 1

        def entry(text, key, conv=str):
            tk.Label(tab, text=text).grid(row=row[0], column=0, sticky="w")
            v = self.bound(tk.StringVar, str(pc[key]), lambda s, key=key: self.set_cfg(name, key, conv(s)))
            tk.Entry(tab, textvariable=v, width=52).grid(row=row[0], column=1, sticky="w")
            row[0] += 1

        def note(text):
            tk.Label(tab, text=text, fg="gray40", justify="left").grid(
                row=row[0], column=0, columnspan=2, sticky="w", pady=(4, 0))
            row[0] += 1

        check("Attivo: spento non viene nemmeno letto", "enabled")
        check("Blocca al limite", "block")
        spin("Limite % delle finestre", "limit", 1, 100)
        spin("Intervallo fra i poll, s", "interval", 30, 86400, inc=30)
        if name == "claude":
            entry("Tetto extra al mese (0 = niente)", "extra_limit", lambda s: float(s.replace(",", ".")))
            entry("Finestre ignorate (regex)", "skip")
            entry("settings.json", "settings")
            spin("Pausa % (0 = spenta): messaggio alle chat al lavoro", "pause", 0, 100)
            check("Al reset manda continue alle chat in pausa o bloccate", "resume")
            note("Il blocco arriva anche alle sessioni aperte, e lo sblocco pure.\n"
                 "Oltre il limite si blocca solo a tetto extra raggiunto.\n"
                 "Pausa e continue passano dal messaging fra sessioni:\n"
                 "serve crossSessionInbound \"accept\" in settings.json.\n"
                 f"A meno di {ug.NEAR} punti dalla soglia il poll passa a {ug.NEAR_INTERVAL}s.")
        elif name == "codex":
            entry("config.toml", "config")
            entry("codex.exe (vuoto: dall'estensione VS Code)", "binary")
            note("Il blocco vale per le conversazioni nuove o riaperte.\n"
                 "I limiti si leggono dall'app-server: serve il login di Codex in VS Code.")
        else:
            entry("Cache della quota", "cache")
            check("Blocca VS Code (subito; chat e agent, non i completamenti)", "block_vscode")
            entry("settings.json di VS Code", "vscode")
            check("Blocca Copilot CLI (vale al prossimo avvio)", "block_cli")
            entry("config.json del CLI", "cli")
            note("La quota viene dalla cache del CLI copilot: si aggiorna quando parte\n"
                 "il runtime di Copilot. JetBrains si vede ma non si blocca.")
        st = self.state.get(name, {})
        if st.get("last") or st.get("err"):
            note(f"Ultimo poll: {st.get('last') or 'nessuno'}" + (f"\nErrore: {st['err']}" if st.get("err") else ""))

    # --- poll -----------------------------------------------------------------
    def reload(self):
        for path, attr in ((ug.CONF, "cfg"), (ug.STATE, "state")):
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                mtime = 0
            if mtime == getattr(self, attr + "_mtime"):
                continue
            try:
                setattr(self, attr, ug.load_config() if attr == "cfg" else ug.load_state())
                if attr == "cfg":
                    self.cfg_err = ""
            except ug.PollError as e:
                self.cfg_err = str(e)  # si tiene la configurazione precedente
            except OSError:
                continue  # file occupato proprio ora: mtime non aggiornato, si riprova al prossimo tick
            setattr(self, attr + "_mtime", mtime)

    def run(self, name, force=False):
        self.running.add(name)
        self.last_run[name] = time.time()
        threading.Thread(target=self.worker, args=(name, force), daemon=True).start()

    def worker(self, name, force):
        try:
            ug.poll(name, force=force)
            self.errs.pop(name, None)
        except Exception as e:  # un bug non deve fermare il loop, ma si deve vedere
            self.errs[name] = f"errore: {e}"
        finally:
            self.running.discard(name)
            self.state_mtime = None  # rilegge lo state al prossimo tick

    def poll_now(self):
        for name in ug.PROVIDERS:
            if self.cfg[name]["enabled"] and name not in self.running:
                self.run(name, force=True)

    def unblock_all(self):
        def work():
            for name in ug.PROVIDERS:
                if self.state.get(name, {}).get("blocked") == 1:
                    ug.unblock(name)
            self.state_mtime = None
        threading.Thread(target=work, daemon=True).start()

    def block_all(self):
        def work():
            for name in ug.PROVIDERS:
                if self.cfg[name]["enabled"] and self.state.get(name, {}).get("blocked") != 1:
                    ug.block(name)
            self.state_mtime = None
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        # Il prossimo tick si programma comunque: un'eccezione qui congelava la riga
        # sull'ultimo valore, coi poll da menu che continuavano senza che si vedesse.
        try:
            self.reload()
            now = time.time()
            for name in ug.PROVIDERS:
                pc = self.cfg[name]
                if not pc["enabled"] or name in self.running:
                    continue  # spento: nessun controllo
                last = max(num(self.state.get(name, {}).get("poll_epoch")), self.last_run.get(name, 0))
                if now - last >= ug.interval(name, pc, self.state.get(name, {})):
                    self.run(name)
            self.render(now)
            if K32.WaitForSingleObject(self.show, 0) == 0:  # WAIT_OBJECT_0: script rilanciato
                self.anchor = mouse_corner()
                self.save_prefs()
            self.place()  # il monitor cambia da solo: risoluzione, monitor staccato
            if self.cfg_err.startswith("errore widget"):
                self.cfg_err = ""  # tick riuscito: l'errore era di passaggio
        except Exception as e:
            self.crash(type(e), e, e.__traceback__)
            self.shown = None  # al prossimo giro la riga mostra l'errore
        finally:
            self.root.after(TICK_MS, self.tick)

    # --- righe ----------------------------------------------------------------
    def row(self, name, now):
        st, pc, p = self.state.get(name, {}), self.cfg[name], self.p
        names = LABELS[p["labels"]]
        blocked = st.get("blocked") == 1
        limit = num(pc.get("limit"), 100)
        segs = [(ug.NAMES[name], DIM, True)]
        if blocked:
            # bianco sul rosso dello sfondo; se lo sfondo quasi non si vede, rosso
            segs.append(("BLOCCATO", "#ffffff" if p["transp"] < 50 else BAD, True))
            until = num(st.get("resets"))
            if until > now:
                segs.append((f"fino alle {time.strftime('%H:%M', time.localtime(until))}, "
                             f"fra {ug.fmt_left(until - now)}", FG, False))
        for i, (label, pct, reset) in enumerate(st.get("windows") or []):
            hidden = f"{name}:{label}" in p["hidden"] or (name == "claude" and label == "session" and not p["show_session"])
            if not hidden:
                # reset passato: la finestra e' ripartita, il numero e' vecchio
                color = (DIM if reset and reset <= now else FG if blocked else BAD if pct >= limit
                         else WARN if pct * 100 >= limit * p["warn"] else OK)
                segs.append((f"{short(label, names)} {pct}%", color, True))
            if i == 0 and p["show_reset"] and reset > now:
                segs.append((ug.fmt_left(reset - now), DIM, False))
        if p["show_credits"] and st.get("credits"):
            # "$1051.67/$4000.00(26%)" diventa "$1051/$4000"
            credits = re.sub(r"\.\d+|\(.*\)", "", st["credits"]) if name == "claude" else st["credits"]
            segs.append((credits, DIM, False))
        if name == "claude" and p["show_overage"] and st.get("mode") == "overages":
            segs.append(("overage", WARN, True))
        retrieved = num(st.get("retrieved"))
        if retrieved and now - retrieved > 3600:  # la cache di Copilot non e' in tempo reale
            segs.append((f"dato di {ug.fmt_left(now - retrieved)} fa", DIM, False))
        err = self.errs.get(name) or st.get("err", "")
        if err:
            segs.append((err if len(err) <= 60 else err[:59] + "…", WARN, False))
        if name in self.running:
            segs.append(("…", DIM, False))
        if len(segs) == 1:
            segs.append(("in attesa del primo poll", DIM, False))
        return segs, blocked

    def render(self, now):
        rows, blocked = [], False
        for name in ug.PROVIDERS:
            if self.cfg[name]["enabled"]:
                segs, b = self.row(name, now)
                rows.append(segs)
                blocked |= b
        if not rows:
            rows.append([("nessun provider attivo: tasto destro, Impostazioni", DIM, False)])
        if self.cfg_err:
            rows.append([(self.cfg_err[:80], WARN, False)])
        if (blocked, rows) == self.shown:
            return
        self.shown = (blocked, rows)
        self.root.configure(bg=BG_BLOCK if blocked else BG)
        for w in self.frame.winfo_children():
            w.destroy()
        font, bold = ("Segoe UI", self.p["font"]), ("Segoe UI", self.p["font"], "bold")
        for segs in rows:
            line = tk.Frame(self.frame, bg=KEY)
            line.pack(anchor="w")
            for text, fg, strong in segs:
                tk.Label(line, text=text, fg=fg, bg=KEY, font=bold if strong else font,
                         padx=3, pady=0).pack(side="left")
        self.top.update_idletasks()
        self.place()  # lo sfondo segue la dimensione del testo


STARTUP_LNK = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs",
                           "Startup", "Claude usage guard.lnk")


def set_autostart(on):
    """Collegamento in Esecuzione automatica: pythonw + questo widget, niente console."""
    if not on:
        with contextlib.suppress(FileNotFoundError):
            os.remove(STARTUP_LNK)
        return
    here = os.path.dirname(os.path.abspath(__file__))
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    # I percorsi passano per l'ambiente: con spazi e apostrofi un -Command quotato a mano si rompe
    env = dict(os.environ, UG_LNK=STARTUP_LNK, UG_EXE=pyw, UG_DIR=here,
               UG_ARGS=f'"{os.path.join(here, os.path.basename(__file__))}"')
    script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:UG_LNK); "
              "$s.TargetPath = $env:UG_EXE; $s.Arguments = $env:UG_ARGS; $s.WorkingDirectory = $env:UG_DIR; "
              "$s.Description = 'Claude usage guard'; $s.Save()")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                       capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode or not os.path.isfile(STARTUP_LNK):
        raise RuntimeError(f"collegamento non creato: {(p.stderr or p.stdout).strip()[:200]}")


def single_instance():
    mutex = K32.CreateMutexW(None, False, "Local\\claude-usage-guard-widget")
    running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS, letto prima di CreateEventW
    show = K32.CreateEventW(None, False, False, "Local\\claude-usage-guard-widget-show")
    if running:
        K32.SetEvent(show)  # il widget aperto viene sul monitor del mouse
        sys.exit(0)
    return mutex, show  # tenuti fino all'uscita


if __name__ == "__main__":
    _mutex, _show = single_instance()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # testo nitido sugli schermi scalati
    except (AttributeError, OSError):
        pass
    try:
        Widget(_show).root.mainloop()
    except Exception:
        # Prima della finestra non c'e' dove mostrarlo: finisce nel log del guard
        with open(ug.LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " widget: ERRORE "
                    + traceback.format_exc().replace("\n", " | ") + "\n")
        raise
