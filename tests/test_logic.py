"""Unit tests for the pure domain logic."""
from datetime import date

import pytest

from app.logic import (
    MICParseError,
    CutoffError,
    DedupRow,
    classify_mic,
    dedupe_group,
    parse_mic,
    validate_cutoffs,
)


@pytest.mark.parametrize(
    "raw,op,num,lo,hi",
    [
        ("2", None, 2.0, 2.0, 2.0),
        (" 0.5 ", None, 0.5, 0.5, 0.5),
        ("<2", "<", 2.0, 0.0, 2.0),
        ("≤2", "<=", 2.0, 0.0, 2.0),
        ("<=2", "<=", 2.0, 0.0, 2.0),
        (">4", ">", 4.0, 4.0, float("inf")),
        ("≥8", ">=", 8.0, 8.0, float("inf")),
        (">=16", ">=", 16.0, 16.0, float("inf")),
    ],
)
def test_parse_mic_valid(raw, op, num, lo, hi):
    mic = parse_mic(raw)
    assert mic.operator == op
    assert mic.number == num
    assert mic.lo == lo
    assert mic.hi == hi


@pytest.mark.parametrize("raw", ["", "abc", "0", "-1", "2mg", "<", ">=  ", "1.2.3"])
def test_parse_mic_invalid(raw):
    with pytest.raises(MICParseError):
        parse_mic(raw)


def test_classify_exact_values():
    s, r = 4.0, 16.0
    assert classify_mic(parse_mic("2"), s, r) == "S"
    assert classify_mic(parse_mic("4"), s, r) == "S"      # S 上限含等号
    assert classify_mic(parse_mic("8"), s, r) == "I"
    assert classify_mic(parse_mic("15.9"), s, r) == "I"
    assert classify_mic(parse_mic("16"), s, r) == "R"     # R 下限含等号
    assert classify_mic(parse_mic("32"), s, r) == "R"


def test_classify_bounded_single_region():
    s, r = 4.0, 16.0
    assert classify_mic(parse_mic("≤4"), s, r) == "S"
    assert classify_mic(parse_mic("<1"), s, r) == "S"
    assert classify_mic(parse_mic(">16"), s, r) == "R"
    assert classify_mic(parse_mic("≥16"), s, r) == "R"


def test_classify_ambiguous_cross_region():
    s, r = 4.0, 16.0
    # >4 可能落在 I 或 R
    assert classify_mic(parse_mic(">4"), s, r) == "AMBIGUOUS"
    # ≤16 覆盖 S/I/R（16 本身就是 R）
    assert classify_mic(parse_mic("≤16"), s, r) == "AMBIGUOUS"
    # <16 覆盖 S 与 I 两档
    assert classify_mic(parse_mic("<16"), s, r) == "AMBIGUOUS"
    # ≥4：4 属于 S，更大的值属于 I/R
    assert classify_mic(parse_mic("≥4"), s, r) == "AMBIGUOUS"


def test_validate_cutoffs():
    validate_cutoffs(4, 16)
    with pytest.raises(CutoffError):
        validate_cutoffs(16, 4)
    with pytest.raises(CutoffError):
        validate_cutoffs(4, 4)       # 必须严格小于
    with pytest.raises(CutoffError):
        validate_cutoffs(0, 8)
    with pytest.raises(CutoffError):
        validate_cutoffs(-1, 8)
    with pytest.raises(CutoffError):
        validate_cutoffs(float("inf"), 8)


def test_dedupe_window_inclusive_day30():
    base = date(2026, 1, 1)
    rows = [
        DedupRow("R1", base),
        DedupRow("R2", date(2026, 1, 11)),   # +10 剔除
        DedupRow("R3", date(2026, 1, 31)),   # +30 含第30天，剔除
        DedupRow("R4", date(2026, 2, 1)),    # +31 入选，成为新锚点
        DedupRow("R5", date(2026, 2, 10)),   # 距 R4 +9 剔除
        DedupRow("R6", date(2026, 3, 4)),    # 距 R4 +31 入选
    ]
    decisions = {d.record_no: d for d in dedupe_group(rows)}
    assert [n for n, d in decisions.items() if d.included] == ["R1", "R4", "R6"]
    assert not decisions["R3"].included
    assert decisions["R3"].anchor_record_no == "R1"
    assert decisions["R5"].anchor_record_no == "R4"


def test_dedupe_same_day_tie_breaks_on_record_no():
    d = date(2026, 3, 1)
    rows = [DedupRow("RB", d), DedupRow("RA", d)]
    decisions = dedupe_group(rows)
    assert decisions[0].record_no == "RA" and decisions[0].included
    assert decisions[1].record_no == "RB" and not decisions[1].included


def test_dedupe_unsorted_input_is_sorted():
    rows = [
        DedupRow("R2", date(2026, 1, 10)),
        DedupRow("R1", date(2026, 1, 1)),
    ]
    decisions = dedupe_group(rows)
    assert decisions[0].record_no == "R1" and decisions[0].included
    assert decisions[1].record_no == "R2" and not decisions[1].included
