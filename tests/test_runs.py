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
