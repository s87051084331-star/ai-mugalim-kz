from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
import io, os, csv, re, json, secrets

app = FastAPI(title="AI Мұғалім көмекшісі — ZEREK Education")
BASE = Path(__file__).parent

# ---------------- File parsing ----------------
def parse_file(name: str, data: bytes) -> str:
    ext = Path(name).suffix.lower()
    try:
        if ext in {".txt", ".csv"}:
            for enc in ("utf-8-sig","utf-8","cp1251"):
                try: return data.decode(enc)
                except Exception: pass
            return data.decode("utf-8", errors="ignore")
        if ext == ".docx":
            from docx import Document
            d = Document(io.BytesIO(data))
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts)
        if ext == ".pdf":
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        if ext == ".xlsx":
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            parts=[]
            for ws in wb.worksheets:
                parts.append(f"[{ws.title}]")
                for row in ws.iter_rows(values_only=True):
                    parts.append(" | ".join("" if v is None else str(v) for v in row))
            return "\n".join(parts)
        if ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(io.BytesIO(data))
            return "\n".join(sh.text for sl in prs.slides for sh in sl.shapes if hasattr(sh, "text"))
        if ext in {".jpg",".jpeg",".png",".webp"}:
            return "Сурет қабылданды. Бұл нұсқада сурет OCR-ы қосылмаған."
        return "Файл форматы оқуға қолдау таппады."
    except Exception as e:
        return f"Файлды оқу қатесі: {e}"

@app.post("/api/parse")
async def parse(file: UploadFile = File(...)):
    data = await file.read()
    return {"message": f"{file.filename} өңделді", "text": parse_file(file.filename, data)}

# ---------------- Class import ----------------
def clean(v): return "" if v is None else str(v).strip()
def looks_name(s):
    s=clean(s)
    if len(s)<3 or s.isdigit(): return False
    return any(ch.isalpha() for ch in s) and s.lower() not in {"аты-жөні","оқушы","фио","name","student"}

def parse_class(name, data):
    ext=Path(name).suffix.lower(); rows=[]
    if ext==".xlsx":
        from openpyxl import load_workbook
        ws=load_workbook(io.BytesIO(data),data_only=True,read_only=True).active
        rows=[[clean(v) for v in row] for row in ws.iter_rows(values_only=True)]
    elif ext in {".csv",".txt"}:
        text=None
        for enc in ("utf-8-sig","utf-8","cp1251"):
            try: text=data.decode(enc); break
            except Exception: pass
        text=text or data.decode("utf-8",errors="ignore")
        try: dialect=csv.Sniffer().sniff(text[:4096],delimiters=";,|\t,")
        except Exception: dialect=csv.excel
        rows=[[clean(v) for v in r] for r in csv.reader(io.StringIO(text),dialect)]
    rows=[r for r in rows if any(r)]
    if not rows:return []
    header=[x.lower() for x in rows[0]]
    idx=None
    for i,h in enumerate(header):
        if any(k in h for k in ("аты-жөні","аты жөні","оқушы","фио","name","student")): idx=i;break
    start=1 if idx is not None else 0
    if idx is None:
        width=max(len(r) for r in rows)
        scores=[sum(looks_name(r[c] if c<len(r) else "") for r in rows[:100]) for c in range(width)]
        idx=max(range(width),key=lambda c:scores[c])
    names=[]
    for r in rows[start:]:
        if idx<len(r) and looks_name(r[idx]) and r[idx] not in names:names.append(r[idx])
    return names[:60]

@app.post("/api/import-class")
async def import_class(file: UploadFile = File(...)):
    data=await file.read(); names=parse_class(file.filename,data)
    return {"count":len(names),"students":names,"message":f"{len(names)} оқушы табылды"}

