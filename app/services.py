"""Application services: CSV validation, cutoff handling and report building."""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from . import db
from .logic import (
    MICParseError,
    CutoffError,
    DedupDecision,
    DedupRow,
    classify_mic,
    dedupe_group,
    is_in_range,
    parse_mic,
    validate_cutoffs,
)

MAX_ERRORS = 50


class ValidationError(Exception):
    """Collects every problem found in a submitted batch."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("；".join(errors[:5]))


# ---------------------------------------------------------------------------
# CSV parsing & validation
# ---------------------------------------------------------------------------

def _parse_utc_day(raw: str) -> date:
    text = raw.strip()
    if not text:
        raise ValueError("UTC采样日为空")
    # Plain calendar date.
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    # Full ISO-8601 instant: timezone is mandatory, then convert to UTC date.
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"UTC采样日格式无法识别: {text!r}（应为 YYYY-MM-DD 或带时区的 ISO 时间）") from exc
    if dt.tzinfo is None:
        raise ValueError(f"UTC采样日带时间时必须显式给出时区(UTC): {text!r}")
    return dt.astimezone(timezone.utc).date()


def _parse_revision(raw: str) -> int:
    text = raw.strip()
    if not text:
        raise ValueError("修订号为空")
    if not text.lstrip("+").isdigit():
        raise ValueError(f"修订号必须为正整数: {text!r}")
    value = int(text)
    if value < 1:
        raise ValueError(f"修订号必须为正整数: {text!r}")
    return value


def _require_text(raw, field: str) -> str:
    if raw is None:
        raise ValueError(f"{field}为空")
    text = str(raw).strip()
    if not text:
        raise ValueError(f"{field}为空")
    if len(text) > 100:
        raise ValueError(f"{field}长度不得超过 100 字符")
    return text


def parse_and_validate_csv(file_bytes: bytes, conn) -> list[dict]:
    """Validate a whole upload. Raises :class:`ValidationError` with all
    row-level problems; nothing is written when this raises.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError([f"文件必须为 UTF-8 编码的 CSV：{exc}"]) from exc

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValidationError(["CSV 为空或缺少表头"])
    missing = [c for c in db.REQUIRED_COLUMNS if c not in reader.fieldnames]
    if missing:
        raise ValidationError([
            "表头缺少必需列: " + ", ".join(missing)
            + f"；必需列为 {', '.join(db.REQUIRED_COLUMNS)}"
        ])
    errors: list[str] = []
    rows: list[dict] = []
    seen_pairs: set[tuple[str, int]] = set()
    record_max_in_batch: dict[str, int] = {}

    for line_no, raw in enumerate(reader, start=2):
        row_err: list[str] = []
        try:
            record_no = _require_text(raw.get("记录号"), "记录号")
        except ValueError as e:
            record_no = f"<第{line_no}行>"
            row_err.append(str(e))
        try:
            revision_no = _parse_revision(raw.get("修订号") or "")
        except ValueError as e:
            revision_no = -1
            row_err.append(str(e))
        try:
            patient_id = _require_text(raw.get("匿名患者号"), "匿名患者号")
        except ValueError as e:
            patient_id = ""
            row_err.append(str(e))
        try:
            sample_date = _parse_utc_day(raw.get("UTC采样日") or "")
        except ValueError as e:
            sample_date = None
            row_err.append(str(e))
        try:
            organism = _require_text(raw.get("菌种"), "菌种")
        except ValueError as e:
            organism = ""
            row_err.append(str(e))
        try:
            drug = _require_text(raw.get("药物"), "药物")
        except ValueError as e:
            drug = ""
            row_err.append(str(e))
        mic_raw = (raw.get("MIC") or "").strip()
        try:
            parse_mic(mic_raw)
        except MICParseError as e:
            row_err.append(str(e))

        if row_err:
            errors.append(f"第 {line_no} 行: " + "；".join(row_err))
            continue

        pair = (record_no, revision_no)
        if pair in seen_pairs:
            errors.append(
                f"第 {line_no} 行: (记录号={record_no}, 修订号={revision_no}) 在文件内重复"
            )
            continue
        seen_pairs.add(pair)

        prev = record_max_in_batch.get(record_no)
        if prev is not None and revision_no <= prev:
            errors.append(
                f"第 {line_no} 行: 记录号 {record_no} 在本批中出现了不高于先前修订号"
                f"（{revision_no} <= {prev}）的记录，同一记录号的修订号必须严格递增"
            )
            continue
        record_max_in_batch[record_no] = revision_no

        rows.append({
            "record_no": record_no,
            "revision_no": revision_no,
            "patient_id": patient_id,
            "sample_date": sample_date,
            "organism": organism,
            "drug": drug,
            "mic_raw": mic_raw,
            "line_no": line_no,
        })

    # Cross-check against already accepted revisions.
    if rows:
        distinct = list(record_max_in_batch.keys())
        placeholders = ",".join("?" for _ in distinct)
        existing = conn.execute(
            f"SELECT record_no, MAX(revision_no) AS max_rev "
            f"FROM accepted_records WHERE record_no IN ({placeholders}) "
            f"GROUP BY record_no",
            distinct,
        ).fetchall()
        existing_map = {r["record_no"]: r["max_rev"] for r in existing}
        for r in rows:
            if r["record_no"] in existing_map and \
                    r["revision_no"] <= existing_map[r["record_no"]]:
                errors.append(
                    f"第 {r['line_no']} 行: 记录号 {r['record_no']} 的修订号 "
                    f"{r['revision_no']} 不高于已接纳的最高修订号 "
                    f"{existing_map[r['record_no']]}；高修订才能更正旧记录"
                )

    if errors:
        raise ValidationError(errors[:MAX_ERRORS]
                              + ([f"……其余 {len(errors) - MAX_ERRORS} 条错误已省略"]
                                 if len(errors) > MAX_ERRORS else []))
    if not rows:
        raise ValidationError(["CSV 中没有任何有效数据行"])
    rows.sort(key=lambda r: (r["record_no"], r["revision_no"]))
    return rows


