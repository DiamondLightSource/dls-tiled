import pytest
from migrate_to_central_authz import FOLDERS, Run, place, plan, tag

from dls_tiled.policy import _path_fields, _tag_for


def run(
    key="run",
    parents=(),
    instrument: str | None = "i22",
    session: str | None = "cm12345-1",
    id=1,
):
    return Run(id, key, tuple(parents), instrument, session)


@pytest.mark.parametrize(
    "parents",
    [
        (),  # flat
        ("i22", "cm12345", "cm12345-1"),  # old tree
        ("i22", "cm12345-1"),  # anywhere else
    ],
)
def test_run_goes_to_raw_of_its_visit(parents):
    assert place(run(parents=parents)) == ("i22", "cm12345-1", "raw")


def test_start_document_lower_cased():
    assert place(run(instrument="I22", session="CM12345-1")) == (
        "i22",
        "cm12345-1",
        "raw",
    )


def test_start_document_beats_old_path():
    old = ("i22", "cm12345", "cm12345-1")
    assert place(run(parents=old, session="cm12345-2")) == ("i22", "cm12345-2", "raw")


@pytest.mark.parametrize("folder", list(FOLDERS))
def test_run_already_in_a_folder_stays(folder):
    parents = ("i22", "cm12345-1", folder)
    # even if its start document says otherwise
    assert place(run(parents=parents, session="cm99-9")) == parents


def test_old_tree_path_without_start_fields():
    old = ("i22", "cm12345", "cm12345-1")
    assert place(run(parents=old, instrument=None, session=None)) == (
        "i22",
        "cm12345-1",
        "raw",
    )


@pytest.mark.parametrize(
    "parents,instrument,session",
    [
        ((), "i22", None),
        ((), None, "cm12345-1"),
        ((), "i22", "not-a-visit"),
        ((), "i22", "12345-1"),  # no proposal code
        ((), "i 22", "cm12345-1"),
        # the session isn't one of the proposal's
        (("i22", "cm12345", "cm999-1"), None, None),
        (("i22", "cm12345-1", "elsewhere"), None, None),
    ],
)
def test_cannot_place(parents, instrument, session):
    assert isinstance(
        place(run(parents=parents, instrument=instrument, session=session)), str
    )


def test_plan():
    flat = run("a", id=1)
    in_place = run("b", ("i22", "cm12345-1", "raw"), id=2)
    old = run("c", ("i22", "cm12345", "cm12345-2"), session="cm12345-2", id=3)
    lost = run("d", session=None, id=4)

    result = plan([flat, in_place, old, lost], occupied=set())

    assert result.placed == [
        (in_place, ("i22", "cm12345-1", "raw")),
        (flat, ("i22", "cm12345-1", "raw")),
        (old, ("i22", "cm12345-2", "raw")),
    ]
    assert [r for r, _ in result.moves] == [flat, old]
    assert [r for r, _ in result.skipped] == [lost]
    assert result.visits() == {("i22", "cm12345-1"), ("i22", "cm12345-2")}


def test_run_not_moved_onto_a_node_of_its_key():
    raw = ("i22", "cm12345-1", "raw")
    first, second = run("a", id=1), run("a", ("i22", "cm12345", "cm12345-1"), id=2)

    result = plan([second, first], occupied={(raw, "b")})
    assert [r for r, _ in result.placed] == [first]
    assert [r for r, _ in result.skipped] == [second]

    result = plan([first], occupied={(raw, "a")})
    assert result.placed == []
    assert "already exists" in result.skipped[0][1]


def test_run_in_place_keeps_its_key_over_one_moving_in():
    raw = ("i22", "cm12345-1", "raw")
    moving, staying = run("a", id=1), run("a", raw, id=2)

    result = plan([moving, staying], occupied={(raw, "a")})

    assert [r for r, _ in result.placed] == [staying]
    assert [r for r, _ in result.skipped] == [moving]


@pytest.mark.parametrize(
    "path",
    [
        ("i22",),
        ("i22", "cm12345-1"),
        *[("i22", "cm12345-1", folder) for folder in FOLDERS],
        *[("i22", "cm12345-1", folder, "run", "primary") for folder in FOLDERS],
    ],
)
def test_tags_are_the_ones_dls_tiled_needs_at_the_path(path):
    instrument, *rest = path[:3]
    assert tag(instrument, *rest) == _tag_for(_path_fields(path))
