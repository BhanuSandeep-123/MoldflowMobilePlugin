"""
ai_report_summary.py
--------------------
Builds the "AI Assistant"-style results summary that goes into the review deck:
a table of PROBLEMATIC results (Result | Issue | Value | Unit) followed by a
severity-ordered "Summary of key concerns" list.

TWO SOURCES, ONE OUTPUT SHAPE
-----------------------------
1. **The live panel** (preferred). `assistant_live.fetch_summary()` asks
   Moldflow 2027's own AI Assistant about the open study and returns its answer
   parsed into results / observations / recommendations. When that is available
   it is what the deck shows: it is Autodesk's own reading of the study, and it
   catches things a threshold rule cannot -- on the study this was built
   against it flagged a shear rate of 281,178 1/s against a material limit of
   100,000, which the local rules below never surfaced at all.

2. **Locally regenerated** (fallback). The panel needs a WebView2 debug port
   and an open Assistant panel; when either is missing the summary is rebuilt
   here from the same inputs the panel itself reads:

     * collect_analysis_summary() in cad_diagnostics.py -- per-dataset
       statistics and the threshold rules that flag them, and
     * ai_assistant.extract() -- material grade, machine limits and Autodesk's
       own result advice, read straight from the .sdy.

Both paths return the SAME dict, so the deck renders one layout and never
branches on where the numbers came from -- it only reads `source` to caption
them honestly.

Everything here is PURE: dicts in, dicts out, no COM, no Synergy, no network.
The deck renders what these functions return; it decides nothing itself.
"""

from __future__ import annotations

import re

# Severity ordering and the markers the assistant's own output uses.
LEVEL_RANK = {"red": 0, "amber": 1, "green": 2}
LEVEL_MARK = {"red": "\U0001F534", "amber": "\U0001F7E0", "green": "\U0001F7E2"}

# Which statistics to print for each result, matching how the assistant quotes
# them: an angle is meaningful as average-and-peak, a shrinkage as its range, a
# shear rate only as its peak. Anything not listed falls back to _default_spec.
VALUE_SPEC = {
    "air traps": ("count",),
    "air traps, including air vents": ("count",),
    "weld lines": ("mean", "max"),
    "shear rate": ("max",),
    "shear rate, bulk": ("max",),
    "shear rate, maximum": ("max",),
    "shear stress at wall": ("max",),
    "volumetric shrinkage": ("min", "max"),
    "average volumetric shrinkage": ("min", "max"),
    "volumetric shrinkage at ejection": ("min", "max"),
    "time to reach ejection temperature": ("mean", "max"),
    "cavity weight": ("min", "max"),
    "fill time": ("max",),
    "extension rate": ("min", "max"),
    "viscosity": ("max",),
    "pressure at v/p switchover": ("mean", "max"),
    "pressure at injection location": ("max",),
    "injection pressure": ("max",),
    "clamp force": ("max",),
}

_STAT_LABEL = {"min": "Min", "max": "Max", "mean": "Avg", "std": "SD"}


def format_number(value):
    """Format a statistic the way the assistant's table does: thousands
    separated above 1000, one decimal in the hundreds, four significant
    figures below that. Reproduces its published figures exactly -- 260,756 /
    248.1 / 87.68 / 12.73 / -0.15 / 0.121."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if v != v:  # NaN
        return "-"
    a = abs(v)
    if a >= 1000:
        return "{0:,.0f}".format(v)
    if a >= 100:
        return "{0:,.1f}".format(v)
    if a >= 1:
        return "{0:.4g}".format(v)
    if v == 0:
        return "0"
    return "{0:.3g}".format(v)


def _default_spec(metric):
    """When a result has no entry in VALUE_SPEC, show the range if it goes
    negative (the sign is usually the point) and the peak otherwise."""
    try:
        if float(metric.get("min", 0)) < 0:
            return ("min", "max")
    except (TypeError, ValueError):
        pass
    return ("max",)


def format_value(metric, spec=None):
    """Render a metric's headline figures, e.g. 'Avg: 87.68 / Max: 248.1'."""
    if not metric:
        return ""
    key = str(metric.get("name", "")).lower()
    spec = spec or VALUE_SPEC.get(key) or _default_spec(metric)
    if spec == ("count",):
        n = metric.get("_count")
        if n is None:
            n = metric.get("n")
        return "{0} locations".format(format_number(n).replace(".0", ""))
    parts = []
    for stat in spec:
        if metric.get(stat) is None:
            continue
        parts.append("{0}: {1}".format(_STAT_LABEL.get(stat, stat.title()),
                                       format_number(metric[stat])))
    return " / ".join(parts)


