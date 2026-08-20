from fastapi import FastAPI, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
import io

app = FastAPI(title="AI Mugalim Komekshisi")
BASE = Path(__file__).parent

def parse_file(name: str, data: bytes):
    ext = Path(name).suffix.lower()
    text = ""
    try:
        if ext in {".txt",".csv"}:
            text = data.decode("utf-8", errors="ignore")
        elif ext == ".docx":
            from docx import Document
            d=Document(io.BytesIO(data))
            text="\n".join(p.text for p in d.paragraphs)
            for t in d.tables:
                for row in t.rows: text += "\n" + " | ".join(c.text for c in row.cells)
        elif ext == ".pdf":
            from pypdf import PdfReader
            text="\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        elif ext == ".xlsx":
            from openpyxl import load_workbook
            wb=load_workbook(io.BytesIO(data), data_only=True)
            for ws in wb.worksheets:
                text += f"\n[{ws.title}]\n"
                for row in ws.iter_rows(values_only=True):
                    text += " | ".join("" if v is None else str(v) for v in row) + "\n"
        elif ext == ".pptx":
            from pptx import Presentation
            prs=Presentation(io.BytesIO(data))
            text="\n".join(sh.text for sl in prs.slides for sh in sl.shapes if hasattr(sh,"text"))
        elif ext in {".jpg",".jpeg",".png"}:
            text="Сурет қабылданды. OCR модулін серверде бөлек қосу қажет."
        else:
            text="Файл қабылданды, бірақ бұл форматқа parser әлі қосылмаған."
    except Exception as e:
        text=f"Файл оқылды, бірақ мәтінді шығару кезінде қате: {e}"
    return text

@app.post("/api/parse")
async def parse(file: UploadFile = File(...)):
    data=await file.read()
    text=parse_file(file.filename, data)
    return {"message":f"{file.filename} өңделді", "text":text}


import csv
import re

def _clean(v):
    return "" if v is None else str(v).strip()

def _looks_like_name(s):
    s=_clean(s)
    if len(s) < 3 or s.isdigit(): return False
    low=s.lower()
    bad=("id","сынып","класс","қатысу","баға","номер","№","телефон","email")
    if low in bad: return False
    return any(ch.isalpha() for ch in s)

def parse_class_list(name: str, data: bytes):
    ext=Path(name).suffix.lower()
    rows=[]
    if ext==".xlsx":
        from openpyxl import load_workbook
        wb=load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws=wb.active
        rows=[[_clean(v) for v in row] for row in ws.iter_rows(values_only=True)]
    elif ext in {".csv",".txt"}:
        text=None
        for enc in ("utf-8-sig","utf-8","cp1251","windows-1251"):
            try:
                text=data.decode(enc); break
            except Exception: pass
        if text is None: text=data.decode("utf-8",errors="ignore")
        sample=text[:4096]
        try: dialect=csv.Sniffer().sniff(sample, delimiters=";,|\t,")
        except Exception: dialect=csv.excel
        rows=[[_clean(v) for v in r] for r in csv.reader(io.StringIO(text), dialect)]
    else:
        return []

    rows=[r for r in rows if any(r)]
    if not rows: return []

    header=[c.lower() for c in rows[0]]
    name_keys=("аты-жөні","аты жөні","оқушы","фио","ф.и.о","full name","name","student")
    idx=None
    for i,h in enumerate(header):
        if any(k in h for k in name_keys):
            idx=i; break

    start=1 if idx is not None else 0
    if idx is None:
        # Choose the column with the most name-like values.
        width=max(len(r) for r in rows)
        scores=[]
        for c in range(width):
            vals=[r[c] if c<len(r) else "" for r in rows[:100]]
            scores.append(sum(_looks_like_name(v) for v in vals))
        idx=max(range(width), key=lambda c:scores[c])

    names=[]
    for r in rows[start:]:
        if idx < len(r):
            v=_clean(r[idx])
            if _looks_like_name(v) and v not in names:
                names.append(v)
    return names[:40]

@app.post("/api/import-class")
async def import_class(file: UploadFile = File(...)):
    data=await file.read()
    names=parse_class_list(file.filename, data)
    return {"count":len(names),"students":names,"message":f"{len(names)} оқушы табылды"}

@app.get("/")
def home(): return FileResponse(BASE/"index.html")

app.mount("/static", StaticFiles(directory=BASE), name="static")

# =========================
# QR attendance API
# =========================
from pydantic import BaseModel
from fastapi import HTTPException
from fastapi.responses import Response
from datetime import datetime, timedelta
from io import BytesIO
import secrets
import qrcode

QR_SESSIONS = {}

class SessionStart(BaseModel):
    class_name: str = "Сынып"
    topic: str = "Сабақ"
    minutes: int = 10

class CheckIn(BaseModel):
    token: str
    student_id: str = ""
    name: str
    class_name: str

@app.get("/api/health")
def health():
    return {"ok": True}

@app.post("/api/session/start")
def session_start(body: SessionStart):
    token = secrets.token_hex(5).upper()
    now = datetime.now()
    minutes = max(1, min(int(body.minutes or 10), 120))
    QR_SESSIONS[token] = {
        "class_name": body.class_name.strip() or "Сынып",
        "topic": body.topic.strip() or "Сабақ",
        "active": True,
        "expires_at": now + timedelta(minutes=minutes),
        "attendance": {}
    }
    s = QR_SESSIONS[token]
    return {
        "token": token,
        "class_name": s["class_name"],
        "topic": s["topic"],
        "expires_at": s["expires_at"].isoformat()
    }

def _session(token: str):
    s = QR_SESSIONS.get(token)
    if not s:
        raise HTTPException(status_code=404, detail="QR-сабақ табылмады.")
    if datetime.now() >= s["expires_at"]:
        s["active"] = False
    return s

@app.get("/api/session/{token}")
def session_info(token: str):
    s = _session(token)
    return {
        "class_name": s["class_name"],
        "topic": s["topic"],
        "active": s["active"],
        "expires_at": s["expires_at"].isoformat()
    }

@app.get("/api/session/{token}/qr.png")
def session_qr(token: str, base: str):
    _session(token)
    join_url = base.rstrip("/") + "/?session=" + token
    img = qrcode.make(join_url)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")

@app.post("/api/session/stop/{token}")
def session_stop(token: str):
    s = _session(token)
    s["active"] = False
    return {"ok": True}

@app.post("/api/checkin")
def checkin(body: CheckIn):
    s = _session(body.token)
    if not s["active"]:
        raise HTTPException(status_code=410, detail="QR-сабақ аяқталған немесе уақыты біткен.")
    name = body.name.strip()
    class_name = body.class_name.strip()
    if not name or not class_name:
        raise HTTPException(status_code=400, detail="Аты-жөні мен сынып міндетті.")
    student_id = body.student_id.strip().upper()
    key = student_id.lower() if student_id else (name + "|" + class_name).lower()
    now = datetime.now().strftime("%H:%M:%S")
    if key not in s["attendance"]:
        s["attendance"][key] = {
            "student_id": student_id,
            "name": name,
            "class_name": class_name,
            "time": now
        }
    return {"ok": True, "time": s["attendance"][key]["time"]}

@app.get("/api/session/{token}/attendance")
def session_attendance(token: str):
    s = _session(token)
    return {
        "class_name": s["class_name"],
        "topic": s["topic"],
        "active": s["active"],
        "expires_at": s["expires_at"].isoformat(),
        "attendance": list(s["attendance"].values())
    }
