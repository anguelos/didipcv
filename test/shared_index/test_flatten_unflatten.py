"""FSDBSharedIndex.flatten / unflatten -- the symmetric compress/expand pair.

flatten expands any selection (bool mask or wire basket) to an explicit charter set; unflatten
compresses it to the minimal {archive_ids, fond_ids, charter_ids} (a fond iff all its charters are
in, an archive iff all its fonds are). Checks the hierarchy cases and the round-trip
receive_basket(unflatten(x)) == x on a synthetic 2-archive FSDB.
"""
import numpy as np
import pytest

pytest.importorskip("fsdb", reason="fsdb not importable")
from fsdb.shared_index import FSDBSharedIndex

A, B, C, D, E = (ch * 32 for ch in "abcde")     # charters
FA, FB, FC = ("1" * 32, "2" * 32, "3" * 32)     # fonds


@pytest.fixture()
def ix(tmp_path):
    # IT-A: FA=[A,B], FB=[C,D]   |   IT-B: FC=[E]
    for arch, fond, chs in [("IT-A", FA, [A, B]), ("IT-A", FB, [C, D]), ("IT-B", FC, [E])]:
        for ch in chs:
            (tmp_path / arch / fond / ch).mkdir(parents=True)
    return FSDBSharedIndex.from_fsdb_root(tmp_path)


def _wire(charters=(), fonds=(), archives=()):
    return {"all_charters": False, "charter_ids": list(charters), "fond_ids": list(fonds),
            "archive_ids": list(archives), "bit_vector": None, "bit_vector_hash": None}


def test_unflatten_full_archive(ix):
    assert ix.unflatten(_wire([A, B, C, D])) == {"archive_ids": ["IT-A"], "fond_ids": [], "charter_ids": []}


def test_unflatten_full_fond_plus_partial(ix):
    u = ix.unflatten(_wire([A, B, C]))          # FA full, FB partial (C only)
    assert u == {"archive_ids": [], "fond_ids": [FA], "charter_ids": [C]}


def test_unflatten_full_fond_but_archive_partial(ix):
    u = ix.unflatten(_wire([A, B, C]))          # FA full but IT-A not (FB partial) -> emit FA, not IT-A
    assert "IT-A" not in u["archive_ids"] and u["fond_ids"] == [FA]


def test_unflatten_everything_is_all_archives(ix):
    u = ix.unflatten(_wire([A, B, C, D, E]))
    assert set(u["archive_ids"]) == {"IT-A", "IT-B"} and not u["fond_ids"] and not u["charter_ids"]


def test_flatten_expands(ix):
    fl = ix.flatten(_wire(archives=["IT-A"]))
    assert fl["archive_ids"] == [] and fl["fond_ids"] == []
    assert sorted(fl["charter_ids"]) == sorted([A, B, C, D])


def test_unflatten_accepts_mask_and_round_trips(ix):
    for sel in ([A, B, C], [A, E], [A, B, C, D, E], [C], []):
        mask = ix.receive_basket(_wire(sel))
        u = ix.unflatten(mask)                   # bool-mask input
        back = ix.receive_basket({**u, "all_charters": False,
                                  "bit_vector": None, "bit_vector_hash": ix.index_hash})
        assert np.array_equal(back, mask), (sel, u)
