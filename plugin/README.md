# Moldflow Synergy 2027 — Python Plugin

A working starting point for automating **Autodesk Moldflow Synergy 2027** from
Python via its COM automation API.

## How it works

Synergy does **not** run Python natively (its built-in command runner uses
Windows Script Host — VBScript/JScript). Instead, Synergy exposes a registered
**out-of-process COM automation server**:

| ProgID           | `synergy.Synergy`                                  |
| ---------------- | -------------------------------------------------- |
| CLSID            | `{ECB5F86C-9418-11D6-8099-001083FF030C}`           |
| Server           | `C:\Program Files\Autodesk\Moldflow Synergy 2027\bin\synergy.exe` |

Python drives that server with [`comtypes`](https://pypi.org/project/comtypes/)
(a pure-Python COM bridge). From the `Synergy` object you reach every manager:
`StudyDoc()`, `PlotManager()`, `Viewer()`, `DiagnosisManager()`,
`MeshGenerator()`, `MoldingProcess()`, and array helpers like
`CreateDoubleArray()` / `CreateIntegerArray()`.

> **Full API reference:** `C:\Program Files\Autodesk\Moldflow Synergy 2027\help\synapi.chm`
> Autodesk's own example scripts: `...\Moldflow Synergy 2027\data\commands\*.vbs`
> — the same objects/methods, just in VBScript. Translating them to Python is
> mechanical.

## Files

| File                   | Purpose                                                            |
| ---------------------- | ----------------------------------------------------------------- |
| `synergy_connect.py`   | Connection helper: attach to the running Synergy session (SAInstance moniker) via a uniform late-bound `Syn` wrapper. |
| `export_dimensions.py` | Main plugin: bounding box + wall thickness + hole detection -> Excel. |
| `mesh_geometry.py`     | Pure-Python geometry: parse `.udm`, extract the 3D boundary surface, detect cylindrical holes (unit-tested offline). |
| `run_plugin.vbs`       | Launcher so Synergy runs the plugin as a UI command (Macro).      |
| `setup.ps1`            | Creates `.venv` and installs dependencies.                        |
| `requirements.txt`     | Dependencies (pywin32, comtypes, openpyxl).                       |
| `assistant_live.py`    | Asks Moldflow's own AI Assistant panel to summarise the open study, via the standalone `AIAssistantPOC`. |
| `ai_report_summary.py` | Shapes that answer into the deck's closing slides — or regenerates an equivalent locally when the panel is unreachable. |
| `enable_assistant_port.bat` | One-time: turns on the WebView2 debug port the Assistant is read through. |

## The review deck

`cad_diagnostics.build_pptx_report()` owns the pipeline — the slide order, the
screenshots and the embedded animations — unchanged. `report_style.py` supplies
only the slides that carry no measured data (cover, contents, section dividers,
study setup, points to highlight) so the look can be iterated on offline; it
never touches a picture, a movie or a poster frame.

Deck order:

| | Slide | Source |
|---|---|---|
| 1 | Cover | study metadata |
| 2 | Contents | what this run actually produced |
| 3–4 | Study setup (divider + card) | `ai_assistant.extract()` — material, mesh, melt/mold temps, machine limits |
| 5 | Engineering results (divider) | — |
| 6… | One slide per result, each followed by its **animation slide** | existing capture pipeline, untouched |
| | Results not produced | existing |
| | Analysis summary (divider) | — |
| | Problematic Results / Summary of Key Concerns | AI Assistant observations, else local threshold rules |
| | Analysis Summary | **always** this project's own threshold findings |
| | Result Statistics | AI Assistant's values, else per-node min/max/avg/std |
| | Points to highlight | AI Assistant recommendations, grouped by subject |
| | How this summary was produced | provenance |

Each result slide leads with **"In this study: …"** — one sentence from the
Assistant about what that result's figure means here — above the existing
description, purpose and target text.

Two Assistant requests per report (the summary, then one comment per exported
result), asked in a single panel session.

The plugin opens the Assistant panel itself when it can: `assistant_panel.py`
finds the launcher through UI Automation, presses it, waits for the chat app to
load, and closes it again once the answer is in. A panel that was already open
is left alone.

**The one catch.** The launcher is not a Moldflow ribbon command — it is an
Autodesk **InfoCenter** button (`ID_IC_SupportAssistantButton`), in the same
strip as Search, Subscription and sign-in. That strip is web-backed and fills
in asynchronously, and on the evidence so far the Assistant button is not in
the UI Automation tree at all until the panel has been created once in that
Synergy session. No button, nothing to press.

Two things follow:

* `find_control()` **waits** (default 25 s, polling) rather than looking once,
  searches every top-level window the process owns, and matches on Name *and*
  AutomationId — so a button that is merely slow to appear is still found.
* If the button genuinely does not exist before first use, searching harder
  cannot conjure it — so the plugin does not rely on it.

**How the panel actually gets opened without you.** Synergy is an MFC
application and persists its docking layout to the registry when it exits. The
Assistant is one of those bars:

```
HKCU\Software\Autodesk\Moldflow Synergy\2027\default\WorkState_v1_1\DockState\Bar-N
    BarID      = 700
    ClassName  = CAutodeskAssistantDialog
    WindowName = Autodesk Assistant
    Visible    = False      <- written ONLY when the bar is hidden
```

`run_startup.vbs` already watches for `synergy.exe` to exit; the moment it
does — which is exactly when that layout has just been written, and the only
moment it can be changed without Synergy overwriting it — it runs
`assistant_panel.py --ensure-visible`, which deletes any `Visible=False` on
that bar. The next launch therefore starts with the Assistant panel already
open, the channel comes up `ready`, and nothing has to be found or pressed.

Close the panel whenever you like: it is repaired after that session ends.

It cannot be done *invisibly*: the Assistant is a WebView2 view, and a view that
is never drawn has no page to read. So the panel is on screen for the half
minute or so the question takes.

The one thing that must be set up in advance is **the WebView2 debug port**: run
`enable_assistant_port.bat` once, then restart Synergy. `disable_assistant_port.bat`
undoes it.

It writes an **app-scoped** policy — `HKCU\Software\Policies\Microsoft\Edge\
WebView2\AdditionalBrowserArguments` with the value `synergy.exe` set to
`--remote-debugging-port=0` — rather than the `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`
environment variable, which applies to *every* WebView2 app the user runs. It
also removes that variable if present, because it outranks the policy in
WebView2's precedence order.

**The port must be 0, not a fixed number.** Synergy runs two WebView2 views (the
Simulation Hub tab and the Assistant panel) and both honour the same setting.
With `--remote-debugging-port=9222` they race for that one port, whichever
starts first wins, and when the hub wins the Assistant has no port at all —
indistinguishable from never having configured anything. Port 0 gives each view
its own free port. `assistant_live.py` detects and names this case specifically.

A WebView2 view keeps the arguments it was created with, so a change reaches a
Synergy session that has not yet opened the Assistant panel, and no other.

Check the channel any time with:

```bash
.venv\Scripts\python.exe assistant_live.py
```

and check that the ribbon control can be found — with Synergy running — with:

```bash
.venv\Scripts\python.exe assistant_panel.py
```

Each report costs **one** request on the signed-in Autodesk account and appears
in that account's chat history. Set `ASK_AI_ASSISTANT = False` in
`cad_diagnostics.py` to send nothing. If the panel cannot be reached for any
reason — port off, panel closed, no answer — those slides fall back to the
locally computed assessment and the report is produced exactly as before.

## What `export_dimensions.py` extracts

Writes `design_dimensions.xlsx` (opens automatically) with three sections:

1. **Bounding Box** — X/Y/Z min/max/size, overall size, diagonal, mesh volume.
2. **Wall Thickness** — for a fusion/midplane mesh, min/avg/max via Thickness
   Diagnosis; for a 3D solid mesh, the nominal thickness (smallest bounding
   dimension) plus a note (true per-element thickness needs a Dual Domain mesh).
3. **Holes** — cylindrical through-hole diameters and centers.

### How it works (fast + mesh-type aware)
* One bulk `Synergy.Project().ExportModel()` to a `.udm` text file, parsed in
  pure Python (no per-node COM calls). `.udm` coordinates are in **meters** and
  are scaled to the display unit (mm/in).
* Hole detection needs the outer **surface**: for a 3D tetra mesh it extracts the
  boundary (triangular faces used by exactly one tet); for fusion, all nodes
  qualify. It then keeps mid-thickness "wall" nodes, clusters them (distance from
  the 3D mesh edge length), and fits circles -- the rectangular outer rim is
  rejected by the circularity test.

### Requirements / assumptions
* The study must be **meshed** (a raw imported STEP/CAD solid has no nodes).
* Make the correct study the **active tab** -- `StudyDoc()` reads the active study.
* Hole detection targets cylindrical through-holes along the part's thinnest
  axis (e.g. holes drilled through a plate). Very small holes on a coarse mesh
  may have too few surface nodes to fit -- use a finer mesh.

## Setup

```powershell
cd "$env:USERPROFILE\Documents\MoldflowSynergyPlugin"
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

This builds `.venv` from Moldflow's bundled 64-bit Python 3.14 and installs
`comtypes`. (64-bit Python is required — it must match the 64-bit COM server.)

## Run it

1. Start **Moldflow Synergy** and open a study (meshed, ideally).
2. Run the plugin:

   ```powershell
   .\.venv\Scripts\python.exe plugin_example.py
   ```

   It prints a report and writes `plugin_report.txt`.

## Run it *from inside* Synergy (the "plugin" experience)

Add `run_plugin.vbs` as a custom Synergy command so it appears as a button:

1. In Synergy, open the scripting/commands customization
   (**Tools → Macro / Run Script**, or add a custom command that points at a
   script — see *Automation* in Synergy Help).
2. Point it at `run_plugin.vbs`.

When triggered from inside Synergy, the VBS starts the Python process, which
attaches to the **current** Synergy session (via the `SAInstance` moniker), so
the plugin operates on the study you're looking at.

## Extending

To add functionality, open `synapi.chm`, find the interface you need, and call
it the same late-bound way:

```python
from synergy_connect import get_synergy

syn = get_synergy()
sd = syn.StudyDoc()
# e.g. drive the study viewer, run an analysis, export results, edit the mesh...
```

### Common gotchas

- **32/64-bit mismatch** → `Class not registered`. Use 64-bit Python.
- **`Operation unavailable`** from `GetActiveObject` → no Synergy is running.
  Start Synergy, or use `get_synergy(allow_launch=True)`.
- **Arrays**: API methods fill `DoubleArray` / `IntegerArray` objects by
  reference. Read them back with `.ToVBSArray()`; fill them with
  `.FromVBSArray(seq)`, `.AddDouble(x)`, `.AddInteger(i)`; index with `.Val(i)`.
- **Booleans**: pass Python `True`/`False` (marshalled as VARIANT_BOOL).
