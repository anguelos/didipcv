"""Flask exposure of an :class:`fsdb.shared_index.FSDBSharedIndex` (``/basket`` routes).

The serving contract, per the ddp_online design: the server-side index is an immutable
load-time reduction; clients cache it locally (OPFS) and re-check only occasionally.

- ``GET /basket``     -> tiny JSON manifest (index_hash, counts, fsdb_root, uptime).
- ``GET /basket/db``  -> the binary shared-index container (``index.to_db_bytes()``),
  ``ETag: "<index_hash>"``, optional gzip; honours ``If-None-Match`` with ``304``.

The request/response *logic* is pure functions (:func:`manifest_dict`, :func:`db_payload`);
:func:`make_sharedindex_blueprint` is a thin Flask adapter over them. Flask is a core
dependency, imported at module top.
"""
import gzip
import time

from flask import Blueprint, Response, jsonify, request


def manifest_dict(index, uptime_s: float | None = None) -> dict:
    """The ``GET /basket`` payload: index hash, entity counts, root, filepattern, uptime."""
    return {
        "index_hash": index.index_hash,
        "counts": {
            "archives": int(len(index.archive_id)),
            "fonds": int(len(index.fond_id)),
            "charters": len(index),
        },
        "fsdb_root": str(index.fsdb_root) if index.fsdb_root is not None else None,
        "filepattern": index.filepattern,
        "uptime_s": uptime_s,
    }


def _if_none_match(header: str | None, etag: str) -> bool:
    """True if an ``If-None-Match`` header matches ``etag`` (handles ``*``, quotes, weak)."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    for tag in header.split(","):
        tag = tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag.strip('"') == etag:
            return True
    return False


def db_payload(index, accept_encoding: str = "", if_none_match: str | None = None
               ) -> tuple[int, dict, bytes]:
    """Compute ``(status, headers, body)`` for ``GET /basket/db``.

    Returns ``304`` (empty body) when ``If-None-Match`` already holds the current
    index_hash; otherwise the container bytes, gzip-encoded when the client accepts it.
    """
    etag = index.index_hash
    quoted = f'"{etag}"'
    if _if_none_match(if_none_match, etag):
        return 304, {"ETag": quoted}, b""
    body = index.to_db_bytes()
    headers = {
        "Content-Type": "application/octet-stream",
        "ETag": quoted,
        "Cache-Control": "no-cache",
    }
    if "gzip" in accept_encoding:
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return 200, headers, body


def make_sharedindex_blueprint(index, *, name: str = "sharedindex", started_at: float | None = None):
    """Build a Flask ``Blueprint`` exposing ``index`` at ``/basket`` and ``/basket/db``."""
    started_at = time.time() if started_at is None else started_at
    bp = Blueprint(name, __name__)

    @bp.route("/basket")
    def basket_manifest():
        """Shared-index manifest.
        ---
        responses:
          200: {description: index hash, counts, fsdb_root and uptime}
        """
        resp = jsonify(manifest_dict(index, round(time.time() - started_at, 1)))
        resp.headers["ETag"] = f'"{index.index_hash}"'
        return resp

    @bp.route("/basket/db")
    def basket_db():
        """Serialised shared index (binary container) for client sync.
        ---
        responses:
          200: {description: FSDBIDX container bytes}
          304: {description: client copy already current}
        """
        status, headers, body = db_payload(
            index,
            request.headers.get("Accept-Encoding", ""),
            request.headers.get("If-None-Match"),
        )
        return Response(body, status=status, headers=headers)

    return bp
