#!/usr/bin/env python3
"""Validate that every charter's md5 is *derivable* from its old monasterium.net URL.

The Static FSDB ``/mom`` route resolves an old URL to a charter WITHOUT a reverse-lookup
index -- it calls :func:`fsdb.charter_md5_from_mom_url`. This script runs that **same**
function over an FSDB root and reports how many charters it gets right, so you can decide
whether to enable the ``/mom`` route or ban it. (The derivation itself lives in
``src/fsdb/momurl.py``; it is deliberately not reimplemented here -- that is the thing under
test.)

Verbosity (fargv ``-v`` count-switch), all diagnostics on stderr, the headline on stdout:

  (default)  headline only: ``reversible: PASS/TOTAL (pct%)``
  -v         also print passing / failing / total
  -vv        also show a tqdm progress bar and the charter / fond / archive counts that
             were involved in mistakes
  -vvv       also print every failing charter path (relative to the root), one per line

``--quit_after_n N`` (default 0 = never) stops after the N-th bad URL and prints EVERYTHING
about the bad charters met -- full path, full URL, the reconstructed atom-id, derived vs
actual md5 -- then exits non-zero.

Usage:
  test/scripts/test_momurl_to_md5.py -f /mnt/data/full_fsdb/fsdb -vv
  test/scripts/test_momurl_to_md5.py -f /mnt/data/full_fsdb/fsdb --quit_after_n 1
"""
import sys
from glob import glob
from pathlib import Path

import fargv
from tqdm import tqdm
from fsdb import charter_md5_from_mom_url, mom_url_to_atom_id

_HEX32 = "[0-9a-f]" * 32


def _read_url(charter_dir):
    try:
        return (charter_dir / "CH.url.txt").read_text().strip()
    except OSError:
        return None


def _fail_reason(url, derived):
    if url is None:
        return "no CH.url.txt"
    if derived is None:
        return "URL is not .../mom/<tail>/charter"
    return "md5 mismatch"


def _print_bad_full(bad, scanned, total, out=sys.stdout):
    """On early quit: dump everything about each bad charter met so far."""
    for i, rec in enumerate(bad, 1):
        print(f"--- bad #{i} ---", file=out)
        print(f"  path:        {rec['path']}", file=out)
        print(f"  rel:         {rec['rel']}", file=out)
        print(f"  url:         {rec['url']!r}", file=out)
        print(f"  reason:      {rec['reason']}", file=out)
        print(f"  dir_md5:     {rec['dir_md5']}", file=out)
        print(f"  derived_md5: {rec['derived_md5']}", file=out)
        atom = mom_url_to_atom_id(rec["url"])
        if atom is not None:
            print(f"  atom_recon:  {atom}", file=out)
    print(f"\nquit after {len(bad)} bad URL(s); scanned {scanned} of {total} charters",
          file=sys.stderr)


def main():
    p, _ = fargv.parse({
        "fsdb_root": "/mnt/data/fsdb",
        "quit_after_n": 0,   # exit after this many bad URLs (0 = never; scan everything)
        "verbosity": fargv.FargvInt(0, short_name="v", is_count_switch=True),
    })

    root = Path(p.fsdb_root)
    charter_dirs = glob(f"{root}/*/{_HEX32}/{_HEX32}")
    total = len(charter_dirs)

    iterable = charter_dirs
    if p.verbosity >= 2:
        iterable = tqdm(charter_dirs, desc="checking charters", unit="charter", file=sys.stderr)

    passing = 0
    bad = []                 # list of failure records
    bad_fonds = set()        # "<archive>/<fond>" of failing charters
    bad_archives = set()     # "<archive>" of failing charters

    for cdir in iterable:
        cpath = Path(cdir)
        dir_md5 = cpath.name
        url = _read_url(cpath)
        derived = charter_md5_from_mom_url(url)      # the function the route uses
        if derived == dir_md5:
            passing += 1
            continue
        rel = str(cpath.relative_to(root))
        parts = rel.split("/")
        bad_archives.add(parts[0])
        bad_fonds.add("/".join(parts[:2]))
        bad.append({"rel": rel, "path": str(cpath), "url": url, "dir_md5": dir_md5,
                    "derived_md5": derived, "reason": _fail_reason(url, derived)})
        if p.quit_after_n and len(bad) >= p.quit_after_n:
            _print_bad_full(bad, passing + len(bad), total)
            sys.exit(1)

    failing = len(bad)

    if p.verbosity >= 3:
        for rec in bad:
            print(rec["rel"])
    if p.verbosity >= 2:
        print(f"mistakes involved {failing} charters across {len(bad_fonds)} fonds "
              f"and {len(bad_archives)} archives", file=sys.stderr)
    if p.verbosity >= 1:
        print(f"passing: {passing}", file=sys.stderr)
        print(f"failing: {failing}", file=sys.stderr)
        print(f"total:   {total}", file=sys.stderr)

    pct = 100.0 * passing / total if total else 0.0
    print(f"reversible: {passing}/{total} ({pct:.4f}%)")
    sys.exit(0 if failing == 0 else 2)


if __name__ == "__main__":
    main()
