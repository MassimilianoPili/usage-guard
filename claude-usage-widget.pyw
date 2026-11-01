"""
claude-usage-widget.pyw: widget floating di usage_guard.py.

Una riga per provider attivo (Claude, Codex, Copilot), in primo piano anche
sopra la barra delle applicazioni: finestre di usage, tempo al reset, crediti,
blocco. Fa anche da loop: lancia i poll di usage_guard dentro il proprio
processo, ciascuno al suo intervallo; un provider spento non viene letto. Se un
--loop da terminale ha fatto il poll da poco, il widget non lo ripete.

Si avvia con claude-usage-guard.sh o `python usage_guard.py`; rilanciato col
widget aperto, lo porta in alto a destra del monitor del mouse. Tasto sinistro
trascina, tasto destro apre il menu. In Impostazioni, scheda Posizione: aggancio a
un punto del monitor, lato fisso del box, allineamento del testo, margini e
padding, con i disegni che cambiano subito. Al primo avvio chiede quali provider
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
import tkinter.font as tkfont
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
PANEL, FAINT = "#141414", "#555555"  # disegni delle Impostazioni: fondo, e linee che non contano

LABELS = {"S, W": ("S", "W"), "Session, W": ("Session", "W"), "Session, Week": ("Session", "Week")}
# Aggancio: punto del monitor -> lati fissi del box, orizzontale e verticale
DOCKS = {"tl": ("left", "top"), "tc": ("center", "top"), "tr": ("right", "top"),
         "bl": ("left", "bottom"), "bc": ("center", "bottom"), "br": ("right", "bottom")}
DOCK_NAMES = {"tl": "in alto a sinistra", "tc": "in alto al centro", "tr": "in alto a destra",
              "bl": "in basso a sinistra", "bc": "in basso al centro", "br": "in basso a destra"}
DEFAULTS = {
    "x": None, "y": None,  # il punto fisso del box (align, valign), non l'angolo in alto a sinistra
    "show_session": True, "show_reset": True, "show_credits": True, "show_overage": True,
    "hidden": [],  # "provider:finestra" nascoste: una finestra nuova si vede
    "labels": "Session, W",
    "transp": 0, "font": 9, "topmost": True, "warn": 80,
    "dock": "free", "align": "right", "valign": "top", "text_align": "center",
    "margin_x": 8, "margin_y": 8, "pad_x": 5, "pad_y": 2,
}
# Un valore fuori range e' una digitazione a meta' (1 verso 120): non si applica
# font da 1: in Tk 0 e' il font di default e un negativo e' in pixel
RANGES = {"transp": (0, 100), "font": (1, 20), "warn": (1, 100),
          "margin_x": (0, 400), "margin_y": (0, 400), "pad_x": (0, 50), "pad_y": (0, 50)}
CHOICES = {"dock": ("free",) + tuple(DOCKS), "align": ("left", "center", "right"),
           "valign": ("top", "bottom"), "text_align": ("left", "center", "right")}
SHARE = {"left": 0, "top": 0, "center": 0.5, "right": 1, "bottom": 1}  # quota del box prima del punto fisso
TEXT_ANCHOR = {"left": "w", "center": "center", "right": "e"}
ALIGN_NAMES = (("left", "Sinistra"), ("center", "Centro"), ("right", "Destra"))
VALIGN_NAMES = (("top", "Alto"), ("bottom", "Basso"))
SMALL, SMALL_BOLD = ("Segoe UI", 7), ("Segoe UI", 8, "bold")
DOCK_W, DOCK_H = 224, 126


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
        saved = json.loads(raw)
    except ValueError:  # formato precedente: "x y trasparenza"
        saved = dict(zip(("x", "y", "transp"), (num(v) for v in raw.split())))
    prefs.update(saved)
    # x, y salvati prima dell'allineamento sono l'angolo in alto a sinistra: si convertono al
    # primo disegno, cosi' il widget non si sposta
    prefs["_topleft"] = "align" not in saved
    prefs.pop("interval", None)  # ora per provider, in usage-guard.json
    # finestre nascoste della versione solo Claude: senza prefisso del provider
    prefs["hidden"] = [h if ":" in h else f"claude:{h}" for h in prefs.get("hidden") or []]
    if prefs["labels"] not in LABELS:
        prefs["labels"] = DEFAULTS["labels"]
    for key, (lo, hi) in RANGES.items():
        prefs[key] = num(prefs[key], -1)
        if not lo <= prefs[key] <= hi:
            prefs[key] = DEFAULTS[key]
    for key, values in CHOICES.items():
        if prefs[key] not in values:
            prefs[key] = DEFAULTS[key]
    if prefs["dock"] in DOCKS:
        prefs["align"], prefs["valign"] = DOCKS[prefs["dock"]]  # agganciato: i lati li da' il punto
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
K32.OpenProcess.restype = ctypes.c_void_p
K32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
K32.CloseHandle.argtypes = [ctypes.c_void_p]
UPDATE_EVERY = 600  # s fra due controlli dal widget; in rete si va solo ogni update.interval_h ore


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


def inward(point, p):
    """Il punto fisso a destra o in basso, con margine 0, cade sul bordo: quel pixel e' gia' del
    monitor accanto. Uno verso l'interno."""
    return point[0] - (p["align"] == "right"), point[1] - (p["valign"] == "bottom")


