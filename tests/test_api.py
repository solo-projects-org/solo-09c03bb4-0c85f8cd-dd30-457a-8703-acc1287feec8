"""End-to-end tests through the FastAPI app + real SQLite database."""
from conftest import make_csv


CSV_HEADER = "记录号,修订号,匿名患者号,UTC采样日,菌种,药物,MIC"


def upload(client, rows):
    return client.post(
        "/api/upload",
        files={"file": ("data.csv", make_csv(rows), "text/csv")},
    )


def set_cutoff(client, organism, drug, s, r):
    return client.post("/api/cutoffs", json={
        "organism": organism, "drug": drug,
        "s_cutoff": s, "r_cutoff": r,
    })


def test_full_flow_dedup_classification_and_ratio(client):
    rows = [
        ["R001", "1", "P1", "2026-01-05", "ECO", "CAZ", "2"],      # S, included
        ["R002", "1", "P1", "2026-01-10", "ECO", "CAZ", "8"],      # excluded (+5d)
        ["R003", "1", "P1", "2026-02-06", "ECO", "CAZ", "16"],     # R, +32d included
        ["R004", "1", "P2", "2026-01-08", "ECO", "CAZ", "≤4"],     # S
        ["R005", "1", "P2", "2026-01-20", "ECO", "CAZ", ">4"],     # excluded (+12d)
        ["R006", "1", "P3", "2026-03-01", "KPN", "MEM", "≥8"],     # R
        ["R007", "1", "P3", "2026-03-15", "KPN", "MEM", "2"],      # excluded
        ["R008", "1", "P4", "2026-03-02", "KPN", "MEM", ">2"],     # AMBIGUOUS (I or R)
    ]
    resp = upload(client, rows)
    assert resp.status_code == 200, resp.text
    assert resp.json()["inserted"] == 8

    assert set_cutoff(client, "ECO", "CAZ", 4, 16).status_code == 200
    assert set_cutoff(client, "KPN", "MEM", 2, 8).status_code == 200

    report = client.get("/api/report").json()
    by_key = {(s["organism"], s["drug"]): s for s in report["summary"]}

    eco = by_key[("ECO", "CAZ")]
    assert eco["included"] == 3            # R001, R003, R004
    assert eco["excluded_in_range"] == 2   # R002, R005
    assert eco["S"] == 2 and eco["I"] == 0 and eco["R"] == 1
    assert eco["ambiguous"] == 0
    assert eco["interpretable"] == 3
    assert abs(eco["r_ratio"] - 1 / 3) < 1e-9

    kpn = by_key[("KPN", "MEM")]
    assert kpn["included"] == 2            # R006, R008
    assert kpn["excluded_in_range"] == 1   # R007
    assert kpn["R"] == 1
    assert kpn["ambiguous"] == 1           # R008 >2 crosses I/R
    assert kpn["interpretable"] == 1       # ambiguous excluded from denominator
    assert kpn["r_ratio"] == 1.0

    retest = {r["record_no"] for r in report["retest"]}
    assert retest == {"R008"}
    retest_row = next(r for r in report["retest"] if r["record_no"] == "R008")
    assert "复测" in retest_row["category_reason"]

    detail = {r["record_no"]: r for r in report["detail"]}
    assert detail["R002"]["included"] is False
    assert detail["R002"]["anchor_record_no"] == "R001"
    assert "30" in detail["R002"]["dedup_reason"]
    assert detail["R008"]["included"] is True
    assert detail["R008"]["category"] == "AMBIGUOUS"


def test_day30_boundary_inclusion(client):
    rows = [
        ["A1", "1", "P9", "2026-01-01", "ORG", "DRG", "1"],
        ["A2", "1", "P9", "2026-01-31", "ORG", "DRG", "1"],   # +30 excluded
        ["A3", "1", "P9", "2026-02-01", "ORG", "DRG", "1"],   # +31 included
    ]
    upload(client, rows)
    report = client.get("/api/report").json()
    detail = {r["record_no"]: r for r in report["detail"]}
    assert detail["A1"]["included"]
    assert not detail["A2"]["included"]
    assert detail["A3"]["included"]


