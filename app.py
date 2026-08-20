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

from pydantic import BaseModel
from fastapi import HTTPException
from datetime import datetime
import secrets
SESSIONS={}
class SessionStart(BaseModel):
    class_name:str="Сынып"
    topic:str="Сабақ"
class CheckIn(BaseModel):
    token:str
    name:str
    class_name:str
@app.post("/api/session/start")
def start_session(body:SessionStart):
    token=secrets.token_urlsafe(6).replace("-","").replace("_","")[:8].upper()
    SESSIONS[token]={"class_name":body.class_name,"topic":body.topic,"active":True,"attendance":{}}
    return {"token":token,"class_name":body.class_name,"topic":body.topic}
@app.post("/api/session/stop/{token}")
def stop_session(token:str):
    if token in SESSIONS:SESSIONS[token]["active"]=False
    return {"ok":True}
@app.post("/api/checkin")
def checkin(body:CheckIn):
    s=SESSIONS.get(body.token)
    if not s or not s.get("active"):raise HTTPException(status_code=404,detail="QR-сабақ табылмады немесе аяқталған.")
    key=(body.name.strip()+"|"+body.class_name.strip()).lower();now=datetime.now().strftime("%H:%M")
    if key not in s["attendance"]:s["attendance"][key]={"name":body.name.strip(),"class_name":body.class_name.strip(),"time":now}
    return {"ok":True,"time":s["attendance"][key]["time"]}
@app.get("/api/session/{token}/attendance")
def get_attendance(token:str):
    s=SESSIONS.get(token)
    if not s:raise HTTPException(status_code=404,detail="Сессия табылмады.")
    return {"class_name":s["class_name"],"topic":s["topic"],"active":s["active"],"attendance":list(s["attendance"].values())}
