"""Request-scoped `scope` proxy (ddp_microservices/scope.py): resolve from ?scope= / POST body,
apply per route, 400 on malformed scope, 409 on an index-mismatched basket. Uses a minimal Flask
app that publishes a real FSDBSharedIndex at app.extensions["ddp_ms"] (mirroring the base wiring).
"""
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("flask", reason="flask not installed")
pytest.importorskip("fsdb", reason="fsdb not importable")
from flask import Flask, jsonify

from fsdb.shared_index import FSDBSharedIndex, IndexMismatch
from ddp_microservices.scope import scope

A, B, C = ("a" * 32, "b" * 32, "c" * 32)
FOND = "f" * 32


@pytest.fixture()
def app(tmp_path):
    for ch in (A, B, C):
        (tmp_path / "IT-X" / FOND / ch).mkdir(parents=True)
    index = FSDBSharedIndex.from_fsdb_root(tmp_path)

    app = Flask("scope_test")
    app.extensions["ddp_ms"] = type("Svc", (), {"index": index})()

    @app.errorhandler(IndexMismatch)               # mirrors DidipMicroservice._register_core_routes
    def _mismatch(e):
        return jsonify({"error": "index_mismatch", "index_hash": getattr(e, "got", None)}), 409

    @app.route("/items", methods=["GET", "POST"])
    def items():
        # candidate = all charters; report how many survive the scope
        res = scope.apply(np.ones(len(index), dtype=bool))
        return jsonify({"active": res.active, "in_scope": res.in_scope, "total": res.total, "note": res.note})

    app._index = index
    return app


def _wire(charters):
    return {"all_charters": False, "charter_ids": list(charters), "fond_ids": [], "archive_ids": [],
            "bit_vector": None, "bit_vector_hash": None}


def test_no_scope_is_inactive(app):
    r = app.test_client().get("/items").get_json()
    assert r["active"] is False and r["in_scope"] == 3 and r["total"] == 3


def test_get_scope_intersects(app):
    q = json.dumps(_wire([A, B]))
    r = app.test_client().get("/items", query_string={"scope": q}).get_json()
    assert r["active"] is True and r["in_scope"] == 2 and r["total"] == 3
    assert r["note"] == "2 of 3 charters in scope"


def test_post_body_scope(app):
    r = app.test_client().post("/items", json={"scope": _wire([A])}).get_json()
    assert r["active"] is True and r["in_scope"] == 1


def test_malformed_scope_is_400(app):
    r = app.test_client().get("/items", query_string={"scope": "{not json"})
    assert r.status_code == 400


def test_index_mismatch_is_409(app):
    # a bit_vector basket referencing a different index_hash -> IndexMismatch -> 409
    bad = {"all_charters": True, "charter_ids": [], "fond_ids": [], "archive_ids": [],
           "bit_vector": None, "bit_vector_hash": "deadbeef"}
    r = app.test_client().get("/items", query_string={"scope": json.dumps(bad)})
    assert r.status_code == 409
    assert r.get_json()["error"] == "index_mismatch"
    assert r.get_json()["index_hash"] == app._index.index_hash
