"""Derive a charter's FSDB md5 from an old monasterium.net (MOM) URL -- no index, no
filesystem access. This is the inverse of the URL that ``CH.url.txt`` stores.

A charter directory name is ``md5(CH.atom_id.txt)``, and the atom-id is
``tag:www.monasterium.net,2011:/charter/<tail>`` where ``<tail>`` is exactly the segment of
the MOM URL between ``/mom/`` and ``/charter`` -- kept with its **literal** percent-encoding
(the atom-id itself stores e.g. ``AA_0021_%7C_x`` encoded, so the tail is hashed verbatim,
never unquoted). So the charter md5 is recomputable from the URL alone.

This is the single source of truth used by the Static ``/mom`` route (instead of a
reverse-lookup index) and validated over a whole FSDB by
``test/scripts/test_momurl_to_md5.py``.
"""
import hashlib

CHARTER_ATOM_PREFIX = "tag:www.monasterium.net,2011:/charter/"


def mom_url_to_atom_id(url):
    """The charter atom-id encoded in an old MOM URL, or ``None`` if ``url`` is not of the
    form ``.../mom/<tail>/charter``. ``<tail>`` is taken verbatim (its literal ``%``-encoding
    is what the atom-id stores)."""
    if url is None or "/mom/" not in url or "/charter" not in url:
        return None
    tail = url.split("/mom/", 1)[1].rsplit("/charter", 1)[0]
    return f"{CHARTER_ATOM_PREFIX}{tail}"


def charter_md5_from_mom_url(url):
    """The charter md5 (32-hex, its FSDB directory name) derived from an old MOM URL, or
    ``None`` if the URL does not encode a charter."""
    atom = mom_url_to_atom_id(url)
    return None if atom is None else hashlib.md5(atom.encode()).hexdigest()
