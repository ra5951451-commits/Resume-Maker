# app.py
import os
import pathlib
import json
import uuid
import datetime
import logging
from functools import wraps
from threading import Lock
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, request, render_template, session,
    redirect, url_for, flash, send_from_directory, abort
)

# ---------- CONFIG ----------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production")
BASE_DIR = pathlib.Path(__file__).parent.resolve()
UPLOAD_FOLDER = BASE_DIR / "static" / "uploads"
DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"
RESUMES_DIR = DATA_DIR / "resumes"

# ensure folders exist
for d in (UPLOAD_FOLDER, DATA_DIR, RESUMES_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Upload config
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024   # 2 MB
ALLOWED_EXT = {".png", ".jpg", ".jpeg"}
ALLOWED_MIMETYPES = {"image/png", "image/jpeg"}

# Simple file lock for JSON writes (works for single-process dev)
_json_lock = Lock()

# Logging
logging.basicConfig(level=logging.INFO)

# ---------- HELPERS ----------
def escape_html(text):
    """Small escaping helper (like Jinja2 |e for plain strings)."""
    if not text:
        return ""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))

def render_inline(text):   # keep new-lines as <br> for address/phone
    return escape_html(text).replace("\n", "<br>")

def render_html(text):     # accept user <br> but escape the rest
    if not text: return ""
    escaped = escape_html(text)
    return escaped.replace("&lt;br&gt;", "<br>").replace("\n", "<br>")

def initials(name):
    return "".join(w[0].upper() for w in (name or " ").split()[:2])

def allowed_file(filename, mimetype=None):
    ext = pathlib.Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        return False
    if mimetype and mimetype not in ALLOWED_MIMETYPES:
        return False
    return True

# JSON persistence helpers
def _ensure_users_file():
    if not USERS_FILE.exists():
        with USERS_FILE.open("w", encoding="utf-8") as f:
            json.dump([], f)

def load_users():
    _ensure_users_file()
    with USERS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_users(users):
    _ensure_users_file()
    with _json_lock:
        with USERS_FILE.open("w", encoding="utf-8") as f:
            json.dump(users, f, indent=2)

def find_user_by_email(email):
    for u in load_users():
        if u.get("email") == email:
            return u
    return None

def add_user(name, email, password_hash):
    users = load_users()
    user_id = uuid.uuid4().hex
    users.append({
        "id": user_id,
        "name": name,
        "email": email,
        "password": password_hash,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z"
    })
    save_users(users)
    return user_id

# Add custom nl2br filter (single definition)
@app.template_filter('nl2br')
def nl2br_filter(text):
    """Convert newlines to <br> tags"""
    if not text:
        return ""
    return escape_html(text).replace('\n', '<br>')

# login decorator
def login_required(f):
    @wraps(f)
    def decorated(*a, **k):
        if "user" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*a, **k)
    return decorated

