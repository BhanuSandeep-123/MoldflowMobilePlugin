"""
embedded_ui.py
---------------
A chrome-less, docked native panel that renders the plugin's workflow
prompts INSIDE the Moldflow Insight 2027 environment -- no browser tab, no
console window, no MessageBox.

Synergy's own COM API has no documented mechanism for hosting a custom pane
inside its window (checked: the full Synergy member list is Build,
ImportFile*, NewProject, OpenProject, Viewer, Silence, SetApplicationWindowPos,
Quit, ... -- nothing for docking a panel). This is the closest thing that is
actually achievable: a borderless tkinter window, styled as a panel rather
than an application, that tracks Synergy's main window and sits docked to its
right edge for as long as Synergy is open.

Contract with the rest of the plugin (see ui_bridge.py):
  - Every ~300ms this process touches ui_heartbeat.txt (its own file, so the
    heartbeat never rewrites — or clobbers — the shared state).
    ui_bridge.prompt_user()/request_project_form()/request_file_path() treat
    a stale heartbeat as "no embedded UI available" and fall back per their
    own policy (native dialog, or silent skip -- never decided in here).
  - It renders whatever is in state["pending_prompt"]:
      kind == "project_form" -> native Create-New-Project form (name +
          directory + Browse button using the OS folder picker).
      kind == "file_picker"  -> a Browse button that opens the OS "Open File"
          picker with the given filters.
      anything else          -> a generic message + option buttons (the
          Yes/No / Continue/Refine/etc. prompts used throughout the workflow).
    The chosen answer is written back to pending_prompt["answer"] and the
    frame is cleared; the very same file-based handshake ui_bridge already
    used before, just answered by this panel instead of a web page.

Exit behavior: if Synergy's window can't be found within STARTUP_TIMEOUT
seconds, this process logs why and exits(1) -- callers (ui_launcher.py)
already treat that as "embedding unavailable" and fall back silently, per
the plugin's silent-startup requirement. Once running, it keeps tracking
Synergy's window and exits cleanly the moment that window disappears.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ui_bridge
import session_context

LOG_FILE = HERE / "embedded_ui_log.txt"

STARTUP_TIMEOUT = 8.0   # seconds to find Synergy's window before giving up
TRACK_INTERVAL_MS = 300
PANEL_WIDTH = 480
HEADER_HEIGHT = 40      # also the panel's height when minimised
# Usable text width INSIDE a card body. The card sits in 12px window padding,
# has a 1px border, its labels use 14px padx, and the body reserves room for a
# scrollbar. A label wrapped wider than this does not just overflow: pack()
# centres a widget it cannot fit, pushing the LEFT edge off the panel, which is
# how "No geometry issues detected" came out as "o geometry issues detected".
CARD_TEXT_WIDTH = PANEL_WIDTH - 78

# Palette lifted from ui_design/Styles.xaml so the panel doesn't look like a
# stray tool window next to Moldflow's own UI.
COLOR_HEADER = "#383838"
COLOR_HEADER_HOVER = "#505050"
COLOR_PRIMARY = "#1A73E8"
COLOR_PRIMARY_HOVER = "#1557B0"
COLOR_BG = "#F8F9FA"
COLOR_CARD = "#FFFFFF"
COLOR_TEXT = "#202124"
COLOR_TEXT_MUTED = "#5F6368"
COLOR_BORDER = "#DADCE0"

# Prompt kinds that report progress instead of asking for something. See the
# _set_waiting call in _tick: these must not raise the "Waiting for your input"
# status line or the minimised header's pending badge.
NON_BLOCKING_KINDS = ("job_manager",)



def _vertical_scrollregion(canvas):
    """Scroll region for a card body: vertical only.

    Using canvas.bbox("all") verbatim lets any oversized child widen the
    region, which allows a horizontal offset and cuts text off at the LEFT
    edge. Cards never scroll sideways, so the region is pinned to the canvas
    width and the x origin is forced back to 0."""
    try:
        box = canvas.bbox("all")
        if not box:
            return
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), box[3]))
        canvas.xview_moveto(0)
    except Exception:
        pass


def log(message: str) -> None:
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Tagged per Synergy window -- two panels share this log file.
        key = session_context.session_key()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} [{key}] - {message}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Locate Synergy's main window (zero COM -- pure Win32, same family of
#  technique already used by moldflow_observer.py's shutdown probe).
# --------------------------------------------------------------------------- #

def _find_synergy_window(only_pid=None):
    """Return (hwnd, (x, y, w, h)) for the largest visible top-level window
    owned by synergy.exe, or None if no such window exists.

    ``only_pid`` restricts the search to ONE synergy.exe -- the one that owns
    this panel's process tree. Without it, two open Synergy windows both dock
    their panel to whichever window happens to be larger, leaving the other
    window with no UI and two panels stacked on one. The unrestricted search
    is kept as a fallback for the case where the owning PID can't be resolved
    (a panel started by hand rather than by Synergy's startup command)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    def _image_is_synergy(pid):
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower() == "synergy.exe"
            return False
        finally:
            kernel32.CloseHandle(h)

    best = {"hwnd": None, "area": -1, "rect": None}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            if only_pid is not None:
                if pid.value != only_pid:
                    return True
            elif not _image_is_synergy(pid.value):
                return True
            rect = wt.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            area = max(w, 0) * max(h, 0)
            if area > best["area"]:
                best.update(hwnd=hwnd, area=area, rect=(rect.left, rect.top, w, h))
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    if best["hwnd"] is None:
        return None
    return best["hwnd"], best["rect"]


def _window_still_valid(hwnd) -> bool:
    try:
        return bool(ctypes.windll.user32.IsWindow(hwnd))
    except Exception:
        return False


def _window_rect(hwnd):
    rect = wt.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)


# Values that mean "nothing has happened here yet" and must render as an empty
# journey row, not as a result. Two of them start with the same hourglass that
# ui_bridge.IN_PROGRESS_PREFIX uses, so they are checked FIRST everywhere --
# otherwise a placeholder left over from an older run would read as a stage
# that is actively underway.
INVALID_VALS = ["N/A", "Not Selected", "Not Imported", "Pending Generation",
                "⏳ Pending Generation", "⏳ Waiting for selection", "None"]


def _is_placeholder(val):
    return not val or not str(val).strip() or str(val).strip() in INVALID_VALS


# --------------------------------------------------------------------------- #
#  The panel itself
# --------------------------------------------------------------------------- #