def _metric_index(summary_data):
    out = {}
    for m in (summary_data or {}).get("metrics") or []:
        out[str(m.get("name", "")).lower()] = m
    return out


def build_problem_rows(summary_data, include_green=False):
    """The 'Problematic Results' table: one row per flagged finding, worst
    first, each carrying the figures that justify it.

    Green findings are excluded by default -- the assistant lists only what
    needs attention, and a table of things that are fine buries the things
    that are not. Pass include_green=True to tabulate everything.
    """
    metrics = _metric_index(summary_data)
    rows = []
    for f in (summary_data or {}).get("findings") or []:
        level = f.get("level", "amber")
        if level == "green" and not include_green:
            continue
        name = f.get("result") or ""
        metric = metrics.get(name.lower(), {})
        issue = (f.get("headline") or "").strip()
        # The rule's own detail sentence carries the numbers behind the
        # verdict; keep it for the deck's second line rather than dropping it.
        rows.append({
            "level": level,
            "result": name,
            "issue": issue,
            "detail": (f.get("detail") or "").strip(),
            "value": format_value(metric),
            "unit": metric.get("unit") or "—",
        })
    rows.sort(key=lambda r: LEVEL_RANK.get(r["level"], 3))
    return rows


def build_key_concerns(rows):
    """The closing 'Summary of key concerns' list: the flagged rows restated
    as marked bullets, red before amber, exactly as the assistant closes its
    answer."""
    concerns = []
    for r in rows:
        if r["level"] == "green":
            continue
        concerns.append({
            "level": r["level"],
            "mark": LEVEL_MARK.get(r["level"], ""),
            "result": r["result"],
            "text": r["issue"],
        })
    return concerns


def build_context(meta):
    """Study context from ai_assistant.extract() -- the part of the summary
    that the results alone cannot supply. Returns {} when the extractor found
    nothing, which is normal and must stay non-fatal."""
    if not meta:
        return {}
    try:
        import ai_assistant
        s = ai_assistant.summary(meta)
    except Exception:
        return {}
    seq = s.get("analysis_sequence")
    if isinstance(seq, (list, tuple)):
        seq = " + ".join(str(x) for x in seq)
    return {
        "material": s.get("material_name"),
        "material_family": s.get("material_family"),
        "fillers": s.get("material_fillers"),
        "melt_temp": s.get("melt_temp"),
        "mold_temp": s.get("mold_temp"),
        "machine": s.get("machine_name"),
        "max_clamp_force": s.get("max_clamp_force"),
        "max_injection_pressure": s.get("max_injection_pressure"),
        "analysis_sequence": seq,
        "mesh_type": s.get("mesh_type"),
        # Autodesk's OWN advice engine, carried through verbatim. It is the one
        # part of this summary that is not ours, so it is never reworded.
        "autodesk_advice": s.get("advice_warnings") or [],
    }


def context_line(context):
    """One-line study provenance for under the summary heading."""
    if not context:
        return ""
    bits = []
    if context.get("material"):
        fam = context.get("material_family")
        bits.append("{0}{1}".format(context["material"],
                                    " ({0})".format(fam) if fam else ""))
    if context.get("melt_temp") is not None and context.get("mold_temp") is not None:
        bits.append("melt {0}°C / mold {1}°C".format(
            format_number(context["melt_temp"]), format_number(context["mold_temp"])))
    if context.get("analysis_sequence"):
        bits.append(str(context["analysis_sequence"]))
    if context.get("mesh_type"):
        bits.append("{0} mesh".format(context["mesh_type"]))
    return "  •  ".join(bits)


# --------------------------------------------------------------------------- #
#  The live-panel path
# --------------------------------------------------------------------------- #
# Words the Assistant uses when something is out of bounds versus merely worth
# a look. Matched on the observation text, because the panel writes prose and
# does not label its own severity. Deliberately conservative: anything that does
# not read as a breach or a caution stays green rather than inflating the count.
_RED_WORDS = ("exceed", "far exceeds", "critical", "violat", "degrad",
              "excessive", "severe", "too high", "too low", "unacceptab",
              "fail", "will not fill", "short shot",
              # A stated consequence, not a hedged one. The Assistant writes
              # "will cause warpage" when it means it; without these the
              # sentence fell through every test and scored GREEN. Study 37
              # tabled "...will likely cause warpage and dimensional issues"
              # as a green row on a slide titled "Problematic Results".
              "will cause", "will likely cause", "is a problem",
              "which is a problem", "serious problem")
