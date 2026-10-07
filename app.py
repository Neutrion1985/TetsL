"""TetsL — сайт для проходження тестів з реєстрацією користувачів."""
import glob
import hashlib
import os
import re
import secrets
import sqlite3
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["DATABASE"] = os.environ.get("TETSL_DB", os.path.join(BASE_DIR, "tetsl.db"))


def load_secret_key():
    """Ключ для підпису сесій: зі змінної оточення або з файлу (створюється один раз)."""
    if os.environ.get("TETSL_SECRET_KEY"):
        return os.environ["TETSL_SECRET_KEY"]
    path = os.path.join(BASE_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.config["SECRET_KEY"] = load_secret_key()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    is_correct INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,
    score INTEGER NOT NULL,
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    option_id INTEGER REFERENCES options(id) ON DELETE SET NULL
);
"""

# ---------- База даних ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # Міграції для баз, створених попередньою версією.
    test_cols = {r["name"] for r in db.execute("PRAGMA table_info(tests)")}
    if "source" not in test_cols:
        db.execute("ALTER TABLE tests ADD COLUMN source TEXT")
        db.execute("ALTER TABLE tests ADD COLUMN source_hash TEXT")
    if "ref" not in {r["name"] for r in db.execute("PRAGMA table_info(questions)")}:
        db.execute("ALTER TABLE questions ADD COLUMN ref TEXT NOT NULL DEFAULT ''")
    import_test_files(db)
    db.commit()


# ---------- Імпорт тестів з папки tests/ ----------

LETTERS = "АБВГДЕ"


def parse_test_file(content):
    """Розбирає markdown-файл тесту (формат див. tests/*.md)."""
    title_m = re.search(r"^#\s+(.+)$", content, re.M)
    if not title_m:
        raise ValueError("немає заголовка '# ...'")
    source_m = re.search(r"^Джерело:\s*(.+)$", content, re.M)
    body, _, answers_part = content.partition("\n---")
    answers = {int(n): (letter, ref.strip()) for n, letter, ref in
               re.findall(r"^\|\s*(\d+)\s*\|\s*([А-Е])\s*\|\s*(.*?)\s*\|\s*$", answers_part, re.M)}
    questions, current = [], None
    for line in body.splitlines():
        q = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        o = re.match(r"^\s*([А-Е])\)\s+(.+)$", line)
        if q:
            current = {"num": int(q.group(1)), "text": q.group(2).strip(), "options": []}
            questions.append(current)
        elif o and current is not None:
            current["options"].append((o.group(1), o.group(2).strip()))
    if not questions:
        raise ValueError("не знайдено жодного питання")
    result = []
    for q in questions:
        if q["num"] not in answers:
            raise ValueError(f"немає відповіді на питання {q['num']}")
        letter, ref = answers[q["num"]]
        letters = [l for l, _ in q["options"]]
        if len(letters) < 2 or letter not in letters:
            raise ValueError(f"питання {q['num']}: некоректні варіанти або відповідь")
        result.append((q["text"], [t for _, t in q["options"]], letters.index(letter), ref))
    return title_m.group(1).strip(), (source_m.group(1).strip() if source_m else ""), result


def import_test_files(db, folder=None):
    """Додає нові та оновлює змінені тести з tests/*.md. Результати проходжень зберігаються."""
    folder = folder or os.path.join(BASE_DIR, "tests")
    for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
        name = os.path.basename(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        digest = hashlib.sha256(content.encode()).hexdigest()
        existing = db.execute("SELECT id, source_hash FROM tests WHERE source = ?", (name,)).fetchone()
        if existing and existing["source_hash"] == digest:
            continue
        try:
            title, description, questions = parse_test_file(content)
        except ValueError as e:
            app.logger.error("Тест %s не імпортовано: %s", name, e)
            continue
        if existing:
            test_id = existing["id"]
            db.execute("UPDATE tests SET title = ?, description = ?, source_hash = ? WHERE id = ?",
                       (title, description, digest, test_id))
            db.execute("DELETE FROM questions WHERE test_id = ?", (test_id,))
        else:
            test_id = db.execute(
                "INSERT INTO tests (title, description, source, source_hash) VALUES (?, ?, ?, ?)",
                (title, description, name, digest)).lastrowid
        for text, opts, correct, ref in questions:
            add_question(db, test_id, text, opts, correct, ref)


def create_test(db, title, description, questions):
    test_id = db.execute("INSERT INTO tests (title, description) VALUES (?, ?)",
                         (title, description)).lastrowid
    for text, opts, correct in questions:
        add_question(db, test_id, text, opts, correct)
    return test_id


def add_question(db, test_id, text, opts, correct, ref=""):
    qid = db.execute("INSERT INTO questions (test_id, text, ref) VALUES (?, ?, ?)",
                     (test_id, text, ref)).lastrowid
    for i, opt in enumerate(opts):
        db.execute("INSERT INTO options (question_id, text, is_correct) VALUES (?, ?, ?)",
                   (qid, opt, int(i == correct)))


# ---------- Авторизація та захист ----------

@app.before_request
def load_user():
    uid = session.get("user_id")
    g.user = None
    if uid is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()
    if request.method == "POST":
        token = session.get("csrf_token")
        if not token or token != request.form.get("csrf_token"):
            abort(400, "Невірний CSRF-токен")


@app.context_processor
def inject_csrf():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return {"csrf_token": session["csrf_token"]}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Увійдіть, щоб продовжити.")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        error = None
        if not (3 <= len(username) <= 32):
            error = "Логін має містити від 3 до 32 символів."
        elif len(password) < 6:
            error = "Пароль має містити щонайменше 6 символів."
        elif password != password2:
            error = "Паролі не збігаються."
        if error is None:
            db = get_db()
            # Перший зареєстрований користувач стає адміністратором.
            is_admin = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            try:
                uid = db.execute(
                    "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), int(is_admin))).lastrowid
                db.commit()
            except sqlite3.IntegrityError:
                error = "Такий логін вже зайнятий."
            else:
                session.clear()
                session["user_id"] = uid
                flash("Реєстрація успішна!" + (" Ви — адміністратор." if is_admin else ""))
                return redirect(url_for("index"))
        flash(error)
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Невірний логін або пароль.")
        else:
            session.clear()
            session["user_id"] = user["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------- Тести ----------

@app.route("/")
def index():
    tests = get_db().execute("""
        SELECT t.*, COUNT(q.id) AS qcount FROM tests t
        LEFT JOIN questions q ON q.test_id = t.id
        GROUP BY t.id ORDER BY t.id""").fetchall()
    stats = {}
    if g.user:
        # Статистика спроб поточного користувача: кількість, найкращий і останній результат.
        for r in get_db().execute("""
            SELECT a.test_id, COUNT(*) AS cnt, MAX(a.score * 1.0 / a.total) AS best_ratio,
                   (SELECT score || '/' || total FROM attempts l
                    WHERE l.test_id = a.test_id AND l.user_id = a.user_id
                    ORDER BY l.id DESC LIMIT 1) AS last,
                   (SELECT id FROM attempts l
                    WHERE l.test_id = a.test_id AND l.user_id = a.user_id
                    ORDER BY l.id DESC LIMIT 1) AS last_id
            FROM attempts a WHERE a.user_id = ? GROUP BY a.test_id""", (g.user["id"],)):
            stats[r["test_id"]] = {"count": r["cnt"], "best": round(100 * r["best_ratio"]),
                                   "last": r["last"], "last_id": r["last_id"]}
    return render_template("index.html", tests=tests, stats=stats)


def load_test(test_id):
    db = get_db()
    test = db.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
    if test is None:
        abort(404)
    questions = []
    for q in db.execute("SELECT * FROM questions WHERE test_id = ? ORDER BY id", (test_id,)):
        opts = db.execute("SELECT * FROM options WHERE question_id = ? ORDER BY id", (q["id"],)).fetchall()
        questions.append({"q": q, "options": opts})
    return test, questions


@app.route("/tests/<int:test_id>", methods=["GET", "POST"])
@login_required
def take_test(test_id):
    test, questions = load_test(test_id)
    if not questions:
        flash("У цьому тесті ще немає питань.")
        return redirect(url_for("index"))
    if request.method == "POST":
        db = get_db()
        chosen = {}
        for item in questions:
            qid = item["q"]["id"]
            raw = request.form.get(f"q{qid}")
            valid_ids = {o["id"] for o in item["options"]}
            chosen[qid] = int(raw) if raw and raw.isdigit() and int(raw) in valid_ids else None
        score = sum(1 for item in questions
                    for o in item["options"] if o["is_correct"] and chosen[item["q"]["id"]] == o["id"])
        attempt_id = db.execute(
            "INSERT INTO attempts (user_id, test_id, score, total) VALUES (?, ?, ?, ?)",
            (g.user["id"], test_id, score, len(questions))).lastrowid
        for qid, oid in chosen.items():
            db.execute("INSERT INTO answers (attempt_id, question_id, option_id) VALUES (?, ?, ?)",
                       (attempt_id, qid, oid))
        db.commit()
        return redirect(url_for("result", attempt_id=attempt_id))
    return render_template("take_test.html", test=test, questions=questions)


@app.route("/results/<int:attempt_id>")
@login_required
def result(attempt_id):
    db = get_db()
    attempt = db.execute("""
        SELECT a.*, u.username FROM attempts a JOIN users u ON u.id = a.user_id
        WHERE a.id = ?""", (attempt_id,)).fetchone()
    if attempt is None:
        abort(404)
    if attempt["user_id"] != g.user["id"] and not g.user["is_admin"]:
        abort(403)
    test, questions = load_test(attempt["test_id"])
    chosen = {r["question_id"]: r["option_id"] for r in
              db.execute("SELECT * FROM answers WHERE attempt_id = ?", (attempt_id,))}
    return render_template("result.html", attempt=attempt, test=test,
                           questions=questions, chosen=chosen)


@app.route("/my-results")
@login_required
def my_results():
    attempts = get_db().execute("""
        SELECT a.*, t.title FROM attempts a JOIN tests t ON t.id = a.test_id
        WHERE a.user_id = ? ORDER BY a.id DESC""", (g.user["id"],)).fetchall()
    return render_template("my_results.html", attempts=attempts)


# ---------- Адміністрування ----------

@app.route("/admin/tests/new", methods=["GET", "POST"])
@admin_required
def new_test():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        if not title:
            flash("Вкажіть назву тесту.")
        else:
            db = get_db()
            test_id = create_test(db, title, description, [])
            db.commit()
            return redirect(url_for("edit_test", test_id=test_id))
    return render_template("new_test.html")


@app.route("/admin/tests/<int:test_id>", methods=["GET", "POST"])
@admin_required
def edit_test(test_id):
    test, questions = load_test(test_id)
    if request.method == "POST":
        text = request.form.get("text", "").strip()
        opts = [request.form.get(f"opt{i}", "").strip() for i in range(6)]
        correct_raw = request.form.get("correct", "")
        filled = [(i, o) for i, o in enumerate(opts) if o]
        correct_idx = int(correct_raw) if correct_raw.isdigit() else -1
        if not text:
            flash("Введіть текст питання.")
        elif len(filled) < 2:
            flash("Потрібно щонайменше 2 варіанти відповіді.")
        elif correct_idx not in [i for i, _ in filled]:
            flash("Позначте правильну відповідь серед заповнених варіантів.")
        else:
            db = get_db()
            new_correct = [i for i, _ in filled].index(correct_idx)
            add_question(db, test_id, text, [o for _, o in filled], new_correct)
            db.commit()
            flash("Питання додано.")
            return redirect(url_for("edit_test", test_id=test_id))
    attempts = get_db().execute("""
        SELECT a.*, u.username FROM attempts a JOIN users u ON u.id = a.user_id
        WHERE a.test_id = ? ORDER BY a.id DESC""", (test_id,)).fetchall()
    return render_template("edit_test.html", test=test, questions=questions, attempts=attempts)


@app.route("/admin/questions/<int:question_id>/delete", methods=["POST"])
@admin_required
def delete_question(question_id):
    db = get_db()
    q = db.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    if q is None:
        abort(404)
    db.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    db.commit()
    return redirect(url_for("edit_test", test_id=q["test_id"]))


@app.route("/admin/tests/<int:test_id>/delete", methods=["POST"])
@admin_required
def delete_test(test_id):
    db = get_db()
    db.execute("DELETE FROM tests WHERE id = ?", (test_id,))
    db.commit()
    flash("Тест видалено.")
    return redirect(url_for("index"))


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
