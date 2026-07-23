#!/usr/bin/env python3
"""The "Static" FSDB gateway service (``ms_id=1``), as a :class:`SharedIndexMicroservice`.

This is the FSDB entity-model surface of the DiDip ecosystem: it serves archives, fonds,
charters and images straight out of a local FSDB slice (see the ``fsdb`` skill), the view
every sibling service links back to. It inherits the whole online contract from
:class:`ddp_microservices.microservice.SharedIndexMicroservice`:

- ``/health`` + ``/info``, Swagger, sibling discovery/monitor, and ``run()`` from
  :class:`DidipMicroservice`;
- the shared sorted charter index (``self.index``) and the ``/basket`` + ``/basket/db``
  routes from :class:`SharedIndexMicroservice`.

It only *adds* the entity/image routes below. Pages render through :meth:`self.render`, so
they extend ``templates/base.html`` and get the topnav with live sibling links for free
(the old hard-coded MOM/slicer links are gone).

Image md5s are indexed by the shared index: ``self.index`` is an
:class:`fsdb.shared_index.FSDBSharedImageIndex` (selected via ``index_class``), so the
``/image`` and ``/iiif`` routes resolve image md5s straight through it (no image glob).

NOTE (TODO): the FSDB is still walked twice at load -- once by
:meth:`SharedIndexMicroservice.load` (the shared charter+image index) and once by
:meth:`StaticFSDB.parse_all` (the md5 -> path dicts the *entity* routes still use).
Those routes should migrate onto ``self.index`` (``charter_path`` / ``charter_relpath`` are
already in place) so the second walk can be dropped; ``/chartermd5_to_path`` already does.

Flask, tqdm and PIL (Pillow) are core dependencies, imported at module top (PIL is already
a top-level requirement via ``ddp_util.iiif`` anyway). Nothing here is imported lazily.
"""
from __future__ import annotations

import os
import sys
import time
from bisect import bisect_left
from pathlib import Path
from typing import Dict, Optional, Tuple

import PIL.Image
from tqdm import tqdm
from flask import Response, jsonify, redirect, request, send_file

from fsdb import FSDB, Charter, Fond, Archive, charter_md5_from_mom_url
from fsdb.shared_index import FSDBSharedImageIndex
from ddp_util import create_pagers
from ddp_util.iiif.iiif import compute_iiif
from ddp_util.config_ms import DdpMsConfigs
from .microservice import SharedIndexMicroservice


class StaticFSDB:
    """Legacy md5 -> path dictionaries (charter / fond / archive) built by a plain filesystem
    walk.

    Retained while the *entity* routes still resolve md5s to absolute paths through these
    dicts; the sorted :class:`FSDBSharedIndex` (``StaticFsdbMicroservice.index``) is the
    intended replacement -- see the module TODO. Image md5s are **no longer** walked here:
    they live in the :class:`FSDBSharedImageIndex` (``self.index``), which the ``/image`` and
    ``/iiif`` routes use directly. The old-MOM-URL -> charter reverse index is gone too: the
    ``/mom`` route now *derives* the charter md5 from the URL (see :meth:`render_charter_momurl`).
    """

    default_root_path: str = "./"
    charter_idx: Dict[str, str] = {}
    fond_idx: Dict[str, str] = {}
    archive_idx: Dict[str, str] = {}
    all_idx: Dict[str, str] = {}
    start_time: float = 0.0
    load_time: float = 0.0

    @staticmethod
    def parse_all(db_root: str, verbose: int = 0
                  ) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str], Dict[str, str]]:
        StaticFSDB.start_time = time.time()
        root = Path(db_root)
        if verbose >= 1:
            print(f"[StaticFSDB] parsing entity paths under {db_root!r} ...", file=sys.stderr, flush=True)
        if verbose >= 2:
            charter_paths = [str(p) for p in tqdm(root.glob('*/*/*'), "Parsing charters", file=sys.stderr) if p.is_dir()]
            fond_paths = [str(p) for p in tqdm(root.glob('*/*'), "Parsing fonds", file=sys.stderr) if p.is_dir()]
            archive_paths = [str(p) for p in tqdm(root.glob('*'), "Parsing archives", file=sys.stderr) if p.is_dir()]
        else:
            charter_paths = [str(p) for p in root.glob('*/*/*') if p.is_dir()]
            fond_paths = [str(p) for p in root.glob('*/*') if p.is_dir()]
            archive_paths = [str(p) for p in root.glob('*') if p.is_dir()]

        charter_idx = {p.split("/")[-1]: p for p in charter_paths}
        fond_idx = {p.split("/")[-1]: p for p in fond_paths}
        archive_idx = {p.split("/")[-1]: p for p in archive_paths}

        all_idx = {}
        all_idx.update(charter_idx)
        all_idx.update(fond_idx)
        all_idx.update(archive_idx)
        StaticFSDB.charter_idx = charter_idx
        StaticFSDB.fond_idx = fond_idx
        StaticFSDB.archive_idx = archive_idx
        StaticFSDB.all_idx = all_idx
        StaticFSDB.load_time = time.time() - StaticFSDB.start_time
        if verbose >= 1:
            print(f"[StaticFSDB] parsed {len(charter_idx)} charters, {len(fond_idx)} fonds, "
                  f"{len(archive_idx)} archives in {StaticFSDB.load_time:.2f}s",
                  file=sys.stderr, flush=True)
        return charter_idx, fond_idx, archive_idx, all_idx

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            path = StaticFSDB.default_root_path
        self.path = path
        self.archives = [str(p).split("/")[-1] for p in Path(path).glob('*') if p.is_dir()]


