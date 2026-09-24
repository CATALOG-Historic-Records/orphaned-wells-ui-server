"""Real Mongo coverage: mock databases cannot detect blocking-sort regressions."""

from unittest.mock import Mock

import pytest
from bson import ObjectId

from ogrre.internal import util
from ogrre.tests.test_attribute_retirement import (
    GROUP,
    USER,
    query_manager,
    retirement_manager,
    schema_manager,
)


def plan_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from plan_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from plan_nodes(child)


def test_large_metadata_queries_use_date_index_without_disk_sort(
    query_manager, monkeypatch
):
    manager = query_manager
    records = manager.db.records
    records.create_index("dateCreated")
    # More than staging's 32 MiB sort limit, which the CI Mongo service also uses.
    # Keep a large active value so hiding retired fields cannot shrink the test.
    body = "x" * (80 * 1024)
    other_group = str(ObjectId())
    record_count = 1000
    for start in range(0, record_count, 100):
        records.insert_many(
            [
                {
                    "record_group_id": GROUP if number % 2 else other_group,
                    "dateCreated": number,
                    "name": f"Record {number}",
                    "review_status": "reviewed" if number % 2 else "unreviewed",
                    "attributesList": [
                        {"key": "body", "value": body},
                        {"key": "old", "value": "hidden", "deleted": True},
                        {
                            "key": "parent",
                            "subattributes": [
                                {
                                    "key": "child",
                                    "value": "also hidden",
                                    "deleted": True,
                                },
                            ],
                        },
                    ],
                }
                for number in range(start, start + 100)
            ]
        )
    records.insert_many(
        [
            {"record_group_id": GROUP, "dateCreated": -1, "deleted": True},
            {"record_group_id": "inaccessible", "dateCreated": -2},
        ]
    )
    # Both schema preparation and spilling to disk would conceal this regression.
    monkeypatch.setattr(
        manager,
        "_ensureRecordGroupsReconciled",
        Mock(side_effect=AssertionError("Unexpected schema work")),
    )
    monkeypatch.setattr(manager.db, "records", records)
    aggregate = records.aggregate
    monkeypatch.setattr(
        records, "aggregate", lambda pipeline: aggregate(pipeline, allowDiskUse=False)
    )
    scope = {"record_group_id": {"$in": [GROUP, other_group]}}
    for direction, page in [(1, 0), (1, 3), (-1, 0), (-1, 3)]:
        pipeline = util.generate_mongo_records_pipeline(
            filter_by=scope,
            primary_sort=["dateCreated", direction],
            page=page,
            records_per_page=25,
            for_ranking=True,
        )
        plan = manager.db.command(
            "explain",
            {
                "aggregate": "records",
                "pipeline": pipeline,
                "cursor": {},
                "allowDiskUse": False,
            },
            verbosity="queryPlanner",
        )
        # Exclude rejected plans and the echoed input command: either may contain
        # a $sort even when the winning plan obtains order directly from the index.
        nodes = [
            part
            for node in plan_nodes(plan)
            if "winningPlan" in node
            for part in plan_nodes(node["winningPlan"])
        ]
        assert any(node.get("indexName") == "dateCreated_1" for node in nodes)
        assert not any(node.get("stage") == "SORT" for node in nodes)
        assert not any("$sort" in stage for stage in plan.get("stages", []))
        rows, count = manager.fetchRecords(
            filter_by=scope,
            sort_by=["dateCreated", direction],
            page=page,
            records_per_page=25,
        )
        expected = list(range(record_count))[::direction][page * 25 : (page + 1) * 25]
        assert count == record_count
        assert [row["dateCreated"] for row in rows] == expected
        assert [row["rank"] for row in rows] == list(
            range(page * 25 + 1, (page + 1) * 25 + 1)
        )
        assert all(
            [attribute["key"] for attribute in row["attributesList"]]
            == ["body", "parent"]
            for row in rows
        )
        assert all(row["attributesList"][1]["subattributes"] == [] for row in rows)
        document = {"_id": rows[0]["_id"]}
        manager.getRecordIndexes(document, scope, ["dateCreated", direction], USER)
        assert document["rank"] == rows[0]["rank"]
        assert document["next_id"] == rows[1]["_id"]
        previous_date = (expected[0] - direction) % record_count
        assert (
            records.find_one({"_id": ObjectId(document["previous_id"])})["dateCreated"]
            == previous_date
        )

    filtered, count = manager.fetchRecords(
        filter_by={
            **scope,
            "$and": [
                {"dateCreated": {"$gte": 990}},
                {"$or": [{"review_status": "reviewed"}, {"name": "Record 990"}]},
                {"$nor": [{"name": "Record 999"}]},
            ],
        },
        page=0,
        records_per_page=25,
    )
    assert count == 5
    assert [row["dateCreated"] for row in filtered] == [990, 991, 993, 995, 997]
    assert records.find_one({"dateCreated": 0})["attributesList"][1]["deleted"]


@pytest.mark.parametrize(
    "query,expected",
    [
        ({"$or": [{"attributesList.value": "needle"}, {"name": "third"}]}, [2, 3]),
        ({"$nor": [{"attributesList.value": "needle"}]}, [1, 3]),
        (
            {"$and": [{"name": {"$ne": "third"}}, {"attributesList.value": "needle"}]},
            [2],
        ),
        ({"$expr": {"$in": ["needle", "$attributesList.value"]}}, [2]),
        ({"attributesList.subattributes.value": "nested needle"}, [2]),
    ],
)
def test_attribute_filters_still_prune_before_matching(query_manager, query, expected):
    manager = query_manager
    manager.db.records.insert_many(
        [
            {
                "record_group_id": GROUP,
                "dateCreated": number,
                "name": name,
                "attributesList": [
                    {
                        "key": "secret",
                        "value": "needle" if number < 3 else "other",
                        "deleted": number == 1,
                    },
                    {
                        "key": "parent",
                        "deleted": number == 1,
                        "subattributes": [
                            {
                                "key": "child",
                                "value": "nested needle" if number < 3 else "other",
                            }
                        ],
                    },
                ],
            }
            for number, name in [(1, "first"), (2, "second"), (3, "third")]
        ]
    )
    rows, count = manager.fetchRecords(
        filter_by={**query, "record_group_id": GROUP}, page=0, records_per_page=1
    )
    assert count == len(expected) and rows[0]["dateCreated"] == expected[0]


def test_attribute_sort_and_projection_preserve_retirement(query_manager):
    manager = query_manager
    for number, value in [(1, 99), (2, 2), (3, 1)]:
        manager.db.records.insert_one(
            {
                "record_group_id": GROUP,
                "dateCreated": number,
                "attributesList": [
                    {"key": "depth", "value": value, "deleted": number == 1}
                ],
            }
        )
    rows, count = manager.fetchRecords(
        filter_by={"record_group_id": GROUP}, sort_by=["attributesList.depth", 1]
    )
    assert count == 3 and [row["dateCreated"] for row in rows] == [1, 3, 2]
    assert rows[0]["attributesList"] == []
    projected, _ = manager.fetchRecords(
        filter_by={"record_group_id": GROUP},
        include_attribute_fields={
            "topLevelFields": ["dateCreated"],
            "attributesList": ["key", "value"],
        },
    )
    assert projected[0]["attributesList"] == []