def test_invalid_batch_is_rejected_atomically(client):
    good = [["G1", "1", "P1", "2026-01-01", "O", "D", "2"]]
    assert upload(client, good).status_code == 200

    bad = [
        ["B1", "1", "P1", "2026-01-02", "O", "D", "not-a-mic"],
        ["B2", "0", "P1", "2026-01-03", "O", "D", "2"],       # bad revision
        ["B3", "1", "P1", "not-a-date", "O", "D", "2"],
        ["B4", "1", "", "2026-01-04", "O", "D", "2"],         # empty patient
        ["B5", "1", "P1", "2026-01-05", "O", "D", "-3"],      # negative MIC
    ]
    resp = upload(client, bad)
    assert resp.status_code == 422
    payload = resp.json()
    assert payload["accepted"] is False
    assert len(payload["errors"]) == len(bad)
    assert all(("第" in e) for e in payload["errors"])

    # Accepted data untouched.
    report = client.get("/api/report").json()
    assert {r["record_no"] for r in report["detail"]} == {"G1"}


def test_missing_header_column_rejected(client):
    body = (CSV_HEADER.replace(",MIC", "") + "\n"
            "X1,1,P1,2026-01-01,O,D\n").encode()
    resp = client.post(
        "/api/upload",
        files={"file": ("x.csv", body, "text/csv")},
    )
    assert resp.status_code == 422
    assert "MIC" in resp.json()["errors"][0]


def test_duplicate_pair_in_same_file_rejected(client):
    rows = [
        ["D1", "1", "P1", "2026-01-01", "O", "D", "2"],
        ["D1", "1", "P1", "2026-01-02", "O", "D", "4"],
    ]
    resp = upload(client, rows)
    assert resp.status_code == 422
    assert any("重复" in e for e in resp.json()["errors"])


def test_non_increasing_revision_in_batch_rejected(client):
    rows = [
        ["D2", "2", "P1", "2026-01-01", "O", "D", "2"],
        ["D2", "1", "P1", "2026-01-02", "O", "D", "4"],
    ]
    resp = upload(client, rows)
    assert resp.status_code == 422
    assert any("修订号" in e for e in resp.json()["errors"])


def test_higher_revision_corrects_older_record(client):
    assert upload(client, [
        ["C1", "1", "P1", "2026-01-01", "O", "D", "32"],   # R
    ]).status_code == 200
    set_cutoff(client, "O", "D", 4, 16)
    report = client.get("/api/report").json()
    assert report["detail"][0]["category"] == "R"

    # Lower/equal revision must be rejected.
    stale = upload(client, [["C1", "1", "P1", "2026-01-01", "O", "D", "2"]])
    assert stale.status_code == 422
    assert any("修订号" in e for e in stale.json()["errors"])

    # Higher revision supersedes the old row (same record_no, one current row).
    ok = upload(client, [["C1", "2", "P1", "2026-01-01", "O", "D", "2"]])
    assert ok.status_code == 200
    assert ok.json()["corrections"] == 1

    report = client.get("/api/report").json()
    assert len(report["detail"]) == 1
    row = report["detail"][0]
    assert row["record_no"] == "C1" and row["revision_no"] == 2
    assert row["category"] == "S"

    history = client.get("/api/records/C1").json()
    assert [r["revision_no"] for r in history["revisions"]] == [1, 2]
    assert history["revisions"][0]["superseded_by"] is not None
    assert history["revisions"][1]["superseded_by"] is None


def test_cutoff_validation_s_must_be_lower_than_r(client):
    resp = set_cutoff(client, "O", "D", 16, 4)
    assert resp.status_code == 422
    assert "严格小于" in resp.json()["errors"][0]
    resp = set_cutoff(client, "O", "D", 4, 4)
    assert resp.status_code == 422
    resp = set_cutoff(client, "O", "D", -1, 4)
    assert resp.status_code == 422
    assert client.get("/api/cutoffs").json()["cutoffs"] == []