_AMBER_WORDS = ("warning", "warn", "may cause", "might", "consider", "should",
                "potential", "risk", "trigger", "high", "long", "uneven",
                "non-uniform", "review", "suggest", "close to", "approaching",
                "present",
                # Unambiguously negative verdict words. NOT "substantial" or
                # "significant": the Assistant uses both as PRAISE ("leaving
                # substantial headroom", "there is significant headroom"), so
                # they flag healthy results as amber.
                "marginal", "problematic", "insufficient", "inadequate",
                "poor")

# Words that carry no identifying weight when matching an observation to the
# result it is about. "(max)" / "(avg)" qualifiers are stripped before this.
_STOPWORDS = {"at", "to", "of", "the", "in", "for", "a", "an", "and", "on",
              "by", "is", "from", "with"}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text):
    """Significant lowercase words of a name or sentence, parentheticals and
    thousands separators removed so '281,178' and '(max)' cannot skew a match."""
    s = re.sub(r"\([^)]*\)", " ", str(text or "").lower())
    s = s.replace(",", "")
    return [t for t in _TOKEN_RE.findall(s) if t not in _STOPWORDS]


# Words that REVERSE the cue that follows them. "indicating no severe
# premature freeze" is reassurance, not a red flag, and plain substring
# matching read it as one.
_NEGATORS = ("no", "not", "never", "without", "avoids", "avoid", "avoiding",
             "prevents", "prevent", "preventing", "free", "unlikely")

# How far back to look for a negator. Long enough for "no risk of severe ..."
# and short enough that the negation of one clause does not silently cancel a
# cue in the next.
_NEGATION_WINDOW = 24


# Cues that must match as WHOLE words. Everything else is deliberately a stem
# ("degrad" -> degradation, "violat" -> violates, "unacceptab" -> unacceptable)
# and keeps matching at a word start. These four are the ones where the stem
# behaviour was wrong: "high" fired on "highly fluid" and "long" on "longer",
# both of which describe healthy results.
_EXACT_CUES = {"high", "long", "present", "poor"}


def _cue_present(text, words):
    """Is any cue in `words` present in `text`, un-negated?

    Anchored at a word START so the stems above keep working, with the
    ambiguous short cues in _EXACT_CUES requiring a full word. "still highly
    fluid at switchover" -- a healthy result -- used to be tabled as a problem
    because "high" matched inside "highly".
    """
    for w in words:
        pattern = r"\b" + re.escape(w) + (r"\b" if w in _EXACT_CUES else "")
        for m in re.finditer(pattern, text):
            lead = text[max(0, m.start() - _NEGATION_WINDOW):m.start()]
            if any(re.search(r"\b" + n + r"\b", lead) for n in _NEGATORS):
                continue          # negated -- keep looking for a real cue
            return True
    return False


def observation_level(text):
    """red / amber / green for one of the Assistant's observations."""
    t = str(text or "").lower()
    if _cue_present(t, _RED_WORDS):
        return "red"
    if _cue_present(t, _AMBER_WORDS):
        return "amber"
    return "green"


def match_result(observation, results):
    """The result row an observation is about, or None.

    Containment, not string equality: the panel writes "Maximum shear stress at
    wall (1.259 MPa) exceeds..." about a result it listed as "Shear stress at
    wall (max)". So a row matches when all of its significant words appear in
    the sentence, and the most specific row wins -- otherwise "Volumetric
    shrinkage" would claim the sentence that belongs to "Average volumetric
    shrinkage".
    """
    words = set(_tokens(observation))
    best, best_score = None, 0
    for row in results or []:
        rt = _tokens(row.get("name"))
        if not rt:
            continue
        hit = sum(1 for t in set(rt) if t in words)
        # Every significant word of the result name must be in the sentence.
        if hit < len(set(rt)):
            continue
        if hit > best_score:
            best, best_score = row, hit
    return best


