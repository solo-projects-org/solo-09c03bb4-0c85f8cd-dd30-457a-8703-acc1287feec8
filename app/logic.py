"""Domain logic for antimicrobial susceptibility statistics.

Pure functions, no framework / no database imports, so they can be unit
tested in isolation.

MIC encoding
------------
An MIC value is either an exact number or a bounded measurement using one of
the operators ``<``, ``<=``, ``>``, ``>=``.  Internally it is represented as
a half-open-ish interval ``(lo, hi, lo_inclusive, hi_inclusive)`` over the
positive reals:

* exact ``2``      -> [2, 2]
* ``<2`` / ``<=2`` -> (0, 2)  / (0, 2]
* ``>2`` / ``>=2`` -> (2, inf) / [2, inf)

Breakpoints for an organism/drug pair define three disjoint regions:

* S (susceptible): 0 < mic <= s_cutoff
* I (intermediate): s_cutoff < mic < r_cutoff
* R (resistant): mic >= r_cutoff

A bounded MIC whose possible range overlaps more than one region cannot be
interpreted safely and is marked "pending retest" (``AMBIGUOUS``). Such a
record is still an *included* (deduplicated) record — it occupies the 30-day
window — but is excluded from the interpretable denominator.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

INF = math.inf

_MIC_RE = re.compile(r"^\s*(?P<op><=|>=|≤|≥|<|>)?\s*(?P<num>\d+(?:\.\d+)?)\s*$")


class MICParseError(ValueError):
    """Raised when an MIC cell cannot be parsed."""


class CutoffError(ValueError):
    """Raised when S/R cutoffs are invalid (must satisfy 0 < S < R)."""


@dataclass(frozen=True)
class MICValue:
    """A parsed MIC measurement expressed as an interval."""

    raw: str
    operator: Optional[str]          # one of None, '<', '<=', '>', '>='
    number: float
    lo: float
    hi: float
    lo_inclusive: bool
    hi_inclusive: bool

    def display(self) -> str:
        """Human-readable, normalized form (ASCII operators)."""
        op = self.operator or ""
        return f"{op}{_fmt_num(self.number)}"


def _fmt_num(x: float) -> str:
    if x == int(x):
        return str(int(x))
    return ("%g" % x)


def normalize_operator(op: Optional[str]) -> Optional[str]:
    if op is None:
        return None
    op = op.strip()
    table = {"≤": "<=", "≥": ">="}
    return table.get(op, op)


def parse_mic(raw: str) -> MICValue:
    """Parse a raw MIC cell into an :class:`MICValue`.

    Accepts a positive number, optionally prefixed with ``<``, ``<=``,
    ``>``, ``>=`` (Unicode ``≤`` / ``≥`` are accepted too).
    """
    if raw is None:
        raise MICParseError("MIC 为空")
    text = str(raw).strip()
    if not text:
        raise MICParseError("MIC 为空")
    m = _MIC_RE.match(text)
    if not m:
        raise MICParseError(f"MIC 格式无法识别: {text!r}（应为正数，可带 <、≤、>、≥ 界值符号）")
    op = normalize_operator(m.group("op"))
    num = float(m.group("num"))
    if num <= 0:
        raise MICParseError(f"MIC 必须为正数，得到 {_fmt_num(num)}")

    if op is None:
        lo, hi, li, hi_ = num, num, True, True
    elif op in ("<", "<="):
        # Both "<2" and "<=2" denote measurements reported as "at most 2":
        # the reported value is not above the bound. We keep the distinction
        # via hi_inclusive but treat the lower end as strictly above 0.
        lo, hi = 0.0, num
        li, hi_ = False, (op == "<=")
    else:  # ">", ">="
        lo, hi = num, INF
        li, hi_ = (op == ">="), False
    return MICValue(raw=text, operator=op, number=num, lo=lo, hi=hi,
                    lo_inclusive=li, hi_inclusive=hi_)


def _intersects(a_lo, a_hi, a_li, a_hi_inc,
                b_lo, b_hi, b_li, b_hi_inc) -> bool:
    """Whether two intervals share at least one point."""
    # max lower endpoints vs min upper endpoints
    if a_lo > b_lo:
        lo, li = a_lo, a_li
    elif b_lo > a_lo:
        lo, li = b_lo, b_li
    else:
        lo, li = a_lo, (a_li and b_li)
    if a_hi < b_hi:
        hi, hi_inc = a_hi, a_hi_inc
    elif b_hi < a_hi:
        hi, hi_inc = b_hi, b_hi_inc
    else:
        hi, hi_inc = a_hi, (a_hi_inc and b_hi_inc)
    if lo < hi:
        return True
    if lo > hi:
        return False
    return li and hi_inc


def validate_cutoffs(s_cutoff: float, r_cutoff: float) -> None:
    if not (isinstance(s_cutoff, (int, float)) and isinstance(r_cutoff, (int, float))):
        raise CutoffError("切点必须为数字")
    if s_cutoff <= 0 or r_cutoff <= 0:
        raise CutoffError("S 上限与 R 下限必须为正数")
    if not s_cutoff < r_cutoff:
        raise CutoffError(f"S 上限必须严格小于 R 下限（当前 S={s_cutoff}, R={r_cutoff}）")
    if math.isnan(s_cutoff) or math.isnan(r_cutoff) or math.isinf(s_cutoff) or math.isinf(r_cutoff):
        raise CutoffError("切点必须为有限正数")


def classify_mic(mic: MICValue, s_cutoff: float, r_cutoff: float) -> str:
    """Classify an MIC against breakpoints.

    Returns one of ``"S"``, ``"I"``, ``"R"`` (single possible region) or
    ``"AMBIGUOUS"`` when the MIC interval overlaps several regions and the
    record must be re-tested before interpretation.
    """
    validate_cutoffs(s_cutoff, r_cutoff)
    regions = {
        # name: (lo, hi, lo_inclusive, hi_inclusive)
        "S": (0.0, s_cutoff, False, True),    # 0 < mic <= s
        "I": (s_cutoff, r_cutoff, False, False),  # s < mic < r
        "R": (r_cutoff, INF, True, False),    # mic >= r
    }
    hits = [
        name for name, iv in regions.items()
        if _intersects(mic.lo, mic.hi, mic.lo_inclusive, mic.hi_inclusive, *iv)
    ]
    if len(hits) == 1:
        return hits[0]
    # No hits should be impossible for any positive interval, but guard anyway.
    if not hits:
        return "AMBIGUOUS"
    return "AMBIGUOUS"


# ---------------------------------------------------------------------------
# 30-day deduplication
# ---------------------------------------------------------------------------

WINDOW_DAYS = 30


@dataclass
class DedupRow:
    """Minimal row shape required by the dedup engine."""

    record_no: str
    sample_date: date


@dataclass
class DedupDecision:
    record_no: str
    sample_date: date
    included: bool
    reason: str
    anchor_record_no: Optional[str] = None
    anchor_date: Optional[date] = None


def dedupe_group(rows: Iterable[DedupRow]) -> list[DedupDecision]:
    """Apply the first-record / 30-day window rule to one patient+organism+drug group.

    Rows must already represent the *accepted* records for the group. They are
    sorted here as well by (sample_date, record_no) so the engine is robust.

    Rule: take the first record; skip every later record whose sample date is
    within 30 calendar days (inclusive of day 30) of the previously *taken*
    record's date; the next record beyond that window is taken and becomes the
    new anchor. Querying a sub-range never resets the window — callers run this
    over the full accepted history and filter by date afterwards.
    """
    ordered = sorted(rows, key=lambda r: (r.sample_date, r.record_no))
    decisions: list[DedupDecision] = []
    anchor: Optional[DedupRow] = None
    for row in ordered:
        if anchor is None:
            anchor = row
            decisions.append(DedupDecision(row.record_no, row.sample_date, True, "首份记录"))
            continue
        delta = (row.sample_date - anchor.sample_date).days
        if delta <= WINDOW_DAYS:
            decisions.append(DedupDecision(
                row.record_no, row.sample_date, False,
                f"距前一入选记录 {anchor.record_no}（{anchor.sample_date.isoformat()}）"
                f"{delta} 天，处于 30 天去重窗口内（含第 30 天）",
                anchor_record_no=anchor.record_no, anchor_date=anchor.sample_date,
            ))
        else:
            anchor = row
            decisions.append(DedupDecision(
                row.record_no, row.sample_date, True,
                f"距前一入选记录已 {delta} 天，超出 30 天窗口，作为下一份入选记录",
            ))
    return decisions


def is_in_range(d: date, start: Optional[date], end: Optional[date]) -> bool:
    if start is not None and d < start:
        return False
    if end is not None and d > end:
        return False
    return True