# ---------------- Optional Gemini helper ----------------
async def gemini_json(prompt: str):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key:return None
    import httpx
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json"}}
    async with httpx.AsyncClient(timeout=60) as client:
        r=await client.post(url,json=payload)
        if r.status_code>=400: raise HTTPException(r.status_code, f"Gemini: {r.text[:300]}")
        text=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

class KmjAnalyze(BaseModel):
    text: str
    hint: str = ""

@app.post("/api/kmj/analyze")
async def kmj_analyze(body: KmjAnalyze):
    prompt=f"""Сен Қазақстан мұғаліміне арналған көмекшісің. ҚМЖ мәтінін талда.
Тек JSON қайтар: subject, class_name, topic, learning_objective, lesson_goal,
tasks массиві: id, title, question, answer, descriptor, type.
Тапсырмаларды ҚМЖ-дан ғана ал. Жоқ жауапты бос қалдыр.
Мұғалім ескертпесі: {body.hint}
ҚМЖ:
{body.text[:45000]}"""
    data=await gemini_json(prompt)
    if data:return {"mode":"gemini","data":data}
    # Offline heuristic fallback
    text=body.text
    def find(label):
        m=re.search(label+r"\s*[:\-]?\s*([^\n|]{3,250})",text,re.I)
        return m.group(1).strip() if m else ""
    topic=find(r"(Сабақтың тақырыбы|Сабақ тақырыбы|Тақырып)")
    objective=find(r"(Оқу мақсаты|Оқу мақсаттары)")
    subject=find(r"(Пән)")
    cls=find(r"(Сынып)")
    tasks=[]
    patterns=re.split(r"(?i)(?=\b\d+\s*[-.)]?\s*тапсырма\b|тапсырма\s*\d+)",text)
    for p in patterns:
        if re.search(r"(?i)тапсырма",p) and len(p.strip())>15:
            q=re.sub(r"\s+"," ",p.strip())[:800]
            tasks.append({"id":len(tasks)+1,"title":f"{len(tasks)+1}-тапсырма","question":q,"answer":"","descriptor":"","type":"short"})
    return {"mode":"heuristic","data":{"subject":subject,"class_name":cls,"topic":topic,"learning_objective":objective,"lesson_goal":"","tasks":tasks[:12]}}

class TaskGenerate(BaseModel):
    lesson: dict
    format: str
    instruction: str = ""

@app.post("/api/tasks/generate")
async def task_generate(body: TaskGenerate):
    prompt=f"""ҚМЖ-дан алынған мәліметті оқушыға арналған интерактивті тапсырмаға бейімде.
Формат: {body.format}
Мұғалім ұсынысы: {body.instruction}
Тек JSON қайтар: tasks массиві. Әр элемент: id,type,question,options(array),correct_answer,descriptor,max_score.
type тек: test, match, fill, truefalse, short.
Оқу мақсаты мен ҚМЖ мазмұнын сақта. Жаңа оқу мақсатын ойдан қоспа.
Дерек: {json.dumps(body.lesson,ensure_ascii=False)[:35000]}"""
    data=await gemini_json(prompt)
    if data:return {"mode":"gemini","data":data}
    source=body.lesson.get("tasks",[])
    tasks=[]
    for i,t in enumerate(source,1):
        tasks.append({"id":i,"type":"short","question":t.get("question",""),"options":[],"correct_answer":t.get("answer",""),"descriptor":t.get("descriptor",""),"max_score":1})
    return {"mode":"heuristic","data":{"tasks":tasks}}

# ---------------- Lesson / task sessions ----------------
LESSONS={}
class PublishLesson(BaseModel):
    subject:str=""
    class_name:str=""
    topic:str=""
    learning_objective:str=""
    tasks:list=[]

@app.post("/api/lesson/publish")
def publish_lesson(body: PublishLesson):
    code=secrets.token_hex(4).upper()
    LESSONS[code]={"subject":body.subject,"class_name":body.class_name,"topic":body.topic,"learning_objective":body.learning_objective,"tasks":body.tasks,"results":[]}
    return {"code":code}

