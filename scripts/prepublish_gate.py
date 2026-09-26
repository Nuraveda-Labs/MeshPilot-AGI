#!/usr/bin/env python3
"""Refuse to publish the OSS template while it still leaks.

Why a script and not a careful read
-----------------------------------
The template is extracted from a repo that IS the operator's working diary.
A human eyeballing a 30k-line diff will miss a brand name in a test fixture,
and the failure is silent — a published repo does not complain. This runs in
CI on every change, not once at publication, because every future sync is
another chance to leak.

What it checks, and why each one is here
----------------------------------------
Every pattern below corresponds to something that ACTUALLY leaked or nearly
did during OSS-1/OSS-2, not to a hypothetical:

  brand ids / names   whatever your config declares — the reason the
                      template exists
  env prefixes        `<PREFIX>_` — a live credential namespace
  people              the operator and the AI-influencer personas
  credential shapes   a key accidentally committed is not recoverable by
                      deleting it; assume anything matching is burned
  real brand configs  brand/configs/*.json other than the example

Lessons this encodes from the OSS-2 CI work, all of which cost a round trip:

  1. MATCH CASE-INSENSITIVELY. A case-sensitive sweep for a lowercase brand id
     here reported ZERO while 30 title-case spellings of it remained.
  2. MATCH ON WORD BOUNDARIES. A raw two-letter prefix search matched INSIDE
     longer unrelated names — a prefix like `XY` hitting the middle of
     `META_PAXY_ID` — and produced 49 false positives out of 65 hits here.
     (The original note spelled that example with a REAL prefix and thereby
     tripped this gate's own rule. Left as a joke that cost a run.)
  3. STRIP HTML BEFORE MATCHING text. A handle split by tags — `@brand<b>_</b>x` —
     reads as a bare `@brand` otherwise.
  4. SCOPE TO WHAT SHIPS. Scanning the whole repo flags docs/plans and
     control-plane, which are deliberately private and always will be.

Usage:
    uv run python scripts/prepublish_gate.py          # scan the ship manifest
    uv run python scripts/prepublish_gate.py --json   # machine-readable
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent

# § 4 of docs/plans/2026-09-21-oss-template-extraction.md. ONLY these are
# scanned: everything else is private by design and flagging it is noise.
# `brand/schema` ships because SHIPPED CODE READS IT: `influencer/persona.py` loads
# `brand/schema/persona.schema.json` unconditionally when jsonschema is installed, and only
# `ImportError` is caught — so without it `load_persona` raises FileNotFoundError. Proven by
# simulation 2026-09-23, not inferred. Same class as the launchd plist in OSS-4: shipped code
# reaching for a file the manifest does not carry.
SHIP_DIRS = ("src", "gateway", "supabase/migrations", "tests", "brand/schema",
             # Both carry ONE generic worked example each, after the named ones were removed
             # (configs.example) or carved out (personas.example). A template whose example
             # directories are empty teaches nothing.
             "brand/configs.example", "brand/personas.example")
# Carve-outs INSIDE a ship dir. `tests/` ships, but `tests/operator/` validates THIS
# deployment's own config against files that never ship (the launchd plist), so it would
# both break on export and carry brand names into the template. Keeping the exception here
# rather than dropping the whole of `tests/` keeps the default "tests ship" — the useful
# posture — and makes each exclusion a line someone has to justify.
# These stay TRACKED, so `tracked_outside_manifest()` still lists them: excluded from the
# scan, never invisible to it. An allow-list you cannot see past is how the plist was missed.
NO_SHIP_DIRS = ("tests/operator",)
# File-level carve-outs, for a dir that ships ALMOST everything — read from the config, because
# WHICH files are private is deployment-specific. `brand/personas.example/` has to ship
# (`influencer/persona.py` resolves `<id>.example.json` there, and the influencer subsystem is 12
# shipped modules that are unusable without one) while an operator's own personas must not: a
# persona IS a public identity, so treat a real one the way you treat a credential.
#
# ⚠️ Listing them here as literals put a persona name INSIDE the shipped engine and failed this
# gate — the same self-reference that forced the pattern split in the first place.
# Same visibility rule as NO_SHIP_DIRS: they stay TRACKED, so the advisory keeps listing them.
# (bound after the config loads, below — `_CFG` does not exist yet here)
# ⚠️ ARCHITECTURE.md was listed under SHIP_DOCS (i.e. `docs/ARCHITECTURE.md`) and that file
# does not exist — the repo's is at the ROOT. The entry resolved to nothing, so the gate never
# scanned it AND a manifest-driven export would have omitted the architecture doc outright.
# `.env.example` was never listed at all, and a template without its env template is not one.
# ⚠️ Adding a file here is how the export becomes RUNNABLE, and the first export proved the
# manifest did not produce a runnable repo at all: no `pyproject.toml` (no deps, no build, no
# pytest/ruff config), no `main.py` (the ASGI entrypoint CI's own smoke test imports), no
# `uv.lock`. A template nobody can install is not a template.
#
# 🔴 `.gitignore` is a SAFETY file here, not housekeeping. It carries
# `brand/configs/*.json` + `!brand/configs/example.json` and `.env`. Ship the template without it
# and the first thing a user does — drop in their real brand config and .env — gets committed by
# the next `git add -A`. That is precisely the `.env.bak` accident § 3 cites as the reason this
# lane exports by fresh repo in the first place.
#
# NOT shipped, deliberately: `CLAUDE.md`, `AGENTS.md`, `KIMI.md` (the operator's own agent
# instructions, thick with private operational detail) and `.fastapicloudignore` (our hosting
# choice, not the template's).
# Files that ship at a DIFFERENT path than they live at: source -> path in the export.
# `template/.github/workflows/ci.yml` cannot live at `.github/workflows/ci.yml` here, because
# this repo's own CI already occupies that path and is drift-aware, gates on `production` and
# builds `web/` — none of which the template has.
#
# ⚠️ Before this existed the template's CI lived ONLY in the public repo, so a mirroring sync
# would have DELETED it on the first run. Keeping it here means the export is a complete
# description of the public tree, which is what makes sync safe to mirror.
# The sources are in the manifest, so the gate scans them like anything else that ships.
TEMPLATE_OVERLAY = {
    "template/.github/workflows/ci.yml": ".github/workflows/ci.yml",
}

SHIP_FILES = (
    "README.md", "LICENSE", "ARCHITECTURE.md", ".env.example",
    "pyproject.toml", "uv.lock", "main.py", ".gitignore",
    "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    ".gitleaks.toml", ".pre-commit-config.yaml",
    # The gate itself ships, so a fork inherits the leak protection — but ONLY the engine and
    # the example config. `prepublish.config.json` (this deployment's real names) must never be
    # here; `config_would_be_published()` fails the run if it ever is.
    "scripts/prepublish_gate.py", "scripts/export_template.py",
    "prepublish.config.example.json",
)
SHIP_DOCS = ("VISION.md", "THE-METHOD.md",
             "DOC-SYSTEM.md", "LANE-LIFECYCLE.md", "ROLES.md")

# ── This deployment's OWN names come from prepublish.config.json, NOT from this file ──
#
# They used to be literals here, which made the gate itself unshippable: its BRANDS, PEOPLE and
# IDENTIFIERS lists ARE our brands, people and infrastructure, so publishing the template meant
# publishing the very list of things we scan for. Splitting engine from patterns is what lets the
# template carry a working gate.
#
# ⚠️ Word-boundary rules, learned the hard way and repeated in the example config:
#   - LEADING `\b` only. `_` is a word character, so `\bnura[_ -]?veda\b` did NOT match
#     `acme_lab` when the brand is `acme` — a live id. Every `_suffix` spelling was missed while the hyphen
#     forms matched, because `-` IS a boundary.
#   - The leading `\b` must STAY for prefixes: it is what stops `XY_` matching `META_PAXY_ID`.
CONFIG = ROOT / "prepublish.config.json"
CONFIG_EXAMPLE = ROOT / "prepublish.config.example.json"


def _load_config() -> dict:
    """Read this deployment's patterns, or fail LOUDLY.

    ⚠️ Deliberately no fallback to the example. Falling back would scan for placeholder names,
    match nothing, and print PASS — a confident green over a repo nobody configured. That is the
    exact failure this whole lane keeps finding (a manifest entry matching nothing, a suffix
    allow-list skipping a file), and it is worse than having no gate at all, because it is
    trusted. Refusing to run is the only safe default.
    """
    if not CONFIG.exists():
        raise SystemExit(
            f"prepublish gate: no {CONFIG.name}.\n"
            f"  cp {CONFIG_EXAMPLE.name} {CONFIG.name}   # then put YOUR names in it\n"
            "  Refusing to scan with the example's placeholders: that would report PASS over a\n"
            "  repo nobody has actually configured."
        )
    cfg = json.loads(CONFIG.read_text())
    for key in ("brands", "prefixes", "people"):
        if not cfg.get(key):
            raise SystemExit(
                f"prepublish gate: {CONFIG.name} has no '{key}'. An empty rule set scans for\n"
                "  nothing and passes everything. Declare it, or delete the key deliberately\n"
                "  after reading what it is for."
            )
    return cfg


_CFG = _load_config()
BRANDS = [tuple(r) for r in _CFG["brands"]]
PREFIXES = [tuple(r) for r in _CFG["prefixes"]]
PEOPLE = [tuple(r) for r in _CFG["people"]]
NO_SHIP_FILES = tuple(_CFG.get("no_ship_files", ()))

# Hosting identifiers that are universal — any Supabase / FastAPI Cloud user wants these, so they
# stay in the engine. Deployment-specific ones (a team or account name) come from the config.
# Found by hand 2026-09-24: the README carried a live Supabase project ref and origin hostname —
# neither a brand nor a person, so every other rule was blind to them.
# A Supabase project ref is exactly 20 lowercase letters; anchored on context so a long lowercase
# word cannot trip it.
IDENTIFIERS = [
    (r"[a-z0-9-]+\.fastapicloud\.dev", "deployment origin hostname"),
    (r"\b[a-z]{20}\.supabase\.(co|in)\b", "Supabase project ref (host)"),
    (r"(?:project|ref|SUPABASE[_A-Z]*)[`'\" :=]+([a-z]{20})\b", "Supabase project ref"),
] + [tuple(r) for r in _CFG.get("identifiers", [])]

# Deliberately shaped, not generic: a bare 32-hex string matches half the
# test suite's uuids, so these anchor on vendor-specific prefixes.
CREDENTIALS = [
    (r"\bsk-[A-Za-z0-9]{20,}", "OpenAI-style secret key"),
    (r"\bsk-ant-[A-Za-z0-9-]{20,}", "Anthropic key"),
    (r"\bghp_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"\bEAA[A-Za-z0-9]{50,}", "Meta long-lived token"),
    (r"\bre_[A-Za-z0-9]{20,}", "Resend key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
]

ALL_RULES = [(*r, kind) for kind, rules in
             (("brand", BRANDS), ("prefix", PREFIXES),
              ("person", PEOPLE), ("identifier", IDENTIFIERS),
              ("credential", CREDENTIALS))
             for r in rules]

TEXT_SUFFIXES = {".py", ".md", ".json", ".sql", ".toml", ".yml", ".yaml",
                 ".html", ".css", ".js", ".ts", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".pytest_cache"}

# ⚠️ EXACTLY ONE exception, named rather than a pattern. A detector's own test
# suite must contain the strings it detects — `BEGIN RSA PRIVATE KEY`,
# every pattern it asserts on — so scanning it makes the gate unpassable by construction.
# This is a single explicit path, not a glob: a broad exclusion is somewhere to
# hide a real leak, which is the opposite of the point.
SELF_TEST = "tests/test_prepublish_gate.py"


def _tracked() -> set[pathlib.Path]:
    """Files git actually knows about.

    ⚠️ This distinction is the whole correctness of the gate. `brand/configs/
    *.json` is GITIGNORED — five real brand configs sit in the working tree
    and NONE of them are in git. A gate that scans the working tree reports
    them as the largest leak in the manifest, which is both alarming and
    false: a git-based export cannot carry an ignored file.
    """
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True)
    return {ROOT / f for f in out.stdout.split("\0") if f}


def untracked_in_manifest() -> list[pathlib.Path]:
    """Files present on disk, inside the ship manifest, that git ignores.

    A git-based export drops these. A `cp -r` export carries them straight
    into the public repo. Reported separately rather than counted as leaks,
    because which one is true depends on HOW you export — and § 3 of the plan
    chose a fresh git repo precisely to avoid this class of accident.
    """
    tracked = _tracked()
    return [p for p in _manifest_paths() if p not in tracked]


def tracked_outside_manifest() -> list[pathlib.Path]:
    """Tracked files the manifest does NOT cover — so the gate never scans them.

    ⚠️ The mirror image of `untracked_in_manifest`, and the one that actually bit.
    `deploy/com.meshpilot.seo-cycle.plist` is tracked, carries a customer's brand terms
    and five `<PREFIX>_*` variable names, and the gate reported a clean scan of 374 files
    without ever opening it — because `deploy/` is not in SHIP_DIRS.

    Scoping to what ships is right (lesson 4), but an unscanned tracked file should be a
    DECISION, not an accident. Advisory, not a failure: most of these are genuinely
    private by design.
    """
    manifest = set(_manifest_paths())
    out = []
    for p in sorted(_tracked()):
        if p in manifest or SKIP_DIRS & set(p.parts):
            continue
        # ⚠️ Decide text-vs-binary by CONTENT, not by extension. The first version of
        # this filtered on TEXT_SUFFIXES and so skipped `.plist` — i.e. it silently
        # excluded the exact file that motivated writing it. An allow-list of
        # extensions is the same shape of blind spot as an allow-list of directories.
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        if b"\0" in blob[:4096]:
            continue
        out.append(p)
    return out


def config_would_be_published() -> bool:
    """True if THIS deployment's private pattern config sits inside the ship manifest.

    ⚠️ Self-protection. The config is the single file that lists, in one place, every brand,
    person and piece of infrastructure we scan for — so publishing it would leak the complete
    index of what we consider sensitive, which is strictly worse than any single finding the
    gate catches. One careless `SHIP_FILES` edit is all it would take, and the gate would
    happily report PASS while doing it (the config's own contents are patterns, not matches).
    """
    return CONFIG.resolve() in {p.resolve() for p in _manifest_paths()}


def unresolved_manifest_entries() -> list[str]:
    """Declared manifest entries that match NO file on disk.

    ⚠️ This is the defect that motivated the check. `ARCHITECTURE.md` sat in SHIP_DOCS — i.e.
    `docs/ARCHITECTURE.md` — for the life of the gate. That file has never existed; the repo's
    is at the root. The entry resolved to nothing, so the gate reported a confident "scanned
    374 files" while never opening the architecture doc, and a manifest-driven export would
    have shipped the template WITHOUT it.

    A manifest is an allow-list, and this repo has now been bitten three times by allow-lists
    that were wrong in a direction nobody could see: SHIP_DIRS missing `deploy/`, the advisory
    filtering on TEXT_SUFFIXES, and this. A silent no-match is the worst of them, because the
    output looks identical to success. So: a typo'd or stale entry is a HARD failure.
    """
    missing = []
    for d in SHIP_DIRS:
        if not (ROOT / d).is_dir():
            missing.append(f"SHIP_DIRS: {d}/")
    for f in SHIP_FILES:
        if not (ROOT / f).exists():
            missing.append(f"SHIP_FILES: {f}")
    for f in SHIP_DOCS:
        if not (ROOT / "docs" / f).exists():
            missing.append(f"SHIP_DOCS: docs/{f}")
    for src, dest in TEMPLATE_OVERLAY.items():
        if not (ROOT / src).exists():
            missing.append(f"TEMPLATE_OVERLAY: {src} (would ship as {dest})")
    return missing


def stale_carve_outs() -> list[str]:
    """NO_SHIP_DIRS entries pointing at nothing — ADVISORY, never fatal.

    ⚠️ The direction of the error is what separates this from `unresolved_manifest_entries`, and
    conflating them made the SHIPPED gate fail on a clean template. A missing SHIP_ entry means
    something we believe ships does NOT: the export silently omits it, and nobody finds out.
    A missing NO_SHIP_ entry means we believe we are excluding something that is not there: it
    excludes nothing, so those files get SCANNED — noisier, never less safe.

    Concretely: `tests/operator/` is carved out here and, by design, does not exist in an export.
    Treating that as a hard failure made a freshly exported template exit 1 while printing PASS.
    """
    stale = [f"NO_SHIP_DIRS: {n}/ (carve-out for a path that is not there)"
             for n in NO_SHIP_DIRS if not (ROOT / n).exists()]
    stale += [f"NO_SHIP_FILES: {n} (carve-out for a path that is not there)"
              for n in NO_SHIP_FILES if not (ROOT / n).exists()]
    return stale


def _manifest_paths() -> list[pathlib.Path]:
    """Every path in the ship manifest, tracked or not."""
    out: list[pathlib.Path] = []
    for d in SHIP_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file() or SKIP_DIRS & set(p.parts):
                continue
            rel = p.relative_to(ROOT).as_posix()
            if any(rel == n or rel.startswith(n + "/") for n in NO_SHIP_DIRS):
                continue
            if rel in NO_SHIP_FILES:
                continue
            out.append(p)
    for f in SHIP_FILES:
        if (ROOT / f).exists():
            out.append(ROOT / f)
    for src in TEMPLATE_OVERLAY:
        if (ROOT / src).exists():
            out.append(ROOT / src)
    for f in SHIP_DOCS:
        if (ROOT / "docs" / f).exists():
            out.append(ROOT / "docs" / f)
    cfgs = ROOT / "brand" / "configs"
    if cfgs.exists():
        out.extend(p for p in cfgs.glob("*.json"))
    return sorted(out)


def ship_files() -> list[pathlib.Path]:
    """What a git-based export would actually publish: manifest ∩ tracked."""
    tracked = _tracked()
    return [p for p in _manifest_paths()
            if p in tracked and str(p.relative_to(ROOT)) != SELF_TEST]


# The org's own name inside its own GitHub URLs is ATTRIBUTION, not leakage — badges,
# the repo link, the "built by" footer. Stripping those would break working links to
# say nothing useful. Narrow on purpose: only a github.com/<org> URL, never a bare
# mention, so a real leak in prose still fires.
#
# ⚠️ The URL-only form left the markdown LINK TEXT behind: `[**Your Org**](url)` became
# `[**Your Org**]( )` and still fired, so the "built in the open by" byline read as a leak
# in both README and ARCHITECTURE. The first alternative consumes the whole link, but ONLY when
# the href is a real `attribution_org` URL — link text alone excuses nothing.
#
# The org name may ALSO be a live brand id (it is in this deployment), so
# the underlying rule has to stay: the publisher's byline is the exception, not the pattern.
# The URL carries the org EXACTLY (`github.com/Your-Org`); the link TEXT beside it is
# written for humans and uses a space ("Your Org"). Matching the exact form in both
# positions makes the byline fire — caught by the round-trip check below, not by reading.
# So: exact for the href, separator-tolerant for the text.
_ORG_RAW = _CFG.get("attribution_org", "")
_ORG = re.escape(_ORG_RAW) or r"(?!)"                       # (?!) never matches
_ORG_LOOSE = r"[-_ ]?".join(re.escape(part) for part in re.split(r"[-_ ]+", _ORG_RAW)) \
    if _ORG_RAW else r"(?!)"
_ATTRIBUTION = re.compile(
    rf"\[[^\]]*{_ORG_LOOSE}[^\]]*\]\(\s*https?://(?:github\.com|img\.shields\.io)/"
    rf"[\w./?=&#%-]*{_ORG}[\w./?=&#%-]*\s*\)"
    rf"|(?:github\.com|img\.shields\.io)/[\w./?=&#%-]*{_ORG}[\w./?=&#%-]*",
    re.IGNORECASE,
)


def scan_text(text: str) -> list[tuple[str, str, str]]:
    """Return (kind, reason, matched) for every rule that fires."""
    # Lesson 3: tags split a handle into fragments that read as clean.
    stripped = re.sub(r"<[^>]+>", "", text)
    stripped = _ATTRIBUTION.sub(" ", stripped)
    hits = []
    for pattern, reason, kind in ALL_RULES:
        # Credentials are case-SENSITIVE: `sk-` is a real prefix, `SK-` is not,
        # and folding case turns every SKIP_DIRS-ish constant into a hit.
        flags = 0 if kind == "credential" else re.IGNORECASE
        for m in re.finditer(pattern, stripped, flags):
            hits.append((kind, reason, m.group(0)))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    findings: list[dict] = []
    files = ship_files()
    for p in files:
        # ⚠️ Decide text-vs-binary by CONTENT, not extension — the same fix `tracked_outside_
        # manifest` already carries, and for the same reason. This loop filtered on
        # TEXT_SUFFIXES and so never opened `.env.example` (suffix `.example`) after it was
        # added to the manifest on 2026-09-23: the gate printed PASS over four real findings
        # in it. Third time an extension allow-list has hidden a leak in this file.
        try:
            blob = p.read_bytes()
        except OSError:
            continue
        if b"\0" in blob[:4096]:
            continue
        text = blob.decode("utf-8", errors="ignore")
        for kind, reason, matched in scan_text(text):
            findings.append({
                "file": str(p.relative_to(ROOT)),
                "kind": kind, "reason": reason,
                # Never echo a credential back into CI logs.
                "match": "<redacted>" if kind == "credential" else matched,
            })

    # A real brand config in the template is its own failure: it carries the
    # env prefix, the handles and the offer of a live customer.
    # Only TRACKED configs are a leak. The five real ones on this machine are
    # gitignored, so a git export cannot carry them — reporting them as leaks
    # was alarming and false. They are surfaced under `untracked` instead,
    # because a `cp -r` export WOULD carry them.
    tracked = _tracked()
    cfgs = ROOT / "brand" / "configs"
    if cfgs.exists():
        for p in cfgs.glob("*.json"):
            if p.stem != "example" and p in tracked:
                findings.append({"file": str(p.relative_to(ROOT)),
                                 "kind": "config", "reason": "non-example brand config",
                                 "match": p.name})

    untracked = [str(p.relative_to(ROOT)) for p in untracked_in_manifest()]

    # Which of those actually contain something the gate would have flagged.
    outside = []
    for p in tracked_outside_manifest():
        try:
            hits = scan_text(p.read_text(errors="ignore"))
        except OSError:
            continue
        if hits:
            outside.append((str(p.relative_to(ROOT)), len(hits)))

    if not files:
        print("❌ scanned ZERO files. This is not a pass — it is the gate having nothing to\n"
              "   look at. `ship_files()` is manifest ∩ TRACKED, so an unversioned tree yields\n"
              "   nothing at all.\n"
              "     • in a fresh export: run `git init && git add -A` first\n"
              "     • otherwise: SHIP_DIRS / SHIP_FILES match nothing that git tracks\n"
              "   Found 2026-09-24 by running this gate inside a fresh export, where it printed\n"
              "   'scanned 0 files — PASS'. A template's FIRST run of its own safety tool must\n"
              "   not be a lie.")
        return 1
    if config_would_be_published():
        print(f"❌ {CONFIG.name} is INSIDE the ship manifest. It lists every brand, person and\n"
              "   piece of infrastructure this gate scans for — publishing it leaks the whole\n"
              "   index at once, and no finding above would have told you. Remove it from\n"
              "   SHIP_FILES; ship prepublish.config.example.json instead.")
        return 1
    unresolved = unresolved_manifest_entries()
    if args.json:
        print(json.dumps({"files_scanned": len(files), "findings": findings,
                          "untracked_in_manifest": untracked,
                          "unresolved_manifest_entries": unresolved,
                          "unscanned_tracked_with_hits": outside}, indent=2))
    else:
        print(f"scanned {len(files)} files in the ship manifest")
        if not findings:
            print("PASS — nothing in the ship manifest leaks")
        else:
            by_kind: dict[str, int] = {}
            for f in findings:
                by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
            print(f"FAIL — {len(findings)} finding(s): "
                  + ", ".join(f"{k} {v}" for k, v in sorted(by_kind.items())))
            seen: set[tuple[str, str]] = set()
            for f in findings:
                key = (f["file"], f["match"])
                if key in seen:
                    continue
                seen.add(key)
                print(f"   {f['file']}: {f['match']}  ({f['reason']})")
        if outside:
            print(f"\n⚠️  {len(outside)} TRACKED file(s) OUTSIDE the ship manifest contain names"
                  f"\n    the gate is built to catch. Not scanned, not counted above:")
            for f, n in outside[:10]:
                print(f"      {f}  ({n} would-be finding(s))")
        if untracked:
            print(f"\n⚠️  {len(untracked)} file(s) in the manifest are NOT in git. A git"
                  f"\n    export drops them; a `cp -r` export would publish them:")
            for u in untracked[:10]:
                print(f"      {u}")
        if stale := stale_carve_outs():
            print(f"\n⚠️  {len(stale)} carve-out(s) point at nothing. Harmless — they exclude"
                  f"\n    nothing, so those paths are scanned — but likely stale or a typo:")
            for c in stale:
                print(f"      {c}")
        if unresolved:
            print(f"\n❌ {len(unresolved)} manifest entr(y/ies) match NOTHING on disk. The gate"
                  f"\n   reports a clean scan of files it never opened, and an export built from"
                  f"\n   this manifest silently omits them:")
            for u in unresolved:
                print(f"      {u}")
    # An unresolved entry fails the gate on its own: a manifest that lies about its own
    # contents makes every other number in this report untrustworthy.
    return 1 if (findings or unresolved) else 0


if __name__ == "__main__":
    raise SystemExit(main())
