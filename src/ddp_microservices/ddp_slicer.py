"""Slicer microservice: download an FSDB slice for a set of charters/fonds/archives.

Migrated to the standard :class:`SharedIndexMicroservice`, so it builds an
:class:`fsdb.shared_index.FSDBSharedIndex` at load and serves ``/basket`` + ``/basket/db``.
That lets the client-side basket widget sync the sorted charter universe and ship the
*current basket* to the new ``POST /download_basket`` route as a compact bit vector (see
``static/fsdb_sharedindex.js`` ``SharedIndex.sendBasket`` and ``receive_basket`` here). The
legacy manual TSV paste form (``POST /download_data``) is unchanged.

The service is ``@scoped_ms``: ``POST /sl/download_basket`` consumes the basket through the
inherited ``scope`` proxy instead of decoding a private ``basket`` field, so the wire parsing,
the 400 on a malformed basket and the 409 on a stale index all come from the base.

**Two different things were both called "scope".** The export scope (``fsdb`` / ``fsdb_noimg`` /
``fsdb_and_apps``: *how much of each charter* to pack) now travels as ``export_scope`` on the
wire; the name ``scope`` belongs exclusively to the basket layer. They collided before, and the
scope guard rejected a working export until ``request_has_real_scope()`` was taught to ignore
non-basket values -- that tolerance is no longer what keeps this service alive.
"""
from __future__ import annotations

import re
import sys
from io import BytesIO

from flask import request, send_file, jsonify

from fsdb import FSDB
from fsdb.slice import write_tar_gz, write_zip

from ddp_util.config_ms import DdpMsConfigs
from .microservice import SharedIndexMicroservice, scoped_ms
from .scope import scope

_EXPORT_FORMATS = ("tar", "tar.gz", "zip")
_EXPORT_SCOPES = ("fsdb_noimg", "fsdb", "fsdb_and_apps")


