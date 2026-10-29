#!/usr/bin/env python3
"""
Academic Literature Hybrid Downloader - Web Application Backend (FastAPI)
Features:
- Three-tier fallback engine: Legal OA (fetchpdf/OpenAlex/Semantic Scholar/arXiv) + Sci-Hub + Anna's Archive
- Single DOI/PMID fast fetch & preview
- Batch process from Excel (.xlsx, .xls), CSV, or TXT
- Live background task progress tracking
- Automatic standard renaming & ZIP archive packaging
- Library file manager with PDF preview & export
"""

import os
import sys
import re
import time
import json
import uuid
import shutil
import zipfile
import threading
import requests
import urllib3
import pypdf
import pandas as pd
from pathlib import Path
from typing import List, Optional, Dict, Any
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from oa_engine import download_paper_pipeline, get_email

urllib3.disable_warnings()

# Base directories
BASE_DIR = Path(os.environ.get("LITERATURE_DATA_DIR", str(Path(__file__).resolve().parent / "data"))).resolve()
DOWNLOADS_DIR = BASE_DIR / "downloads"
RENAMED_DIR = BASE_DIR / "renamed_papers"
ZIP_PATH = BASE_DIR / "已下载论文汇总.zip"
ENV_FILE = BASE_DIR / "settings.json"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
RENAMED_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
}

SCIHUB_MIRRORS = []
ANNAS_MIRRORS = []

# Task storage
TASKS: Dict[str, Dict[str, Any]] = {}

app = FastAPI(title="Academic Literature Downloader Web UI", docs_url=None, redoc_url=None)

@app.get("/api/settings")
def get_settings():
    return {"email": get_email(), "scihub_mirrors": [], "annas_mirrors": [], "env_path": "由管理员配置"}

@app.get("/api/mirrors/test")
def test_sources():
    results = []
    for url in ('https://arxiv.org', 'https://api.openalex.org'):
        start = time.monotonic()
        try:
            response = requests.get(url, timeout=8)
            status = 'HEALTHY' if response.ok else 'OFFLINE'
        except requests.RequestException:
            status = 'OFFLINE'
        results.append({'type': '开放获取', 'url': url, 'status': status, 'latency_ms': round((time.monotonic() - start) * 1000)})
    return results


# Static & Templates setup
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def sanitize_filename(name: str) -> str:
    s = re.sub(r'[\/\\:\*\?\"<>\|]', '_', str(name))
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'_+', '_', s)
    return s.strip()

def clean_identifier(raw: str) -> str:
    s = str(raw).strip()
    s = re.sub(r"^https?://(dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    if s.endswith(".0"):
        s = s[:-2]
    return s.strip()

# ==================== Retrieval Engines ====================





# ==================== Background Batch Task Runner ====================

def run_batch_task(task_id: str, items: List[Dict[str, str]], workers: int, engines: List[str]):
    task = TASKS[task_id]
    task["status"] = "RUNNING"
    task["total"] = len(items)
    
    def process_item(item_idx):
        if task["status"] == "STOPPED":
            return None
        
        idx, it = item_idx
        ident = it.get("doi") or it.get("pmid") or it.get("identifier")
        title = it.get("title", "")
        num = it.get("num", f"#{idx+1}")
        journal = it.get("journal", "")
        
        res = download_paper_pipeline(ident, title, engines, DOWNLOADS_DIR)
        res["num"] = num
        res["journal"] = journal
        if not res["title"]:
            res["title"] = title
            
        task["completed"] += 1
        if res["status"] == "SUCCESS":
            task["success"] += 1
        else:
            task["failed"] += 1
            
        task["items"].append(res)
        return res

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = list(executor.map(process_item, enumerate(items)))

    task["status"] = "COMPLETED"
    task["end_time"] = time.strftime("%Y-%m-%d %H:%M:%S")

# ==================== API Endpoints ====================

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    index_file = TEMPLATES_DIR / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return "<h1>Web UI Template not found</h1>"

@app.get("/api/stats")
async def get_stats():
    pdfs = list(DOWNLOADS_DIR.glob("*.pdf")) + list(RENAMED_DIR.glob("*.pdf"))
    unique_pdfs = set(p.name for p in pdfs)
    total_size_mb = sum(p.stat().st_size for p in set(DOWNLOADS_DIR.glob("*.pdf")).union(RENAMED_DIR.glob("*.pdf"))) / (1024 * 1024)
    return {
        "pdf_count": len(unique_pdfs),
        "total_size_mb": round(total_size_mb, 2),
        "email": get_email(),
        "active_mirrors": len(SCIHUB_MIRRORS) + len(ANNAS_MIRRORS),
    }

class SingleDownloadRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=300)
    engines: Optional[List[str]] = ["oa"]

