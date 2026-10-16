from migrate_node_structure import plan_moves


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
                ("i22", {"tags": ["beamline:i22"]}),
                ("cm12345", {"tags": ["beamline:i22,proposal:12345"]}),
                (
                    "cm12345-1",
                    {"tags": ["beamline:i22,proposal:12345,session:12345-1"]},
                ),
            ],
        )
    ]
    assert [key for key, *_ in skipped] == ["run-b", "i22"]