def build_from_assistant(parsed, summary_data=None, meta=None, study_name=None):
    """Turn `assistant_live.fetch_summary()`'s parsed answer into the same dict
    build_summary() returns for the local path.

    The Assistant's three sections map onto the deck's summary slides:

        OBSERVATIONS    -> Problematic Results + Summary of Key Concerns
        RESULTS         -> the Result Statistics table
        RECOMMENDATIONS -> the closing "Points to highlight" slide, grouped by
                           group_recommendations()

    The Analysis Summary slide is deliberately NOT fed from here: it carries
    the threshold findings this project computes itself, and showing the
    Assistant's recommendations there as well only printed the same five
    sentences twice in one deck.

    `summary_data` and `meta` are still consulted, but only for context the
    panel does not return (the melt/mold temperatures and Autodesk's own advice
    strings). No measured value is mixed in: every number on those slides comes
    from the answer.
    """
    results = list(parsed.get("results") or [])
    observations = list(parsed.get("observations") or [])
    recommendations = list(parsed.get("recommendations") or [])

    # One problem row per observation, carrying the figures of the result it
    # refers to. An observation that names no listed result still gets a row --
    # dropping it would lose a finding the Assistant chose to make.
    rows = []
    for obs in observations:
        row = match_result(obs, results)
        level = observation_level(obs)
        rows.append({
            "level": level,
            "result": (row or {}).get("name") or "Analysis note",
            "issue": obs,
            "detail": "",
            "value": (row or {}).get("value") or "",
            "unit": (row or {}).get("unit") or "—",
        })
    rows.sort(key=lambda r: LEVEL_RANK.get(r["level"], 3))

    counts = {
        "red": sum(1 for r in rows if r["level"] == "red"),
        "amber": sum(1 for r in rows if r["level"] == "amber"),
        "green": sum(1 for r in rows if r["level"] == "green"),
    }

    context = build_context(meta)
    line = context_line(context)
    if not line:
        # No .sdy metadata: caption from what the answer itself stated.
        bits = [b for b in (parsed.get("material"), parsed.get("analysis")) if b]
        line = "  •  ".join(bits)

    return {
        "source": "assistant",
        "study": parsed.get("study") or study_name or context.get("study_name") or "",
        "problems": rows,
        "concerns": build_key_concerns(rows),
        "context": context,
        "context_line": line,
        "counts": counts,
        "headline": headline_sentence(rows, counts),
        # The Result Statistics table: the values it read from the study.
        "stat_rows": [{"name": r.get("name", ""), "value": r.get("value", ""),
                       "unit": r.get("unit", "") or "—"} for r in results],
        "observations": observations,
        "recommendations": recommendations,
        "answer": parsed.get("answer", ""),
        "prompt": parsed.get("prompt", ""),
        "elapsed": parsed.get("elapsed", 0.0),
    }


# Which heading a recommendation belongs under on the "Points to highlight"
# slide. Ordered: the first rule that matches wins, so the more specific
# subjects are listed before the general ones. A flat list of five sentences is
# the slide everyone skips; grouped by what a reader would act on, it is the
# slide they take away.
_REC_GROUPS = (
    ("Cooling", ("cool", "cycle time", "ejection temperature", "coolant",
                 "channel", "conformal", "heat")),
    ("Tooling & venting", ("vent", "air trap", "gate", "runner", "sprue",
                           "insert", "ejector", "tool", "mould", "mold design",
                           "cavity")),
    # Before "Process settings" on purpose: "Run a Packing simulation" is a
    # request for another analysis, not a packing profile change, and the
    # process rules would otherwise claim it on the word "packing".
    ("Further analysis", ("run a", "re-run", "rerun", "simulate", "simulation",
                          "analysis to", "study to", "iterate", "validate")),
    ("Process settings", ("melt temperature", "mold temperature", "injection "
                          "speed", "injection time", "packing", "pack ",
                          "hold ", "pressure profile", "flow rate", "velocity",
                          "switchover")),
    ("Part design", ("wall thickness", "rib", "boss", "geometry", "draft",
                     "part design", "redesign")),
)


def group_recommendations(recommendations, material=None):
    """[(heading, [recommendation, ...])] for the deck's highlights slide.

    Anything that matches no rule goes under "Design & process" rather than
    being dropped -- a recommendation the classifier does not recognise is
    still a recommendation.
    """
    buckets = {}
    order = []
    for rec in recommendations or []:
        text = str(rec).strip()
        if not text:
            continue
        heading = "Design & process"
        low = text.lower()
        for name, words in _REC_GROUPS:
            if any(w in low for w in words):
                heading = name
                break
        if heading not in buckets:
            buckets[heading] = []
            order.append(heading)
        buckets[heading].append(text)

    groups = [(h, buckets[h]) for h in order]
    # Four cards is what the slide holds. Beyond that, fold the tail into the
    # last card rather than losing it off the bottom.
    if len(groups) > 4:
        head, tail = groups[:3], groups[3:]
        merged = []
        for _h, lines in tail:
            merged.extend(lines)
        groups = head + [("Also consider", merged)]
    return groups