def test_zero_denominator_ratio_not_computable(client):
    # Only an ambiguous MIC -> interpretable denominator 0.
    upload(client, [["Z1", "1", "P1", "2026-01-01", "O", "D", ">4"]])
    set_cutoff(client, "O", "D", 4, 16)
    report = client.get("/api/report").json()
    bucket = report["summary"][0]
    assert bucket["interpretable"] == 0
    assert bucket["r_ratio"] is None
    assert "无法计算" in bucket["ratio_note"]

    # No cutoff at all -> NO_CUTOFF, also excluded from denominator.
    upload(client, [["Z2", "1", "P2", "2026-01-01", "O2", "D2", "2"]])
    report = client.get("/api/report").json()
    no_cut = next(s for s in report["summary"] if s["organism"] == "O2")
    assert no_cut["no_cutoff"] == 1 and no_cut["r_ratio"] is None


def test_date_range_does_not_reset_dedup_window(client):
    rows = [
        ["W1", "1", "P1", "2026-01-01", "O", "D", "2"],
        ["W2", "1", "P1", "2026-01-20", "O", "D", "2"],   # excluded (window from W1)
        ["W3", "1", "P1", "2026-02-05", "O", "D", "2"],   # +35 -> included
    ]
    upload(client, rows)
    # Query only Feb: W1 is outside range but still anchors the window,
    # so W3 is included and W2 (also outside) simply isn't shown.
    report = client.get(
        "/api/report", params={"start": "2026-02-01", "end": "2026-02-28"}
    ).json()
    numbers = {r["record_no"]: r for r in report["detail"]}
    assert set(numbers) == {"W3"}
    assert numbers["W3"]["included"] is True


def test_snapshot_version_monotonic_and_reported(client):
    v0 = client.get("/health").json()["snapshot_version"]
    upload(client, [["S1", "1", "P1", "2026-01-01", "O", "D", "2"]])
    v1 = client.get("/health").json()["snapshot_version"]
    set_cutoff(client, "O", "D", 4, 16)
    v2 = client.get("/health").json()["snapshot_version"]
    assert (v0, v1, v2) == (0, 1, 2)
    report = client.get("/api/report").json()
    assert report["snapshot_version"] == v2


def test_filters_and_html_pages(client):
    upload(client, [
        ["F1", "1", "P1", "2026-01-01", "ECO", "CAZ", "2"],
        ["F2", "1", "P2", "2026-02-01", "KPN", "MEM", "2"],
    ])
    set_cutoff(client, "ECO", "CAZ", 4, 16)
    set_cutoff(client, "KPN", "MEM", 2, 8)

    page = client.get("/")
    assert page.status_code == 200 and "院感药敏" in page.text
    rep = client.get(
        "/report", params={"organism": "ECO", "start": "2026-01-01",
                           "end": "2026-01-31"}
    )
    assert rep.status_code == 200 and "F1" in rep.text and "F2" not in rep.text

    history = client.get("/records/F1")
    assert history.status_code == 200 and "v1" in history.text
    assert client.get("/records/NOPE").status_code == 404

    bad_date = client.get("/report", params={"start": "01/01/2026"})
    assert bad_date.status_code == 400

    api_rep = client.get("/api/report", params={"patient_id": "P2"}).json()
    assert {r["record_no"] for r in api_rep["detail"]} == {"F2"}


def test_iso_datetime_and_unicode_operators_accepted(client):
    rows = [
        ["T1", "1", "P1", "2026-01-01T23:30:00Z", "O", "D", "≤4"],
        ["T2", "1", "P1", "2026-02-10T02:00:00+08:00", "O", "D", "≥16"],
    ]
    assert upload(client, rows).status_code == 200
    set_cutoff(client, "O", "D", 4, 16)
    report = client.get("/api/report").json()
    detail = {r["record_no"]: r for r in report["detail"]}
    # +08:00 instant 2026-02-10 02:00 converts to UTC date 2026-02-09.
    assert detail["T1"]["sample_date"] == "2026-01-01"
    assert detail["T2"]["sample_date"] == "2026-02-09"
    assert detail["T1"]["category"] == "S"
    assert detail["T2"]["category"] == "R"


def test_html_upload_error_page_does_not_mutate(client):
    upload(client, [["H1", "1", "P1", "2026-01-01", "O", "D", "2"]])
    body = (CSV_HEADER + "\nHB1,1,P1,2026-01-02,O,D,garbage\n").encode()
    resp = client.post(
        "/upload", files={"file": ("bad.csv", body, "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "整批未接纳" in resp.text
    report = client.get("/api/report").json()
    assert {r["record_no"] for r in report["detail"]} == {"H1"}
