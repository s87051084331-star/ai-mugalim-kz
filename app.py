from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timedelta
from io import BytesIO
import io, os, csv, re, json, secrets, base64, sqlite3, hashlib, hmac

app = FastAPI(title="AI Мұғалім көмекшісі — ZEREK Education")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_hex(32)), same_site="lax", https_only=True)
BASE = Path(__file__).parent


# ---------------- Access control ----------------
DB_PATH=os.getenv("AUTH_DB_PATH", str(BASE/"users.db"))
def db():
    c=sqlite3.connect(DB_PATH);c.row_factory=sqlite3.Row;return c
def hash_password(password,salt=None):
    salt=salt or secrets.token_hex(16)
    dk=hashlib.pbkdf2_hmac("sha256",password.encode(),salt.encode(),200000)
    return salt+"$"+dk.hex()
def verify_password(password,stored):
    try:
        salt,digest=stored.split("$",1)
        return hmac.compare_digest(hash_password(password,salt).split("$",1)[1],digest)
    except Exception:return False
def init_auth():
    c=db();c.execute("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,password_hash TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',role TEXT NOT NULL DEFAULT 'teacher',created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    ae=os.getenv("ADMIN_EMAIL","").strip().lower();ap=os.getenv("ADMIN_PASSWORD","").strip()
    if ae and ap:
        row=c.execute("SELECT id FROM users WHERE email=?",(ae,)).fetchone()
        if row:c.execute("UPDATE users SET role='admin',status='approved' WHERE email=?",(ae,))
        else:c.execute("INSERT INTO users(email,name,password_hash,status,role) VALUES(?,?,?,?,?)",(ae,"Administrator",hash_password(ap),"approved","admin"))
    c.commit();c.close()
init_auth()
class RegisterBody(BaseModel): email:str; name:str; password:str
class LoginBody(BaseModel): email:str; password:str
class ApprovalBody(BaseModel): user_id:int; approve:bool
@app.post("/api/auth/register")
def register(body:RegisterBody):
    email=body.email.strip().lower()
    if "@" not in email or len(body.password)<6 or not body.name.strip():raise HTTPException(400,"Email, аты-жөні және кемінде 6 таңбалы құпиясөз қажет.")
    c=db()
    try:c.execute("INSERT INTO users(email,name,password_hash,status,role) VALUES(?,?,?,?,?)",(email,body.name.strip(),hash_password(body.password),"pending","teacher"));c.commit()
    except sqlite3.IntegrityError:raise HTTPException(409,"Бұл email бұрын тіркелген.")
    finally:c.close()
    return {"ok":True,"status":"pending"}
@app.post("/api/auth/login")
def login(body:LoginBody,request:Request):
    c=db();u=c.execute("SELECT * FROM users WHERE email=?",(body.email.strip().lower(),)).fetchone();c.close()
    if not u or not verify_password(body.password,u["password_hash"]):raise HTTPException(401,"Email немесе құпиясөз қате.")
    if u["status"]!="approved":raise HTTPException(403,"Аккаунт әлі әкімші тарапынан мақұлданбаған.")
    request.session["uid"]=u["id"];return {"ok":True,"user":{"id":u["id"],"email":u["email"],"name":u["name"],"role":u["role"]}}
@app.post("/api/auth/logout")
def logout(request:Request):request.session.clear();return {"ok":True}
@app.get("/api/auth/me")
def me(request:Request):
    uid=request.session.get("uid")
    if not uid:return {"authenticated":False}
    c=db();u=c.execute("SELECT id,email,name,status,role FROM users WHERE id=?",(uid,)).fetchone();c.close()
    return {"authenticated":bool(u and u["status"]=="approved"),"user":dict(u) if u and u["status"]=="approved" else None}
def require_admin(request):
    uid=request.session.get("uid")
    if not uid:raise HTTPException(401,"Кіру қажет.")
    c=db();u=c.execute("SELECT role,status FROM users WHERE id=?",(uid,)).fetchone();c.close()
    if not u or u["role"]!="admin" or u["status"]!="approved":raise HTTPException(403,"Әкімші рұқсаты қажет.")
@app.get("/api/admin/users")
def admin_users(request:Request):
    require_admin(request);c=db();rows=c.execute("SELECT id,email,name,status,role,created_at FROM users ORDER BY id DESC").fetchall();c.close();return {"users":[dict(r) for r in rows]}
@app.post("/api/admin/approve")
def admin_approve(body:ApprovalBody,request:Request):
    require_admin(request);c=db();c.execute("UPDATE users SET status=? WHERE id=?",("approved" if body.approve else "rejected",body.user_id));c.commit();c.close();return {"ok":True}
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


# ---------------- KMJ image extraction ----------------
def _data_uri(blob: bytes, mime: str):
    return "data:"+mime+";base64,"+base64.b64encode(blob).decode("ascii")

def extract_images(name: str, data: bytes):
    ext=Path(name).suffix.lower()
    out=[]
    try:
        if ext==".docx":
            import zipfile as _zf
            with _zf.ZipFile(io.BytesIO(data)) as z:
                for n in z.namelist():
                    if n.startswith("word/media/"):
                        b=z.read(n); suf=Path(n).suffix.lower()
                        mime={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".gif":"image/gif",".webp":"image/webp"}.get(suf,"application/octet-stream")
                        out.append({"name":Path(n).name,"mime":mime,"data_url":_data_uri(b,mime),"source":"DOCX"})
        elif ext==".pptx":
            from pptx import Presentation
            prs=Presentation(io.BytesIO(data))
            seen=set()
            for si,slide in enumerate(prs.slides,1):
                for shape in slide.shapes:
                    if hasattr(shape,"image"):
                        b=shape.image.blob
                        key=hash(b)
                        if key in seen: continue
                        seen.add(key)
                        mime=shape.image.content_type or "image/png"
                        out.append({"name":f"slide-{si}-{shape.image.filename}","mime":mime,"data_url":_data_uri(b,mime),"source":f"PPTX {si}-слайд"})
        elif ext==".xlsx":
            from openpyxl import load_workbook
            wb=load_workbook(io.BytesIO(data))
            for ws in wb.worksheets:
                for i,img in enumerate(getattr(ws,"_images",[]),1):
                    try:
                        b=img._data()
                        fmt=(getattr(img,"format","png") or "png").lower()
                        mime="image/jpeg" if fmt in ("jpg","jpeg") else "image/png"
                        out.append({"name":f"{ws.title}-{i}.{fmt}","mime":mime,"data_url":_data_uri(b,mime),"source":f"XLSX {ws.title}"})
                    except Exception: pass
        elif ext==".pdf":
            # Extract embedded raster images where pypdf exposes them.
            from pypdf import PdfReader
            reader=PdfReader(io.BytesIO(data))
            for pi,page in enumerate(reader.pages,1):
                try:
                    for ii,img in enumerate(page.images,1):
                        b=img.data
                        nm=getattr(img,"name",f"page-{pi}-{ii}.png")
                        suf=Path(nm).suffix.lower()
                        mime={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".jp2":"image/jp2"}.get(suf,"image/png")
                        out.append({"name":nm,"mime":mime,"data_url":_data_uri(b,mime),"source":f"PDF {pi}-бет"})
                except Exception: pass
    except Exception:
        pass
    return out[:40]

@app.post("/api/kmj/images")
async def kmj_images(file: UploadFile=File(...)):
    data=await file.read()
    images=extract_images(file.filename,data)
    return {"count":len(images),"images":images}

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

def _ai_err(provider,e):
    code=e.status_code if isinstance(e,HTTPException) else 503
    msg=str(e.detail if isinstance(e,HTTPException) else e).replace("\n"," ")[:700]
    return {"provider":provider,"status":code,"message":msg}

async def _retry_ai(provider: str, prompt: str, attempts: int=3):
    errs=[]
    for n in range(attempts):
        try:
            data=await (openai_text_json(prompt) if provider=="openai" else gemini_text_json(prompt))
            return {"ok":True,"data":data,"attempts":n+1,"errors":errs}
        except Exception as e:
            x=_ai_err(provider,e);x["attempt"]=n+1;errs.append(x)
            if x["status"] in (400,401,403,404): break
            if n<attempts-1: await asyncio.sleep(1.5*(2**n))
    return {"ok":False,"errors":errs}

async def ai_json(provider: str, prompt: str):
    requested=(provider or "gemini").lower()
    if requested not in ("gemini","openai"): requested="gemini"
    configured={"gemini":bool(os.getenv("GEMINI_API_KEY","").strip()),"openai":bool(os.getenv("OPENAI_API_KEY","").strip())}
    other="openai" if requested=="gemini" else "gemini"
    order=[requested]+([other] if configured[other] else [])
    allerrs=[]
    for p in order:
        if not configured[p]:
            allerrs.append({"provider":p,"status":400,"message":"API key жоқ","attempt":0});continue
        r=await _retry_ai(p,prompt,3);allerrs+=r["errors"]
        if r["ok"]:
            return {"provider_used":p,"requested_provider":requested,"fallback":p!=requested,"attempts":r["attempts"],"data":r["data"]}
    latest={}
    for e in allerrs:latest[e["provider"]]=e
    summary=" | ".join(f'{p}: HTTP {latest[p]["status"]} — {latest[p]["message"]}' for p in order if p in latest)
    raise HTTPException(503,{"message":"AI провайдерлерінің екеуі де жауап бере алмады.","summary":summary,"errors":latest})


@app.get("/api/ai/diagnostics")
def ai_diagnostics():
    return {"gemini":{"configured":bool(os.getenv("GEMINI_API_KEY","").strip()),"model":os.getenv("GEMINI_MODEL","gemini-3.6-flash")},"openai":{"configured":bool(os.getenv("OPENAI_API_KEY","").strip()),"model":os.getenv("OPENAI_MODEL","gpt-5.6")},"fallback":"selected -> retry x3 -> other provider -> retry x3"}

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
        return {"ok":True,"mode":ai["provider_used"],"provider":ai["provider_used"],"fallback":ai["fallback"],"requested_provider":ai["requested_provider"],"attempts":ai["attempts"],"data":data}
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
        return {"ok":True,"mode":ai["provider_used"],"provider":ai["provider_used"],"fallback":ai["fallback"],"requested_provider":ai["requested_provider"],"attempts":ai["attempts"],"data":data}
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


class DesignAnalyze(BaseModel):
    provider: str = "gemini"
    image_data_url: str
    instruction: str = ""

@app.post("/api/design/analyze")
async def design_analyze(body: DesignAnalyze):
    import base64 as _b64, httpx
    if "," not in body.image_data_url:
        raise HTTPException(400,"Үлгі сурет оқылмады.")
    meta,b64=body.image_data_url.split(",",1)
    mime=(meta.split(";")[0].split(":")[-1] or "image/png")
    instruction=body.instruction or "Үлгінің жалпы орналасуын, түстерін және карточка құрылымын сипатта."
    prompt="""Бұл мұғалім жіберген жұмыс парағының дизайн үлгісі.
Суреттегі тапсырма мәтінін көшірме. Тек визуалдық құрылымды талда.
Тек JSON қайтар:
{"layout":"grid2x2|grid1x4|grid2x3|free","columns":2,"card_count":4,
"palette":["#hex"],"background":"#hex","title_style":"pill|plain|banner",
"border_radius":18,"border_width":3,"font_scale":1.0,
"image_position":"right|left|top|none","notes":"қысқа сипаттама"}.
Мұғалім нұсқауы: """+instruction
    provider=(body.provider or "gemini").lower()
    if provider=="openai":
        key=os.getenv("OPENAI_API_KEY","").strip()
        if not key: raise HTTPException(400,"OPENAI_API_KEY жоқ.")
        model=os.getenv("OPENAI_MODEL","gpt-5.6")
        payload={"model":model,"input":[{"role":"user","content":[
            {"type":"input_text","text":prompt},
            {"type":"input_image","image_url":body.image_data_url}
        ]}]}
        async with httpx.AsyncClient(timeout=120) as client:
            r=await client.post("https://api.openai.com/v1/responses",headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json=payload)
            if r.status_code>=400: raise HTTPException(r.status_code,f"OpenAI vision: {r.text[:500]}")
            d=r.json(); text=""
            for item in d.get("output",[]):
                if item.get("type")=="message":
                    for c in item.get("content",[]):
                        if c.get("type")=="output_text": text+=c.get("text","")
            return {"provider":"openai","design":_json_from_text(text)}
    key=os.getenv("GEMINI_API_KEY","").strip()
    if not key: raise HTTPException(400,"GEMINI_API_KEY жоқ.")
    model=os.getenv("GEMINI_MODEL","gemini-3.6-flash")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload={"contents":[{"parts":[{"text":prompt},{"inline_data":{"mime_type":mime,"data":b64}}]}],"generationConfig":{"responseMimeType":"application/json"}}
    async with httpx.AsyncClient(timeout=120) as client:
        r=await client.post(url,json=payload)
        if r.status_code>=400: raise HTTPException(r.status_code,f"Gemini vision: {r.text[:500]}")
        text=r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return {"provider":"gemini","design":_json_from_text(text)}

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

@app.get("/favicon.png", include_in_schema=False)
def favicon(): return FileResponse(BASE/"favicon.png")

@app.get("/zerek-logo.png", include_in_schema=False)
def zerek_logo(): return FileResponse(BASE/"zerek-logo.png")

@app.get("/manifest.webmanifest",include_in_schema=False)
def pwa_manifest():return FileResponse(BASE/"manifest.webmanifest",media_type="application/manifest+json")
@app.get("/sw.js",include_in_schema=False)
def sw():return FileResponse(BASE/"sw.js",media_type="application/javascript")
@app.get("/robots.txt",include_in_schema=False)
def robots():return FileResponse(BASE/"robots.txt",media_type="text/plain")
@app.get("/sitemap.xml",include_in_schema=False)
def sitemap():return FileResponse(BASE/"sitemap.xml",media_type="application/xml")
@app.get("/icon-180.png",include_in_schema=False)
def i180():return FileResponse(BASE/"icon-180.png")
@app.get("/icon-192.png",include_in_schema=False)
def i192():return FileResponse(BASE/"icon-192.png")
@app.get("/icon-512.png",include_in_schema=False)
def i512():return FileResponse(BASE/"icon-512.png")
@app.get("/")
def home():return FileResponse(BASE/"index.html")
app.mount("/static",StaticFiles(directory=BASE),name="static")
