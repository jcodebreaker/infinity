"""superchat-dashboard: numbers computed from files, typed numbers refused, one self-contained page."""

import importlib.machinery
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader(
    "superchat_dashboard", str(HERE.parent / "bin" / "superchat-dashboard")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
dashboard = importlib.util.module_from_spec(spec)
loader.exec_module(dashboard)

FIXTURE = HERE.parent / "share" / "dashboard" / "fixtures" / "sales.spec.json"

CSV = """date,region,revenue,units
2026-01-05,East,100,1
2026-01-20,West,50,2
2026-02-03,East,30,3
2026-02-14,West,20,4
2026-03-01,East,"1,000",5
"""


def embedded(page: str) -> dict:
    raw = re.search(
        r'<script type="application/json" id="superchat-dashboard-data">(.*?)</script>',
        page,
        re.S,
    )
    assert raw
    return json.loads(raw.group(1))


class DashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name).resolve()
        (self.dir / "sales.csv").write_text(CSV, "utf-8")
        # The temp folder plays the person's workspace (~/workspace in the host).
        self.workspace(self.dir)

    def workspace(self, root: Path) -> None:
        patcher = mock.patch.dict("os.environ", {"OMNIGENT_RUNNER_WORKSPACE": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, spec: dict) -> tuple[dict, str]:
        path = self.dir / "spec.json"
        path.write_text(json.dumps(spec), "utf-8")
        result = dashboard.build(path, str(self.dir / "out.html"))
        return result, Path(result["path"]).read_text("utf-8")

    def test_chart_data_comes_from_the_csv(self) -> None:
        result, page = self.build(
            {
                "title": "Sales",
                "source": "sales.csv",
                "charts": [
                    {"title": "By region", "type": "bar", "x": "region", "y": "revenue"}
                ],
            }
        )
        self.assertTrue(result["path"].endswith("out.dashboard.html"))
        [chart] = embedded(page)["charts"]
        self.assertEqual(chart["labels"], ["East", "West"])
        self.assertEqual(chart["datasets"][0]["data"], [1130, 70])
        self.assertIn("Source: sales.csv, revenue summed by region", page)
        self.assertIn("Generated ", page)

    def test_a_spec_on_stdin_leaves_no_file_and_reads_paths_from_here(self) -> None:
        spec = {"source": "sales.csv", "charts": [{"title": "T", "x": "region", "y": "revenue"}]}
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(spec))),
            mock.patch("pathlib.Path.cwd", return_value=self.dir),
        ):
            result = dashboard.build(dashboard.STDIN, str(self.dir / "out.html"))
        self.assertEqual(result["sources"], ["sales.csv"])
        left = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(left, ["out.dashboard.html", "sales.csv"])

    def test_date_buckets_mean_and_split(self) -> None:
        _, page = self.build(
            {
                "source": "sales.csv",
                "charts": [
                    {
                        "title": "Mean",
                        "type": "line",
                        "x": "date",
                        "bucket": "month",
                        "y": "units",
                        "agg": "mean",
                    },
                    {
                        "title": "Split",
                        "type": "bar",
                        "x": "date",
                        "bucket": "quarter",
                        "y": "revenue",
                        "split": "region",
                    },
                ],
            }
        )
        mean, split = embedded(page)["charts"]
        self.assertEqual(mean["labels"], ["Jan", "Feb", "Mar"])
        self.assertEqual(mean["datasets"][0]["data"], [1.5, 3.5, 5])
        self.assertEqual(split["labels"], ["Q1"])
        self.assertEqual(
            {d["label"]: d["data"] for d in split["datasets"]},
            {"East": [1130], "West": [70]},
        )

    def test_kpi_latest_period_and_change(self) -> None:
        _, page = self.build(
            {
                "source": "sales.csv",
                "kpis": [
                    {
                        "label": "Revenue",
                        "value": "revenue",
                        "date": "date",
                        "bucket": "month",
                        "format": {"prefix": "$"},
                    }
                ],
            }
        )
        self.assertIn("$1,000", page)
        self.assertIn("· Mar</span>", page)
        self.assertIn("vs Feb", page)
        self.assertEqual(embedded(page)["kpis"][0]["values"], [150, 50, 1000])

    def test_limit_folds_the_rest_into_other(self) -> None:
        _, page = self.build(
            {
                "source": "sales.csv",
                "charts": [
                    {
                        "title": "Top",
                        "type": "bar",
                        "x": "date",
                        "y": "units",
                        "limit": 2,
                    }
                ],
            }
        )
        [chart] = embedded(page)["charts"]
        self.assertEqual(chart["labels"][-1], "Other")
        self.assertEqual(sum(chart["datasets"][0]["data"]), 15)

    def test_typed_numbers_are_refused(self) -> None:
        for bad in (
            {
                "charts": [
                    {
                        "title": "T",
                        "type": "bar",
                        "data": {"labels": ["a"], "series": {"s": [1]}},
                    }
                ]
            },
            {
                "charts": [{"title": "T", "type": "bar", "x": "region", "y": [1, 2]}],
                "source": "sales.csv",
            },
            {"kpis": [{"label": "Revenue", "value": 120}]},
        ):
            with self.assertRaises(dashboard.SpecError) as caught:
                self.build(bad)
            self.assertIn("derived", str(caught.exception))

    def test_derived_numbers_are_allowed_and_labelled(self) -> None:
        _, page = self.build(
            {
                "charts": [
                    {
                        "title": "T",
                        "type": "bar",
                        "derived": "growth rates from the Q3 board pack",
                        "data": {"labels": ["a", "b"], "series": {"s": [1, 2]}},
                    }
                ],
                "where": {"year": [2025, 2026]},
            }
        )
        self.assertIn("Derived: growth rates from the Q3 board pack", page)

    def test_unknown_column_names_the_columns(self) -> None:
        with self.assertRaises(dashboard.SpecError) as caught:
            self.build(
                {
                    "source": "sales.csv",
                    "charts": [{"title": "T", "x": "regoin", "y": "revenue"}],
                }
            )
        self.assertIn("Columns: date, region, revenue, units", str(caught.exception))

    def test_page_is_self_contained(self) -> None:
        _, page = self.build(
            {
                "source": "sales.csv",
                "charts": [
                    {"title": "T", "type": "pie", "x": "region", "y": "revenue"}
                ],
            }
        )
        self.assertNotRegex(page, r'(?:src|href)\s*=\s*["\']?(?:https?:)?//')
        self.assertNotRegex(page, r"@import|url\(\s*['\"]?https?:")
        self.assertIn("Chart.js v4.4.3", page)
        self.assertEqual(page.count("<script"), 3)

    def test_script_cannot_be_closed_by_data(self) -> None:
        (self.dir / "x.csv").write_text("name,v\n</script><b>x,1\n", "utf-8")
        _, page = self.build(
            {"source": "x.csv", "charts": [{"title": "T", "x": "name", "y": "v"}]}
        )
        self.assertNotIn("</script><b>", page)
        self.assertEqual(embedded(page)["charts"][0]["labels"], ["</script><b>x"])

    def test_format_matches_the_page_rules(self) -> None:
        self.assertEqual(
            dashboard.format_value(228753.39, {"prefix": "$", "compact": True}), "$229K"
        )
        self.assertEqual(
            dashboard.format_value(0.256, {"percent": True, "decimals": 1}), "25.6%"
        )
        self.assertEqual(dashboard.format_value(-1500, {"prefix": "$"}), "-$1,500")
        self.assertEqual(dashboard.format_value(None, {}), "–")

    def test_the_sales_fixture_builds(self) -> None:
        self.workspace(FIXTURE.parent)
        result = dashboard.build(FIXTURE, str(self.dir / "sales.dashboard.html"))
        self.assertEqual((result["kpis"], result["charts"]), (4, 5))

    # -- What the page shows: provenance, labels and periods read like a person wrote them --

    def test_sources_are_cited_by_their_workspace_path_never_absolute(self) -> None:
        files = self.dir / "your_files"
        files.mkdir()
        (files / "sales_2026.csv").write_text(CSV, "utf-8")
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        spec = Path(scratch.name) / "spec.json"  # a spec in /tmp, an absolute source path
        spec.write_text(
            json.dumps(
                {
                    "source": str(files / "sales_2026.csv"),
                    "kpis": [{"label": "Revenue", "value": "revenue"}],
                    "charts": [{"title": "T", "x": "region", "y": "revenue"}],
                }
            ),
            "utf-8",
        )
        result = dashboard.build(spec, str(self.dir / "out.html"))
        page = Path(result["path"]).read_text("utf-8")
        self.assertEqual(result["sources"], ["your_files/sales_2026.csv"])
        self.assertIn("Source: your_files/sales_2026.csv, revenue summed by region", page)
        footer = page[page.index('<footer class="foot">') :]
        self.assertIn("from your_files/sales_2026.csv (5 rows", footer)
        self.assertNotIn(str(self.dir), page)
        self.assertNotIn(scratch.name, page)

    def test_a_source_outside_the_workspace_is_refused(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        monthly = Path(scratch.name) / "monthly.csv"  # the Muse's pre-aggregated /tmp copy
        monthly.write_text("month,revenue\n2026-01,1\n", "utf-8")
        with self.assertRaises(dashboard.SpecError) as caught:
            self.build(
                {"source": str(monthly), "charts": [{"title": "T", "x": "month", "y": "revenue"}]}
            )
        self.assertIn("outside the workspace", str(caught.exception))
        self.assertIn('"weight"', str(caught.exception))

    def test_weighted_mean_and_agg2_cover_a_ratio_without_a_scratch_file(self) -> None:
        (self.dir / "s.csv").write_text(
            "month,region,revenue,margin_pct\n"
            "2026-01,NA,300,20\n2026-01,EU,100,40\n"
            "2026-02,NA,100,10\n2026-02,EU,100,30\n",
            "utf-8",
        )
        _, page = self.build(
            {
                "source": "s.csv",
                "kpis": [
                    {"label": "Margin", "value": "margin_pct", "agg": "mean", "weight": "revenue"}
                ],
                "charts": [
                    {
                        "title": "Revenue vs margin",
                        "type": "combo",
                        "x": "month",
                        "y": "revenue",
                        "y2": "margin_pct",
                        "agg2": "mean",
                        "weight": "revenue",
                    }
                ],
            }
        )
        [chart] = embedded(page)["charts"]
        bars, line = chart["datasets"]
        self.assertEqual(bars["data"], [400, 200])  # summed
        self.assertEqual(line["data"], [25, 20])  # (300*20+100*40)/400, (100*10+100*30)/200
        self.assertEqual(line["axis"], "y2")
        self.assertIn(
            "revenue summed and margin_pct averaged (weighted by revenue) by month", page
        )
        self.assertIn(">23.3<", page)  # KPI: (6000+4000+1000+3000)/600
        with self.assertRaises(dashboard.SpecError):
            self.build(
                {"source": "s.csv", "charts": [{"x": "month", "y": "revenue", "weight": "revenue"}]}
            )

    def test_series_are_labelled_for_people(self) -> None:
        self.assertEqual(dashboard.humanise("revenue"), "Revenue")
        self.assertEqual(dashboard.humanise("margin_pct"), "Margin %")
        self.assertEqual(dashboard.humanise("orderCount"), "Order count")
        self.assertEqual(dashboard.humanise("customer_id"), "Customer ID")
        self.assertEqual(dashboard.humanise("Revenue (USD)"), "Revenue (USD)")
        _, page = self.build(
            {
                "source": "sales.csv",
                "kpis": [{"value": "revenue"}],
                "charts": [
                    {"title": "A", "type": "line", "x": "region", "y": ["revenue", "units"],
                     "names": {"units": "Units sold"}},
                    {"title": "B", "x": "date", "bucket": "month", "y": "revenue",
                     "split": "region"},
                ],
            }
        )
        both, split = embedded(page)["charts"]
        self.assertEqual([d["label"] for d in both["datasets"]], ["Revenue", "Units sold"])
        self.assertEqual({d["label"] for d in split["datasets"]}, {"East", "West"})
        self.assertIn('<p class="label">Revenue', page)

    def test_period_labels_read_as_months_and_quarters(self) -> None:
        (self.dir / "m.csv").write_text(
            "month,v\n2026-02,2\n2026-01,1\n2026-03,3\n", "utf-8"
        )
        (self.dir / "y.csv").write_text(
            "day,v\n2025-11-03,1\n2025-12-03,2\n2026-01-03,3\n", "utf-8"
        )
        (self.dir / "q.csv").write_text("quarter,v\n2026-Q2,2\n2026-Q1,1\n", "utf-8")
        _, page = self.build(
            {
                "charts": [
                    # a text column of months is bucketed by itself and sorted in time
                    {"title": "M", "type": "line", "source": "m.csv", "x": "month", "y": "v"},
                    {"title": "Y", "type": "line", "source": "y.csv", "x": "day",
                     "bucket": "month", "y": "v"},
                    {"title": "Q", "type": "line", "source": "y.csv", "x": "day",
                     "bucket": "quarter", "y": "v"},
                    {"title": "Q text", "type": "bar", "source": "q.csv", "x": "quarter",
                     "y": "v"},
                ]
            }
        )
        months, spanning, quarters, quarter_text = embedded(page)["charts"]
        self.assertEqual(months["labels"], ["Jan", "Feb", "Mar"])
        self.assertEqual(months["datasets"][0]["data"], [1, 2, 3])
        self.assertIn("Source: m.csv, v summed by month<", page)
        self.assertEqual(spanning["labels"], ["Nov 2025", "Dec 2025", "Jan 2026"])
        self.assertEqual(quarters["labels"], ["Q4 2025", "Q1 2026"])
        self.assertEqual(quarter_text["labels"], ["Q1", "Q2"])


if __name__ == "__main__":
    unittest.main()