def build_summary(summary_data, meta=None, study_name=None, assistant=None):
    """Everything the deck needs, in one call.

    summary_data: the dict from collect_analysis_summary() (or a loaded
                  analysis_summary.json).
    meta:         optional ai_assistant.extract() result for study context.
    assistant:    optional parsed answer from assistant_live.fetch_summary().
                  When present it is the source of the summary and the local
                  statistics are not consulted for it.
    """
    if assistant:
        return build_from_assistant(assistant, summary_data, meta, study_name)

    rows = build_problem_rows(summary_data)
    concerns = build_key_concerns(rows)
    context = build_context(meta)
    counts = (summary_data or {}).get("counts") or {}
    return {
        "source": "local",
        "study": study_name or context.get("study_name") or "",
        "problems": rows,
        "concerns": concerns,
        "context": context,
        "context_line": context_line(context),
        "counts": counts,
        "headline": headline_sentence(rows, counts),
        # The local path leaves this empty: the deck then renders the per-node
        # statistics it already computes, exactly as before.
        "stat_rows": [],
    }


def headline_sentence(rows, counts=None):
    """The single sentence that opens the summary.

    It must account for EVERY row the table below it renders. The assistant
    path keeps green observations as rows (dropping one would lose a finding
    the Assistant chose to make), so a sentence counting only red and amber
    left study 37 saying "2 results need action and 2 need review" above a
    five-row table. Green rows are now stated as noted-but-not-flagged rather
    than silently omitted from the count.
    """
    counts = counts or {}
    red = sum(1 for r in rows if r["level"] == "red") or counts.get("red", 0)
    amber = sum(1 for r in rows if r["level"] == "amber") or counts.get("amber", 0)
    green = sum(1 for r in rows if r["level"] == "green")
    if not red and not amber:
        return ("No problematic results were flagged — every measured "
                "result sits within its guide values.")
    parts = []
    if red:
        parts.append("{0} result{1} need{2} action".format(
            red, "s" if red != 1 else "", "" if red != 1 else "s"))
    if amber:
        parts.append("{0} need{1} review".format(amber, "" if amber != 1 else "s"))
    if green:
        parts.append("{0} noted".format(green))
    if len(parts) == 1:
        text = parts[0]
    else:
        text = ", ".join(parts[:-1]) + " and " + parts[-1]
    return "Problematic results — " + text + "."


def to_markdown(summary):
    """The summary as a Markdown table plus concerns list -- the same shape the
    assistant prints in its chat panel. Handy for the log, a .md sidecar, or
    pasting into a ticket; the deck uses the structured lists instead."""
    lines = []
    if summary.get("study"):
        lines.append("Results from study **{0}** — Problematic Results:".format(
            summary["study"]))
        lines.append("")
    if summary.get("context_line"):
        lines.append("_{0}_".format(summary["context_line"]))
        lines.append("")
    rows = summary.get("problems") or []
    if rows:
        lines.append("| Result | Issue | Value | Unit |")
        lines.append("| --- | --- | --- | --- |")
        for r in rows:
            lines.append("| {0} | {1} | {2} | {3} |".format(
                r["result"], r["issue"], r["value"] or "—", r["unit"]))
        lines.append("")
    concerns = summary.get("concerns") or []
    if concerns:
        lines.append("Summary of key concerns:")
        for c in concerns:
            lines.append("{0} **{1}**: {2}".format(c["mark"], c["result"], c["text"]))
    else:
        lines.append(summary.get("headline", ""))
    # The panel's recommendations, when the panel is where this came from.
    for i, rec in enumerate(summary.get("recommendations") or []):
        if i == 0:
            lines.append("")
            lines.append("Recommendations:")
        lines.append("{0}. {1}".format(i + 1, rec))
    if summary.get("stat_rows"):
        lines.append("")
        lines.append("| Result | Value | Unit |")
        lines.append("| --- | --- | --- |")
        for r in summary["stat_rows"]:
            lines.append("| {0} | {1} | {2} |".format(
                r["name"], r["value"] or "—", r["unit"]))
    for advice in (summary.get("context") or {}).get("autodesk_advice") or []:
        lines.append("")
        lines.append("> Autodesk advice: {0}".format(advice))
    lines.append("")
    lines.append("_Source: {0}_".format(
        "Moldflow AI Assistant (live panel)" if summary.get("source") == "assistant"
        else "regenerated locally from this study's statistics"))
    return "\n".join(lines)
