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

# ---------------- Selectable AI provider: Gemini / OpenAI ----------------
import base64
import asyncio

def _json_from_text(text: str):
    text=(text or "").strip()
    if text.startswith("```"):
        text=re.sub(r"^```(?:json)?\s*","",text)
        text=re.sub(r"\s*```$","",text)
    return json.loads(text)

async def gemini_text_json(prompt: str):
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: raise HTTPException(400,"GEMINI_API_KEY Render Environment ішінде орнатылмаған.")
    import httpx
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload={"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"responseMimeType":"application/json"}}
    async with httpx.AsyncClient(timeout=90) as client:
        r=await client.post(url,json=payload)
        if r.status_code>=400: raise HTTPException(r.status_code,f"Gemini: {r.text[:500]}")
        text=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return _json_from_text(text)

async def openai_text_json(prompt: str):
    key=os.getenv("OPENAI_API_KEY","").strip()
    if not key: raise HTTPException(400,"OPENAI_API_KEY Render Environment ішінде орнатылмаған.")
    import httpx
    model=os.getenv("OPENAI_MODEL","gpt-5.6")
    payload={"model":model,"input":prompt}
    async with httpx.AsyncClient(timeout=120) as client:
        r=await client.post("https://api.openai.com/v1/responses",headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json=payload)
        if r.status_code>=400: raise HTTPException(r.status_code,f"OpenAI: {r.text[:500]}")
        data=r.json()
        chunks=[]
        for item in data.get("output",[]):
            if item.get("type")=="message":
                for c in item.get("content",[]):
                    if c.get("type")=="output_text": chunks.append(c.get("text",""))
        return _json_from_text("\n".join(chunks))

async def _retry_ai(provider: str, prompt: str, attempts: int=3):
    last=None
    for n in range(attempts):
        try:
            return await (openai_text_json(prompt) if provider=="openai" else gemini_text_json(prompt))
        except HTTPException as e:
            last=e
            if e.status_code not in (429,500,502,503,504): raise
            if n<attempts-1: await asyncio.sleep(2**n)
    raise last or HTTPException(503,"AI уақытша қолжетімсіз.")

async def ai_json(provider: str, prompt: str):
    requested=(provider or "gemini").lower()
    order=[requested]
    if requested=="gemini" and os.getenv("OPENAI_API_KEY","").strip(): order.append("openai")
    if requested=="openai" and os.getenv("GEMINI_API_KEY","").strip(): order.append("gemini")
    for p in order:
        try:
            return {"provider_used":p,"fallback":p!=requested,"data":await _retry_ai(p,prompt)}
        except HTTPException:
            pass
    raise HTTPException(503,"AI қызметі уақытша бос емес. Жүйе автоматты түрде 3 рет қайталап көрді. Біраздан соң «AI талдауын қайталау» батырмасын басыңыз.")

@app.get("/api/ai/status")
def ai_status():
    return {
        "gemini": bool(os.getenv("GEMINI_API_KEY","").strip()),
        "openai": bool(os.getenv("OPENAI_API_KEY","").strip()),
        "gemini_model": os.getenv("GEMINI_MODEL","gemini-2.5-flash"),
        "openai_model": os.getenv("OPENAI_MODEL","gpt-5.6")
    }


def _pick(d, *keys, default=""):
    if not isinstance(d, dict): return default
    for k in keys:
        v=d.get(k)
        if v not in (None,"",[],{}): return v
    return default

def normalize_task(t, i):
    if not isinstance(t, dict):
        t={"question":str(t)}
    options=_pick(t,"options","choices","variants",default=[])
    if not isinstance(options,list): options=[]
    return {
        "id": _pick(t,"id","number","task_number",default=i),
        "title": str(_pick(t,"title","name",default=f"{i}-тапсырма")),
        "question": str(_pick(t,"question","task","text","prompt",default="")),
        "answer": str(_pick(t,"answer","correct_answer","correctAnswer",default="")),
        "correct_answer": str(_pick(t,"correct_answer","correctAnswer","answer",default="")),
        "descriptor": str(_pick(t,"descriptor","descriptors","criterion","criteria",default="")),
        "type": str(_pick(t,"type","format",default="short")),
        "options": options,
        "max_score": _pick(t,"max_score","maxScore","score",default=1),
        "work_mode": str(_pick(t,"work_mode","workMode","mode",default="individual")),
        "group_name": str(_pick(t,"group_name","groupName","group",default="")),
        "external_url": str(_pick(t,"external_url","externalUrl","url","link",default=""))
    }

def normalize_lesson(raw):
    # Accept wrappers commonly returned by models.
    if isinstance(raw, list):
        raw={"tasks":raw}
    if not isinstance(raw, dict):
        raw={}
    for wrapper in ("data","lesson","result","analysis"):
        if isinstance(raw.get(wrapper),dict):
            merged=dict(raw)
            inner=merged.pop(wrapper)
            raw={**merged,**inner}
            break
    tasks=_pick(raw,"tasks","assignments","exercises","questions",default=[])
    if isinstance(tasks,dict): tasks=list(tasks.values())
    if not isinstance(tasks,list): tasks=[]
    return {
        "subject": str(_pick(raw,"subject","subject_name"," пән","пән",default="")),
        "class_name": str(_pick(raw,"class_name","class","grade","сынып",default="")),
        "topic": str(_pick(raw,"topic","lesson_topic","title","тақырып",default="")),
        "learning_objective": str(_pick(raw,"learning_objective","learningObjective","learning_objectives","оқу мақсаты",default="")),
        "lesson_goal": str(_pick(raw,"lesson_goal","lessonGoal","lesson_objective","сабақ мақсаты",default="")),
        "tasks": [normalize_task(t,i+1) for i,t in enumerate(tasks)]
    }

def normalize_generated(raw):
    if isinstance(raw,list): raw={"tasks":raw}
    if not isinstance(raw,dict): raw={}
    for wrapper in ("data","result","lesson"):
        if isinstance(raw.get(wrapper),dict):
            raw={**raw,**raw[wrapper]}
            break
    tasks=_pick(raw,"tasks","assignments","exercises","questions",default=[])
    if isinstance(tasks,dict): tasks=list(tasks.values())
    if not isinstance(tasks,list): tasks=[]
    return {"tasks":[normalize_task(t,i+1) for i,t in enumerate(tasks)]}

class KmjAnalyze(BaseModel):
    text: str
    hint: str = ""
    provider: str = "gemini"

@app.post("/api/kmj/analyze")
async def kmj_analyze(body: KmjAnalyze):
    prompt=f"""Сен Қазақстан мұғаліміне арналған AI көмекшісің. ҚМЖ мәтінін мұқият талда.
Тек жарамды JSON қайтар: subject, class_name, topic, learning_objective, lesson_goal,
tasks массиві: id, title, question, answer, descriptor, type.
ҚМЖ-дағы нақты тапсырмаларды жоғалтпа. Жоқ жауапты бос қалдыр. Ойдан оқу мақсатын қоспа.
Мұғалім ескертпесі: {body.hint}
ҚМЖ:
{body.text[:50000]}"""
    try:
        ai=await ai_json(body.provider,prompt)
        raw=ai["data"]
        data=normalize_lesson(raw)
        if not any([data["subject"],data["class_name"],data["topic"],data["learning_objective"],data["tasks"]]):
            raise HTTPException(422,"AI жауап берді, бірақ ҚМЖ құрылымы анықталмады. ҚМЖ мәтіні/файлын тексеріңіз.")
        return {"ok":True,"mode":ai["provider_used"],"provider":ai["provider_used"],"fallback":ai["fallback"],"data":data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500,f"AI талдау қатесі: {e}")

class TaskGenerate(BaseModel):
    lesson: dict
    format: str
    instruction: str = ""
    provider: str = "gemini"

@app.post("/api/tasks/generate")
async def task_generate(body: TaskGenerate):
    prompt=f"""Сен мұғалім берген ҚМЖ негізінде интерактивті HTML тапсырма құрылымын дайындайсың.
Формат: {body.format}
Мұғалімнің міндетті ұсынысы: {body.instruction}
Тек жарамды JSON қайтар: {{"tasks":[...]}}.
Әр тапсырма: id,type,question,options(array),correct_answer,descriptor,max_score.
type тек test, match, fill, truefalse, short.
Оқу мақсатын сақта. ҚМЖ-да жоқ мазмұнды орынсыз қоспа.
ҚМЖ дерегі:
{json.dumps(body.lesson,ensure_ascii=False)[:40000]}"""
    try:
        ai=await ai_json(body.provider,prompt)
        raw=ai["data"]
        data=normalize_generated(raw)
        if not data["tasks"]:
            raise HTTPException(422,"AI тапсырма құрылымын қайтармады. ҚМЖ-дан тапсырма табылғанын және нұсқауды тексеріңіз.")
        return {"ok":True,"mode":ai["provider_used"],"provider":ai["provider_used"],"fallback":ai["fallback"],"data":data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500,f"AI тапсырма жасау қатесі: {e}")

class StudentAnalyze(BaseModel):
    provider: str = "gemini"
    lesson: dict
    student: dict

@app.post("/api/student/analyze")
async def student_analyze(body: StudentAnalyze):
    prompt=f"""Сен мұғалімге арналған көмекші AI-сің. Оқушыға баға қойма.
Оқу мақсатына жетуін дәлелдермен талда және мұғалімге қысқа педагогикалық қорытынды бер.
Камерадағы қозғалыс көрсеткішін зейін, эмоция немесе оқу жетістігі деп түсіндірме.
Тек JSON қайтар: summary, objective_status, strengths(array), needs_support(array), evidence(array), next_step.
Сабақ: {json.dumps(body.lesson,ensure_ascii=False)}
Оқушы дерегі: {json.dumps(body.student,ensure_ascii=False)}
"""
    ai=await ai_json(body.provider,prompt)
    return {"mode":ai["provider_used"],"fallback":ai["fallback"],"data":ai["data"]}

@app.post("/api/audio/transcribe")
async def audio_transcribe(file: UploadFile=File(...), provider: str="openai"):
    data=await file.read()
    provider=(provider or "openai").lower()
    if provider=="openai":
        key=os.getenv("OPENAI_API_KEY","").strip()
        if not key: raise HTTPException(400,"OPENAI_API_KEY орнатылмаған.")
        import httpx
        model=os.getenv("OPENAI_TRANSCRIBE_MODEL","gpt-4o-mini-transcribe")
        files={"file":(file.filename or "answer.webm",data,file.content_type or "audio/webm")}
        form={"model":model,"language":"kk"}
        async with httpx.AsyncClient(timeout=120) as client:
            r=await client.post("https://api.openai.com/v1/audio/transcriptions",headers={"Authorization":f"Bearer {key}"},data=form,files=files)
            if r.status_code>=400: raise HTTPException(r.status_code,f"OpenAI transcription: {r.text[:500]}")
            return {"provider":"openai","text":r.json().get("text","")}
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: raise HTTPException(400,"GEMINI_API_KEY орнатылмаған.")
    import httpx
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    mime=file.content_type or "audio/webm"
    payload={"contents":[{"parts":[
        {"text":"Осы аудиодағы қазақша сөйлеуді дәл мәтінге түсір. Тек транскрипт мәтінін қайтар."},
        {"inline_data":{"mime_type":mime,"data":base64.b64encode(data).decode("ascii")}}
    ]}]}
    async with httpx.AsyncClient(timeout=120) as client:
        r=await client.post(url,json=payload)
        if r.status_code>=400: raise HTTPException(r.status_code,f"Gemini audio: {r.text[:500]}")
        text=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return {"provider":"gemini","text":text.strip()}


URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')

class ResourceExtract(BaseModel):
    text: str

@app.post("/api/resources/extract")
def extract_resources(body: ResourceExtract):
    urls=[]
    for u in URL_RE.findall(body.text or ""):
        u=u.rstrip(".,;:")
        if u not in urls: urls.append(u)
    return {"resources":[{"url":u,"approved":False} for u in urls[:30]]}

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
