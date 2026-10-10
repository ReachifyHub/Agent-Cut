"""Hosted version. Deploy: modal deploy modal_app.py  (or push to GitHub, see README).

The ONLY secret needed is a Modal Secret named "agentcut" containing:  APP_PASSWORD
The AI settings (base URL, API key, model) are typed into the web page and kept in your browser.
"""
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path

import modal

APP_DIR = Path(__file__).parent
app = modal.App("agentcut")
vol = modal.Volume.from_name("agentcut-data", create_if_missing=True)
jobs = modal.Dict.from_name("agentcut-jobs", create_if_missing=True)
secret = modal.Secret.from_name("agentcut")

SITE = "/usr/local/lib/python3.11/site-packages/nvidia"
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-dejavu-core", "fonts-liberation", "fontconfig")
    .pip_install("faster-whisper", "nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*", "requests",
                 "fastapi[standard]", "python-multipart")
    .env({
        "AGENTCUT_WORKSPACE": "/data/workspace",
        "WHISPER_CACHE": "/data/models",
        "WHISPER_MODEL": "medium",
        "LD_LIBRARY_PATH": f"{SITE}/cublas/lib:{SITE}/cudnn/lib",
    })
    .add_local_dir(APP_DIR / "core", "/app/core")
    .add_local_dir(APP_DIR / "sfx", "/app/sfx")
    .add_local_dir(APP_DIR / "web", "/app/web")
    .add_local_file(APP_DIR / "AGENTS.md", "/app/AGENTS.md")
)

SAFE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")


def _project(name):
    import sys
    sys.path.insert(0, "/app")
    from core.tools import Project
    if not SAFE.match(name or ""):
        raise ValueError("bad project id")
    return Project(Path("/data/workspace") / name)


@app.function(image=image, volumes={"/data": vol}, secrets=[secret], cpu=4, memory=8192, timeout=1800)
def ingest(project_id: str):
    """Unpack the uploaded zip and build the manifest in the background."""
    key = "ingest:" + project_id
    try:
        vol.reload()
        p = _project(project_id)
        z = p.root / "upload.zip"
        p.init(z, keep_old=True)
        z.unlink(missing_ok=True)
        vol.commit()
        jobs[key] = {"status": "ready", "summary": p.summary()}
    except Exception as e:
        jobs[key] = {"status": "error", "error": f"could not read the zip: {e}"}


@app.function(image=image, volumes={"/data": vol}, gpu="T4", cpu=4, memory=16384, timeout=1800)
def transcribe_gpu(project_id: str):
    """Whisper on a T4. Only this step uses the GPU, so you only pay GPU time while transcribing."""
    vol.reload()
    p = _project(project_id)
    p.transcribe()
    vol.commit()


@app.function(image=image, volumes={"/data": vol}, cpu=8, memory=16384, timeout=3600)
def run_job(job_id: str, project_id: str, message: str, agent: dict):
    import sys
    sys.path.insert(0, "/app")
    from core.pipeline import run_turn

    vol.reload()
    p = _project(project_id)
    history = json.loads(p.chat_f.read_text()) if p.chat_f.exists() else []
    state = {"events": [], "progress": None, "done": False}

    def put():
        jobs[job_id] = state

    def emit(e):
        if e["k"] == "pct":
            state["progress"] = e
        else:
            state["progress"] = None
            state["events"].append(e)
        if e["k"] == "done":
            state["done"] = True
        put()

    def transcriber(_project_obj):
        transcribe_gpu.remote(project_id)
        vol.reload()

    try:
        history = run_turn(p, message, agent, history, emit, transcriber=transcriber, checkpoint=vol.commit)
        p.chat_f.write_text(json.dumps(history))
    except Exception as ex:
        emit({"k": "error", "text": f"{type(ex).__name__}: {ex}"})
    emit({"k": "done"})
    vol.commit()


