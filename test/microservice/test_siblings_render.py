"""Render test for templates/_siblings.html -- the cross-service topnav box.

Checks: the box includes THIS service (is_self) as a non-clickable highlighted "current" entry;
icons come from each service's /<prefix>/icon.ico route; a live sibling deep-links only for a view
type it ADVERTISES (a service that doesn't accept the entity view -- e.g. Slicer, a basket exporter
that advertises only 'root' -- gets its home link, never a charter deep link); a DOWN sibling is
greyed with a prefix badge; a bordered box; the absolute_links origin toggle. Pure Jinja2.
"""
from pathlib import Path

import pytest

jinja2 = pytest.importorskip("jinja2", reason="jinja2 not installed")

TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "ddp_microservices" / "templates"
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)))

SELF = {"name": "Static", "prefix": "st", "url": "http://h:5001",
        "views": ["charter", "fond", "archive", "root"], "launch_cmd": "ddpa_static_fsdb_serve",
        "alive": True, "is_self": True}
LAYOUT = {"name": "Layout", "prefix": "ly", "url": "http://h:5003",
          "views": ["charter", "root"], "launch_cmd": "ddpa_layout_serve", "alive": True, "is_self": False}
SLICER = {"name": "Slicer", "prefix": "sl", "url": "http://h:5005",
          "views": ["root"], "launch_cmd": "ddpa_slicer_serve", "alive": True, "is_self": False}  # exporter: root only
DOWN = {"name": "Detection", "prefix": "dt", "url": "http://h:5002",
        "views": [], "launch_cmd": "ddpa_detection_serve", "alive": False, "is_self": False}


def render(**ctx):
    ctx.setdefault("route_prefix", "/st")
    return ENV.get_template("_siblings.html").render(**ctx)


def test_bordered_box():
    assert "border:" in render(siblings=[])


def test_self_current_icon():
    html = render(siblings=[SELF], absolute_links=False)
    assert '<span class="topnav-sibling current"' in html and 'src="/st/icon.ico"' in html
    assert 'href="/st' not in html


def test_handoff_only_for_advertised_views():
    html = render(siblings=[DOWN, LAYOUT, SLICER, SELF], sibling_md5="abcd", absolute_links=False)
    # Layout advertises charter -> enabled deep hand-off
    assert 'href="/ly/charter/abcd"' in html
    # Slicer advertises only 'root' -> on a charter page it is GREYED (no link at all, not even home)
    assert 'href="/sl/charter/abcd"' not in html and 'href="/sl/"' not in html
    assert '— can\'t open this charter here' in html          # greyed-incapable title
    assert 'src="/sl/icon.ico"' in html                       # but its icon still shows (it is up)
    # Layout icon shown; Detection is DOWN -> greyed prefix badge
    assert 'src="/ly/icon.ico"' in html
    assert 'class="topnav-sibling disabled"' in html and '<span class="prefix-badge">dt</span>' in html


def test_no_context_links_home_for_all_live():
    # a root/list page (no sibling_md5 / handoff): every live sibling links to its home, even Slicer
    html = render(siblings=[LAYOUT, SLICER, SELF], absolute_links=False)
    assert 'href="/ly/"' in html and 'href="/sl/"' in html
    assert 'charter' not in html


def test_proxyless_absolute():
    html = render(siblings=[LAYOUT, SELF], sibling_md5="abcd", absolute_links=True)
    assert 'href="http://h:5003/ly/charter/abcd"' in html and 'src="http://h:5003/ly/icon.ico"' in html