@app.get("/api/lesson/{code}")
def get_lesson(code:str):
    if code not in LESSONS:raise HTTPException(404,"Сабақ коды табылмады.")
    return LESSONS[code]

class TaskSubmit(BaseModel):
    code:str
    student_id:str=""
    name:str
    answers:dict

@app.post("/api/lesson/submit")
def submit_tasks(body:TaskSubmit):
    if body.code not in LESSONS:raise HTTPException(404,"Сабақ табылмады.")
    lesson=LESSONS[body.code]; items=[]; score=0; total=0
    for t in lesson["tasks"]:
        tid=str(t.get("id","")); ans=str(body.answers.get(tid,"")).strip()
        correct=str(t.get("correct_answer","")).strip()
        maxs=float(t.get("max_score",1) or 1); total+=maxs
        ok=bool(correct) and ans.lower()==correct.lower()
        got=maxs if ok else 0;score+=got
        items.append({"id":tid,"answer":ans,"correct_answer":correct,"correct":ok,"score":got,"max_score":maxs,"descriptor":t.get("descriptor","")})
    result={"student_id":body.student_id,"name":body.name,"score":score,"total":total,"items":items,"time":datetime.now().isoformat()}
    lesson["results"].append(result)
    return result

# ---------------- QR attendance ----------------
import qrcode
QR_SESSIONS={}
class SessionStart(BaseModel):
    class_name:str="Сынып"; topic:str="Сабақ"; minutes:int=10
class CheckIn(BaseModel):
    token:str; student_id:str=""; name:str; class_name:str

@app.post("/api/session/start")
def session_start(body:SessionStart):
    token=secrets.token_hex(5).upper();now=datetime.now();minutes=max(1,min(body.minutes,120))
    QR_SESSIONS[token]={"class_name":body.class_name,"topic":body.topic,"active":True,"expires_at":now+timedelta(minutes=minutes),"attendance":{}}
    s=QR_SESSIONS[token];return {"token":token,"class_name":s["class_name"],"topic":s["topic"],"expires_at":s["expires_at"].isoformat()}
def qs(token):
    s=QR_SESSIONS.get(token)
    if not s:raise HTTPException(404,"QR-сабақ табылмады.")
    if datetime.now()>=s["expires_at"]:s["active"]=False
    return s
@app.get("/api/session/{token}")
def session_info(token:str):
    s=qs(token);return {"class_name":s["class_name"],"topic":s["topic"],"active":s["active"],"expires_at":s["expires_at"].isoformat()}
@app.get("/api/session/{token}/qr.png")
def session_qr(token:str,base:str):
    qs(token);img=qrcode.make(base.rstrip("/")+"/?session="+token);buf=BytesIO();img.save(buf,format="PNG");return Response(buf.getvalue(),media_type="image/png")
@app.post("/api/session/stop/{token}")
def session_stop(token:str):
    s=qs(token);s["active"]=False;return {"ok":True}
@app.post("/api/checkin")
def checkin(body:CheckIn):
    s=qs(body.token)
    if not s["active"]:raise HTTPException(410,"QR аяқталған.")
    key=(body.student_id.strip() or (body.name+"|"+body.class_name)).lower()
    if key not in s["attendance"]:s["attendance"][key]={"student_id":body.student_id.strip().upper(),"name":body.name.strip(),"class_name":body.class_name.strip(),"time":datetime.now().strftime("%H:%M:%S")}
    return {"ok":True,"time":s["attendance"][key]["time"]}
@app.get("/api/session/{token}/attendance")
def session_attendance(token:str):
    s=qs(token);return {"class_name":s["class_name"],"topic":s["topic"],"active":s["active"],"attendance":list(s["attendance"].values())}

@app.get("/")
def home():return FileResponse(BASE/"index.html")
app.mount("/static",StaticFiles(directory=BASE),name="static")
