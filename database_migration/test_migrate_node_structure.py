import pytest
from migrate_node_structure import plan_moves, tags, tags_from_own_tag


def test_plan_moves():
    moves, skipped = plan_moves(
        [
            (1, "run-a", "i22", "cm12345-1"),
            (2, "run-b", "i22", "garbage"),
            (3, "i22", None, None),
        ]
    )
    assert moves == [
        (
            1,
            [
                ("i22", {"tags": ["beamline:i22", "visible:beamline:i22"]}),
                (
                    "cm12345",
                    {
                        "tags": [
                            "beamline:i22,proposal:12345",
                            "beamline:i22",
                            "visible:beamline:i22,proposal:12345",
                        ]
                    },
                ),
                (
                    "cm12345-1",
                    {
                        "tags": [
                            "beamline:i22,proposal:12345,session:12345-1",
                            "beamline:i22,proposal:12345",
                            "beamline:i22",
                        ]
                    },
                ),
            ],
        )
    ]
    assert [key for key, *_ in skipped] == ["run-b", "i22"]


@pytest.mark.parametrize("level", [("i15-1",), ("i15-1", 12345), ("i15-1", 12345, 2)])
def test_own_tag_gives_full_tags(level):
    blob = tags(*level)
    assert tags_from_own_tag(blob["tags"][0]) == blob


@pytest.mark.parametrize(
    "tag",
    [
        "11",  # bare session id from the old policy; moved with its run instead
        "visible:beamline:i22",
        "beamline:i22,proposal:12345,session:999-1",  # session of another proposal
        "beamline:i22,session:12345-1",
    ],
)
def test_other_tags_left_alone(tag):
    assert tags_from_own_tag(tag) is None


SESSIONS = {"11": {"beamline": "b18", "proposal_number": 2024, "visit_number": 1}}


def test_session_id_gives_that_sessions_tags():
    assert tags_from_own_tag("11", SESSIONS) == tags("b18", 2024, 1)


@pytest.mark.parametrize("sessions", [None, SESSIONS])
def test_unknown_session_id_left_alone(sessions):
    assert tags_from_own_tag("12", sessions) is None


def test_own_tag_needs_no_sessions():
    blob = tags("b18", 2024, 1)
    assert tags_from_own_tag(blob["tags"][0], SESSIONS) == blob