class EmbeddedPanel:
    def __init__(self, root: tk.Tk, synergy_hwnd):
        self.root = root
        self.synergy_hwnd = synergy_hwnd
        self._last_rect = None
        self._current_prompt_sig = None  # detects when pending_prompt changes
        self._job_card = None            # live job card's kept widget refs

        # Manual placement. None = docked to Synergy's right edge (the
        # default). After the user drags the panel this holds its offset from
        # Synergy's top-left corner, NOT an absolute screen position: the panel
        # then keeps the spot the user chose *relative to Synergy*, so moving
        # or resizing Synergy still carries it along instead of stranding it.
        self._manual_offset = None
        self._manual_height = None
        self._drag = None
        self._collapsed = False
        # Highest panel-state command already applied (see _apply_panel_command).
        # Commands left behind by a previous run are dropped rather than
        # replayed, so a session never opens folded because the last one
        # happened to end during meshing.
        self._panel_cmd_seq = 0
        try:
            ui_bridge.clear_panel_command()
        except Exception:
            pass

        root.overrideredirect(True)   # no titlebar / borders -- reads as a panel, not an app
        root.configure(bg=COLOR_BG)
        try:
            root.wm_attributes("-toolwindow", True)  # keep it off the taskbar/alt-tab
        except Exception:
            pass
        root.attributes("-topmost", True)

        header = tk.Frame(root, bg=COLOR_HEADER, height=HEADER_HEIGHT,
                          cursor="fleur")
        header.pack(side="top", fill="x")
        header.pack_propagate(False)
        title = tk.Label(header, text="Uno_Moldflow Automation", bg=COLOR_HEADER,
                         fg="white", font=("Segoe UI", 11, "bold"), cursor="fleur")
        title.pack(side="left", padx=12)
        # Window controls. The panel is 480px of a Synergy window, so being
        # able to fold it down to its header matters more here than it would
        # for a normal tool window.
        def _hdr_btn(text, cmd, tip):
            b = tk.Button(header, text=text, command=cmd, bg=COLOR_HEADER,
                          fg="white", activebackground=COLOR_HEADER_HOVER,
                          activeforeground="white", relief="flat", bd=0,
                          font=("Segoe UI", 11), padx=8, pady=0,
                          highlightthickness=0, cursor="hand2")
            b.pack(side="right", padx=(0, 6))
            return b

        self._btn_max = _hdr_btn("□", self._maximize, "restore")
        self._btn_min = _hdr_btn("—", self._minimize, "minimise")
        self._dock_hint = tk.Label(header, text="drag to move",
                                   bg=COLOR_HEADER, fg="#B0B0B0",
                                   font=("Segoe UI", 7), cursor="fleur")
        self._dock_hint.pack(side="right", padx=10)

        # The header is the drag handle. overrideredirect() removed the OS
        # titlebar, so without this the panel could only ever sit where
        # _sync_geometry put it.
        for w in (header, title, self._dock_hint):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
            w.bind("<ButtonRelease-1>", self._drag_end)
            w.bind("<Double-Button-1>", self._redock)

        # Footer FIRST, body second. Pack hands out the cavity in packing
        # order, so whatever is packed last is what gets squeezed when the
        # content is taller than the panel. The body expands, so packing it
        # first let it claim the whole cavity and left the "Powered by" footer
        # to be crushed or drawn over the card -- which is exactly what the
        # taller cards (CAD diagnostics, process settings, results review) did
        # to it. Reserving the footer's strip up front is the same rule
        # _card_scaffold already relies on for the card's own action bar.
        footer = tk.Frame(root, bg=COLOR_BG)
        footer.pack(side="bottom", fill="x", pady=(10, 16))
        self._footer = footer

        tk.Label(footer, text="Powered by", bg=COLOR_BG, fg=COLOR_TEXT_MUTED,
                 font=("Segoe UI", 9, "italic")).pack(side="top", anchor="center")

        # Load and display logo image if logo.png exists in project folder
        self._load_footer_logo(footer)

        self.body = tk.Frame(root, bg=COLOR_BG)
        self.body.pack(side="top", fill="both", expand=True)

        self.status_label = tk.Label(self.body, text="Working automatically...",
                                      bg=COLOR_BG, fg=COLOR_TEXT_MUTED,
                                      font=("Segoe UI", 9), wraplength=PANEL_WIDTH - 24,
                                      justify="left")
        # Hidden until a prompt actually needs an answer (see _tick).

        self.prompt_frame = tk.Frame(self.body, bg=COLOR_CARD, highlightthickness=1,
                                      highlightbackground=COLOR_BORDER)
        # Packed/unpacked on demand -- only visible while there's a pending prompt.

        self.journey_frame = tk.Frame(self.body, bg=COLOR_CARD, highlightthickness=1,
                                       highlightbackground=COLOR_BORDER)
        self.journey_frame.pack(side="top", fill="x", padx=12, pady=(12, 12))
        self._last_journey_sig = None

        self._sync_geometry(force=True)
        self._tick()

    def _load_footer_logo(self, parent_frame):
        # Scan for logo file variations (handles Windows hidden extension logo.png.png)
        logo_path = None
        candidates = list(HERE.glob("*logo*")) + [HERE / "logo.png", HERE / "logo.jpg"]
        for p in candidates:
            if p.is_file() and p.suffix.lower() in [".png", ".jpg", ".jpeg", ".gif"]:
                logo_path = p
                break

        if logo_path:
            # Method 1: PIL high-quality resize
            try:
                from PIL import Image, ImageTk
                img = Image.open(logo_path)
                img.thumbnail((180, 60))
                self.logo_img = ImageTk.PhotoImage(img)
                tk.Label(parent_frame, image=self.logo_img, bg=COLOR_BG).pack(side="top", anchor="center", pady=(4, 0))
                return
            except Exception:
                pass

            # Method 2: Native Tkinter PhotoImage with subsample scaling
            try:
                img = tk.PhotoImage(file=str(logo_path))
                # Calculate scale factor so logo width fits ~180px
                scale = max(1, img.width() // 180)
                self.logo_img = img.subsample(scale) if scale > 1 else img
                tk.Label(parent_frame, image=self.logo_img, bg=COLOR_BG).pack(side="top", anchor="center", pady=(4, 0))
                return
            except Exception:
                pass

        # Fallback text if logo file is not found yet
        tk.Label(parent_frame, text="[ Your Logo Here ]", bg=COLOR_BG, fg=COLOR_PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack(side="top", anchor="center", pady=(4, 0))

    # ---- Fusion icon ---------------------------------------------------------
    #
    # The "Go back to Fusion" buttons are the one place in the panel where the
    # user is being asked to leave Synergy for a different application, so they
    # carry that application's own icon. It is read from the INSTALLED
    # Fusion360.exe rather than shipped as a picture of someone else's brand:
    # whatever Fusion build is on the machine is what the user will actually be
    # switched into, and there is nothing to keep in sync.
    #
    # Everything here is optional. No Fusion install, no PIL, no pywin32, an
    # icon that will not extract -- any of these just means the buttons keep
    # the "↩️" glyph they already had, which is why the callers pass the text
    # unchanged and only ADD the image when one exists.

    _FUSION_ICON_CACHE = HERE / "assets" / "fusion_app_icon.png"

    @staticmethod
    def _find_fusion_exe():
        """Path to Fusion360.exe, or None. Fusion installs per-user under a
        hashed webdeploy folder that changes with every update, so the folder
        is globbed rather than remembered; the newest one wins."""
        import glob
        roots = []
        for env in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                roots.append(Path(base) / "Autodesk" / "webdeploy" / "production")
        found = []
        for root in roots:
            try:
                found += glob.glob(str(root / "*" / "Fusion360.exe"))
            except Exception:
                continue
        if not found:
            return None
        try:
            found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except Exception:
            pass
        return found[0]

    def _extract_fusion_icon(self, size):
        """Render Fusion's application icon to a transparent PNG and return its
        path.

        DrawIcon paints onto an uninitialised bitmap whose alpha channel is
        meaningless, so the icon is drawn TWICE -- once over white, once over
        black -- and the real alpha recovered from the pair. A pixel of colour
        C at coverage a comes back as C*a + 255*(1-a) on white and C*a on
        black, so the difference between the two renders is 255*(1-a): the
        transparency falls straight out of it, and the colour with it.

        The alternative (filling with one flat colour and saving RGB) is
        simpler but wrong here -- these buttons have two different backgrounds,
        blue for the primary and near-white for the secondary, so any single
        fill leaves a visible patch behind the icon on one of them."""
        exe = self._find_fusion_exe()
        if not exe:
            return None
        try:
            import win32gui
            import win32ui
            from PIL import Image, ImageChops
        except Exception:
            return None

        large, small = [], []
        try:
            large, small = win32gui.ExtractIconEx(exe, 0)
            handles = large or small
            if not handles:
                return None
            # Draw at 64px and downsample: DrawIcon picks the closest stock
            # size, and shrinking a large one is far cleaner than asking the
            # 16px variant to grow.
            box = 64
            screen = win32gui.GetDC(0)
            hdc = win32ui.CreateDCFromHandle(screen)

            def render(fill):
                mdc = hdc.CreateCompatibleDC()
                bmp = win32ui.CreateBitmap()
                bmp.CreateCompatibleBitmap(hdc, box, box)
                mdc.SelectObject(bmp)
                mdc.FillSolidRect((0, 0, box, box), fill)
                mdc.DrawIcon((0, 0), handles[0])
                return Image.frombuffer(
                    "RGBA", (box, box), bmp.GetBitmapBits(True),
                    "raw", "BGRA", 0, 1).convert("RGB")

            on_white = render(0xFFFFFF)
            on_black = render(0x000000)

            # alpha = 255 - (white - black); un-multiply to recover the colour.
            diff = ImageChops.difference(on_white, on_black).convert("L")
            alpha = ImageChops.invert(diff)
            a = alpha.load()
            src = on_black.load()
            flat = Image.new("RGB", on_black.size)
            dst = flat.load()
            for y in range(box):
                for x in range(box):
                    av = a[x, y]
                    if av <= 0:
                        continue
                    r, g, b = src[x, y]
                    dst[x, y] = (min(255, r * 255 // av),
                                 min(255, g * 255 // av),
                                 min(255, b * 255 // av))
            img = flat.convert("RGBA")
            img.putalpha(alpha)

            # DrawIcon renders the closest stock size into the top-left of the
            # box, so a 48px icon in a 64px surface leaves a transparent band
            # down two sides. Trim back to the mark itself, otherwise the
            # padding is scaled down with it and the icon reads as much smaller
            # than the space it occupies on the button.
            bbox = alpha.getbbox()
            if bbox:
                img = img.crop(bbox)
            # thumbnail(), not resize(): the crop above is only square if the
            # icon's artwork happens to be, and forcing a square would squash it.
            img.thumbnail((int(size), int(size)), Image.LANCZOS)
            self._FUSION_ICON_CACHE.parent.mkdir(parents=True, exist_ok=True)
            img.save(str(self._FUSION_ICON_CACHE))
            return self._FUSION_ICON_CACHE
        except Exception as e:
            log(f"Could not extract the Fusion icon from {exe}: {e}")
            return None
        finally:
            for h in list(large) + list(small):
                try:
                    win32gui.DestroyIcon(h)
                except Exception:
                    pass

    def _fusion_icon(self, size=20):
        """A tk image of Fusion's icon, or None. Cached on the instance —
        a PhotoImage that nothing holds a reference to is collected and the
        button silently loses its picture."""
        cached = getattr(self, "_fusion_icon_img", "unset")
        if cached != "unset":
            return cached

        self._fusion_icon_img = None
        try:
            from PIL import Image, ImageTk
            path = self._FUSION_ICON_CACHE
            if not path.is_file():
                path = self._extract_fusion_icon(size * 3)
            if path and Path(path).is_file():
                img = Image.open(str(path))
                img.thumbnail((int(size), int(size)))
                self._fusion_icon_img = ImageTk.PhotoImage(img)
        except Exception as e:
            log(f"Fusion icon unavailable: {e}")
        return self._fusion_icon_img

    def _fusion_button(self, parent, text, primary, command):
        """A 'go to Fusion' button carrying Fusion's own icon on its left.

        Falls back to the plain text button (with its "↩️" glyph) whenever the
        icon cannot be produced, so the layout and behaviour are exactly what
        they were before on a machine without Fusion installed."""
        icon = self._fusion_icon()
        style = dict(
            bg=COLOR_PRIMARY if primary else "#F1F5F9",
            fg="white" if primary else COLOR_TEXT,
            activebackground=COLOR_PRIMARY_HOVER if primary else "#E2E8F0",
            relief="flat", font=("Segoe UI", 8, "bold"),
            padx=12, pady=6, anchor="w", command=command,
        )
        if primary:
            style["activeforeground"] = "white"
        if icon is None:
            return tk.Button(parent, text=text, **style)
        # The glyph was standing in for exactly this icon; showing both reads
        # as two separate marks on one button.
        return tk.Button(parent, text=text.replace("↩️ ", ""), image=icon,
                         compound="left", **{**style, "padx": 10})

    # ---- geometry tracking -------------------------------------------------

    def _sync_geometry(self, force=False):
        # Never reposition mid-drag -- the tick runs 3x a second and would
        # yank the panel back out from under the pointer.
        if self._drag is not None:
            return
        rect = _window_rect(self.synergy_hwnd)
        if rect is None:
            return
        if not force and rect == self._last_rect:
            return
        self._last_rect = rect
        sx, sy, sw, sh = rect
        h = HEADER_HEIGHT if self._collapsed else max(sh, 200)
        if self._manual_offset is None:
            x = sx + sw - PANEL_WIDTH          # docked to the right edge
            y = sy
        else:
            dx, dy = self._manual_offset
            x, y = sx + dx, sy + dy
            if not self._collapsed:
                h = self._manual_height or h
            x, y = self._clamp_to_screen(x, y)
        self.root.geometry(f"{PANEL_WIDTH}x{h}+{x}+{y}")

    # ---- moving the panel ----------------------------------------------------

    def _clamp_to_screen(self, x, y):
        """Keep at least the header reachable. A panel dragged past an edge --
        or carried there by Synergy being moved -- must never end up somewhere
        the user cannot grab it again."""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return x, y
        x = max(-PANEL_WIDTH + 80, min(x, sw - 80))
        y = max(0, min(y, sh - 40))
        return x, y

    def _drag_start(self, event):
        self._drag = (event.x_root, event.y_root,
                      self.root.winfo_x(), self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag is None:
            return
        px, py, wx, wy = self._drag
        x = wx + (event.x_root - px)
        y = wy + (event.y_root - py)
        h = self.root.winfo_height()
        x, y = self._clamp_to_screen(x, y)
        self.root.geometry(f"{PANEL_WIDTH}x{h}+{x}+{y}")

    def _drag_end(self, _event):
        if self._drag is None:
            return
        self._drag = None
        rect = _window_rect(self.synergy_hwnd)
        if rect is None:
            return
        sx, sy, _sw, _sh = rect
        self._manual_offset = (self.root.winfo_x() - sx,
                               self.root.winfo_y() - sy)
        self._manual_height = self.root.winfo_height()
        self._last_rect = rect
        log("Panel moved to offset {0} from Synergy's window.".format(
            self._manual_offset))

    def _redock(self, _event=None):
        """Double-click the header: back to the docked right-edge position."""
        self._drag = None
        self._manual_offset = None
        self._manual_height = None
        self._sync_geometry(force=True)
        log("Panel re-docked to Synergy's right edge.")

    # ---- minimise / maximise -------------------------------------------------

    def _minimize(self):
        """Fold the panel down to its header, leaving Synergy the full window.

        The process keeps running -- heartbeat, prompt polling and the workflow
        are untouched -- so a prompt raised while minimised is still waiting
        when the panel is restored. The header stays visible on purpose: an
        overrideredirect window has no taskbar entry, so hiding it completely
        would leave nothing to click."""
        if self._collapsed:
            return
        self._collapsed = True
        self.body.pack_forget()
        self._footer.pack_forget()
        self._apply_collapsed_geometry()
        # Reflect the pending state immediately rather than waiting for the
        # next tick -- if the panel is folded away over a live prompt, the
        # badge is the only thing telling the user the workflow is blocked.
        try:
            pending = ui_bridge.load_state().get("pending_prompt")
            self._pending_badge(bool(pending) and pending.get("answer") is None)
        except Exception:
            pass
        log("Panel minimised to its header.")

    def _maximize(self):
        """Restore the panel to full height (and re-dock if it was minimised
        from a dragged position, keeping that position)."""
        if not self._collapsed:
            # Already open: make it fill Synergy's height again.
            self._manual_height = None
            self._sync_geometry(force=True)
            return
        self._collapsed = False
        # Footer before body, for the reason given where they are created:
        # re-packing restarts the allocation order, so restoring in the other
        # order would reintroduce the overlap every time the panel was folded
        # and opened again.
        self._footer.pack(side="bottom", fill="x", pady=(10, 16))
        self.body.pack(side="top", fill="both", expand=True)
        self._sync_geometry(force=True)
        # The card itself is visible again, so the header badge has nothing
        # left to announce.
        self._pending_badge(False)
        log("Panel restored.")

    def _apply_panel_command(self):
        """Honour a minimise/restore asked for by the workflow process.

        Only acts on a state that differs from the current one, so a repeated
        request is a no-op and the user's own header clicks are never fought
        over: if they reopen the panel during meshing it stays open, because
        the next command only arrives when the mesh ends."""
        try:
            cmd = ui_bridge.poll_panel_command(self._panel_cmd_seq)
        except Exception:
            return
        if not cmd:
            return
        seq, want = cmd
        self._panel_cmd_seq = seq
        if want == "minimized":
            self._minimize()
        else:
            self._maximize()

    def _apply_collapsed_geometry(self):
        x, y = self.root.winfo_x(), self.root.winfo_y()
        self.root.geometry(f"{PANEL_WIDTH}x{HEADER_HEIGHT}+{x}+{y}")

    def _set_waiting(self, waiting, busy_message=None):
        """Drive the status line above the journey card.

        A prompt on screen wins ("Waiting for your input"). Otherwise, if a
        step is underway, say WHICH -- the panel used to go completely silent
        between one stage finishing and the next one's window appearing, which
        is exactly when the user needs to be told the automation is still
        working."""
        try:
            text = "Waiting for your input" if waiting else busy_message
            if text:
                self.status_label.configure(text=text)
                if not self.status_label.winfo_ismapped():
                    self.status_label.pack(side="top", anchor="w", padx=12,
                                           pady=(12, 0), before=self.journey_frame)
            elif self.status_label.winfo_ismapped():
                self.status_label.pack_forget()
        except Exception:
            pass
        self._pending_badge(waiting)

    def _pending_badge(self, waiting):
        """While minimised there is no card to see, so the header itself has to
        say that the workflow is blocked on the user."""
        try:
            if waiting and self._collapsed:
                self._btn_min.configure(text="!", fg="#FFE082")
            else:
                self._btn_min.configure(text="—", fg="white")
        except Exception:
            pass

    # ---- prompt rendering ---------------------------------------------------

    def _clear_prompt_ui(self):
        for w in self.prompt_frame.winfo_children():
            w.destroy()
        self.prompt_frame.pack_forget()
        self._card_parts = None
        # The job card's kept widget references are dead now; holding them
        # would let _job_card_reusable hand out destroyed widgets.
        self._job_card = None

    # ---- card scaffold -------------------------------------------------------
    #
    # Every prompt is built as: a SCROLLING body + an action bar pinned to the
    # bottom of the card. This is not cosmetic. The panel is exactly as tall as
    # Synergy's window and has no scrollbar of its own, so any card whose
    # content grew past that height simply had its lower part cut off -- and
    # the lower part is where the buttons are. That is why the "Workflow
    # Complete" summary had no reachable OK, and why expanding "Show Details"
    # on the mesh-diagnostics card hid Continue.
    #
    # The action bar is packed FIRST with side="bottom", so Tk reserves its
    # space before the body gets any; the body then takes what is left and
    # scrolls internally when it needs more. Buttons can no longer be pushed
    # anywhere.

    def _card_space(self):
        """Pixels the card's SCROLLING BODY may occupy without pushing anything
        off the panel.

        Measured, not estimated. The previous version subtracted a fixed 110
        for the footer and a blanket 90 for padding from the whole window, and
        that arithmetic was wrong in three ways at once: the footer is 99px
        with a logo and a different height without one, the "Waiting for your
        input" status line was not subtracted at all even though it is only
        ever visible WHILE a card is up, and the card's own action bar,
        separator and padding were not subtracted either -- so every card was
        allowed to be roughly 60-80px taller than the space it actually had.

        Working from self.body's real allocation removes the header and footer
        from the sum entirely: with the footer's strip reserved first (see
        __init__), body's height already IS what is left for the card."""
        try:
            self.root.update_idletasks()
            avail = self.body.winfo_height()
            if avail <= 1:                      # not mapped yet
                return 420

            # Siblings the card shares `body` with, plus their pack padding.
            if self.status_label.winfo_ismapped():
                avail -= self.status_label.winfo_height() + 12
            if self.journey_frame.winfo_ismapped():
                avail -= self.journey_frame.winfo_height() + 24
            avail -= 24                          # prompt_frame's own pady(12, 12)

            # The card's furniture below the scrolling body: the action bar,
            # its padding and the separator above it.
            parts = getattr(self, "_card_parts", None)
            if parts:
                actions, sep = parts[4], parts[3]
                for w in (actions, sep):
                    try:
                        avail -= max(w.winfo_height(), w.winfo_reqheight())
                    except Exception:
                        pass
                avail -= 18                      # actions' pady(6, 12)

            # A floor so a very tall journey summary cannot collapse the card to
            # nothing; below this the body scrolls instead.
            return max(120, avail)
        except Exception:
            return 420

    def _card_scaffold(self):
        """Start a new card. Returns (body, actions):
             body     -- put the content here; it scrolls if it gets tall.
             actions  -- put the buttons here; always visible."""
        self._clear_prompt_ui()

        actions = tk.Frame(self.prompt_frame, bg=COLOR_CARD)
        actions.pack(side="bottom", fill="x", padx=14, pady=(6, 12))
        sep = tk.Frame(self.prompt_frame, bg=COLOR_BORDER, height=1)
        sep.pack(side="bottom", fill="x")

        holder = tk.Frame(self.prompt_frame, bg=COLOR_CARD)
        holder.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=COLOR_CARD, highlightthickness=0)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        body = tk.Frame(canvas, bg=COLOR_CARD)
        win = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)

        body.bind("<Configure>",
                  lambda _e, c=canvas: _vertical_scrollregion(c))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))

        def _wheel(event):
            canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _wheel)   # rebound per card; harmless

        # `actions` is kept so _card_space can subtract the real height of the
        # button bar rather than assume one.
        self._card_parts = (canvas, bar, body, sep, actions)
        return body, actions

    def _resize_card(self):
        """Re-measure the body and show/hide its scrollbar. Call after anything
        that changes the card's height (Show Details, a growing list)."""
        if not getattr(self, "_card_parts", None):
            return
        canvas, bar, body, _sep, _actions = self._card_parts
        try:
            self.root.update_idletasks()
            need = body.winfo_reqheight()
            avail = self._card_space()
            canvas.configure(height=min(need, avail))
            if need > avail:
                bar.pack(side="right", fill="y")
            else:
                bar.pack_forget()
        except Exception:
            pass

    def _finish_card(self):
        """Show the card and size its body. Every _show_* ends with this."""
        self.prompt_frame.pack(side="top", fill="x", padx=12, pady=12)
        self._resize_card()

    def _show_generic_prompt(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", ""), bg=COLOR_CARD,
                 fg=COLOR_TEXT, font=("Segoe UI", 10, "bold"),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", ""), bg=COLOR_CARD,
                 fg=COLOR_TEXT_MUTED, font=("Segoe UI", 9),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 12))

        options = prompt.get("options") or ["Yes", "No"]
        if len(options) > 3 or any(len(str(o)) > 15 for o in options):
            # Vertical layout for multi-choice or long-label lists (e.g. sequence selection)
            for opt in options:
                tk.Button(actions, text=opt, bg=COLOR_PRIMARY, fg="white",
                          activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                          relief="flat", font=("Segoe UI", 9), padx=10, pady=4, anchor="w",
                          command=lambda o=opt: self._answer(o)).pack(fill="x", pady=2)
        else:
            # Horizontal row for simple Yes/No/OK choices
            for opt in reversed(options):
                tk.Button(actions, text=opt, bg=COLOR_PRIMARY, fg="white",
                          activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                          relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                          command=lambda o=opt: self._answer(o)).pack(
                    side="right", padx=(6, 0))
        self._finish_card()

    def _show_project_form(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", "Create New Project"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", ""), bg=COLOR_CARD,
                 fg=COLOR_TEXT_MUTED, font=("Segoe UI", 9),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 10))

        tk.Label(body, text="Project name", bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(0, 2))
        name_var = tk.StringVar(value=prompt.get("default_name") or "MyProject")
        name_entry = tk.Entry(body, textvariable=name_var, font=("Segoe UI", 9),
                              relief="solid", bd=1, highlightthickness=0)
        name_entry.pack(fill="x", padx=14, pady=(0, 10), ipady=4)

        tk.Label(body, text="Location", bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=14, pady=(0, 2))
        default_dir = prompt.get("default_dir") or str(Path.home() / "Documents" / "Moldflow Projects")
        dir_var = tk.StringVar(value=default_dir)
        dir_row = tk.Frame(body, bg=COLOR_CARD)
        dir_row.pack(fill="x", padx=14, pady=(0, 12))
        dir_entry = tk.Entry(dir_row, textvariable=dir_var, font=("Segoe UI", 9),
                             relief="solid", bd=1, highlightthickness=0)
        dir_entry.pack(side="left", fill="x", expand=True, ipady=4)

        def browse_dir():
            chosen = filedialog.askdirectory(initialdir=dir_var.get() or str(Path.home()),
                                              title="Select project location",
                                              parent=self.root)
            if chosen:
                dir_var.set(chosen)

        tk.Button(dir_row, text="Browse...", command=browse_dir, relief="flat",
                  bg="#E8F0FE", fg=COLOR_PRIMARY, activebackground="#D2E3FC",
                  font=("Segoe UI", 8, "bold"), padx=10, pady=3).pack(side="left", padx=(8, 0))

        # There is no Skip here: a project is the root of everything that
        # follows (import, mesh, solve, report), so the only way forward is to
        # create one. Submitting with a blank name or location would produce
        # exactly the same dead end Skip did, so it is refused inline instead
        # of being sent to the workflow.
        warn = tk.Label(body, text="", bg=COLOR_CARD, fg="#D32F2F",
                        font=("Segoe UI", 8), wraplength=CARD_TEXT_WIDTH,
                        justify="left")

        def submit():
            name = name_var.get().strip()
            directory = dir_var.get().strip()
            if not name or not directory:
                warn.configure(text="Enter a project name and a location to continue.")
                if not warn.winfo_ismapped():
                    warn.pack(anchor="w", padx=14, pady=(0, 8))
                self._resize_card()
                return
            self._answer({"name": name, "dir": directory})

        tk.Button(actions, text="Create", bg=COLOR_PRIMARY, fg="white",
                  activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                  command=submit).pack(side="right")
        name_entry.bind("<Return>", lambda _e: submit())
        dir_entry.bind("<Return>", lambda _e: submit())
        self._finish_card()

    def _show_file_picker(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", "Import Model"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", ""), bg=COLOR_CARD,
                 fg=COLOR_TEXT_MUTED, font=("Segoe UI", 9),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 12))

        filters = prompt.get("filters") or [("All files", "*.*")]

        def browse_file():
            chosen = filedialog.askopenfilename(title=prompt.get("title", "Import Model"),
                                                 filetypes=filters, parent=self.root)
            if chosen:
                self._answer(chosen)

        tk.Button(actions, text="Skip", relief="flat", font=("Segoe UI", 9),
                  padx=10, pady=4, command=lambda: self._answer("Skip")).pack(
            side="right", padx=(6, 0))
        tk.Button(actions, text="Browse...", bg=COLOR_PRIMARY, fg="white",
                  activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                  command=browse_file).pack(side="right")
        self._finish_card()

    def _show_cad_diagnostics(self, prompt):
        body, actions = self._card_scaffold()

        report = prompt.get("report") or {}
        LABELS = [
            ("edge_edge_intersections", "Edge-edge intersections"),
            ("face_face_intersections", "Face-face intersections"),
            ("edge_self_intersections", "Edge self-intersections"),
            ("face_self_intersections", "Face self-intersections"),
            ("non_manifold_bodies", "Non-manifold bodies"),
            ("non_manifold_edges", "Non-manifold edges"),
            ("toxic_bodies", "Toxic bodies"),
            ("sliver_faces", "Sliver faces"),
        ]

        compute_ok = report.get("_compute_ok", True) if isinstance(report, dict) else True
        total_issues = sum(
            int((report.get(k) or {}).get("count", 0)) if isinstance(report.get(k), dict) else 0
            for k, _ in LABELS
        )
        # A check whose API call failed reports count 0, which is NOT the same
        # as "no issues" -- it means the check never ran. Treating those as
        # clean is exactly how the panel came to show "No issues were found"
        # for a model that visibly has problems.
        failed_checks = [
            label for k, label in LABELS
            if isinstance(report.get(k), dict) and not report[k].get("success", False)
        ]

        # Two mutually exclusive flows: a clean model gets the Yes/No automation
        # prompt; a failed compute or any DETECTED issue routes to Model Error
        # Recovery instead -- never both.
        #
        # `failed_checks` must NOT force the error path. The four checks
        # (non-manifold bodies, non-manifold edges, toxic bodies, sliver faces)
        # do not exist in this Synergy build's API at all -- every casing
        # variant returns "Unknown name" -- so failed_checks is non-empty for
        # EVERY model, forever. Including it here meant a perfectly clean model
        # (0 issues, compute OK) was still headlined "Model Error detected" and
        # offered "Go back to Fusion", which sent an undamaged part through the
        # Fusion round trip; the trip returns geometry with one edge-edge
        # intersection, so the next pass DID show a problem and offered Fusion
        # again -- a loop the model itself never justified. Verified 2026-07-28:
        # Part_study reported 0 issues / 0 groups immediately before each trip.
        #
        # An unavailable API is a property of the Synergy build, not of the
        # user's geometry. It is still surfaced (the 'Not checked' rows and the
        # note below), but it no longer triggers a repair the model never needed.
        has_problem = (not compute_ok) or total_issues > 0

        if has_problem:
            title = "Model Error Recovery"
            if not compute_ok:
                message = ("CAD Diagnostic / Model import error detected.\n"
                           "Details: CAD Diagnostics could not resolve any CAD bodies "
                           "to check -- the results below are not meaningful.")
            elif total_issues > 0 and failed_checks:
                message = ("CAD Diagnostic / Model import error detected.\n"
                           "Details: {0} geometry issue group(s) found, and {1} check(s) "
                           "could not be run at all ({2}) -- the real total may be "
                           "higher.").format(total_issues, len(failed_checks),
                                             ", ".join(failed_checks))
            else:
                message = ("CAD Diagnostic / Model import error detected.\n"
                           "Details: CAD Diagnostics detected {0} geometry issue "
                           "group(s) in the areas listed below.").format(total_issues)
            title_color = "#D32F2F"
        else:
            title = prompt.get("title", "CAD Diagnostics Summary")
            message = prompt.get("message", "Review CAD geometry diagnostics below:")
            if failed_checks:
                # Kept to two lines on purpose: the panel has no scrollbar, and
                # a taller message pushes the buttons off the visible area. The
                # per-row "Not checked" markers already name which ones.
                message = ("No geometry issues detected. Note: {0} check(s) are not "
                           "available in this Synergy build -- shown as 'Not "
                           "checked', result UNKNOWN.").format(len(failed_checks))
            title_color = COLOR_TEXT

        tk.Label(body, text=title, bg=COLOR_CARD, fg=title_color,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=message, bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                 font=("Segoe UI", 8), wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        grid_frame = tk.Frame(body, bg=COLOR_CARD)
        grid_frame.pack(fill="x", padx=14, pady=(0, 10))

        for key, label in LABELS:
            row = tk.Frame(grid_frame, bg=COLOR_CARD)
            row.pack(fill="x", pady=1)
            tk.Label(row, text=label, bg=COLOR_CARD, fg=COLOR_TEXT,
                     font=("Segoe UI", 8)).pack(side="left")
            item = report.get(key) if isinstance(report, dict) else None
            count = int(item.get("count", 0)) if isinstance(item, dict) else 0
            check_ran = isinstance(item, dict) and item.get("success", False)
            if not compute_ok or not check_ran:
                # "Not run" must never look like "clean".
                tk.Label(row, text="Not checked", bg=COLOR_CARD, fg="#B45309",
                         font=("Segoe UI", 8, "bold")).pack(side="right")
            elif count > 0:
                tk.Label(row, text=f"{count} issue(s)", bg=COLOR_CARD, fg="#D32F2F",
                         font=("Segoe UI", 8, "bold")).pack(side="right")
            else:
                tk.Label(row, text="No issues", bg=COLOR_CARD, fg="#2E7D32",
                         font=("Segoe UI", 8)).pack(side="right")

        # "Show Details" only matters for the error-recovery flow -- it lists
        # the exact affected geometry (body/edge/face IDs and coordinates) so
        # the user knows what to look for before picking a recovery option.
        detail_text = prompt.get("detail_text")
        if has_problem and detail_text:
            detail_frame = tk.Frame(body, bg=COLOR_CARD)
            detail_box_ref = [None]

            def toggle_details():
                if detail_box_ref[0] is None:
                    txt = tk.Text(detail_frame, height=8, font=("Consolas", 8),
                                  bg="#F8F9FA", fg=COLOR_TEXT, relief="solid", bd=1)
                    txt.insert("1.0", detail_text)
                    txt.config(state="disabled")
                    txt.pack(fill="x", pady=(2, 0))
                    detail_box_ref[0] = txt
                    toggle_btn.config(text="Hide Details")
                else:
                    detail_box_ref[0].destroy()
                    detail_box_ref[0] = None
                    toggle_btn.config(text="Show Details")
                # The card just changed height: re-measure so the body scrolls
                # instead of growing past the panel and taking the buttons
                # with it.
                self._resize_card()

            toggle_btn = tk.Button(body, text="Show Details", fg="#1976D2",
                                   bg=COLOR_CARD, relief="flat", font=("Segoe UI", 8, "underline"),
                                   activebackground=COLOR_CARD, command=toggle_details)
            toggle_btn.pack(anchor="w", padx=14, pady=(0, 4))
            detail_frame.pack(fill="x", padx=14, pady=(0, 6))

        if not has_problem:
            # Scenario 1: nothing DETECTED -- Yes/No to start automation.
            #
            # Do NOT claim the model is clean. Moldflow's CADDiagnostic and
            # Fusion's Validate check different things, and the four checks
            # missing from this Synergy build (non-manifold bodies, non-manifold
            # edges, toxic bodies, sliver faces) are exactly the categories
            # Fusion typically reports. A part that Fusion calls faulty can
            # legitimately produce 0 issues here -- not because it is sound, but
            # because the checks that would have caught it cannot run. Saying
            # "No issues were found" in that situation is simply false, and it
            # is the kind of false reassurance that sends a bad model into a
            # two-hour solve.
            ran = len(LABELS) - len(failed_checks)
            if failed_checks:
                headline = ("No issues found in the {0} check(s) that ran.\n"
                            "{1} check(s) could NOT run on this Synergy build -- those "
                            "are UNKNOWN, not clean. Start the automation workflow?"
                            ).format(ran, len(failed_checks))
            else:
                headline = ("CAD Diagnostics completed successfully. No issues were found.\n"
                            "Do you want to start the analysis automation workflow?")

            tk.Label(body, text=headline,
                     bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 8, "bold"),
                     wraplength=CARD_TEXT_WIDTH, justify="left").pack(
                anchor="w", padx=14, pady=(6, 10))

            btn_row = tk.Frame(actions, bg=COLOR_CARD)
            btn_row.pack(anchor="e", padx=14, pady=(0, 6))
            tk.Button(btn_row, text="No", relief="flat", font=("Segoe UI", 9),
                      padx=12, pady=4, command=lambda: self._answer("No")).pack(
                side="right", padx=(6, 0))
            tk.Button(btn_row, text="Yes", bg=COLOR_PRIMARY, fg="white",
                      activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                      relief="flat", font=("Segoe UI", 9), padx=12, pady=4,
                      command=lambda: self._answer("Yes")).pack(side="right")

            # Keep the Fusion route reachable. Removing the forced trip must not
            # remove the CHOICE -- if Fusion's own Validate flagged the part, the
            # user knows something this build's API cannot measure, and they need
            # a way to act on it. Secondary styling: available, not urged.
            self._fusion_button(
                actions, "↩️ Repair in Fusion anyway (Fusion found errors)",
                primary=False,
                command=lambda: self._answer("Go back to Fusion to fix model")
            ).pack(fill="x", padx=14, pady=(0, 14))
        else:
            # Scenario 2: compute failed or issues found -- recovery options only,
            # never the Yes/No prompt.
            tk.Label(body, text="Select recovery option:", bg=COLOR_CARD,
                     fg=COLOR_TEXT, font=("Segoe UI", 8, "bold")).pack(
                anchor="w", padx=14, pady=(0, 8))

            btn_box = tk.Frame(actions, bg=COLOR_CARD)
            btn_box.pack(fill="x", padx=14, pady=(0, 14))

            # "Go back to Fusion" is FIRST and primary because it is the option
            # that actually works: it drives Modeler.ModifiedWithInventorFusion,
            # which is documented and now verified end to end. "Translate
            # Surface" probes for TranslateSurface/RepairGeometry, and neither
            # appears anywhere in the Synergy API reference -- it has never
            # succeeded. It stays as a secondary option rather than being
            # removed, but it must not sit above the working one.
            self._fusion_button(
                btn_box, "↩️ Go back to Fusion (Fix Model in CAD)", primary=True,
                command=lambda: self._answer("Go back to Fusion to fix model")
            ).pack(fill="x", pady=(0, 2))

            # Caption under the button rather than a second graphic: the icon is
            # what makes the destination recognisable at a glance, and this says
            # in words what it means -- that clicking leaves Synergy. Sized and
            # padded to sit inside the existing button block, so nothing below
            # it moves.
            if self._fusion_icon() is not None:
                tk.Label(btn_box, text="Opens the model in Autodesk Fusion",
                         bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                         font=("Segoe UI", 7)).pack(anchor="w", padx=2, pady=(0, 6))
            else:
                tk.Frame(btn_box, bg=COLOR_CARD, height=4).pack(fill="x")

            tk.Button(btn_box, text="🔄 Translate Surface (Attempt Automatic Repair)",
                      bg="#F1F5F9", fg=COLOR_TEXT, activebackground="#E2E8F0",
                      relief="flat", font=("Segoe UI", 8, "bold"), padx=12, pady=6, anchor="w",
                      command=lambda: self._answer("Translate Surface")).pack(fill="x")

        self._finish_card()

    def _show_mesh_diagnostics(self, prompt):
        """Full Mesh Diagnostics dialog: every value collect_mesh_diagnostics()
        read from the API, in a scrollable table, plus the decision buttons.

        This replaces the one-line "Diagnostics passed / Click Continue"
        generic prompt. The workflow is blocked until one of these buttons is
        pressed, and ui_bridge only accepts that answer after this method has
        acknowledged (via pending_prompt["rendered"]) that the table exists and
        has rows -- so 'shown' can never again mean 'a message was published'.
        """
        body, actions = self._card_scaffold()

        report = prompt.get("report") or {}
        status = str(report.get("status", "Failed"))
        items = report.get("items") or []
        counts = report.get("counts") or {}
        SEV_COLOR = {"info": COLOR_TEXT, "warning": "#B45309", "error": "#D32F2F"}
        head_color = {"Passed": "#2E7D32", "Warning": "#B45309"}.get(status, "#D32F2F")

        tk.Label(body, text="Mesh Diagnostics ({0})".format(status),
                 bg=COLOR_CARD, fg=head_color, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))

        subtitle = "Mesh type: {0}    |    Total elements: {1}    |    {2} value(s) read".format(
            report.get("mesh_type") or "n/a", report.get("total_elements", 0), len(items))
        tk.Label(body, text=subtitle, bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                 font=("Segoe UI", 8), wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 6))
        if counts.get("warning") or counts.get("error"):
            tk.Label(body,
                     text="{0} error(s), {1} warning(s) — see the highlighted rows.".format(
                         counts.get("error", 0), counts.get("warning", 0)),
                     bg=COLOR_CARD, fg=head_color, font=("Segoe UI", 8, "bold"),
                     wraplength=CARD_TEXT_WIDTH, justify="left").pack(
                anchor="w", padx=14, pady=(0, 6))

        # --- the diagnostics table (scrollable: ~27 rows never fit the panel) ---
        table_host = tk.Frame(body, bg=COLOR_CARD, highlightthickness=1,
                              highlightbackground=COLOR_BORDER)
        table_host.pack(fill="x", padx=14, pady=(0, 8))

        table_h = 150 if len(items) > 7 else max(28 * max(len(items), 1), 30)
        canvas = tk.Canvas(table_host, bg=COLOR_CARD, highlightthickness=0,
                           height=table_h)
        vbar = tk.Scrollbar(table_host, orient="vertical", command=canvas.yview)
        rows_frame = tk.Frame(canvas, bg=COLOR_CARD)
        rows_frame.bind("<Configure>",
                        lambda _e, c=canvas: _vertical_scrollregion(c))
        win = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        def _on_wheel(event):
            canvas.yview_scroll(-1 * int(event.delta / 120), "units")
        for w in (canvas, rows_frame):
            w.bind("<MouseWheel>", _on_wheel)

        rows_drawn = 0
        for it in items:
            value = it.get("value")
            if isinstance(value, float):
                vtxt = "{0:.3f}".format(value).rstrip("0").rstrip(".")
            else:
                vtxt = str(value)
            sev = it.get("severity", "info")
            row = tk.Frame(rows_frame, bg=COLOR_CARD)
            row.pack(fill="x", padx=8, pady=1)
            row.bind("<MouseWheel>", _on_wheel)
            tk.Label(row, text=str(it.get("label", it.get("name", ""))), bg=COLOR_CARD,
                     fg=COLOR_TEXT, font=("Segoe UI", 8), anchor="w").pack(side="left")
            tk.Label(row, text=vtxt, bg=COLOR_CARD, fg=SEV_COLOR.get(sev, COLOR_TEXT),
                     font=("Segoe UI", 8, "bold" if sev != "info" else "normal")).pack(
                side="right")
            rows_drawn += 1
            if it.get("note"):
                note = tk.Label(rows_frame, text="    {0}".format(it["note"]), bg=COLOR_CARD,
                                fg=SEV_COLOR.get(sev, COLOR_TEXT_MUTED),
                                font=("Segoe UI", 7), anchor="w",
                                wraplength=CARD_TEXT_WIDTH, justify="left")
                note.pack(fill="x", padx=8)
                note.bind("<MouseWheel>", _on_wheel)

        if not items:
            tk.Label(rows_frame, text="No diagnostic values were returned by the API.",
                     bg=COLOR_CARD, fg="#D32F2F", font=("Segoe UI", 8, "bold")).pack(
                anchor="w", padx=8, pady=4)

        # No "Show Details" here on purpose: the scrolling table above already
        # carries every value the API returned, so the toggle only duplicated
        # them. Anything the API did NOT expose is still noted below, and the
        # full text remains in mesh_diagnostics_report.json / diagnostics_log.
        unavailable = report.get("unavailable") or []
        if unavailable:
            tk.Label(body,
                     text="Not exposed by this Moldflow API version: {0}".format(
                         ", ".join(str(u) for u in unavailable)),
                     bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 7),
                     wraplength=CARD_TEXT_WIDTH, justify="left").pack(
                anchor="w", padx=14, pady=(0, 6))

        tk.Label(body, text=prompt.get("message", ""), bg=COLOR_CARD,
                 fg=COLOR_TEXT, font=("Segoe UI", 8, "bold"),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(4, 8))

        options = prompt.get("options") or ["Continue"]
        btn_box = tk.Frame(actions, bg=COLOR_CARD)
        btn_box.pack(fill="x", padx=14, pady=(0, 14))
        for idx, opt in enumerate(options):
            primary = (idx == 0)
            tk.Button(btn_box, text=opt,
                      bg=COLOR_PRIMARY if primary else "#F1F5F9",
                      fg="white" if primary else COLOR_TEXT,
                      activebackground=COLOR_PRIMARY_HOVER if primary else "#E2E8F0",
                      activeforeground="white" if primary else COLOR_TEXT,
                      relief="flat", font=("Segoe UI", 9, "bold" if primary else "normal"),
                      padx=12, pady=5, anchor="w",
                      command=lambda o=opt: self._answer(o)).pack(fill="x", pady=2)

        self._finish_card()

        # Render acknowledgement -- only AFTER tkinter has actually laid the
        # widgets out. ui_bridge refuses any answer until it sees this, so the
        # workflow can never continue past a dialog that was not fully drawn.
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self._ack_rendered(rows_drawn)

    def _ack_rendered(self, rows_drawn):
        try:
            state = ui_bridge.load_state()
            prompt = state.get("pending_prompt")
            if prompt is not None and prompt.get("answer") is None:
                prompt["rendered"] = int(rows_drawn) or True
                state["pending_prompt"] = prompt
                ui_bridge.save_state(state)
                # The signature we track must include the ack we just wrote,
                # otherwise the next tick sees a "changed" prompt and redraws
                # the dialog underneath the user.
                self._current_prompt_sig = json.dumps(prompt, sort_keys=True)
            log("Mesh Diagnostics dialog rendered ({0} row(s)).".format(rows_drawn))
        except Exception as exc:
            log("Could not acknowledge dialog render: {0}".format(exc))

    # The queue's status vocabulary, taken from the frozen enum in the Job
    # Viewer's own bundle: PRECREATED, CREATED, QUEUED, SCHEDULED, INPROGRESS,
    # COMPLETED, CANCELED, FAILED, TIMEDOUT. There is no "RUNNING" and no
    # "PENDING" -- guessing those is what first rendered a live solve as a grey
    # "Inprogress", since the real value matched no entry here.
    #
    # The pre-run states stay muted on purpose: nothing is happening yet, and a
    # blue "running" tone on a queued job reads as progress that has not begun.
    _JOB_STATUS_COLORS = {
        "INPROGRESS": COLOR_PRIMARY,
        "COMPLETED": "#137333",
        "FAILED": "#D32F2F",
        "CANCELED": "#D32F2F",
        "TIMEDOUT": "#D32F2F",
        "PRECREATED": COLOR_TEXT_MUTED,
        "CREATED": COLOR_TEXT_MUTED,
        "QUEUED": COLOR_TEXT_MUTED,
        "SCHEDULED": COLOR_TEXT_MUTED,
    }

    # str.title() is wrong for every compound value here ("Inprogress",
    # "Timedout"), so the labels are spelled out rather than derived.
    _JOB_STATUS_LABELS = {
        "INPROGRESS": "In progress",
        "COMPLETED": "Completed",
        "FAILED": "Failed",
        "CANCELED": "Canceled",
        "TIMEDOUT": "Timed out",
        "PRECREATED": "Preparing",
        "CREATED": "Created",
        "QUEUED": "Queued",
        "SCHEDULED": "Scheduled",
    }

    @staticmethod
    def _fmt_elapsed(seconds):
        """42 -> '42s', 254 -> '4m 14s', 7300 -> '2h 1m'."""
        try:
            total = max(0, int(seconds))
        except Exception:
            return ""
        if total < 60:
            return "{0}s".format(total)
        if total < 3600:
            return "{0}m {1}s".format(total // 60, total % 60)
        return "{0}h {1}m".format(total // 3600, (total % 3600) // 60)

    def _show_job_manager(self, prompt):
        """Live card shown while the solver runs, standing in for Autodesk's
        separate "Simulation Job Viewer" window.

        The job data is read from the local Simulation Compute Manager REST API
        (see compute_jobs.py) and rendered here rather than by launching
        ComputeBrowser.exe, so the user watches the solve in the same docked
        panel that ran every other stage instead of a second Chromium window
        landing on top of Synergy.

        Non-blocking and button-less: the solve owns the workflow until it ends,
        and there is nothing here for the user to decide. It exists so the wait
        is not blind -- this stage used to fold the panel away entirely."""
        body, _actions = self._card_scaffold()

        tk.Label(body, text=prompt.get("title", "Analysis Running"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 2))
        tk.Label(body,
                 text=("Your analysis is queued with Autodesk's Simulation "
                       "Compute Manager. This is the same job the Job Manager "
                       "window shows."),
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        # Every changing widget is built ONCE and kept, so _apply_job_manager
        # can rewrite its text on each refresh. See _job_card_reusable for why
        # this card must not be rebuilt the way the others are.
        widgets = {}

        widgets["name"] = tk.Label(
            body, bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 9, "bold"),
            wraplength=CARD_TEXT_WIDTH, justify="left")
        widgets["name"].pack(anchor="w", padx=14, pady=(0, 2))

        # The headline line. Before the job shows up in the queue there is no
        # status to report, so `message` carries the explanation instead -- an
        # empty card would read as a stall.
        widgets["headline"] = tk.Label(
            body, bg=COLOR_CARD, font=("Segoe UI", 9, "bold"),
            wraplength=CARD_TEXT_WIDTH, justify="left")
        widgets["headline"].pack(anchor="w", padx=14, pady=(0, 6))

        # Hand-drawn progress bar, matching _show_result_presentation -- ttk is
        # not used anywhere in this panel.
        widgets["bar_w"] = PANEL_WIDTH - 28
        widgets["bar"] = tk.Canvas(body, height=6, width=widgets["bar_w"],
                                   bg="#E8EAED", highlightthickness=0)
        widgets["bar"].pack(anchor="w", padx=14, pady=(0, 10))

        # The phases line comes and goes, and re-pack()ing a label would move it
        # to the bottom of the card. Packing a permanent holder and toggling the
        # label INSIDE it keeps the running order fixed; an empty holder
        # collapses to nothing.
        widgets["phases_holder"] = tk.Frame(body, bg=COLOR_CARD)
        widgets["phases_holder"].pack(anchor="w", fill="x")
        widgets["phases"] = tk.Label(
            widgets["phases_holder"], bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
            font=("Segoe UI", 7), wraplength=CARD_TEXT_WIDTH, justify="left")

        widgets["footer"] = tk.Label(
            body, bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 7),
            wraplength=CARD_TEXT_WIDTH, justify="left")
        widgets["footer"].pack(anchor="w", padx=14, pady=(0, 10))

        # "Open Job Manager" -- Autodesk's own Job Viewer window, on demand.
        # This card covers the common question (what is MY run doing, right
        # now) at panel width; the full viewer is a ~1900px five-column table
        # of every job ever run, so it is a click away rather than embedded.
        #
        # _answer_live, not _answer: this button does NOT end the stage. The
        # workflow consumes the answer with take_live_card_answer() and the card
        # keeps refreshing, so it stays pressable (for instance after closing
        # the viewer).
        widgets["options"] = list(prompt.get("options") or [])
        for option in widgets["options"]:
            tk.Button(body, text="{0} ▸".format(option), bg="#F1F5F9",
                      fg=COLOR_TEXT, activebackground="#E2E8F0", relief="flat",
                      font=("Segoe UI", 9), padx=12, pady=5, anchor="w",
                      command=lambda o=option: self._answer_live(o)).pack(
                fill="x", padx=14, pady=(0, 6))

        tk.Frame(body, bg=COLOR_CARD, height=8).pack()
        self._job_card = widgets
        self._apply_job_manager(prompt)
        self._finish_card()

    def _job_card_reusable(self, prompt):
        """True when the job card on screen can be refreshed in place.

        It MUST be, whenever possible. `elapsed` changes on every publish, so
        the prompt signature changes every few seconds, and the normal path
        would tear the card down and rebuild it that often -- destroying and
        recreating the button underneath the user. Tk only fires a button's
        command when press AND release land on the same live widget, so a click
        that overlapped a rebuild was silently swallowed. That is what made
        "Open Job Manager" fire only sometimes.

        A change to the BUTTONS is the one thing an in-place update cannot
        express, so that still forces a rebuild."""
        widgets = getattr(self, "_job_card", None)
        if not widgets:
            return False
        try:
            if not widgets["name"].winfo_exists():
                return False
        except Exception:
            return False
        return list(prompt.get("options") or []) == widgets["options"]

    def _update_job_manager(self, prompt):
        """Refresh the job card without rebuilding it."""
        self._apply_job_manager(prompt)
        self._resize_card()

    def _apply_job_manager(self, prompt):
        """Write the prompt's values into the job card's existing widgets."""
        widgets = getattr(self, "_job_card", None)
        if not widgets:
            return

        status = str(prompt.get("status") or "").strip().upper()
        percent = int(prompt.get("percent") or 0)
        message = str(prompt.get("message") or "")
        accent = self._JOB_STATUS_COLORS.get(status, COLOR_TEXT_MUTED)

        widgets["name"].configure(text=str(prompt.get("name") or ""))

        headline = message
        if status:
            # An unknown status is shown verbatim rather than dropped: a value
            # this map has not seen is worth putting in front of the user.
            headline = "{0} · {1}%".format(
                self._JOB_STATUS_LABELS.get(status, status.capitalize()), percent)
        widgets["headline"].configure(text=headline, fg=accent)

        bar = widgets["bar"]
        bar.delete("fill")
        if percent > 0:
            bar.create_rectangle(
                0, 0, int(widgets["bar_w"] * min(percent / 100.0, 1.0)), 6,
                fill=accent, width=0, tags="fill")

        # Which phase of a multi-stage sequence is actually running. A
        # Cool+Flow+Warp job sitting at 40% means little on its own.
        parts = []
        for phase in (prompt.get("phases") or []):
            try:
                parts.append("{0} {1}%".format(
                    phase.get("name") or "?", int(phase.get("percent") or 0)))
            except Exception:
                continue
        if parts:
            widgets["phases"].configure(text="Phases: " + "  ·  ".join(parts))
            if not widgets["phases"].winfo_ismapped():
                widgets["phases"].pack(anchor="w", padx=14, pady=(0, 4))
        elif widgets["phases"].winfo_ismapped():
            widgets["phases"].pack_forget()

        footer = []
        if prompt.get("sequence"):
            footer.append(str(prompt.get("sequence")))
        footer.append("Cloud" if prompt.get("cloud") else "Local")
        if prompt.get("worker"):
            footer.append(str(prompt.get("worker")))
        spent = self._fmt_elapsed(prompt.get("elapsed"))
        if spent:
            footer.append("elapsed " + spent)
        widgets["footer"].configure(text="  ·  ".join(footer))

    def _show_result_presentation(self, prompt):
        """Live card shown while the workflow walks the result plots one at a
        time. Non-blocking: the workflow keeps showing plots and only checks
        this card's answer between results, so 'Skip to review' takes effect at
        the next result boundary instead of interrupting a capture."""
        body, actions = self._card_scaffold()

        index = int(prompt.get("index") or 0)
        total = int(prompt.get("total") or 0)
        label = str(prompt.get("label") or "")
        shown = prompt.get("shown") or []

        # The same card serves the paced presentation and the export progress
        # that follows Export & Report -- they differ only in wording and in
        # whether a Skip button is offered (options empty == not skippable).
        options = prompt.get("options") or []
        exporting = not options

        tk.Label(body, text=prompt.get("title", "Presenting Results"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 2))
        tk.Label(body,
                 text=("Capturing a screenshot and an animation for each result "
                       "you selected. This takes a moment per result."
                       if exporting else
                       "Each result is displayed in Synergy for a few seconds so you "
                       "can see it. Nothing is captured yet."),
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        tk.Label(body,
                 text="{0} {1} of {2}:  {3}".format(
                     "Exporting" if exporting else "Now showing", index, total, label),
                 bg=COLOR_CARD, fg=COLOR_PRIMARY, font=("Segoe UI", 9, "bold"),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 6))

        # Progress bar drawn by hand -- ttk is not used anywhere else in here.
        bar_w = PANEL_WIDTH - 28
        bar = tk.Canvas(body, height=6, width=bar_w, bg="#E8EAED",
                        highlightthickness=0)
        bar.pack(anchor="w", padx=14, pady=(0, 10))
        if total:
            bar.create_rectangle(0, 0, int(bar_w * min(index / float(total), 1.0)), 6,
                                 fill=COLOR_PRIMARY, width=0)

        if shown:
            tk.Label(body,
                     text=("Done: " if exporting else "Already shown: ")
                          + ", ".join(shown[-6:]),
                     bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 7),
                     wraplength=CARD_TEXT_WIDTH, justify="left").pack(
                anchor="w", padx=14, pady=(0, 8))

        if exporting:
            tk.Frame(body, bg=COLOR_CARD, height=6).pack()
        else:
            tk.Button(body, text="Skip to review ▸", bg="#F1F5F9",
                      fg=COLOR_TEXT, activebackground="#E2E8F0", relief="flat",
                      font=("Segoe UI", 9), padx=12, pady=5, anchor="w",
                      command=lambda: self._answer("SKIP")).pack(
                fill="x", padx=14, pady=(0, 14))

        self._finish_card()

    def _show_result_review(self, prompt):
        """Live card for the interactive review: a checkbox list of the results
        this study produced -- the same set, in the same order, as Synergy's own
        Results tree -- plus the Export & Report button that ends the stage.

        Ticking a result asks the workflow (via ui_bridge.request_result_focus)
        to display it in Synergy and open its F1 help, so ticking is also how
        you inspect.
        A result the user opens directly in Synergy's tree is ticked here too,
        which is what makes the two lists agree.

        The list grows if the user opens something outside the Top 12, so this
        card is re-rendered whenever it changes -- ticks are remembered in
        self._review_ticks and restored on each redraw."""
        body, actions = self._card_scaffold()

        # `available` is the full produced-results list; `visited` are the ones
        # opened in Synergy so far (shown with a marker, and ticked).
        available = prompt.get("available") or []
        visited = prompt.get("visited") or []
        entries = list(available) + [v for v in visited if v not in available]
        guide = prompt.get("guide") or {}
        if not hasattr(self, "_review_ticks"):
            self._review_ticks = {}
        for v in visited:                      # opening a result ticks it
            self._review_ticks.setdefault(v, True)

        tk.Label(body, text="Review Results", bg=COLOR_CARD,
                 fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 2))
        tk.Label(body,
                 text="Tick the results you want in the report. Ticking one shows "
                      "it in Synergy and opens its F1 help. You can also open "
                      "results in Synergy's own Results tree — the automation "
                      "waits for you either way.",
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        list_host = tk.Frame(body, bg=COLOR_CARD, highlightthickness=1,
                             highlightbackground=COLOR_BORDER)
        list_host.pack(fill="x", padx=14, pady=(0, 6))

        if not entries:
            tk.Label(list_host, text="No result opened yet.", bg=COLOR_CARD,
                     fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8, "italic")).pack(
                anchor="w", padx=10, pady=8)
        else:
            list_h = 190 if len(entries) > 8 else max(24 * len(entries), 26)
            canvas = tk.Canvas(list_host, bg=COLOR_CARD, highlightthickness=0,
                               height=list_h)
            vbar = tk.Scrollbar(list_host, orient="vertical", command=canvas.yview)
            rows = tk.Frame(canvas, bg=COLOR_CARD)
            rows.bind("<Configure>",
                      lambda _e, c=canvas: _vertical_scrollregion(c))
            win = canvas.create_window((0, 0), window=rows, anchor="nw")
            canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
            canvas.configure(yscrollcommand=vbar.set)
            canvas.pack(side="left", fill="both", expand=True)
            vbar.pack(side="right", fill="y")
            canvas.bind("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1 * int(e.delta / 120), "units"))

            self._review_vars = {}
            for label in entries:
                var = tk.BooleanVar(value=self._review_ticks.get(label, False))
                self._review_vars[label] = var
                self._review_ticks.setdefault(label, var.get())

                row = tk.Frame(rows, bg=COLOR_CARD)
                row.pack(fill="x", padx=4, pady=0)

                def _on_tick(lbl=label, v=var):
                    self._review_ticks[lbl] = bool(v.get())
                    if v.get():
                        # Ticking is also how you inspect: show it in Synergy
                        # and open its F1 help.
                        self._request_result_focus(lbl)
                        self._review_info = lbl
                    elif getattr(self, "_review_info", None) == lbl:
                        self._review_info = None
                    # Always redraw: the guide text and the selected count on
                    # the button both have to follow the ticks.
                    self._rerender_review(prompt)

                tk.Checkbutton(row, text=label, variable=var, command=_on_tick,
                               bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 8),
                               activebackground=COLOR_CARD, anchor="w",
                               selectcolor="white").pack(side="left")
                if label in visited:
                    tk.Label(row, text="opened", bg=COLOR_CARD, fg="#137333",
                             font=("Segoe UI", 7)).pack(side="right", padx=6)

        # What the most recently ticked result is for -- the plugin's own guide
        # text, shown here so the information is on screen even when Synergy's
        # help window opens behind its main window.
        info_label = getattr(self, "_review_info", None)
        info = guide.get(info_label) if info_label else None
        if info:
            box = tk.Frame(body, bg="#F1F5F9")
            box.pack(fill="x", padx=14, pady=(0, 6))
            tk.Label(box, text=info_label, bg="#F1F5F9", fg=COLOR_TEXT,
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(
                fill="x", padx=8, pady=(6, 0))
            tk.Label(box, text=info, bg="#F1F5F9", fg=COLOR_TEXT_MUTED,
                     font=("Segoe UI", 7), wraplength=CARD_TEXT_WIDTH,
                     justify="left", anchor="w").pack(fill="x", padx=8, pady=(2, 6))

        tk.Label(body,
                 text="Ticked results are exported with a screenshot and an "
                      "animation, then written to the report.",
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 7),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        def do_export():
            selected = [lbl for lbl, var in getattr(self, "_review_vars", {}).items()
                        if var.get()]
            self._answer({"action": "EXPORT", "selected": selected})

        n_ticked = sum(1 for v in getattr(self, "_review_vars", {}).values() if v.get())

        btn_box = tk.Frame(actions, bg=COLOR_CARD)
        btn_box.pack(fill="x", padx=14, pady=(0, 14))
        # Plain text on purpose: the 📤 glyph has no coverage in the Segoe UI
        # face tkinter picks here and renders as a tofu box.
        # Disabled at zero: exporting nothing produces an empty report, and a
        # button that silently does that reads as a broken one.
        tk.Button(btn_box,
                  text=("Export & Report ({0} selected)".format(n_ticked)
                        if n_ticked else "Export & Report — tick a result first"),
                  bg=COLOR_PRIMARY if n_ticked else "#B9C6D8", fg="white",
                  activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                  relief="flat", font=("Segoe UI", 9, "bold"), padx=12, pady=6,
                  anchor="w", command=do_export,
                  state=("normal" if n_ticked else "disabled")).pack(
            fill="x", pady=(0, 6))
        tk.Button(btn_box, text="Cancel (no report)", bg="#F1F5F9", fg=COLOR_TEXT,
                  activebackground="#E2E8F0", relief="flat", font=("Segoe UI", 8),
                  padx=12, pady=5, anchor="w",
                  command=lambda: self._answer({"action": "CANCEL",
                                                "selected": []})).pack(fill="x")

        self._finish_card()

    def _request_result_focus(self, label):
        """Ask the workflow to bring `label` up in Synergy and open its F1 help.

        Only the workflow process holds the COM connection, so the panel cannot
        show a plot itself -- it leaves a request in the prompt and the review
        loop picks it up on its next poll (~0.3s). The counter is what makes a
        repeat tick of the SAME result register as a new request."""
        try:
            seq = ui_bridge.request_result_focus(label)
            log("Requested focus/help for '{0}' (seq {1}).".format(label, seq))
        except Exception as exc:
            log("Could not request result focus for '{0}': {1}".format(label, exc))

    def _rerender_review(self, prompt):
        """Redraw the review card in place (after a tick) without waiting for
        the next state poll, so the guide text appears immediately.

        Deferred with after(): this runs from a Checkbutton's own command, and
        rebuilding the card destroys that very widget -- doing it inline leaves
        Tk unwinding into a dead command."""
        def _go():
            try:
                self._show_result_review(prompt)
            except Exception as exc:
                log("Review card redraw failed: {0}".format(exc))
        self.root.after(1, _go)

    def _show_error_recovery(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", "Model Error Recovery"),
                 bg=COLOR_CARD, fg="#D32F2F", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", "Model or geometry error detected."),
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 10))

        tk.Label(body, text="Select recovery option:", bg=COLOR_CARD,
                 fg=COLOR_TEXT, font=("Segoe UI", 8, "bold")).pack(
            anchor="w", padx=14, pady=(0, 8))

        btn_box = tk.Frame(actions, bg=COLOR_CARD)
        btn_box.pack(fill="x", padx=14, pady=(0, 14))

        tk.Button(btn_box, text="🔄 Translate Surface (Attempt Automatic Repair)",
                  bg=COLOR_PRIMARY, fg="white", activebackground=COLOR_PRIMARY_HOVER,
                  activeforeground="white", relief="flat", font=("Segoe UI", 8, "bold"),
                  padx=12, pady=6, anchor="w",
                  command=lambda: self._answer("Translate Surface")).pack(fill="x", pady=(0, 6))

        tk.Button(btn_box, text="↩️ Go back to Fusion (Fix Model in CAD)",
                  bg="#F1F5F9", fg=COLOR_TEXT, activebackground="#E2E8F0",
                  relief="flat", font=("Segoe UI", 8, "bold"), padx=12, pady=6, anchor="w",
                  command=lambda: self._answer("Go back to Fusion to fix model")).pack(fill="x")

        self._finish_card()


    def _show_material_selector(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", "Select Material"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", "Select thermoplastics material:"),
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        common = prompt.get("common_materials") or [
            "POLYFLAM RIPP 3625 CS1: A Schulman GMBH",
            "Generic PP: Polymer Database",
            "Generic ABS: Polymer Database"
        ]
        mat_var = tk.StringVar(value=prompt.get("default_material") or common[0])

        tk.Label(body, text="Commonly used materials:", bg=COLOR_CARD,
                 fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=14)

        for mat in common:
            r = tk.Radiobutton(body, text=mat, variable=mat_var, value=mat,
                               bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 8),
                               activebackground=COLOR_CARD, justify="left", anchor="w")
            r.pack(fill="x", padx=14, pady=1)

        btn_row = tk.Frame(actions, bg=COLOR_CARD)
        btn_row.pack(anchor="e", padx=14, pady=(8, 14))
        tk.Button(btn_row, text="Skip", relief="flat", font=("Segoe UI", 9),
                  padx=10, pady=4, command=lambda: self._answer("Skip")).pack(
            side="right", padx=(6, 0))
        tk.Button(btn_row, text="Select OK", bg=COLOR_PRIMARY, fg="white",
                  activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                  command=lambda: self._answer(mat_var.get())).pack(side="right")
        self._finish_card()

    def _show_process_settings(self, prompt):
        body, actions = self._card_scaffold()
        tk.Label(body, text=prompt.get("title", "Process Settings"),
                 bg=COLOR_CARD, fg=COLOR_TEXT, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=14, pady=(14, 4))
        tk.Label(body, text=prompt.get("message", "Leave values unchanged to keep defaults."),
                 bg=COLOR_CARD, fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8),
                 wraplength=CARD_TEXT_WIDTH, justify="left").pack(
            anchor="w", padx=14, pady=(0, 8))

        fields = prompt.get("fields") or []
        var_map = {}

        if fields:
            for f in fields:
                label = f.get("label", "")
                tc = f.get("tcode")
                kind = f.get("kind")
                val = f.get("value")
                opts = f.get("options") or []

                tk.Label(body, text=label, bg=COLOR_CARD,
                         fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=14)

                if kind == "enum" and opts:
                    var = tk.StringVar(value=opts[0] if opts else "Automatic")
                    var_map[tc] = (kind, var, opts)
                    opt_menu = tk.OptionMenu(body, var, *opts)
                    opt_menu.config(bg="white", font=("Segoe UI", 8), highlightthickness=1)
                    opt_menu.pack(fill="x", padx=14, pady=(0, 6))
                else:
                    var = tk.StringVar(value=str(val if val is not None else 60.0))
                    var_map[tc] = (kind, var, None)
                    tk.Entry(body, textvariable=var, font=("Segoe UI", 9)).pack(
                        fill="x", padx=14, pady=(0, 6))
        else:
            tk.Label(body, text="Mold surface temperature (°C)", bg=COLOR_CARD,
                     fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=14)
            mold_var = tk.StringVar(value=str(prompt.get("mold_temp", 60)))
            var_map[11108] = ("temp", mold_var, None)
            tk.Entry(body, textvariable=mold_var, font=("Segoe UI", 9)).pack(
                fill="x", padx=14, pady=(0, 6))

            tk.Label(body, text="Melt temperature (°C)", bg=COLOR_CARD,
                     fg=COLOR_TEXT_MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=14)
            melt_var = tk.StringVar(value=str(prompt.get("melt_temp", 200)))
            var_map[11002] = ("temp", melt_var, None)
            tk.Entry(body, textvariable=melt_var, font=("Segoe UI", 9)).pack(
                fill="x", padx=14, pady=(0, 6))

        def submit_form():
            out = {}
            for tc, (kind, var, opts) in var_map.items():
                if kind == "temp":
                    try:
                        out[tc] = float(var.get())
                    except ValueError:
                        pass
                elif kind == "enum":
                    out[tc] = var.get()
            if 11108 in var_map:
                try:
                    out["mold_temp"] = float(var_map[11108][1].get())
                except ValueError:
                    pass
            if 11002 in var_map:
                try:
                    out["melt_temp"] = float(var_map[11002][1].get())
                except ValueError:
                    pass
            self._answer(out)

        btn_row = tk.Frame(actions, bg=COLOR_CARD)
        btn_row.pack(anchor="e", padx=14, pady=(6, 14))
        tk.Button(btn_row, text="Cancel", relief="flat", font=("Segoe UI", 9),
                  padx=10, pady=4, command=lambda: self._answer("Skip")).pack(
            side="right", padx=(6, 0))
        tk.Button(btn_row, text="OK", bg=COLOR_PRIMARY, fg="white",
                  activebackground=COLOR_PRIMARY_HOVER, activeforeground="white",
                  relief="flat", font=("Segoe UI", 9), padx=10, pady=4,
                  command=submit_form).pack(side="right")
        self._finish_card()

    def _journey_items(self, state):
        """The journey rows, in workflow order. Also used to find which step is
        currently underway, so the header status line and the row agree."""
        params = state.get("params") or {}

        # CAD diagnostics should stay Pending until the workflow puts something
        # in params -- either an in-progress marker (set_step_in_progress) once
        # the stage starts, or its final result.
        cad_diag = params.get("cad_diagnostics")

        return [
            ("Project Name", params.get("project_name")),
            ("Location", params.get("location") or params.get("project_dir")),
            ("Imported Model", params.get("cad_file")),
            ("CAD Diagnostics", cad_diag),
            ("Analysis Sequence", params.get("analysis_sequence")),
            ("Material Selection", params.get("material")),
            ("Process Settings", params.get("process_settings") or (params.get("temperatures") if params.get("temperatures") != "Defaults Loaded" else None)),
            ("Mesh Generation", params.get("mesh_status")),
            ("Analysis Solver", params.get("solver_status")),
            ("Export & Report", params.get("export_status")),
        ]

    def _in_progress_message(self, state):
        """Message of the first journey step that is underway, or None.

        Reading it back out of params (rather than keeping a separate 'busy'
        field) means the message disappears exactly when the step's final value
        is written -- one update, nothing left to reset."""
        for _label, val in self._journey_items(state):
            if not _is_placeholder(val) and ui_bridge.is_in_progress(val):
                return ui_bridge.in_progress_text(val)
        return None

    def _update_journey_summary(self, state):
        items = self._journey_items(state)

        sig = json.dumps(items)
        if sig == self._last_journey_sig:
            return
        self._last_journey_sig = sig

        for w in self.journey_frame.winfo_children():
            w.destroy()

        hdr = tk.Frame(self.journey_frame, bg="#F1F5F9")
        hdr.pack(fill="x")
        tk.Label(hdr, text="📋  User Journey & Workflow Stage History", bg="#F1F5F9", fg=COLOR_TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=6)

        content_box = tk.Frame(self.journey_frame, bg=COLOR_CARD)
        content_box.pack(fill="x", padx=12, pady=6)

        for label, val in items:
            row = tk.Frame(content_box, bg=COLOR_CARD)
            row.pack(fill="x", pady=2.5)

            tk.Label(row, text=f"{label}:", bg=COLOR_CARD, fg=COLOR_TEXT_MUTED,
                     font=("Segoe UI", 8, "bold"), width=17, anchor="w").pack(side="left")

            if _is_placeholder(val):
                tk.Label(row, text="⏳ Pending...", bg=COLOR_CARD, fg="#9CA3AF",
                         font=("Segoe UI", 8, "italic"), anchor="w").pack(side="left")
            elif ui_bridge.is_in_progress(val):
                # Underway, not finished: amber, and never a tick. This is the
                # row that used to read "Pending..." while the automation was
                # busy opening the next stage.
                val_str = ui_bridge.in_progress_text(val)
                badge = tk.Frame(row, bg="#FEF3C7", highlightthickness=0)
                badge.pack(side="left", fill="x", expand=True)
                tk.Label(badge, text="⏳ {0}".format(val_str), bg="#FEF3C7", fg="#B45309",
                         font=("Segoe UI", 8, "bold"), anchor="w", padx=6, pady=1.5).pack(side="left")
            else:
                val_str = str(val).strip()
                if "issue" in val_str.lower() and "no issue" not in val_str.lower() and "passed" not in val_str.lower():
                    badge_bg = "#FEF3C7"
                    badge_fg = "#B45309"
                    icon = "⚠️"
                else:
                    badge_bg = "#E6F4EA"
                    badge_fg = "#137333"
                    icon = "✓"

                if len(val_str) > 34:
                    val_str = val_str[:15] + "..." + val_str[-15:]

                badge = tk.Frame(row, bg=badge_bg, highlightthickness=0)
                badge.pack(side="left", fill="x", expand=True)
                tk.Label(badge, text=f"{icon} {val_str}", bg=badge_bg, fg=badge_fg,
                         font=("Segoe UI", 8, "bold"), anchor="w", padx=6, pady=1.5).pack(side="left")

    def _answer(self, value):
        state = ui_bridge.load_state()
        prompt = state.get("pending_prompt")
        if prompt is not None:
            kind = prompt.get("kind")
            params = state.get("params", {})
            if kind == "project_form" and isinstance(value, dict):
                if value.get("name"):
                    params["project_name"] = value.get("name")
                if value.get("dir"):
                    params["location"] = value.get("dir")
            elif kind == "file_picker" and isinstance(value, str) and value != "Skip":
                params["cad_file"] = os.path.basename(value)
            elif kind == "cad_diagnostics_report":
                if value == "Yes":
                    params["cad_diagnostics"] = "No issues found"
                else:
                    params["cad_diagnostics"] = "Report reviewed"
            elif prompt.get("title") == "Analysis Sequence Selection" and isinstance(value, str):
                params["analysis_sequence"] = value

            state["params"] = params
            prompt["answer"] = value
            state["pending_prompt"] = prompt
            ui_bridge.save_state(state)
        self._clear_prompt_ui()
        self._current_prompt_sig = None

    def _answer_live(self, value):
        """Record a click on a card that STAYS UP.

        _answer() tears the card down and resets the signature, which is right
        for a prompt whose answer ends the stage. A live card's button is not
        an ending -- clearing the UI here would make the card vanish on click
        and reappear seconds later on the next refresh, and would destroy the
        button the user is still touching.

        The workflow consumes the answer with ui_bridge.take_live_card_answer(),
        which resets it so the button can be pressed again."""
        try:
            state = ui_bridge.load_state()
            prompt = state.get("pending_prompt")
            if prompt is None:
                return
            prompt["answer"] = value
            state["pending_prompt"] = prompt
            ui_bridge.save_state(state)
        except Exception:
            pass

    # ---- main loop -----------------------------------------------------------

    def _tick(self):
        if not _window_still_valid(self.synergy_hwnd):
            log("Synergy window closed — embedded panel shutting down.")
            self.root.destroy()
            return

        self._sync_geometry()

        # Heartbeat: tells ui_bridge "someone is watching this state right now".
        # Its own file on purpose -- writing it into ui_state.json meant this
        # tick rewrote the whole state 3x a second and could put back a copy
        # taken before the workflow published a prompt, losing the prompt.
        ui_bridge.touch_heartbeat()

        # Fold/unfold on the workflow's request (meshing) before anything is
        # rendered, so a card published in the same tick lands in the right
        # layout instead of being sized against the geometry we are leaving.
        self._apply_panel_command()

        state = ui_bridge.load_state()

        # Update User Journey summary card live
        self._update_journey_summary(state)

        prompt = state.get("pending_prompt")
        if prompt is not None and prompt.get("answer") is not None:
            prompt = None
        # The status line follows the pending state on EVERY tick, not only
        # when the prompt signature changes. Answering a prompt clears
        # _current_prompt_sig, so the next tick saw "no change" and skipped the
        # branch below -- which is how "Waiting for your input" stayed on
        # screen for the rest of the run with no card under it.
        # A card normally means the workflow is blocked on the user, which is
        # what _set_waiting announces. NON_BLOCKING_KINDS are the exception:
        # they report progress and have no controls at all, so during a solve
        # that runs for hours the badge would sit there nagging the user to
        # come back and answer a card that cannot be answered.
        blocking = prompt is not None and prompt.get("kind") not in NON_BLOCKING_KINDS
        self._set_waiting(blocking, self._in_progress_message(state))
        sig = json.dumps(prompt, sort_keys=True) if prompt else None
        if sig != self._current_prompt_sig:
            self._current_prompt_sig = sig
            if prompt is None:
                self._clear_prompt_ui()
            else:
                kind = prompt.get("kind")
                if kind == "project_form":
                    self._show_project_form(prompt)
                elif kind == "file_picker":
                    self._show_file_picker(prompt)
                elif kind == "cad_diagnostics_report":
                    self._show_cad_diagnostics(prompt)
                elif kind == "mesh_diagnostics_report":
                    self._show_mesh_diagnostics(prompt)
                elif kind == "job_manager":
                    # In place whenever possible -- a rebuild here destroys the
                    # button mid-click. See _job_card_reusable.
                    if self._job_card_reusable(prompt):
                        self._update_job_manager(prompt)
                    else:
                        self._show_job_manager(prompt)
                elif kind == "result_presentation":
                    self._show_result_presentation(prompt)
                elif kind == "result_review":
                    self._show_result_review(prompt)
                elif kind == "material_selector":
                    self._show_material_selector(prompt)
                elif kind == "process_settings":
                    self._show_process_settings(prompt)
                elif kind == "error_recovery":
                    self._show_error_recovery(prompt)
                else:
                    self._show_generic_prompt(prompt)

        self.root.after(TRACK_INTERVAL_MS, self._tick)


def main() -> int:
    # Dock to the Synergy window that owns THIS process tree, so a second
    # Synergy window's panel does not attach itself to the first one's window.
    owner_pid = session_context.synergy_pid()
    log(f"Embedded panel starting ({session_context.describe()}).")
    if owner_pid is None:
        log("Owning synergy.exe could not be resolved from the process tree; "
            "falling back to the largest visible Synergy window. With more "
            "than one Synergy open this may dock to the wrong window.")

    found = None
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        found = _find_synergy_window(only_pid=owner_pid)
        if found:
            break
        time.sleep(0.3)

    if not found:
        log("Could not locate a visible Synergy window within "
            f"{STARTUP_TIMEOUT:.0f}s — embedded panel cannot be docked. Exiting.")
        return 1

    hwnd, _rect = found
    log(f"Docking embedded panel to Synergy window (hwnd={hwnd}, pid={owner_pid}).")

    try:
        root = tk.Tk()
        panel = EmbeddedPanel(root, hwnd)
        root.mainloop()
        log("Embedded panel event loop ended.")
        return 0
    except Exception as e:
        log(f"Embedded panel failed to initialise: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