@app.post("/api/download/single")
def api_download_single(req: SingleDownloadRequest):
    ident = clean_identifier(req.identifier)
    if not ident:
        raise HTTPException(status_code=400, detail="Identifier cannot be empty")
    
    res = download_paper_pipeline(ident, "", req.engines or ["oa"], DOWNLOADS_DIR)
    return res

class TextBatchRequest(BaseModel):
    text: str = Field(min_length=1, max_length=60000)
    workers: int = Field(default=3, ge=1, le=6)
    engines: Optional[List[str]] = ["oa"]

@app.post("/api/download/batch/text")
async def api_batch_text(req: TextBatchRequest, background_tasks: BackgroundTasks):
    lines = [l.strip() for l in req.text.splitlines() if l.strip() and not l.startswith("#")]
    if not lines:
        raise HTTPException(status_code=400, detail="No valid identifiers found in text")
        
    if len(lines) > 200:
        raise HTTPException(400, "每批最多 200 条")
    items = [{"identifier": l, "doi": l, "title": "", "num": f"#{i+1}"} for i, l in enumerate(lines)]
    task_id = str(uuid.uuid4())[:8]
    TASKS[task_id] = {
        "task_id": task_id,
        "name": f"文本输入批量 ({len(items)} 条)",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PENDING",
        "total": len(items),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "items": []
    }
    background_tasks.add_task(run_batch_task, task_id, items, req.workers or 6, req.engines or ["oa"])
    return {"task_id": task_id, "total": len(items)}

@app.post("/api/download/batch/upload")
async def api_batch_upload(
    file: UploadFile = File(...),
    workers: int = Form(6),
    engines: str = Form("oa"),
    background_tasks: BackgroundTasks = None
):
    if not 1 <= workers <= 6:
        raise HTTPException(400, "并发数必须在 1–6 之间")
    contents = await file.read(2 * 1024 * 1024 + 1)
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(413, "文件上限 2 MB")
    temp_path = DOWNLOADS_DIR / f"_upload_{uuid.uuid4().hex}_{Path(file.filename or 'input.txt').name}"
    with open(temp_path, "wb") as f:
        f.write(contents)

    ext = temp_path.suffix.lower()
    items = []
    
    try:
        if ext in [".xlsx", ".xls"]:
            df = pd.read_excel(temp_path)
        elif ext == ".csv":
            df = pd.read_csv(temp_path)
        else:
            lines = [l.strip() for l in open(temp_path, encoding="utf-8") if l.strip() and not l.startswith("#")]
            df = pd.DataFrame({"DOI": lines})

        # Match columns
        id_col = None
        for c in df.columns:
            if str(c).lower() in ["doi", "pmid", "pubmed id", "identifier", "doi link"]:
                id_col = c
                break
        if not id_col:
            id_col = df.columns[0]

        title_col = None
        for c in df.columns:
            if str(c).lower() in ["title", "paper title", "publication title", "论文标题"]:
                title_col = c
                break

        num_col = None
        for c in df.columns:
            if str(c).lower() in ["编号", "num", "id", "no", "序号"]:
                num_col = c
                break

        journal_col = None
        for c in df.columns:
            if str(c).lower() in ["journal", "publication title", "期刊"]:
                journal_col = c
                break

        for idx, row in df.iterrows():
            ident_val = str(row.get(id_col, "")).strip()
            if ident_val and ident_val != "nan":
                items.append({
                    "identifier": ident_val,
                    "doi": ident_val,
                    "title": str(row.get(title_col, "")) if title_col else "",
                    "num": str(row.get(num_col, f"#{idx+1}")) if num_col else f"#{idx+1}",
                    "journal": str(row.get(journal_col, "")) if journal_col else "",
                })
    finally:
        if temp_path.exists():
            temp_path.unlink()

    if len(items) > 200:
        raise HTTPException(400, "每批最多 200 条")
    if not items:
        raise HTTPException(status_code=400, detail="No records parsed from uploaded file")

    engine_list = [e.strip().lower() for e in engines.split(",") if e.strip()]
    task_id = str(uuid.uuid4())[:8]
    TASKS[task_id] = {
        "task_id": task_id,
        "name": f"{file.filename} ({len(items)} 条)",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "PENDING",
        "total": len(items),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "items": []
    }
    background_tasks.add_task(run_batch_task, task_id, items, workers, engine_list)
    return {"task_id": task_id, "total": len(items), "filename": file.filename}

@app.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    return TASKS[task_id]

@app.get("/api/tasks")
async def list_all_tasks():
    return list(TASKS.values())[-100:]