@scoped_ms
class SlicerMicroservice(SharedIndexMicroservice):
    """FSDB slice-export service (``ddpa_slicer_serve``, ms_id=5).

    Launch (if not running):  ddpa_slicer_serve
    """

    config_class = DdpMsConfigs.MsSlicer
    GLOBAL_ROUTE_PREFIX = "sl"                   # every route is /sl/... (own-the-prefix)
    LAUNCH_CMD = "ddpa_slicer_serve"
    VIEWS = ("root",)   # slicer is a set/basket exporter, NOT an entity viewer: it accepts only the
                        # 'root' hand-off (its export form). You scope it with a basket, not a single
                        # charter/fond/archive. (The /sl/charter etc. routes still exist for direct
                        # pre-fill + 404 when the entity isn't in the slice, but are not advertised.)
    index_class = None  # the charter-only base index is all the slice download needs

    def load(self):
        super().load()  # builds self.index (sorted charter universe) + knows fsdb_root
        self.fsdb = FSDB(self.cfg.fsdb_root)
        self.max_charters_allowed = getattr(self.cfg, "max_charter_count_allowed", -1)

    # ---- helpers -----------------------------------------------------------------------
    def _render_form(self, *, context_type="root", context_value=None, charter_ids="",
                     fond_ids="", archive_ids="", filename="fsdb_slice",
                     export_format="tar", export_scope="fsdb"):
        return self.render("slicer_download_form.html", fsdb_root=self.fsdb.root_path,
                           context_type=context_type, context_value=context_value,
                           default_charter_ids=charter_ids, default_fond_ids=fond_ids,
                           default_archive_names=archive_ids, prefered_filename=filename,
                           prefered_format=export_format, prefered_scope=export_scope)

    def _slice_response(self, *, charter_ids, fond_ids, archive_ids, file_format, export_scope,
                        tolerate_missing, prefered_filename):
        """Build the tar/tar.gz/zip attachment for the given selection (shared by the manual
        form and the basket download). ``export_scope`` is how much of each charter to pack --
        NOT the basket scope."""
        if file_format not in _EXPORT_FORMATS or export_scope not in _EXPORT_SCOPES:
            return self.render("generic_error_400.html",
                               error_message="Invalid export format or scope."), 400
        blobs = self.fsdb.generate_files_names_and_blobs(
            charter_ids=charter_ids, fond_ids=fond_ids, archive_ids=archive_ids,
            scope=export_scope, tolerate_missing=tolerate_missing,
            verbose=getattr(self.cfg, "verbosity", 0), max_charters_allowed=self.max_charters_allowed)
        out_f = BytesIO()
        if file_format == "zip":
            write_zip(blobs, out_f)
            mimetype, ext = "application/zip", "zip"
        else:
            write_tar_gz(blobs, out_f, mode="w:gz" if file_format == "tar.gz" else "w")
            mimetype, ext = "application/gzip", file_format
        out_f.seek(0)
        print(f"Sending {ext} ({len(out_f.getvalue())} bytes) for {len(charter_ids or [])} charters",
              file=sys.stderr)
        return send_file(BytesIO(out_f.getvalue()), mimetype=mimetype, as_attachment=True,
                         download_name=f"{prefered_filename}.{ext}")

    # ---- routes ------------------------------------------------------------------------
    def register_routes(self):
        super().register_routes()  # /basket + /basket/db

        # @scoped_ms: every route below must declare itself. Only the export consumes the basket;
        # the form and the pre-fill views are unscoped_route, so a basket sent to one of them is a
        # caller error (400) rather than a silently whole-DB answer.
        @self.unscoped_route("/sl/")
        @self.unscoped_route("/sl/download_form")
        def download_form():
            """The slice-export form (optionally pre-filled from query args).
            ---
            responses:
              200: {description: the export form}
            """
            charters = "\n".join(re.findall(r"[a-fA-F0-9]{32}", request.args.get("charters", "")))
            fonds = "\n".join(re.findall(r"[a-fA-F0-9]{32}", request.args.get("fonds", "")))
            archives = "\n".join(re.findall(r"[\w\-\_\.]+", request.args.get("archives", "")))
            return self._render_form(
                charter_ids=charters, fond_ids=fonds, archive_ids=archives,
                filename=request.args.get("prefered_filename", "fsdb_slice"),
                export_format=request.args.get("export_type", "tar"),
                export_scope=request.args.get("export_scope", "fsdb"))

        @self.unscoped_route("/sl/charter/<md5>")
        def request_export_single_charter(md5):
            """Pre-filled export form for one charter (also the shared charter view). 404 when the
            charter is not in THIS slice -- a cross-service hand-off may target a charter that lives
            in another service's slice.
            ---
            responses:
              200: {description: the pre-filled export form}
              404: {description: charter not in this slice}
            """
            if md5 not in self.index:
                return f"Charter {md5} is not in this slice", 404
            return self._render_form(context_type="charter", context_value=md5, charter_ids=md5)

        @self.unscoped_route("/sl/fond/<md5>")
        def request_export_single_fond(md5):
            """Pre-filled export form for one fond. 404 if the fond is not in this slice."""
            if not (self.index.fond_id == md5.encode("ascii")).any():
                return f"Fond {md5} is not in this slice", 404
            return self._render_form(context_type="fond", context_value=md5, fond_ids=md5)

        @self.unscoped_route("/sl/archive/<name>")
        def request_export_single_archive(name):
            """Pre-filled export form for one archive. 404 if the archive is not in this slice."""
            if not (self.index.archive_id == name.encode("ascii")).any():
                return f"Archive {name} is not in this slice", 404
            return self._render_form(context_type="archive", context_value=name, archive_ids=name)

        @self.unscoped_route("/sl/download_data", methods=["POST"])
        def download_data():
            """Export a slice from the manually pasted charter/fond/archive TSV.
            ---
            responses:
              200: {description: the slice archive}
              400: {description: no valid ids}
            """
            charter_ids = [c for c in request.form.get("tsv_input", "").split() if len(c) == 32]
            fond_ids = [f for f in request.form.get("tsv_input_fond", "").split() if len(f) == 32]
            archive_ids = [a for a in request.form.get("tsv_input_archive", "").split() if a]
            if not (charter_ids or fond_ids or archive_ids):
                return self.render("generic_error_400.html",
                                   error_message="No valid charter, fond, or archive IDs provided."), 400
            return self._slice_response(
                charter_ids=charter_ids, fond_ids=fond_ids, archive_ids=archive_ids,
                file_format=request.form.get("format", "tar.gz"),
                export_scope=request.form.get("export_scope", "fsdb"),
                tolerate_missing=request.form.get("tolerate_missing", "on") == "on",
                prefered_filename=request.form.get("prefered_filename", "fsdb_slice"))

        @self.scoped_route("/sl/download_basket")
        def download_basket():
            """Export a slice for the client's CURRENT basket, sent as the standard ``scope``
            wire basket (id lists or a packed bit vector against the shared index).

            The basket is read through the inherited ``scope`` proxy, so a malformed basket is a
            400 and a stale ``bit_vector_hash`` a 409 ``index_mismatch``, both from the base --
            the client re-syncs ``/basket/db`` and resends. An *absent* scope means the whole DB
            here, which for an exporter is a foot-gun rather than a default, so it is refused.
            ---
            responses:
              200: {description: the slice archive}
              400: {description: malformed basket, or no basket at all}
              409: {description: index_mismatch -- resync and retry}
            """
            # The scope arrives via ``request.values`` OR a JSON body (the base handles both), so
            # the sibling options must accept the same two shapes -- reading them from the JSON
            # body alone silently defaulted format/export_scope on every form POST.
            payload = request.get_json(silent=True) or request.values
            if not scope.active:
                return jsonify({"error": "no_scope",
                                "detail": "send the basket as 'scope'; refusing to export the "
                                          "whole database implicitly"}), 400
            charter_ids = self.index.flatten(scope.charters)["charter_ids"]
            if not charter_ids:
                return jsonify({"error": "empty_basket"}), 400
            return self._slice_response(
                charter_ids=charter_ids, fond_ids=[], archive_ids=[],
                file_format=payload.get("format", "tar.gz"),
                export_scope=payload.get("export_scope", "fsdb"),
                tolerate_missing=bool(payload.get("tolerate_missing", True)),
                prefered_filename=payload.get("prefered_filename", "fsdb_slice"))


def main_launch_slicer_microservice():
    """``ddpa_slicer_serve`` entry point: build the Slicer service and serve it."""
    service = SlicerMicroservice()
    cfg = service.cfg
    if getattr(cfg, "verbosity", 0):
        print(f"Slicer on {cfg.url} over FSDB {cfg.fsdb_root} "
              f"({len(service.index)} charters, index {service.index.index_hash[:8]})", file=sys.stderr)
    print(f"\n\n{cfg.url}/sl/download_form\n")
    print(f"{cfg.url}/sl/charter/934d2d1d74da8be69f525282909cc363")
    print(f"{cfg.url}/sl/fond/fff44d7897207d30dddef035b9e6a5ca")
    print(f"{cfg.url}/sl/archive/AT-DOZA")
    service.run()


if __name__ == "__main__":
    main_launch_slicer_microservice()