class StaticFsdbMicroservice(SharedIndexMicroservice):
    """FSDB gateway service: entity/image routes over a local FSDB slice.

    Launch (if not running):  ddpa_static_fsdb_serve
    """

    config_class = DdpMsConfigs.MsStatic
    GLOBAL_ROUTE_PREFIX = "st"                   # every route is /st/... (own-the-prefix)
    LAUNCH_CMD = "ddpa_static_fsdb_serve"
    VIEWS = ("charter", "fond", "archive", "root")  # hand-off view types (serves /st/charter,/fond,/archive,/)
    filepattern = None  # index the whole charter namespace; no app-output overlay
    index_class = FSDBSharedImageIndex  # self.index carries the per-image universe too

    def load(self):
        super().load()  # builds self.index (charter + image namespace, knows fsdb_root)
        # TODO: second walk -- the md5 -> path dicts the *entity* routes still use.
        StaticFSDB.default_root_path = self.cfg.fsdb_root
        StaticFSDB.parse_all(self.cfg.fsdb_root, verbose=getattr(self.cfg, "verbosity", 0))

    def register_routes(self):
        super().register_routes()  # /basket + /basket/db
        app = self.app

        @app.route('/st/image/<md5>')
        def serve_image(md5):
            """Serve the raw image bytes for an image md5.
            ---
            responses:
              200: {description: image bytes}
              404: {description: unknown image}
            """
            try:
                path = self.index.image_path(md5)
            except (KeyError, FileNotFoundError):
                return f"Unknown image {md5}", 404
            file_ext = path.suffix.lower().lstrip(".")
            if file_ext in ("jpg", "jpeg"):
                return send_file(str(path), mimetype='image/jpeg')
            elif file_ext == "png":
                return send_file(str(path), mimetype='image/png')
            raise Exception(f"Unknown file extension {file_ext}")

        @app.route('/st/iiif/<md5>')
        @app.route('/st/iiif/<md5>/<region>')
        @app.route('/st/iiif/<md5>/<region>/<size>')
        @app.route('/st/iiif/<md5>/<region>/<size>/<rotation>')
        @app.route('/st/iiif/<md5>/<region>/<size>/<rotation>/<quality>')
        @app.route('/st/iiif/<md5>/<region>/<size>/<rotation>/<quality>.<format>')
        def serve_iiif(md5, region="full", size="max", rotation="0", quality="default", format="jpg"):
            """IIIF image API for an image md5.
            ---
            responses:
              200: {description: transformed image}
              404: {description: unknown image}
            """
            try:
                img_path = self.index.image_path(md5)
            except (KeyError, FileNotFoundError):
                return f"Unknown image {md5}", 404
            pil_image = PIL.Image.open(str(img_path))
            buffer, mimetype = compute_iiif(pil_image, md5, region, size=size, rotation=rotation,
                                            quality=quality, format=format)
            return send_file(buffer, mimetype=mimetype)

        @app.route('/st/chartermd5_to_path/<md5>')
        def charter_md5_to_path(md5):
            """Charter md5 -> its ``archive/fond/charter`` relative path (for Thea).
            ---
            responses:
              200: {description: relative path}
              404: {description: unknown charter}
            """
            if md5 in self.index:
                return jsonify({"path": self.index.charter_relpath(md5)}), 200
            return jsonify({"error": f"Charter with md5 {md5} not found"}), 404

        @app.route('/st/mom/<path:old_path>')
        def render_charter_momurl(old_path):
            """Resolve an old monasterium.net URL to its charter view.

            The charter md5 is *derived*, not looked up: an old URL is
            ``https://www.monasterium.net/mom/<atom-tail>/charter`` and the charter dir name
            is ``md5("tag:www.monasterium.net,2011:/charter/" + <atom-tail>)``. The atom-tail
            keeps its literal ``%``-encoding, so we read it from the RAW request URI (Werkzeug
            percent-decodes ``old_path``); we fall back to the decoded path if the raw URI is
            unavailable.
            ---
            responses:
              200: {description: charter page}
              400: {description: malformed mom URL}
              404: {description: unknown charter}
            """
            raw = request.environ.get('RAW_URI') or request.environ.get('REQUEST_URI') or ('/' + old_path)
            md5 = charter_md5_from_mom_url(raw)
            if md5 is None:
                return "Malformed mom URL", 400
            if md5 not in self.index:
                return "Unknown charter", 404
            charter = Charter(path=str(self.index.charter_path(md5)))
            return self.render('static_charter.html', obj=charter)

        @app.route('/st/get_cei/<md5>')
        def serve_cei(md5):
            """Download a charter's CEI XML.
            ---
            responses:
              200: {description: CEI xml}
            """
            charter = Charter(path=StaticFSDB.charter_idx[md5])
            return Response(charter.cei_str, mimetype="application/xml",
                            headers={"Content-Disposition": "attachment; filename=cei.xml"})

        @app.route('/st/charter/<md5>')
        def render_charter(md5):
            """Shared charter view (images via IIIF + metadata + live sibling links).
            ---
            responses:
              200: {description: rendered charter}
            """
            file_format = request.args.get('format') or 'html'
            charter = Charter(path=StaticFSDB.charter_idx[md5])
            return self.render('static_charter.html', obj=charter, format=file_format)

        def _deprecate(new_prefix, old_prefix, md5, skip, item_count):
            """302-redirect a deprecated /paged_* URL to its canonical /* route, with a
            Deprecation/Warning header, a server log, and a ?_deprecated hint the landing page
            surfaces in the message bar."""
            tail = f"{md5}/{skip}/{item_count}" if skip is not None else f"{md5}"
            target = f"{new_prefix}/{tail}"
            sys.stderr.write(f"[deprecated] {request.path} -> {target} (use {new_prefix})\n")
            resp = redirect(f"{target}?_deprecated={old_prefix}", code=302)
            resp.headers["Deprecation"] = "true"
            resp.headers["Warning"] = f'299 - "{old_prefix} is deprecated; use {new_prefix}"'
            return resp

        @app.route('/st/archive/<md5>')
        @app.route('/st/archive/<md5>/<skip>/<item_count>')
        def render_paged_archive(md5: str, skip: int = 0, item_count: int = 10):
            """Paged list of an archive's fonds (the standardized Archive view).
            ---
            responses:
              200: {description: fond list page}
            """
            skip = int(skip)
            item_count = int(item_count)
            file_format = request.args.get('format') or 'html'
            archive = Archive(path=StaticFSDB.archive_idx[md5])
            fond_paths = sorted(archive.fond_paths)[skip:skip + item_count]
            fonds = [Fond(archive.path / f) for f in fond_paths]
            fond_n_id_descr = [(skip + n + 1, f.name, str(f)) for n, f in enumerate(fonds)]
            pagers = create_pagers(len(archive.fond_paths), skip, item_count)
            return self.render('static_paged_archive.html', obj=archive,
                               query_str_short_descr=f"Showing fonds {skip + 1} to "
                                                     f"{min(skip + item_count, len(archive.fond_paths))} of {len(archive.fond_paths)}",
                               fondlist=fond_n_id_descr, fpcnl_pagers=pagers,
                               paging_base_url=f"/st/archive/{md5}/", format=file_format)

        @app.route('/st/paged_archive/<md5>')
        @app.route('/st/paged_archive/<md5>/<skip>/<item_count>')
        def deprecated_paged_archive(md5, skip=None, item_count=None):
            """DEPRECATED alias -> /archive/<id>. (kept as a redirect for old links)"""
            return _deprecate('/st/archive', '/st/paged_archive', md5, skip, item_count)

        @app.route('/st/fond/<md5>')
        @app.route('/st/fond/<md5>/<skip>/<item_count>')
        def render_paged_fond(md5: str, skip: int = 0, item_count: int = 10):
            """Paged list of a fond's charters (Fond view + page-scoped Charter-set view).
            ---
            responses:
              200: {description: charter list page}
            """
            skip = int(skip)
            item_count = int(item_count)
            file_format = request.args.get('format') or 'html'
            fond = Fond(path=StaticFSDB.fond_idx[md5])
            charter_paths = sorted(fond.charter_paths)[skip:skip + item_count]
            charters = [Charter(fond.path / f) for f in charter_paths]
            charter_n_id_descr = [(skip + n + 1, c.md5_id, '/'.join(c.archival_signature.split("/")[-2:]))
                                  for n, c in enumerate(charters)]
            pagers = create_pagers(len(fond.charter_paths), skip, item_count)
            return self.render('static_paged_fond.html', obj=fond,
                               query_str_short_descr=f"Showing charters {skip + 1} to "
                                                     f"{min(skip + item_count, len(fond.charter_paths))} of {len(fond.charter_paths)}",
                               charterlist=charter_n_id_descr, fpcnl_pagers=pagers,
                               viewed_charter_list=[c.md5_id for c in charters],  # the page's charter set
                               paging_base_url=f"/st/fond/{md5}/", format=file_format)

        @app.route('/st/paged_fond/<md5>')
        @app.route('/st/paged_fond/<md5>/<skip>/<item_count>')
        def deprecated_paged_fond(md5, skip=None, item_count=None):
            """DEPRECATED alias -> /fond/<md5>. (kept as a redirect for old links)"""
            return _deprecate('/st/fond', '/st/paged_fond', md5, skip, item_count)

        @app.route('/st/')
        @app.route('/st/paged_fsdb/')
        @app.route('/st/paged_fsdb/<skip>/<item_count>')
        def render_paged_fsdb(skip: int = 0, item_count: int = 10):
            """Paged list of the slice's archives (grouped by country flag).
            ---
            responses:
              200: {description: archive list page}
            """
            skip = int(skip)
            item_count = int(item_count)
            file_format = request.args.get('format') or 'html'
            fsdb = FSDB(root_path=StaticFSDB.default_root_path)
            archive_names = sorted(p.split("/")[-1] for p in fsdb.archive_paths if Path(p).is_dir())
            countries = [a.split("-")[0].upper() for a in archive_names]
            country_count = sorted((Archive.flags.get(c, '🇺🇳'), bisect_left(archive_names, c), countries.count(c))
                                   for c in set(countries))
            archive_paths = sorted(fsdb.archive_paths)[skip:skip + item_count]
            archives = [Archive(fsdb.root_path / f) for f in archive_paths]
            archive_n_id_descr = [(skip + n + 1, a.name, str(a)) for n, a in enumerate(archives)]
            pagers = create_pagers(len(fsdb.archive_paths), skip, item_count)
            return self.render('static_paged_fsdb.html', obj=fsdb, country_count=country_count,
                               query_str_short_descr=f"Showing archives {skip + 1} to "
                                                     f"{min(skip + item_count, len(fsdb.archive_paths))} of {len(fsdb.archive_paths)}",
                               archivelist=archive_n_id_descr, fpcnl_pagers=pagers,
                               paging_base_url="/st/paged_fsdb/", format=file_format)


def main_launch_fsdb_microservice():
    """``ddpa_static_fsdb_serve`` entry point: build the service and serve it."""
    StaticFsdbMicroservice().run()


if __name__ == "__main__":
    main_launch_fsdb_microservice()