@app.get("/api/library")
async def get_library():
    # Scan both renamed_papers and downloads
    items = []
    
    # Check renamed folder first
    for f in sorted(DOWNLOADS_DIR.glob("*.pdf")):
        try:
            reader = pypdf.PdfReader(str(f))
            pages = len(reader.pages)
        except:
            pages = "N/A"
            
        m = re.match(r"^\[(RE\d+|#\d+|\w+)\]\s*(.*)\.pdf$", f.name)
        num = m.group(1) if m else "-"
        title = m.group(2) if m else f.name
        
        items.append({
            "num": num,
            "title": title,
            "filename": f.name,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "pages": pages,
            "folder": "downloads",
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime))
        })
        
    return {
        "count": len(items),
        "items": items,
        "zip_available": ZIP_PATH.exists(),
        "zip_size_mb": round(ZIP_PATH.stat().st_size / (1024*1024), 2) if ZIP_PATH.exists() else 0
    }

@app.get("/api/files/download/{folder}/{filename}")
async def download_file(folder: str, filename: str, inline: bool = False):
    if folder not in ("renamed", "downloads") or Path(filename).name != filename or "\\" in filename or not filename.endswith(".pdf"):
        raise HTTPException(400, "Invalid file path")
    target_folder = RENAMED_DIR if folder == "renamed" else DOWNLOADS_DIR
    target_file = target_folder / filename
    if not target_file.exists():
        raise HTTPException(status_code=404, detail="File not found")
        
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        path=str(target_file),
        media_type="application/pdf",
        filename=filename, content_disposition_type=disposition
    )

@app.get("/api/files/zip")
async def download_zip():
    if not ZIP_PATH.exists():
        raise HTTPException(status_code=404, detail="ZIP package not generated yet")
    return FileResponse(
        path=str(ZIP_PATH),
        media_type="application/zip",
        filename="已下载论文汇总.zip"
    )

@app.post("/api/package/zip")
async def trigger_packaging():
    # Sync and rename files from downloads to renamed_papers
    excel_path = BASE_DIR / "file_260901_193923_91590.xlsx"
    df = pd.read_excel(excel_path) if excel_path.exists() else pd.DataFrame()
    
    # 1. Match unorganized downloads to Excel if available
    if not df.empty:
        for idx, row in df.iterrows():
            num = str(row.get("编号", f"RE{idx+1:03d}")).strip()
            title = str(row.get("Title", "")).strip()
            clean_t = sanitize_filename(title)
            dest = RENAMED_DIR / f"[{num}] {clean_t}.pdf"
            
            if not dest.exists():
                doi = str(row.get("DOI", "")).strip()
                doi_part = doi.split("/")[-1].lower() if "/" in doi else doi.lower()
                matched = [f for f in DOWNLOADS_DIR.glob("*.pdf") if doi_part in f.name.lower()]
                if matched:
                    shutil.copy2(matched[0], dest)

    # 2. Build index CSV
    records = []
    for pdf_file in sorted(DOWNLOADS_DIR.glob("*.pdf")):
        m = re.match(r"^\[(RE\d+|#\d+|\w+)\]\s*(.*)\.pdf$", pdf_file.name)
        num = m.group(1) if m else "-"
        title = m.group(2) if m else pdf_file.name
        
        try:
            pages = len(pypdf.PdfReader(str(pdf_file)).pages)
        except:
            pages = "N/A"
            
        records.append({
            "编号": num,
            "文件名称": pdf_file.name,
            "论文标题": title,
            "页数": pages,
            "文件大小(KB)": round(pdf_file.stat().st_size / 1024, 1)
        })

    summary_df = pd.DataFrame(records)
    index_csv = RENAMED_DIR / "文献清单索引.csv"
    summary_df.to_csv(index_csv, index=False, encoding="utf-8-sig")

    # 3. Create zip
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zipf:
        for f in sorted(DOWNLOADS_DIR.glob("*.pdf")):
            zipf.write(f, arcname=f.name)
        if index_csv.exists():
            zipf.write(index_csv, arcname=index_csv.name)

    return {
        "status": "SUCCESS",
        "total_files": len(records),
        "zip_size_mb": round(ZIP_PATH.stat().st_size / (1024*1024), 2),
        "download_url": "/tools/literature/api/files/zip"
    }

class SettingsUpdate(BaseModel):
    email: Optional[str] = None

@app.post('/api/settings')
def update_settings(cfg: SettingsUpdate):
    email = (cfg.email or '').strip()
    if not re.fullmatch(r'[^\s@\r\n]{1,100}@[^\s@\r\n]{1,100}\.[A-Za-z]{2,24}', email):
        raise HTTPException(400, '请输入有效联系邮箱')
    ENV_FILE.write_text(json.dumps({'email': email}), encoding='utf-8')
    os.environ['LITERATURE_CONTACT_EMAIL'] = email
    return {'status': 'SUCCESS', 'email': email}




if __name__ == "__main__":
    import uvicorn
    print("🚀 启动 Academic Literature Hybrid Downloader Web 服务在 http://localhost:7860 ...")
    uvicorn.run(app, host="127.0.0.1", port=7860)