@app.function(image=image, volumes={"/data": vol}, secrets=[secret], timeout=3600, max_containers=1)
@modal.concurrent(max_inputs=30)
@modal.asgi_app()
def web():
    from fastapi import FastAPI, Header, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse
    from pydantic import BaseModel

    api = FastAPI()
    api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def auth(key):
        want = os.environ.get("APP_PASSWORD") or ""
        if not want or not hmac.compare_digest(str(key or ""), want):
            raise HTTPException(401, "wrong password")

    def refresh():
        try:
            vol.reload()
        except Exception:
            pass

    @api.get("/", response_class=HTMLResponse)
    def index():
        return Path("/app/web/index.html").read_text()

    # ---- chunked upload (keeps every request short)
    @api.post("/api/upload/start")
    def up_start(x_key: str = Header(None)):
        auth(x_key)
        pid = "p" + uuid.uuid4().hex[:10]
        _project(pid).root.mkdir(parents=True, exist_ok=True)
        jobs["ingest:" + pid] = {"status": "uploading"}
        return {"project": pid}

    @api.post("/api/upload/chunk")
    async def up_chunk(request: Request, project: str, offset: int, x_key: str = Header(None)):
        auth(x_key)
        root = _project(project).root
        if not root.exists():
            raise HTTPException(404, "unknown project")
        data = await request.body()
        z = root / "upload.zip"
        with open(z, "r+b" if z.exists() else "wb") as f:
            f.seek(offset)
            f.write(data)
        return {"ok": True}

    @api.post("/api/upload/finish")
    def up_finish(project: str, x_key: str = Header(None)):
        auth(x_key)
        if not (_project(project).root / "upload.zip").exists():
            raise HTTPException(400, "no upload received")
        vol.commit()
        jobs["ingest:" + project] = {"status": "processing", "since": time.time()}
        ingest.spawn(project)
        return {"ok": True}

    @api.get("/api/ingest/{project}")
    def ingest_status(project: str, x_key: str = Header(None)):
        auth(x_key)
        return jobs.get("ingest:" + project, {"status": "unknown"})

    # ---- state (so a reload restores everything)
    @api.get("/api/state/{project}")
    def state(project: str, x_key: str = Header(None)):
        auth(x_key)
        refresh()
        p = _project(project)
        st = jobs.get("ingest:" + project)
        if not p.manifest_f.exists():
            return {"known": False, "ingest": st}
        active = jobs.get("active:" + project)
        if active:
            j = jobs.get(active)
            if not j or j.get("done"):
                active = None
        return {"known": True, "summary": p.summary(),
                "chat": json.loads(p.chat_f.read_text()) if p.chat_f.exists() else [],
                "outputs": [n for n in ("final.mp4", "preview.mp4") if (p.output / n).exists()],
                "active_job": active}

    class Agent(BaseModel):
        base: str = ""
        key: str = ""
        model: str = ""

    class Chat(BaseModel):
        project: str
        message: str
        agent: Agent

    @api.post("/api/chat")
    def chat(body: Chat, x_key: str = Header(None)):
        auth(x_key)
        st = jobs.get("ingest:" + body.project)
        if not st or st.get("status") != "ready":
            raise HTTPException(409, "the zip is not unpacked yet")
        active = jobs.get("active:" + body.project)
        if active:
            j = jobs.get(active)
            if j and not j.get("done") and time.time() - j.get("t0", 0) < 7200:
                raise HTTPException(409, "a job is already running for this project")
        job_id = uuid.uuid4().hex[:12]
        jobs[job_id] = {"events": [], "progress": None, "done": False, "t0": time.time()}
        jobs["active:" + body.project] = job_id
        run_job.spawn(job_id, body.project, body.message, body.agent.model_dump())
        return {"job": job_id}

    @api.get("/api/jobs/{job_id}")
    def job(job_id: str, after: int = 0, x_key: str = Header(None)):
        auth(x_key)
        j = jobs.get(job_id)
        if j is None:
            raise HTTPException(404, "unknown job")
        return {"events": j["events"][after:], "next": len(j["events"]), "progress": j.get("progress"),
                "done": j["done"]}

    @api.post("/api/test")
    def test_ai(agent: Agent, x_key: str = Header(None)):
        """Tiny call (about 20 tokens) so you can check the AI settings before a real job."""
        auth(x_key)
        import sys
        sys.path.insert(0, "/app")
        from core import llm
        try:
            text, u = llm.chat(agent.model_dump(), [{"role": "user", "content": "Reply with the word OK"}],
                               max_tokens=8, retries=0)
            return {"ok": True, "reply": text.strip()[:60], "tokens": u["prompt"] + u["completion"]}
        except llm.LLMError as e:
            return {"ok": False, "error": str(e)}

    @api.get("/files/{project}/{name}")
    def files(project: str, name: str, key: str = ""):
        auth(key)
        if name not in ("final.mp4", "preview.mp4"):
            raise HTTPException(404)
        refresh()
        path = _project(project).output / name
        if not path.exists():
            raise HTTPException(404, "not rendered yet")
        return FileResponse(path, media_type="video/mp4", filename=f"{project}-{name}")

    return api
