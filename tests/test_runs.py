from courts_scraper.runs import latest_run, list_runs


def test_list_runs_empty(tmp_path):
    assert list_runs(tmp_path / "does-not-exist") == []
    assert list_runs(tmp_path) == []


def test_list_runs_sorted_newest_first_with_counts(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    make_run_dir(data, "20260202T000000Z__supreme", done=0, total=3)

    runs = list_runs(data)

    assert [r.name for r in runs] == [
        "20260202T000000Z__supreme",  # newest first
        "20260101T000000Z__supreme",
    ]
    newest, older = runs
    assert (newest.total, newest.done) == (3, 0)
    assert (older.total, older.done) == (2, 1)
    assert older.courts == ("Supreme Court",)
    assert latest_run(data).name == "20260202T000000Z__supreme"


def test_run_summary_shows_progress(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=2)
    (info,) = list_runs(data)
    assert "1/2 downloaded" in info.summary


def test_run_summary_complete(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1)
    (info,) = list_runs(data)
    assert info.is_complete
    assert "complete" in info.summary


def test_list_runs_ignores_non_run_dirs(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme")
    (data / "not-a-run").mkdir(parents=True)  # no judgments.sqlite
    assert [r.name for r in list_runs(data)] == ["20260101T000000Z__supreme"]


# -- listing completeness read from the manifest ---------------------------
_FULL = {
    "complete": True,
    "truncated": False,
    "max_pages": None,
    "pages_fetched": 26,
    "pages_available": 26,
}
_TRUNCATED = {
    "complete": True,
    "truncated": True,
    "max_pages": 3,
    "pages_fetched": 3,
    "pages_available": 27,
}


def test_truncated_run_summary_tells_the_truth(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1, listing=_TRUNCATED)

    (run,) = list_runs(data)
    # Downloads are complete, but the listing was capped -- say so.
    assert run.is_complete is True
    assert run.listing_truncated is True
    assert run.listing_verified is True
    assert "listing truncated at 3 of 27 pages" in run.summary
    assert run.to_dict()["listing_truncated"] is True
    assert run.to_dict()["pages_available"] == 27


def test_full_verified_run_reads_complete(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1, listing=_FULL)

    (run,) = list_runs(data)
    assert run.listing_truncated is False
    assert run.listing_verified is True
    assert run.summary.endswith("(complete, 1 PDFs)")


def test_pre_feature_run_is_unverified_not_full(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=1, total=1)  # no listing block

    (run,) = list_runs(data)
    # Absent block => unknown, but not falsely flagged truncated.
    assert run.listing_verified is False
    assert run.listing_truncated is False
    assert run.summary.endswith("(complete, 1 PDFs)")


def test_partial_download_with_truncated_listing(make_run_dir, tmp_path):
    data = tmp_path / "data"
    make_run_dir(data, "20260101T000000Z__supreme", done=0, total=2, listing=_TRUNCATED)

    (run,) = list_runs(data)
    assert run.is_complete is False
    assert "listing truncated" in run.summary
    assert "0/2 downloaded" in run.summary