def dock_point(p, l, t, r, b):
    """Punto fisso del box agganciato: il lato del monitor dato da align e valign, ai margini."""
    x = {"left": l + p["margin_x"], "center": (l + r) // 2, "right": r - p["margin_x"]}[p["align"]]
    y = {"top": t + p["margin_y"], "bottom": b - p["margin_y"]}[p["valign"]]
    return x, y


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
        self.rows, self.editing, self.edit_close = [], False, None
        self.layout_vars, self.layout_radios, self.dock_canvas, self.box_canvas = {}, [], None, None
        self.anchor, self.topleft = None, None  # punto fisso del box, o angolo da convertire
        # aggiornamenti: il thread scrive solo questi campi, Tk li legge nel tick
        self.update_tag, self.update_busy, self.update_last, self.restart_pending = None, False, 0, False
        self.update_label, self.closed = None, False
        # scheda Sessioni: il thread d'invio scrive solo sess_msg e sess_busy
        self.sess_list, self.sess_shown, self.sess_sel, self.sess_by_key = None, None, set(), {}
        self.sess_msg, self.sess_busy, self.sess_buttons, self.sess_result = "", False, [], None
        root = self.root = tk.Tk()
        top = self.top = tk.Toplevel(root)
        for win in (root, top):
            win.overrideredirect(True)
            self.bind(win)  # sulla toplevel vale anche per frame e label (bindtags)
        top.configure(bg=KEY)
        top.attributes("-transparentcolor", KEY)
        root.report_callback_exception = self.crash
        self.frame = tk.Frame(top, bg=KEY)  # padding da apply()
        self.frame.pack()
        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Poll ora", command=self.poll_now)
        self.menu.add_command(label="Sblocca", command=self.unblock_all)
        self.menu.add_command(label="Blocca", command=self.block_all)
        self.menu.add_command(label="Impostazioni...", command=self.settings)
        self.menu.add_command(label="Apri log", command=self.open_log)
        self.menu.add_command(label="Aggiorna", command=self.update_now)
        self.update_index = self.menu.index("end")
        self.autostart = tk.BooleanVar(value=os.path.isfile(STARTUP_LNK))
        self.menu.add_checkbutton(label="Avvia con Windows", variable=self.autostart, command=self.toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="Chiudi", command=root.destroy)
        self.reload()
        self.apply()
        self.start_pos()
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
        legacy = self.p.pop("_topleft", False)
        if not (isinstance(x, int) and isinstance(y, int)):
            self.topleft = self.root.winfo_screenwidth() - 420, 8  # primo avvio
        elif legacy:
            self.topleft = x, y
        else:
            self.anchor = x, y  # ci pensa place() a tenerlo dentro il monitor

    def fixed(self, x, y, w, h):
        """Punto fisso del box che ha l'angolo in alto a sinistra in (x, y)."""
        return x + round(w * SHARE[self.p["align"]]), y + round(h * SHARE[self.p["valign"]])

    def origin(self, w, h):
        """Angolo in alto a sinistra del box, dal punto fisso."""
        fx, fy = self.anchor
        return fx - round(w * SHARE[self.p["align"]]), fy - round(h * SHARE[self.p["valign"]])

    def place(self):
        # anchor e' il punto fisso del box, dove l'hai messo o dove l'hai agganciato: il box cresce
        # e si stringe attorno a quello. Qui si sposta solo quel che serve per stare nel monitor:
        # una riga larga per poco non lo trascina via per sempre.
        w, h = self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()
        p = self.p
        if p["dock"] != "free":
            # agganciato: il punto fisso e' il punto d'aggancio del monitor dove sta il widget
            ref = inward(self.anchor, p) if self.anchor else self.topleft or mouse_corner()
            self.topleft = None
            fixed = dock_point(p, *monitor_rect(*ref))
            if fixed != self.anchor:
                self.anchor = fixed
                self.save_prefs()
        elif self.topleft:
            # angolo in alto a sinistra (primo avvio, versione precedente, rilancio, cambio di lato
            # fisso): al primo disegno con le righe vere diventa il punto fisso, e il box non si muove
            x, y = self.topleft
            if self.shown is not None:
                self.topleft = None
                self.anchor = self.fixed(x, y, w, h)
                self.save_prefs()
        if not self.topleft:
            x, y = self.origin(w, h)
        if p["dock"] == "free" and not attached(x, y, w, h):
            # monitor staccato: il clamp lo lascerebbe nell'angolo piu' vicino. Si salva,
            # cosi' il monitor che torna non lo riporta via.
            x, y = mouse_corner()
            fixed = self.fixed(x, y, w, h)
            if fixed != self.anchor:  # senza nessun monitor non riscrive a ogni tick
                self.anchor, self.topleft = fixed, None
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
        if self.p["dock"] != "free":
            self.p["dock"] = "free"  # trascinato via dall'aggancio: torna libero dove lo lasci
        w, h = self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()
        self.topleft = None
        self.anchor, self.moved = self.fixed(e.x_root - self.dx, e.y_root - self.dy, w, h), True
        self.place()

    def release(self, _e):
        if self.moved:
            self.anchor = self.fixed(*self.geom)  # trascinato oltre il bordo: resta dove lo vedi
            self.save_prefs()
            self.draw_layout()

    def popup(self, e):
        on = [n for n in ug.PROVIDERS if self.cfg[n]["enabled"]]
        self.menu.entryconfigure(0, state="normal" if any(n not in self.running for n in on) else "disabled")
        blocked = [self.state.get(n, {}).get("blocked") == 1 for n in on]
        self.menu.entryconfigure(1, state="normal" if any(blocked) else "disabled")
        self.menu.entryconfigure(2, state="normal" if not all(blocked) else "disabled")
        tag = self.update_tag
        label = ("Aggiornamento in corso..." if self.update_busy else
                 f"Aggiorna a {tag}" if tag and ug.installed() else
                 f"Aggiorna a {tag} (solo dalla copia installata)" if tag else "Aggiorna (nessuna versione nuova)")
        self.menu.entryconfigure(self.update_index, label=label,
                                 state="normal" if tag and ug.installed() and not self.update_busy else "disabled")
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
        self.frame.configure(padx=p["pad_x"], pady=p["pad_y"])
        self.shown = None

    def save_prefs(self):
        self.save_job = None
        if self.anchor:  # prima del primo disegno c'e' solo l'angolo da convertire
            self.p["x"], self.p["y"] = self.anchor
        try:
            with open(PREFS, "w", encoding="utf-8") as f:
                json.dump(self.p, f, indent=1)
        except OSError as e:
            self.cfg_err = f"preferenze non salvate: {e}"

    def set_pref(self, key, value):
        if key in RANGES and not RANGES[key][0] <= value <= RANGES[key][1]:
            return
        if key in CHOICES and value not in CHOICES[key]:
            return
        self.p[key] = value
        self.apply()
        self.render(time.time())  # il widget e i disegni cambiano subito, non al prossimo tick
        # lo slider manda decine di valori al secondo: si salva a mano ferma
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self.save_prefs)

    # --- configurazione dei provider ------------------------------------------
    def set_cfg(self, name, key, value):
        pc = self.cfg[name]
        if key in ug.RANGES and not ug.RANGES[key][0] <= value <= ug.RANGES[key][1]:
            return
        if key == "extra_limit" and value < 0:
            return
        if key in ug.CLAUDE_RANGES and not ug.CLAUDE_RANGES[key][0] <= value <= ug.CLAUDE_RANGES[key][1]:
            return
        if key == "source" and value not in ug.SOURCES:
            return
        if key in ("pause_include", "pause_exclude", "skip"):
            try:
                re.compile(value)
            except re.error:
                return  # regex scritta a meta': si salva quando e' valida
        if name == "update" and (key == "mode" and value not in ug.UPDATE_MODES
                                 or key == "interval_h" and not 1 <= value <= 720):
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
        # i disegni, il campo aperto e la tabella delle sessioni erano della finestra chiusa
        self.dock_canvas = self.box_canvas = self.edit_close = self.sess_list = None
        self.editing = False
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
        self.layout_tab(nb)
        tabs = {name: self.provider_tab(nb, name) for name in ug.PROVIDERS}
        nb.insert(tabs["codex"], self.sessions_tab(nb))  # subito dopo Claude
        self.update_tab(nb)
        if first:
            nb.select(tabs["claude"])

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

    # --- scheda Sessioni ---------------------------------------------------------
    def sessions_tab(self, nb):
        """Le sessioni di Claude Code aperte: chi e' nel registro (avra' il continue al reset), le
        regole sempre/mai, e i pulsanti per mandare pausa e continue a mano alle spuntate."""
        tab = tk.Frame(nb, padx=8, pady=6)
        nb.add(tab, text="Sessioni")
        self.sess_list = tk.Frame(tab)
        self.sess_list.grid(row=0, column=0, sticky="w")
        self.sess_shown = None
        # all'apertura sono spuntate quelle nel registro: durante una pausa il continue e' a un clic
        self.sess_sel = {ug.skey(s) for s in self.state.get("claude", {}).get("paused_sessions") or []}
        bar = tk.Frame(tab)
        bar.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.sess_buttons = []
        for text, cmd in (("Manda pausa", lambda: self.sess_send(True)), ("Manda continue", lambda: self.sess_send(False)),
                          ("Aggiungi al registro", lambda: self.sess_register(True)),
                          ("Togli dal registro", lambda: self.sess_register(False))):
            b = tk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.sess_buttons.append(b)
        self.sess_result = tk.Label(tab, text=self.sess_msg, fg="gray40", justify="left", wraplength=620)
        self.sess_result.grid(row=2, column=0, sticky="w", pady=(4, 0))
        rules = tk.LabelFrame(tab, text="Regole per pausa e continue automatici", padx=8, pady=4)
        rules.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        pc = self.cfg["claude"]
        for row, (key, text) in enumerate((("pause_include", "Sempre, anche se idle (regex)"),
                                           ("pause_exclude", "Mai in pausa (regex)"))):
            tk.Label(rules, text=text).grid(row=row, column=0, sticky="w")
            tk.Entry(rules, width=48, textvariable=self.bound(
                tk.StringVar, pc.get(key) or "", lambda v, key=key: self.set_cfg("claude", key, v))).grid(
                row=row, column=1, sticky="w")
        tk.Label(rules, text="Su nome o cartella, maiuscole indifferenti; Mai vince su Sempre. Es. Orchestr|framework-agentico\n"
                             "Le regole valgono per i messaggi automatici; i pulsanti mandano alle spuntate, sempre.\n"
                             "Aggiungi durante una pausa manda anche la pausa; fuori da una pausa registra e basta.",
                 fg="gray40", justify="left").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.draw_sessions()
        return tab

    def draw_sessions(self):
        """La tabella delle sessioni vive, rifatta solo quando cambia qualcosa (ogni tick, 2 s)."""
        if not (self.sess_list and self.win and self.win.winfo_exists() and self.sess_list.winfo_exists()):
            return
        if self.sess_result and self.sess_result.winfo_exists():
            self.sess_result.configure(text=self.sess_msg)
            for b in self.sess_buttons:
                b.configure(state="disabled" if self.sess_busy else "normal")
        sessions = sorted(ug.local_sessions(), key=lambda s: (str(s.get("cwd") or "").lower(), str(s.get("name") or "")))
        reg = {ug.skey(s) for s in self.state.get("claude", {}).get("paused_sessions") or []}
        pc = self.cfg["claude"]
        self.sess_by_key = {ug.skey(s): s for s in sessions}
        rows = [(ug.skey(s), s.get("name") or "?", os.path.basename(str(s.get("cwd") or "").rstrip("\\/")),
                 s.get("status") or "?", ug.skey(s) in reg, ug.session_rule(pc, s), num(s.get("startedAt")) // 1000)
                for s in sessions]
        self.sess_sel &= set(self.sess_by_key) | reg  # le chiuse escono dalla selezione
        if rows == self.sess_shown:
            return
        self.sess_shown = rows
        for w in self.sess_list.winfo_children():
            w.destroy()
        for col, text in enumerate(("Sessione", "Cartella", "Stato", "Registro", "Regola", "Aperta")):
            tk.Label(self.sess_list, text=text, font=("Segoe UI", 9, "bold")).grid(row=0, column=col, sticky="w", padx=(0, 10))
        if not rows:
            tk.Label(self.sess_list, text="Nessuna sessione di Claude Code aperta.", fg="gray40").grid(
                row=1, column=0, columnspan=6, sticky="w")
        for i, (k, name, folder, status, in_reg, rule, started) in enumerate(rows, 1):
            def toggle(on, k=k):
                (self.sess_sel.add if on else self.sess_sel.discard)(k)
            tk.Checkbutton(self.sess_list, text=name, anchor="w",
                           variable=self.bound(tk.BooleanVar, k in self.sess_sel, toggle)).grid(row=i, column=0, sticky="w")
            tk.Label(self.sess_list, text=folder, fg="gray30").grid(row=i, column=1, sticky="w", padx=(0, 10))
            working = status in ug.WORKING
            tk.Label(self.sess_list, text="al lavoro" if working else status,
                     fg="dark green" if working else "gray40").grid(row=i, column=2, sticky="w", padx=(0, 10))
            tk.Label(self.sess_list, text="si'" if in_reg else "", fg="dark orange").grid(row=i, column=3, sticky="w")
            tk.Label(self.sess_list, text=rule, fg="firebrick" if rule == "mai" else "navy").grid(row=i, column=4, sticky="w")
            tk.Label(self.sess_list, text=time.strftime("%d/%m %H:%M", time.localtime(started)) if started else "",
                     fg="gray40").grid(row=i, column=5, sticky="w")

    def sess_targets(self):
        return [self.sess_by_key[k] for k in self.sess_sel if k in self.sess_by_key]

    def sess_send(self, pause):
        to = self.sess_targets()
        kind = "pausa" if pause else "continue"
        if self.sess_busy:
            return
        if not to:
            self.sess_msg = "Nessuna sessione spuntata."
            return
        self.sess_busy = True
        self.sess_msg = f"{time.strftime('%H:%M')} invio {kind} a {len(to)} chat (claude -p, fino a qualche minuto)..."

        def work():
            try:
                res = ug.message(pause, to)
                self.sess_msg = f"{time.strftime('%H:%M')} {kind}: " + "; ".join(
                    f"{n}: {'ERRORE ' + r[7:80] if r.startswith('ERRORE') else 'ok'}" for n, r in res)
            except Exception as e:  # claude assente, timeout, nessun SendMessage: si vede e si riprova
                self.sess_msg = f"{time.strftime('%H:%M')} {kind} non inviato: {e}"
                ug.log("claude", f"ERRORE {kind} dalla scheda: {e}")
            finally:
                self.sess_busy, self.state_mtime = False, None  # il registro e' cambiato: si rilegge
        threading.Thread(target=work, daemon=True).start()

    def sess_register(self, add):
        to = self.sess_targets()
        if self.sess_busy:
            return
        if not to:
            self.sess_msg = "Nessuna sessione spuntata."
            return
        if add and self.state.get("claude", {}).get("pause_reason"):
            self.sess_send(True)  # pausa in corso: chi entra adesso riceve anche la pausa
            return
        try:
            ug.register(to, add)
            self.sess_msg = (f"{time.strftime('%H:%M')} " + ("aggiunte al" if add else "tolte dal")
                             + f" registro: {', '.join(s['name'] for s in to)}")
        except (OSError, ug.Busy) as e:
            self.sess_msg = f"registro non cambiato: {e}"
        self.state_mtime = None

    def update_tab(self, nb):
        box = tk.Frame(nb, padx=10, pady=6)
        nb.add(box, text="Aggiornamenti")
        u = self.cfg["update"]
        tk.Label(box, text="Modalita'").grid(row=0, column=0, sticky="w")
        ttk.Combobox(box, values=ug.UPDATE_MODES, state="readonly", width=7,
                     textvariable=self.bound(tk.StringVar, u["mode"],
                                             lambda v: self.set_cfg("update", "mode", v))).grid(row=0, column=1, sticky="w")
        tk.Label(box, text="Controllo ogni, ore").grid(row=1, column=0, sticky="w")
        tk.Spinbox(box, from_=1, to=720, width=5,
                   textvariable=self.bound(tk.IntVar, u["interval_h"],
                                           lambda v: self.set_cfg("update", "interval_h", v))).grid(row=1, column=1, sticky="w")
        tk.Button(box, text="Controlla ora", command=lambda: self.update_now(check_only=True)).grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        self.update_label = tk.Label(box, text=self.update_text(), fg="gray40", justify="left")
        self.update_label.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 4))
        tk.Label(box, text="off: nessun controllo. notify: lo dice il widget, installi dal menu Aggiorna.\n"
                           "auto: installa e riavvia, mai durante un blocco o una pausa.\n"
                           "Si aggiorna solo la copia installata in %LOCALAPPDATA%\\usage-guard.",
                 fg="gray40", justify="left").grid(row=4, column=0, columnspan=2, sticky="w")

    # --- scheda Posizione ------------------------------------------------------
    def layout_tab(self, nb):
        tab = tk.Frame(nb, padx=6, pady=4)
        nb.add(tab, text="Posizione")
        box = tk.LabelFrame(tab, text="Aggancio", padx=8, pady=4)
        box.grid(row=0, column=0, sticky="ew", padx=2, pady=3)
        self.dock_canvas = tk.Canvas(box, width=DOCK_W, height=DOCK_H, bg=PANEL, highlightthickness=0)
        self.dock_canvas.pack(anchor="w")
        self.dock_caption = tk.Label(box, fg="gray40", justify="left", wraplength=380)
        self.dock_caption.pack(anchor="w")
        box = tk.LabelFrame(tab, text="Box e testo", padx=8, pady=4)
        box.grid(row=1, column=0, sticky="ew", padx=2, pady=3)
        self.box_canvas = tk.Canvas(box, bg=PANEL, highlightthickness=0)
        self.box_canvas.pack(anchor="w")
        self.box_caption = tk.Label(box, fg="gray40", justify="left", wraplength=380)
        self.box_caption.pack(anchor="w")
        radios = tk.Frame(box)
        radios.pack(anchor="w", pady=(4, 0))
        self.layout_vars, self.layout_radios = {}, []
        for row, (key, text, opts) in enumerate((("align", "Box fisso a", ALIGN_NAMES),
                                                 ("valign", "Verticale", VALIGN_NAMES),
                                                 ("text_align", "Testo", ALIGN_NAMES))):
            tk.Label(radios, text=text).grid(row=row, column=0, sticky="w", padx=(0, 6))
            v = self.layout_vars[key] = self.bound(tk.StringVar, self.p[key],
                                                   lambda val, key=key: self.set_layout(key, val))
            for col, (value, label) in enumerate(opts, 1):
                rb = tk.Radiobutton(radios, text=label, value=value, variable=v)
                rb.grid(row=row, column=col, sticky="w")
                if key != "text_align":
                    self.layout_radios.append(rb)  # agganciato li decide il punto: spenti
        self.draw_layout()

    def set_layout(self, key, value):
        if self.p.get(key) == value:
            return
        if key in ("align", "valign") and self.geom:
            self.topleft = self.geom[:2]  # il box resta dov'e': cambia solo il lato che sta fermo
        self.set_pref(key, value)

    def set_dock(self, d):
        if not self.geom or d == self.p["dock"]:
            return
        x, y, w, h = self.geom
        if d == "free":
            self.topleft = x, y  # resta dov'e', da qui si trascina
        else:
            self.p["align"], self.p["valign"] = DOCKS[d]
            self.anchor, self.topleft = (x + w // 2, y + h // 2), None  # il monitor dove sta ora
        self.set_pref("dock", d)

    def draw_layout(self):
        """I due disegni della scheda Posizione, rifatti a ogni cambio."""
        if self.editing or not (self.win and self.win.winfo_exists() and self.box_canvas):
            return
        free = self.p["dock"] == "free"
        for rb in self.layout_radios:
            rb.configure(state="normal" if free else "disabled")
        for key, v in self.layout_vars.items():
            if v.get() != self.p[key]:
                v.set(self.p[key])  # cambiati dal punto d'aggancio, o a mano nel file
        self.draw_dock(self.dock_canvas)
        self.draw_box(self.box_canvas)

    def draw_dock(self, c):
        """Il monitor con i 7 punti: 6 d'aggancio sul bordo, al centro Libero. Il rettangolo
        pieno e' il widget, in scala, dove sta ora."""
        c.delete("all")
        w, h, p = DOCK_W, DOCK_H, self.p
        c.create_rectangle(1, 1, w - 2, h - 2, outline=DIM)
        c.create_text(6, h / 2, text="monitor", anchor="w", fill=DIM, font=SMALL)  # a meta' lato: niente punti
        if self.geom:
            x, y, ww, wh = self.geom
            l, t, r, b = monitor_rect(x + ww // 2, y + wh // 2)
            sx, sy = (w - 3) / max(1, r - l), (h - 3) / max(1, b - t)
            x0, y0 = 1 + (x - l) * sx, 1 + (y - t) * sy
            c.create_rectangle(x0, y0, x0 + max(4, ww * sx), y0 + max(3, wh * sy), fill=FG, outline=FG)
        i = 14
        spots = {d: ({"l": i, "c": w / 2, "r": w - i}[d[1]], {"t": i, "b": h - i}[d[0]]) for d in DOCKS}
        spots["free"] = (w / 2, h / 2)
        for d, (px, py) in spots.items():
            on = d == p["dock"]
            rad = 6 if on else 5
            c.create_oval(px - rad, py - rad, px + rad, py + rad, width=2, tags=("spot", f"spot:{d}"),
                          fill=WARN if on else PANEL, outline=WARN if on else DIM)
            c.tag_bind(f"spot:{d}", "<Button-1>", lambda _e, d=d: self.set_dock(d))
        c.create_text(w / 2, h / 2 + 9, text="libero", anchor="n", font=SMALL,
                      fill=WARN if p["dock"] == "free" else DIM)
        c.tag_bind("spot", "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind("spot", "<Leave>", lambda _e: c.configure(cursor=""))
        if p["dock"] == "free":
            text = "Libero: trascina il widget dove vuoi. Clic su un punto del bordo per agganciarlo."
        else:
            text = (f"Agganciato {DOCK_NAMES[p['dock']]}, a {p['margin_x']}/{p['margin_y']} px dai bordi. "
                    "Trascinalo, o clic sul punto centrale, per tornare libero.")
        self.dock_caption.configure(text=text)

    def draw_box(self, c):
        """Box model come in DevTools: margine, sfondo col padding, testo. Le fasce non sono in
        scala, i numeri si'. Il testo e' quello vero del widget, coi suoi colori e il suo font."""
        c.delete("all")
        p = self.p
        font = tkfont.Font(family="Segoe UI", size=p["font"])
        bold = tkfont.Font(family="Segoe UI", size=p["font"], weight="bold")
        rows = list(self.rows) or [[("in attesa del primo poll", DIM, False)]]
        if len(rows) == 1:
            rows.append([("riga d'esempio", FAINT, False)])  # con una riga sola l'allineamento non si vede
        widths = [sum((bold if s else font).measure(t) + 6 for t, _fg, s in segs) for segs in rows]
        line = bold.metrics("linespace")
        cw, ch = max(widths), line * len(rows)
        m, pd, o = 30, 24, 3
        w, h = cw + 2 * (m + pd + o), ch + 2 * (m + pd + o)
        c.configure(width=w, height=h)
        free = p["dock"] == "free"
        mcol = FAINT if free else DIM  # in Libero i margini non contano
        mx0, my0, mx1, my1 = o, o, w - o, h - o
        sx0, sy0, sx1, sy1 = mx0 + m, my0 + m, mx1 - m, my1 - m
        cx0, cy0 = sx0 + pd, sy0 + pd
        c.create_rectangle(mx0, my0, mx1, my1, outline=mcol, dash=(3, 3))
        c.create_text(mx0 + 4, my0 + 3, text="margine", anchor="nw", fill=mcol, font=SMALL)
        c.create_rectangle(sx0, sy0, sx1, sy1, fill=BG, outline=DIM)
        c.create_text(sx0 + 4, sy0 + 3, text="padding", anchor="nw", fill=OK, font=SMALL)
        c.create_rectangle(cx0, cy0, cx0 + cw, cy0 + ch, outline=OK, dash=(2, 2))
        for i, (segs, rw) in enumerate(zip(rows, widths)):
            x = cx0 + {"left": 0, "center": (cw - rw) / 2, "right": cw - rw}[p["text_align"]]
            for t, fg, strong in segs:
                f = bold if strong else font
                c.create_text(x + 3, cy0 + i * line, text=t, fill=fg, font=f, anchor="nw")
                x += f.measure(t) + 6
        mval = FAINT if free else FG
        for key, x, y, fg in (("margin_y", w / 2, my0 + m / 2, mval), ("margin_y", w / 2, my1 - m / 2, mval),
                              ("margin_x", mx0 + m / 2, h / 2, mval), ("margin_x", mx1 - m / 2, h / 2, mval),
                              ("pad_y", w / 2, sy0 + pd / 2, FG), ("pad_y", w / 2, sy1 - pd / 2, FG),
                              ("pad_x", sx0 + pd / 2, h / 2, FG), ("pad_x", sx1 - pd / 2, h / 2, FG)):
            c.create_text(x, y, text=str(p[key]), fill=fg, font=SMALL_BOLD, tags=("val", f"val:{key}"))
        for key in ("margin_x", "margin_y", "pad_x", "pad_y"):
            c.tag_bind(f"val:{key}", "<Button-1>", lambda _e, key=key: self.edit_value(c, key))
        c.tag_bind("val", "<Enter>", lambda _e: c.configure(cursor="hand2"))
        c.tag_bind("val", "<Leave>", lambda _e: c.configure(cursor=""))
        # il punto che sta fermo: il box cresce e si stringe dagli altri lati
        fx, fy = sx0 + (sx1 - sx0) * SHARE[p["align"]], sy0 + (sy1 - sy0) * SHARE[p["valign"]]
        c.create_rectangle(fx - 4, fy - 4, fx + 4, fy + 4, fill=WARN, outline=WARN)
        grow = {"left": "destra", "center": "entrambi i lati", "right": "sinistra"}[p["align"]]
        down = {"top": "il basso", "bottom": "l'alto"}[p["valign"]]
        text = (f"Quadratino giallo: il punto che sta fermo. Il testo piu' lungo allarga il box verso {grow}, "
                f"una riga in piu' lo allunga verso {down}. Clic su un numero per cambiarlo.")
        if free:
            text += " In Libero i margini non contano: valgono da agganciato."
        self.box_caption.configure(text=text)

    def edit_value(self, c, key):
        """Clic su un numero del disegno: un campo li' sopra. Invio o clic fuori applica, Esc annulla,
        un valore fuori range non si applica."""
        if self.edit_close:
            self.edit_close(True)  # un altro campo aperto: si chiude prima
        item = c.find_withtag("current")
        if not item:
            return
        x, y = c.coords(item[0])
        self.editing = True
        e = tk.Entry(c, width=4, justify="center")
        e.insert(0, str(self.p[key]))
        e.select_range(0, "end")
        win = c.create_window(x, y, window=e)

        def close(save):
            if self.edit_close is not close:
                return  # Invio e poi FocusOut: una volta sola
            self.editing, self.edit_close = False, None
            try:
                value = e.get()
                c.delete(win)
                e.destroy()
            except tk.TclError:
                return  # Impostazioni chiuse col campo aperto
            if save:
                with contextlib.suppress(ValueError):
                    self.set_pref(key, int(value))
            self.draw_layout()
        self.edit_close = close
        e.bind("<Return>", lambda _e: close(True))
        e.bind("<KP_Enter>", lambda _e: close(True))
        e.bind("<Escape>", lambda _e: close(False))
        e.bind("<FocusOut>", lambda _e: close(True))
        e.focus_set()

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

        def choice(text, key, values):
            tk.Label(tab, text=text).grid(row=row[0], column=0, sticky="w")
            ttk.Combobox(tab, values=values, state="readonly", width=7,
                         textvariable=cfg(tk.StringVar, key)).grid(row=row[0], column=1, sticky="w")
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
            choice("Fonte dell'uso", "source", ug.SOURCES)
            entry("Tetto extra al mese (0 = niente)", "extra_limit", lambda s: float(s.replace(",", ".")))
            entry("Finestre ignorate (regex)", "skip")
            entry("settings.json", "settings")
            spin("Pausa % (0 = spenta): messaggio alle chat al lavoro", "pause", 0, 100)
            spin("Poll veloce a meno di N punti dalla soglia (0 = mai)", "near", 0, 50)
            spin("Intervallo del poll veloce, s", "near_interval", 30, 3600, inc=15)
            check("Al reset manda continue alle chat in pausa o bloccate", "resume")
            note("Fonte auto: API OAuth (pochi secondi), se fallisce claude -p /usage.\n"
                 "Il blocco arriva anche alle sessioni aperte, e lo sblocco pure.\n"
                 "Oltre il limite si blocca solo a tetto extra raggiunto.\n"
                 "Pausa e continue passano dal messaging fra sessioni:\n"
                 "serve crossSessionInbound \"accept\" in settings.json.\n"
                 "Poll veloce sotto i 60 s: l'API risponde 429 (a 45 s dopo ~17 minuti).\n"
                 "Dopo un 429 si torna all'intervallo normale: claude -p usa la stessa API.")
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
        return tab

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

    # --- aggiornamenti ------------------------------------------------------------
    def update_tick(self, now):
        """Il controllo della versione nuova, in un thread; con auto anche l'installazione, che pero'
        aspetta la fine di blocchi e pause: dopo, il widget si riavvia."""
        mode = ug.update_config(self.cfg)["mode"]
        if mode == "off":
            self.update_tag = None
            return
        if self.update_busy or now - self.update_last < UPDATE_EVERY:
            return
        self.update_last, self.update_busy = now, True

        def work():
            try:
                self.update_tag = ug.check_update()
                if mode == "auto" and self.update_tag and ug.installed():
                    busy = ug.update_busy()
                    if busy:
                        ug.log("update", f"{self.update_tag} rimandata: {busy}")
                    else:
                        ug.install_release(self.update_tag)
                        self.restart_pending = True
            except Exception as e:  # rete, zip, disco: si riprova al prossimo giro
                self.cfg_err = f"aggiornamento: {e}"
                ug.log("update", f"ERRORE {e}")
            finally:
                self.update_busy = False
        threading.Thread(target=work, daemon=True).start()

    def update_now(self, check_only=False):
        """Dal menu installa la versione nuova; dalle Impostazioni (check_only) controlla subito."""
        if self.update_busy:
            return
        self.update_busy, self.update_last = True, time.time()
        tag = self.update_tag

        def work():
            try:
                if check_only:
                    self.update_tag = ug.check_update(force=True)
                else:
                    ug.install_release(tag)
                    self.restart_pending = True
            except Exception as e:
                self.cfg_err = f"aggiornamento: {e}"
                ug.log("update", f"ERRORE {e}")
            finally:
                self.update_busy = False
        threading.Thread(target=work, daemon=True).start()

    def restart(self):
        """Nuovo widget dalla copia installata, che aspetta l'uscita di questo per prendere il mutex."""
        ug.log("update", f"riavvio del widget su v{ug.__version__} installata")
        ug.launch_widget(after=os.getpid())
        self.closed = True  # il tick non si riprogramma su un Tk distrutto
        self.root.destroy()

    def update_text(self):
        st = self.state.get("update", {})
        checked = num(st.get("checked"))
        parts = [f"v{ug.__version__}, " + ("copia installata" if ug.installed() else "copia di sviluppo"), ug.HERE,
                 "ultimo controllo: " + (time.strftime("%d/%m %H:%M", time.localtime(checked)) if checked else "mai")
                 + (f", ultima release {st['latest']}" if st.get("latest") else "")]
        if st.get("err"):
            parts.append(f"errore: {st['err']}")
        return "\n".join(parts)

    def tick(self):
        # Il prossimo tick si programma comunque: un'eccezione qui congelava la riga
        # sull'ultimo valore, coi poll da menu che continuavano senza che si vedesse.
        try:
            if self.restart_pending:
                self.restart()
                return
            self.reload()
            now = time.time()
            self.update_tick(now)
            if self.update_label and self.update_label.winfo_exists():
                self.update_label.configure(text=self.update_text())
            self.draw_sessions()
            for name in ug.PROVIDERS:
                pc = self.cfg[name]
                if not pc["enabled"] or name in self.running:
                    continue  # spento: nessun controllo
                last = max(num(self.state.get(name, {}).get("poll_epoch")), self.last_run.get(name, 0))
                if now - last >= ug.interval(name, pc, self.state.get(name, {})):
                    self.run(name)
            self.render(now)
            if K32.WaitForSingleObject(self.show, 0) == 0:  # WAIT_OBJECT_0: script rilanciato
                # sul monitor del mouse: libero in alto a destra, agganciato allo stesso punto
                if self.p["dock"] == "free":
                    self.topleft = mouse_corner()
                else:
                    self.anchor, self.topleft = None, mouse_corner()
            self.place()  # il monitor cambia da solo: risoluzione, monitor staccato
            if self.cfg_err.startswith("errore widget"):
                self.cfg_err = ""  # tick riuscito: l'errore era di passaggio
        except Exception as e:
            self.crash(type(e), e, e.__traceback__)
            self.shown = None  # al prossimo giro la riga mostra l'errore
        finally:
            if not self.closed:
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
        if self.update_tag:
            rows.append([(f"aggiornamento {self.update_tag}: tasto destro, Aggiorna" if ug.installed() else
                          f"{self.update_tag} disponibile: python usage_guard.py --install", WARN, False)])
        if self.cfg_err:
            rows.append([(self.cfg_err[:80], WARN, False)])
        if (blocked, rows) == self.shown:
            return
        self.shown, self.rows = (blocked, rows), rows
        self.root.configure(bg=BG_BLOCK if blocked else BG)
        for w in self.frame.winfo_children():
            w.destroy()
        font, bold = ("Segoe UI", self.p["font"]), ("Segoe UI", self.p["font"], "bold")
        for segs in rows:
            line = tk.Frame(self.frame, bg=KEY)
            line.pack(anchor=TEXT_ANCHOR[self.p["text_align"]])
            for text, fg, strong in segs:
                tk.Label(line, text=text, fg=fg, bg=KEY, font=bold if strong else font,
                         padx=3, pady=0).pack(side="left")
        self.top.update_idletasks()
        self.place()  # lo sfondo segue la dimensione del testo
        self.draw_layout()  # e il disegno delle Impostazioni le righe vere


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


def wait_exit(pid, timeout_ms=20000):
    """Dopo un aggiornamento: aspetta l'uscita del widget vecchio, che tiene il mutex."""
    h = K32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        K32.WaitForSingleObject(h, timeout_ms)
        K32.CloseHandle(h)


if __name__ == "__main__":
    if "--after" in sys.argv[1:-1]:
        wait_exit(num(sys.argv[sys.argv.index("--after") + 1]))
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
