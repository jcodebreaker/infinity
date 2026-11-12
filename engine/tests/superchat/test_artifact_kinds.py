"""Artifact kinds the Muse may save: spreadsheets, CSV and chart images included."""

import pytest

from omnigent.superchat.artifact_kinds import KIND_MIME, kind_for_name


@pytest.mark.parametrize(
    ("name", "kind", "mime"),
    [
        ("data.csv", "csv", "text/csv; charset=utf-8"),
        (
            "Report.XLSX",
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("chart.png", "png", "image/png"),
    ],
)
def test_data_kinds_allowed(name: str, kind: str, mime: str) -> None:
    assert kind_for_name(name) == kind
    assert KIND_MIME[kind] == mime


def test_unknown_kind_rejected() -> None:
    assert kind_for_name("run.exe") is None
