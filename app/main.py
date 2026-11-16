"""FastAPI application: HTML dashboard + JSON API.

Both the browser forms and a machine-readable JSON API are exposed. All
mutations validate the entire payload before opening the write transaction,
so a rejected batch never touches accepted data.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, services
from .services import ValidationError

DB_PATH = os.environ.get("APP_DB_PATH", "/data/susceptibility.db")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db.init_db(DB_PATH)
    yield


app = FastAPI(title="院感药敏耐药统计", version="1.0.0", lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db.init_db(DB_PATH)
    conn = db.get_conn(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _parse_query_date(value: Optional[str], name: str) -> Optional[date]:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError([f"{name} 必须是 YYYY-MM-DD 格式，收到 {value!r}"]) from exc


def _check_date_order(start: Optional[date], end: Optional[date]) -> None:
    if start and end and start > end:
        raise ValidationError([f"起始日期 {start} 不能晚于结束日期 {end}"])


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, conn=Depends(get_db)):
    with db.read_snapshot(conn):
        cutoffs = db.list_cutoffs(conn)
        version = db.current_snapshot_version(conn)
        batches = conn.execute(
            "SELECT batch_ref, filename, row_count, created_at "
            "FROM upload_batches ORDER BY id DESC LIMIT 20"
        ).fetchall()
    return templates.TemplateResponse(request, "index.html", {
        "cutoffs": cutoffs, "batches": batches,
        "snapshot_version": version, "report": None,
        "msg": request.query_params.get("msg", ""),
        "err": request.query_params.get("err", ""),
    })


@app.get("/report", response_class=HTMLResponse)
def report_page(request: Request, start: Optional[str] = None,
                end: Optional[str] = None, organism: Optional[str] = None,
                drug: Optional[str] = None, patient_id: Optional[str] = None,
                conn=Depends(get_db)):
    try:
        d_start = _parse_query_date(start, "起始日期")
        d_end = _parse_query_date(end, "结束日期")
        _check_date_order(d_start, d_end)
        # One read transaction -> the whole page reflects one committed state.
        with db.read_snapshot(conn):
            report = services.build_report_locked(
                conn, d_start, d_end,
                (organism or "").strip() or None,
                (drug or "").strip() or None,
                (patient_id or "").strip() or None,
            )
            cutoffs = db.list_cutoffs(conn)
            batches = conn.execute(
                "SELECT batch_ref, filename, row_count, created_at "
                "FROM upload_batches ORDER BY id DESC LIMIT 20"
            ).fetchall()
    except ValidationError as exc:
        return templates.TemplateResponse(request, "error.html", {
            "errors": exc.errors,
        }, status_code=400)
    return templates.TemplateResponse(request, "index.html", {
        "cutoffs": cutoffs, "batches": batches,
        "snapshot_version": report["snapshot_version"], "report": report,
        "msg": "", "err": "",
    })


@app.get("/records/{record_no}", response_class=HTMLResponse)
def record_history_page(request: Request, record_no: str, conn=Depends(get_db)):
    with db.read_snapshot(conn):
        rows = db.fetch_record_history(conn, record_no)
    if not rows:
        return templates.TemplateResponse(request, "error.html", {
            "errors": [f"未找到记录号 {record_no}"],
        }, status_code=404)
    return templates.TemplateResponse(request, "history.html", {
        "record_no": record_no, "rows": rows,
    })


@app.post("/upload")
async def upload_html(file: UploadFile = File(...), conn=Depends(get_db)):
    data = await file.read()
    if not data:
        return RedirectResponse("/?err=" + _q("上传文件为空"), status_code=303)
    if len(data) > MAX_UPLOAD_BYTES:
        return RedirectResponse(
            "/?err=" + _q(f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"),
            status_code=303,
        )
    try:
        rows = services.parse_and_validate_csv(data, conn)
        with db.write_transaction(conn):
            result = db.insert_batch(conn, file.filename or "upload.csv", rows)
    except ValidationError as exc:
        return HTMLResponse(_render_upload_errors(exc.errors), status_code=400)
    msg = (f"批次 {result['batch_ref'][:8]} 已接纳：新增/更新 {result['inserted']} 条，"
           f"其中高修订更正 {result['corrections']} 条；"
           f"当前快照版本 v{result['snapshot_version']}")
    return RedirectResponse("/?msg=" + _q(msg), status_code=303)


@app.post("/cutoffs")
def cutoff_html(organism: str = Form(...), drug: str = Form(...),
                s_cutoff: str = Form(...), r_cutoff: str = Form(...),
                conn=Depends(get_db)):
    organism = organism.strip()
    drug = drug.strip()
    if not organism or not drug:
        return RedirectResponse("/?err=" + _q("菌种与药物名称不能为空"), status_code=303)
    try:
        s_val, r_val = services.validate_cutoff_inputs(s_cutoff, r_cutoff)
        with db.write_transaction(conn):
            result = db.upsert_cutoff(conn, organism, drug, s_val, r_val)
    except ValidationError as exc:
        return RedirectResponse("/?err=" + _q("；".join(exc.errors)), status_code=303)
    msg = (f"切点已保存：{organism} / {drug}，S≤{result['s_cutoff']:g}，"
           f"R≥{result['r_cutoff']:g}；快照版本 v{result['snapshot_version']}")
    return RedirectResponse("/?msg=" + _q(msg), status_code=303)


def _q(text: str) -> str:
    from urllib.parse import quote
    return quote(text, safe="")


def _render_upload_errors(errors: list[str]) -> str:
    items = "".join(f"<li>{_escape(e)}</li>" for e in errors)
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>批次被拒绝</title>"
        "<style>body{font-family:sans-serif;margin:2rem;line-height:1.6}"
        "a{display:inline-block;margin-top:1rem}</style></head><body>"
        "<h2>整批未接纳，已接纳数据未做任何改动</h2><ul>" + items + "</ul>"
        "<a href='/'>返回首页修正后重新上传</a></body></html>"
    )


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------

@app.get("/health")
def health(conn=Depends(get_db)):
    return {"status": "ok",
            "snapshot_version": db.current_snapshot_version(conn)}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...), conn=Depends(get_db)):
    data = await file.read()
    if not data:
        return JSONResponse({"accepted": False, "errors": ["上传文件为空"]},
                            status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"accepted": False,
             "errors": [f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"]},
            status_code=413,
        )
    try:
        rows = services.parse_and_validate_csv(data, conn)
        with db.write_transaction(conn):
            result = db.insert_batch(conn, file.filename or "upload.csv", rows)
    except ValidationError as exc:
        return JSONResponse(
            {"accepted": False, "errors": exc.errors,
             "note": "整批非法输入已拒绝，已接纳数据未改动"},
            status_code=422,
        )
    return {"accepted": True, **result}


@app.post("/api/cutoffs")
async def api_cutoff(payload: dict, conn=Depends(get_db)):
    try:
        organism = str(payload.get("organism", "")).strip()
        drug = str(payload.get("drug", "")).strip()
        if not organism or not drug:
            raise ValidationError(["organism 与 drug 不能为空"])
        s_val, r_val = services.validate_cutoff_inputs(
            payload.get("s_cutoff", ""), payload.get("r_cutoff", ""))
        with db.write_transaction(conn):
            result = db.upsert_cutoff(conn, organism, drug, s_val, r_val)
    except ValidationError as exc:
        return JSONResponse({"accepted": False, "errors": exc.errors},
                            status_code=422)
    return {"accepted": True, **result}


@app.get("/api/cutoffs")
def api_list_cutoffs(conn=Depends(get_db)):
    return {"cutoffs": db.list_cutoffs(conn),
            "snapshot_version": db.current_snapshot_version(conn)}


@app.get("/api/report")
def api_report(start: Optional[str] = None, end: Optional[str] = None,
               organism: Optional[str] = None, drug: Optional[str] = None,
               patient_id: Optional[str] = None, conn=Depends(get_db)):
    try:
        d_start = _parse_query_date(start, "start")
        d_end = _parse_query_date(end, "end")
        _check_date_order(d_start, d_end)
        report = services.build_report(
            conn, d_start, d_end,
            (organism or "").strip() or None,
            (drug or "").strip() or None,
            (patient_id or "").strip() or None,
        )
    except ValidationError as exc:
        return JSONResponse({"errors": exc.errors}, status_code=400)
    report["detail"] = [
        {**vars(row), "sample_date": row.sample_date.isoformat()}
        for row in report["detail"]
    ]
    report["retest"] = [
        {**vars(row), "sample_date": row.sample_date.isoformat()}
        for row in report["retest"]
    ]
    return report


@app.get("/api/records/{record_no}")
def api_record_history(record_no: str, conn=Depends(get_db)):
    rows = db.fetch_record_history(conn, record_no)
    if not rows:
        return JSONResponse({"errors": [f"未找到记录号 {record_no}"]},
                            status_code=404)
    return {"record_no": record_no,
            "revisions": [dict(r) for r in rows]}
