import os
import xml.etree.ElementTree as ET
import re
import uuid
import shutil
import base64
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer
from dotenv import load_dotenv
from app.database import get_db, init_db

load_dotenv()

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "changeme")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key")
signer = URLSafeSerializer(SECRET_KEY)
COOKIE_NAME = "quotes_auth"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/uploads", StaticFiles(directory="/app/data/uploads"), name="uploads")

UPLOAD_DIR = "/app/data/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
templates = Jinja2Templates(directory="app/templates")
def bold_first_line(text):
    """Return text with first line wrapped in <strong> tags."""
    if not text:
        return text
    lines = text.split('\n', 1)
    first = f'<strong>{lines[0]}</strong>'
    if len(lines) > 1:
        return first + '\n' + lines[1]
    return first

templates.env.filters['bold_first_line'] = bold_first_line



def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    try:
        data = signer.loads(token)
        return data == "authenticated"
    except Exception:
        return False


def require_auth(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


# --- Auth ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if password == SITE_PASSWORD:
        response = RedirectResponse("/", status_code=303)
        token = signer.dumps("authenticated")
        response.set_cookie(COOKIE_NAME, token, max_age=86400 * 30, httponly=True)
        return response
    return templates.TemplateResponse("login.html", {"request": request, "error": "Wrong password"})


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# --- Home ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, sort: str = "alpha"):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()

    if sort == "alpha":
        order = "ORDER BY e.content COLLATE NOCASE ASC"
    else:
        order = "ORDER BY e.created_at DESC"

    rows = await db.execute_fetchall(f"""
        SELECT e.*, GROUP_CONCAT(t.name, ', ') as topics
        FROM entries e
        LEFT JOIN entry_topics et ON e.id = et.entry_id
        LEFT JOIN topics t ON et.topic_id = t.id
        GROUP BY e.id
        {order}
    """)
    topic_counts = await db.execute_fetchall("""
        SELECT t.id, t.name, COUNT(et.entry_id) as count
        FROM topics t
        LEFT JOIN entry_topics et ON t.id = et.topic_id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
    """)
    total = await db.execute_fetchall("SELECT COUNT(*) as c FROM entries")
    await db.close()
    return templates.TemplateResponse("home.html", {
        "request": request,
        "entries": rows,
        "topics": topic_counts,
        "total": total[0]["c"],
        "sort": sort,
    })


# --- Add ---

@app.get("/add", response_class=HTMLResponse)
async def add_form(request: Request, type: str = "quote"):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    topics = await db.execute_fetchall("SELECT * FROM topics ORDER BY name COLLATE NOCASE")
    await db.close()
    return templates.TemplateResponse("add.html", {
        "request": request,
        "entry_type": type,
        "topics": topics,
    })


@app.post("/add", response_class=HTMLResponse)
async def add_submit(
    request: Request,
    entry_type: str = Form(...),
    content: str = Form(...),
    source: str = Form(""),
    author: str = Form(""),
    existing_topics: list[str] = Form(default=[]),
    new_topics: str = Form(""),
    image: UploadFile = File(default=None),
):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)

    # Handle image upload
    image_filename = ""
    if image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext in ALLOWED_IMAGE_TYPES:
            image_filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(UPLOAD_DIR, image_filename)
            with open(filepath, "wb") as f:
                shutil.copyfileobj(image.file, f)

    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO entries (type, content, source, author, image) VALUES (?, ?, ?, ?, ?)",
        (entry_type, content.strip(), source.strip(), author.strip(), image_filename)
    )
    entry_id = cursor.lastrowid

    # Collect all topic IDs
    topic_ids = [int(tid) for tid in existing_topics if tid]

    # Create new topics
    if new_topics.strip():
        for name in new_topics.split(","):
            name = name.strip()
            if not name:
                continue
            existing = await db.execute_fetchall("SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (name,))
            if existing:
                topic_ids.append(existing[0]["id"])
            else:
                cur = await db.execute("INSERT INTO topics (name) VALUES (?)", (name,))
                topic_ids.append(cur.lastrowid)

    # Link topics
    for tid in set(topic_ids):
        await db.execute("INSERT OR IGNORE INTO entry_topics (entry_id, topic_id) VALUES (?, ?)", (entry_id, tid))

    await db.commit()
    await db.close()
    return RedirectResponse("/", status_code=303)


# --- Browse Topics ---

@app.get("/browse", response_class=HTMLResponse)
async def browse_topics(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    topics = await db.execute_fetchall("""
        SELECT t.id, t.name, COUNT(et.entry_id) as count
        FROM topics t
        LEFT JOIN entry_topics et ON t.id = et.topic_id
        GROUP BY t.id
        ORDER BY t.name COLLATE NOCASE
    """)
    await db.close()
    return templates.TemplateResponse("browse.html", {"request": request, "topics": topics})


@app.get("/topic/{topic_id}", response_class=HTMLResponse)
async def topic_view(request: Request, topic_id: int):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    topic = await db.execute_fetchall("SELECT * FROM topics WHERE id = ?", (topic_id,))
    if not topic:
        await db.close()
        raise HTTPException(status_code=404)
    entries = await db.execute_fetchall("""
        SELECT e.*, GROUP_CONCAT(t.name, ', ') as topics
        FROM entries e
        JOIN entry_topics et ON e.id = et.entry_id
        LEFT JOIN entry_topics et2 ON e.id = et2.entry_id
        LEFT JOIN topics t ON et2.topic_id = t.id
        WHERE et.topic_id = ?
        GROUP BY e.id
        ORDER BY e.created_at DESC
    """, (topic_id,))
    await db.close()
    return templates.TemplateResponse("topic.html", {
        "request": request,
        "topic": topic[0],
        "entries": entries,
    })


# --- Search ---

@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    entries = []
    if q.strip():
        db = await get_db()
        entries = await db.execute_fetchall("""
            SELECT e.*, GROUP_CONCAT(t.name, ', ') as topics
            FROM entries e
            LEFT JOIN entry_topics et ON e.id = et.entry_id
            LEFT JOIN topics t ON et.topic_id = t.id
            WHERE e.content LIKE ? OR e.source LIKE ? OR e.author LIKE ? OR t.name LIKE ?
            GROUP BY e.id
            ORDER BY e.created_at DESC
        """, (f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"))
        await db.close()
    return templates.TemplateResponse("search.html", {
        "request": request,
        "query": q,
        "entries": entries,
    })


# --- View/Edit/Delete ---

@app.get("/entry/{entry_id}", response_class=HTMLResponse)
async def view_entry(request: Request, entry_id: int):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    entry = await db.execute_fetchall("""
        SELECT e.*, GROUP_CONCAT(t.name, ', ') as topics
        FROM entries e
        LEFT JOIN entry_topics et ON e.id = et.entry_id
        LEFT JOIN topics t ON et.topic_id = t.id
        WHERE e.id = ?
        GROUP BY e.id
    """, (entry_id,))
    if not entry:
        await db.close()
        raise HTTPException(status_code=404)
    # Get topic IDs for this entry
    entry_topic_ids = await db.execute_fetchall(
        "SELECT topic_id FROM entry_topics WHERE entry_id = ?", (entry_id,)
    )
    all_topics = await db.execute_fetchall("SELECT * FROM topics ORDER BY name COLLATE NOCASE")
    await db.close()
    return templates.TemplateResponse("entry.html", {
        "request": request,
        "entry": entry[0],
        "entry_topic_ids": [r["topic_id"] for r in entry_topic_ids],
        "all_topics": all_topics,
    })


@app.post("/entry/{entry_id}/edit", response_class=HTMLResponse)
async def edit_entry(
    request: Request,
    entry_id: int,
    content: str = Form(...),
    source: str = Form(""),
    author: str = Form(""),
    existing_topics: list[str] = Form(default=[]),
    new_topics: str = Form(""),
    image: UploadFile = File(default=None),
    remove_image: str = Form(default=""),
):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()

    # Handle image
    image_filename = None
    if remove_image == "1":
        # Delete old image file
        old = await db.execute_fetchall("SELECT image FROM entries WHERE id=?", (entry_id,))
        if old and old[0]["image"]:
            old_path = os.path.join(UPLOAD_DIR, old[0]["image"])
            if os.path.exists(old_path):
                os.remove(old_path)
        image_filename = ""
    elif image and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext in ALLOWED_IMAGE_TYPES:
            # Delete old image
            old = await db.execute_fetchall("SELECT image FROM entries WHERE id=?", (entry_id,))
            if old and old[0]["image"]:
                old_path = os.path.join(UPLOAD_DIR, old[0]["image"])
                if os.path.exists(old_path):
                    os.remove(old_path)
            image_filename = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(UPLOAD_DIR, image_filename)
            with open(filepath, "wb") as f:
                shutil.copyfileobj(image.file, f)

    if image_filename is not None:
        await db.execute(
            "UPDATE entries SET content=?, source=?, author=?, image=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (content.strip(), source.strip(), author.strip(), image_filename, entry_id)
        )
    else:
        await db.execute(
            "UPDATE entries SET content=?, source=?, author=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (content.strip(), source.strip(), author.strip(), entry_id)
        )
    # Rebuild topic links
    await db.execute("DELETE FROM entry_topics WHERE entry_id=?", (entry_id,))
    topic_ids = [int(tid) for tid in existing_topics if tid]
    if new_topics.strip():
        for name in new_topics.split(","):
            name = name.strip()
            if not name:
                continue
            existing = await db.execute_fetchall("SELECT id FROM topics WHERE name = ? COLLATE NOCASE", (name,))
            if existing:
                topic_ids.append(existing[0]["id"])
            else:
                cur = await db.execute("INSERT INTO topics (name) VALUES (?)", (name,))
                topic_ids.append(cur.lastrowid)
    for tid in set(topic_ids):
        await db.execute("INSERT OR IGNORE INTO entry_topics (entry_id, topic_id) VALUES (?, ?)", (entry_id, tid))
    await db.commit()
    await db.close()
    return RedirectResponse(f"/entry/{entry_id}", status_code=303)


@app.post("/entry/{entry_id}/delete")
async def delete_entry(request: Request, entry_id: int):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    db = await get_db()
    # Delete image file
    old = await db.execute_fetchall("SELECT image FROM entries WHERE id=?", (entry_id,))
    if old and old[0]["image"]:
        old_path = os.path.join(UPLOAD_DIR, old[0]["image"])
        if os.path.exists(old_path):
            os.remove(old_path)
    await db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    await db.commit()
    await db.close()
    return RedirectResponse("/", status_code=303)