# ---------- AUTH ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        pwd      = request.form.get("password")
        confirm  = request.form.get("confirm_password")
        if not all([name, email, pwd, confirm]):
            flash("All fields are required.", "error")
        elif pwd != confirm:
            flash("Passwords do not match.", "error")
        elif find_user_by_email(email):
            flash("E-mail already registered.", "error")
        else:
            pwd_hash = generate_password_hash(pwd)
            user_id = add_user(name, email, pwd_hash)
            # store only non-sensitive info in session
            session["user"] = {"id": user_id, "name": name, "email": email}
            flash("Account created and logged in.", "success")
            return redirect(url_for("dashboard"))
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        pwd   = request.form.get("password")
        user = find_user_by_email(email)
        if user and check_password_hash(user.get("password", ""), pwd):
            session["user"] = {"id": user["id"], "name": user["name"], "email": user["email"]}
            flash("Logged in successfully.", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Logged out.", "info")
    return redirect(url_for("home"))

# ---------- PUBLIC HOME ----------
@app.route("/")
def home():
    """Public landing page (no login required)."""
    return render_template("home.html")

# ---------- DASHBOARD / RESUME BUILDER (requires login) ----------
@app.route("/dashboard")
@login_required
def dashboard():
    """Logged-in user's main page."""
    return render_template("index.html", user=session["user"]["name"])

@app.route("/generate", methods=["POST"])
@login_required
def generate():
    try:
        # Basic fields
        data = {
            "name": request.form.get("name", "").strip(),
            "title": request.form.get("title", "").strip(),
            "email": request.form.get("email", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "address": request.form.get("address", "").strip(),
            "summary": request.form.get("summary", "").strip(),
            "skills": [s.strip() for s in request.form.get("skills", "").split(",") if s.strip()],
            "languages": [L.strip() for L in request.form.getlist("languages[]") if L.strip()],
            "experience": [],
            "education": [],
            "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        }

        # Experience blocks
        exp_titles = request.form.getlist("experience_title[]")
        exp_companies = request.form.getlist("experience_company[]")
        exp_durations = request.form.getlist("experience_duration[]")
        exp_descs = request.form.getlist("experience_description[]")
        for i in range(len(exp_titles)):
            data["experience"].append({
                "title": exp_titles[i],
                "company": exp_companies[i] if i < len(exp_companies) else "",
                "duration": exp_durations[i] if i < len(exp_durations) else "",
                "description": exp_descs[i] if i < len(exp_descs) else ""
            })

        # Education blocks
        edu_degrees = request.form.getlist("education_degree[]")
        edu_unis = request.form.getlist("education_university[]")
        edu_years = request.form.getlist("education_year[]")
        for i in range(len(edu_degrees)):
            data["education"].append({
                "degree": edu_degrees[i],
                "university": edu_unis[i] if i < len(edu_unis) else "",
                "year": edu_years[i] if i < len(edu_years) else ""
            })

        # Optional photo with validation
        photo_file = request.files.get("photo")
        if photo_file and photo_file.filename:
            filename_raw = secure_filename(photo_file.filename)
            mimetype = photo_file.mimetype
            if not allowed_file(filename_raw, mimetype):
                flash("Invalid file type. Only PNG and JPEG allowed.", "error")
                return redirect(url_for("dashboard"))

            # Get size by seeking the file stream (works for Werkzeug FileStorage)
            try:
                photo_file.stream.seek(0, os.SEEK_END)
                file_size = photo_file.stream.tell()
                photo_file.stream.seek(0)
            except Exception:
                # fallback: attempt to read bytes (not ideal for large files)
                photo_bytes = photo_file.read()
                file_size = len(photo_bytes)
                # reset file pointer (wrap bytes back)
                from io import BytesIO
                photo_file.stream = BytesIO(photo_bytes)

            if file_size > app.config["MAX_CONTENT_LENGTH"]:
                flash("File too large. Maximum allowed is 2MB.", "error")
                return redirect(url_for("dashboard"))

            ext = pathlib.Path(filename_raw).suffix.lower()
            filename = f"{uuid.uuid4().hex}{ext}"
            filepath = UPLOAD_FOLDER / filename
            try:
                photo_file.save(filepath)
                data["photo"] = f"/static/uploads/{filename}"
                data["photo_exists"] = True
            except Exception as e:
                logging.exception("Error saving uploaded photo")
                flash("Error saving photo. Please try again.", "error")
                data["photo_exists"] = False
        else:
            data["photo_exists"] = False

        # Store resume persistently per user (simple JSON file)
        user_id = session["user"]["id"]
        resume_id = uuid.uuid4().hex
        resume_path = RESUMES_DIR / f"{user_id}_{resume_id}.json"
        with resume_path.open("w", encoding="utf-8") as f:
            json.dump({"id": resume_id, "user_id": user_id, "data": data}, f, indent=2)

        # For template rendering, keep in session a pointer to resume file
        session["last_resume_file"] = str(resume_path)

        tmpl = request.form.get("template", "template1")
        if tmpl not in [f"template{i}" for i in range(1, 9)]:
            flash("Invalid template selected.", "error")
            return redirect(url_for("dashboard"))

        return redirect(url_for(tmpl))

    except Exception as e:
        logging.exception("Error generating resume")
        flash("Error generating resume. Please try again.", "error")
        return redirect(url_for("dashboard"))

# ---------- TEMPLATE ROUTES ----------
def template_route(template):
    @login_required
    def wrapper():
        resume_file = session.get("last_resume_file")
        if not resume_file:
            abort(400, description="No resume data found. Please build your resume first.")
        resume_path = pathlib.Path(resume_file)
        if not resume_path.exists():
            abort(400, description="Saved resume not found on server.")
        with resume_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        d = payload.get("data", {})

        normalized_data = {
            "name": d.get("name", ""),
            "title": d.get("title", ""),
            "email": d.get("email", ""),
            "phone": d.get("phone", ""),
            "address": d.get("address", ""),
            "summary": d.get("summary", ""),
            "skills": d.get("skills", []),
            "languages": d.get("languages", []),
            "experience": d.get("experience", []),
            "education": d.get("education", []),
            "photo": d.get("photo"),
            "photo_exists": d.get("photo_exists", False)
        }

        context = {
            "data": normalized_data,
            "photo": normalized_data.get("photo"),
            "photo_exists": normalized_data.get("photo_exists", False),
            "initials": initials(normalized_data.get("name")),
            "e": escape_html,
            "render_inline": render_inline,
            "render_html": render_html
        }

        # Some templates expect individual variables
        if template in ["template1", "template5", "template6", "template7", "template8"]:
            context.update({
                "name": normalized_data["name"],
                "title": normalized_data["title"],
                "email": normalized_data["email"],
                "phone": normalized_data["phone"],
                "address": normalized_data["address"],
                "summary": normalized_data["summary"],
                "skills": normalized_data["skills"],
                "languages": normalized_data["languages"],
                "experience": normalized_data["experience"],
                "education": normalized_data["education"]
            })

        return render_template(f"{template}.html", **context)

    wrapper.__name__ = template
    return wrapper

# register the eight endpoints
for t in ("template1","template2","template3","template4",
          "template5","template6","template7","template8"):
    app.add_url_rule(f"/{t}", t, template_route(t))

# ---------- STATIC FALLBACK ----------
# Keep an explicit static route if you want compatibility; Flask already serves /static/<path>.
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(BASE_DIR / "static"), filename)

# ---------- Download resume JSON (simple) ----------
@app.route("/resume/download")
@login_required
def download_resume_json():
    """Allow user to download the last created resume JSON for backup."""
    resume_file = session.get("last_resume_file")
    if not resume_file:
        flash("No resume available to download.", "warning")
        return redirect(url_for("dashboard"))
    resume_path = pathlib.Path(resume_file)
    if not resume_path.exists():
        flash("Saved resume not found.", "error")
        return redirect(url_for("dashboard"))
    return send_from_directory(directory=str(resume_path.parent),
                               path=resume_path.name,
                               as_attachment=True,
                               download_name=resume_path.name)

# ---------- ERROR HANDLERS ----------
@app.errorhandler(413)
def too_large(e):
    flash("Uploaded file is too large (max 2MB).", "error")
    return redirect(url_for("dashboard"))

@app.errorhandler(400)
def bad_request(e):
    # if AJAX or API you'd return JSON instead
    return render_template("400.html", message=getattr(e, "description", str(e))), 400

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

@app.errorhandler(500)
def server_error(e):
    logging.exception("Server error")
    return render_template("500.html"), 500

# ---------- RUN ----------
if __name__ == "__main__":
    # For development only: use debug=True locally
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