def validate_cutoff_inputs(s_raw: str, r_raw: str) -> tuple[float, float]:
    try:
        s_val = float(str(s_raw).strip())
        r_val = float(str(r_raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValidationError(["S 上限与 R 下限必须为正数（可含小数）"]) from exc
    try:
        validate_cutoffs(s_val, r_val)
    except CutoffError as exc:
        raise ValidationError([str(exc)]) from exc
    return s_val, r_val


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class ReportRow:
    record_no: str
    revision_no: int
    patient_id: str
    sample_date: date
    organism: str
    drug: str
    mic_raw: str
    included: bool
    dedup_reason: str
    anchor_record_no: Optional[str]
    category: Optional[str]          # S / I / R / AMBIGUOUS / NO_CUTOFF
    category_reason: str
    in_range: bool


def build_report(conn, start: Optional[date], end: Optional[date],
                 organism: Optional[str] = None,
                 drug: Optional[str] = None,
                 patient_id: Optional[str] = None) -> dict:
    """Compute the full report from one committed snapshot."""
    with db.read_snapshot(conn):
        return build_report_locked(conn, start, end, organism, drug, patient_id)


def build_report_locked(conn, start: Optional[date], end: Optional[date],
                        organism: Optional[str] = None,
                        drug: Optional[str] = None,
                        patient_id: Optional[str] = None) -> dict:
    """Variant for callers that already hold an open snapshot transaction,
    so an entire page can be rendered from one committed state.
    """
    version = db.current_snapshot_version(conn)
    records = db.fetch_all_current_records_for_dedup(conn)
    cutoff_rows = db.list_cutoffs(conn)

    cutoffs = {(c["organism"], c["drug"]): (c["s_cutoff"], c["r_cutoff"])
               for c in cutoff_rows}

    # Group full history for dedup (windows are never reset by query ranges).
    groups: dict[tuple, list[DedupRow]] = defaultdict(list)
    for r in records:
        key = (r["patient_id"], r["organism"], r["drug"])
        d = date.fromisoformat(r["sample_date"])
        groups[key].append(DedupRow(r["record_no"], d))

    decisions: dict[str, DedupDecision] = {}
    for key, grp_rows in groups.items():
        for decision in dedupe_group(grp_rows):
            decisions[decision.record_no] = decision

    detail: list[ReportRow] = []
    # summary keyed by (organism, drug)
    summary: dict[tuple, dict] = {}

    def _bucket(o: str, d: str) -> dict:
        key = (o, d)
        if key not in summary:
            summary[key] = {
                "organism": o, "drug": d,
                "included": 0, "excluded_in_range": 0,
                "S": 0, "I": 0, "R": 0,
                "ambiguous": 0, "no_cutoff": 0,
            }
        return summary[key]

    for r in records:
        rec_no = r["record_no"]
        d = date.fromisoformat(r["sample_date"])
        if organism and r["organism"] != organism:
            continue
        if drug and r["drug"] != drug:
            continue
        if patient_id and r["patient_id"] != patient_id:
            continue
        in_range = is_in_range(d, start, end)
        decision = decisions[rec_no]
        bucket = _bucket(r["organism"], r["drug"])

        if not decision.included:
            if in_range:
                bucket["excluded_in_range"] += 1
                detail.append(ReportRow(
                    rec_no, r["revision_no"], r["patient_id"], d,
                    r["organism"], r["drug"], r["mic_raw"],
                    False, decision.reason, decision.anchor_record_no,
                    None, "去重剔除（不参与判读与分母）", True,
                ))
            continue

        if not in_range:
            # Included but outside the requested range: still occupies its
            # window (already accounted for in dedup), but not shown as data.
            continue

        bucket["included"] += 1
        cutoff = cutoffs.get((r["organism"], r["drug"]))
        category: Optional[str]
        category_reason: str
        if cutoff is None:
            category = "NO_CUTOFF"
            category_reason = "尚未录入该菌药组合的 S/R 切点，无法判读"
            bucket["no_cutoff"] += 1
        else:
            s_c, r_c = cutoff
            mic = parse_mic(r["mic_raw"])
            category = classify_mic(mic, s_c, r_c)
            if category == "AMBIGUOUS":
                category_reason = (
                    f"界值 {mic.display()} 的可能取值跨越多个判读档"
                    f"（S≤{s_c:g}，R≥{r_c:g}），需复测原始标本后才能判读；"
                    "该记录计入去重窗口但不计入可判读分母"
                )
                bucket["ambiguous"] += 1
            else:
                label = {"S": "敏感(S)", "I": "中介(I)", "R": "耐药(R)"}[category]
                category_reason = (
                    f"MIC {mic.display()} 按切点 S≤{s_c:g}、R≥{r_c:g} 判读为 {label}"
                )
                bucket[category] += 1
        detail.append(ReportRow(
            rec_no, r["revision_no"], r["patient_id"], d,
            r["organism"], r["drug"], r["mic_raw"],
            True, decision.reason, decision.anchor_record_no,
            category, category_reason, True,
        ))

    # Finalize summary rows with R ratio.
    summary_rows = []
    for bucket in summary.values():
        if bucket["included"] == 0 and bucket["excluded_in_range"] == 0:
            continue
        interpretable = bucket["S"] + bucket["I"] + bucket["R"]
        bucket["interpretable"] = interpretable
        bucket["r_ratio"] = (bucket["R"] / interpretable) if interpretable else None
        if interpretable == 0:
            bucket["ratio_note"] = "可判读分母为 0，无法计算 R 占比"
        else:
            bucket["ratio_note"] = ""
        summary_rows.append(bucket)
    summary_rows.sort(key=lambda b: (b["organism"], b["drug"]))

    detail.sort(key=lambda x: (x.organism, x.drug, x.patient_id,
                               x.sample_date, x.record_no))

    retest = [row for row in detail if row.category == "AMBIGUOUS"]
    return {
        "snapshot_version": version,
        "start": start.isoformat() if start else "",
        "end": end.isoformat() if end else "",
        "organism": organism or "",
        "drug": drug or "",
        "patient_id": patient_id or "",
        "summary": summary_rows,
        "detail": detail,
        "retest": retest,
        "cutoffs": cutoff_rows,
    }
