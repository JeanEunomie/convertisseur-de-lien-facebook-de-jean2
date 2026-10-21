from flask import Flask, request, jsonify, send_from_directory
import requests
import re
import html
import sqlite3
import time
from urllib.parse import urlparse, urljoin, parse_qs, urlencode

app = Flask(__name__)

DB_FILE = "facebook_cache.db"
CACHE_DAYS = 30


# ---------------------------------------------------------
# BASE SQLITE
# ---------------------------------------------------------

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                original_url TEXT PRIMARY KEY,
                final_url TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0
            )
        """)


def get_cache(url):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            row = conn.execute("""
                SELECT final_url, created_at
                FROM cache
                WHERE original_url = ?
            """, (url,)).fetchone()

            if not row:
                return None

            final_url, created_at = row

            # Cache valable 30 jours
            if time.time() - created_at > CACHE_DAYS * 86400:
                conn.execute(
                    "DELETE FROM cache WHERE original_url = ?",
                    (url,)
                )
                return None

            conn.execute("""
                UPDATE cache
                SET last_used_at = ?, hits = hits + 1
                WHERE original_url = ?
            """, (int(time.time()), url))

            return final_url

    except sqlite3.Error:
        return None


def save_cache(original_url, final_url):
    try:
        now = int(time.time())

        with sqlite3.connect(DB_FILE) as conn:
            conn.execute("""
                INSERT INTO cache
                    (original_url, final_url, created_at, last_used_at, hits)
                VALUES (?, ?, ?, ?, 0)

                ON CONFLICT(original_url) DO UPDATE SET
                    final_url = excluded.final_url,
                    created_at = excluded.created_at,
                    last_used_at = excluded.last_used_at
            """, (
                original_url,
                final_url,
                now,
                now
            ))

    except sqlite3.Error:
        pass


# ---------------------------------------------------------
# FACEBOOK
# ---------------------------------------------------------

def facebook_url(url):
    try:
        p = urlparse(url)
        h = (p.hostname or "").lower()

        return (
            p.scheme in ("http", "https")
            and (
                h == "facebook.com"
                or h.endswith(".facebook.com")
                or h == "fb.com"
                or h.endswith(".fb.com")
            )
        )

    except Exception:
        return False


def is_login_url(url):
    try:
        return urlparse(url).path.lower().startswith("/login")
    except Exception:
        return False


def clean_facebook_url(url):
    try:
        p = urlparse(url)
        qs = parse_qs(p.query)

        path = p.path.rstrip("/") or "/"

        if path.lower() == "/profile.php" and qs.get("id"):
            fb_id = qs["id"][0]

            if fb_id.isdigit():
                return (
                    "https://www.facebook.com/profile.php?"
                    + urlencode({"id": fb_id})
                )

        keep = {}

        for key in ("id", "story_fbid", "fbid", "v"):
            if key in qs and qs[key]:
                keep[key] = qs[key][0]

        base = "https://www.facebook.com" + path

        return base + (
            "?" + urlencode(keep)
            if keep else ""
        )

    except Exception:
        return url


def try_people_numeric(session, url):
    """
    Cette requête supplémentaire n'est effectuée
    QUE si Facebook nous donne une URL /people/.
    """

    try:
        if not urlparse(url).path.lower().startswith("/people/"):
            return None

        r = session.get(
            url,
            timeout=8,
            allow_redirects=True
        )

        if is_login_url(r.url):
            return None

        fp = urlparse(r.url)
        fq = parse_qs(fp.query)

        if (
            fp.path.rstrip("/").lower() == "/profile.php"
            and fq.get("id")
            and fq["id"][0].isdigit()
        ):
            return fq["id"][0]

        text = html.unescape(r.text)

        patterns = (
            r'"userID"\s*:\s*"(\d{8,25})"',
            r'"user_id"\s*:\s*"(\d{8,25})"',
            r'"profile_id"\s*:\s*"(\d{8,25})"',
            r'profile\.php\?id=(\d{8,25})'
        )

        for pattern in patterns:
            m = re.search(pattern, text, re.I)

            if m:
                return m.group(1)

    except requests.RequestException:
        pass

    return None


# ---------------------------------------------------------
# ROUTES
# ---------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(".", "index.html")


@app.post("/api/resolve")
def resolve():

    data = request.get_json(silent=True) or {}
    original_url = data.get("url", "").strip()

    if not facebook_url(original_url):
        return jsonify(
            error="Colle un lien Facebook valide."
        ), 400

    # ---------------------------------------------
    # 1. CACHE AVANT FACEBOOK
    # ---------------------------------------------

    cached = get_cache(original_url)

    if cached:
        return jsonify(
            final_url=cached,
            cached=True,
            blocked=False
        )

    # ---------------------------------------------
    # 2. FACEBOOK UNIQUEMENT SI ABSENT DU CACHE
    # ---------------------------------------------

    session = requests.Session()

    session.headers.update({
        "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36",

        "Accept":
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8",

        "Accept-Language":
            "fr-FR,fr;q=0.9,en;q=0.8"
    })

    try:

        current = original_url

        # Maximum 5 redirections.
        # Dès qu'on a terminé, on arrête.
        for _ in range(5):

            r = session.get(
                current,
                allow_redirects=False,
                timeout=8
            )

            if r.is_redirect or r.is_permanent_redirect:

                location = r.headers.get("Location", "")
                nxt = urljoin(current, location)

                if not facebook_url(nxt):
                    return jsonify(
                        error="La redirection quitte Facebook."
                    ), 400

                # Facebook demande une connexion :
                # on s'arrête immédiatement.
                if is_login_url(nxt):
                    return jsonify(
                        error="Facebook bloque temporairement la conversion automatique.",
                        blocked=True,
                        original_url=original_url
                    ), 502

                current = nxt

                # Petite pause entre deux redirections.
                time.sleep(0.25)

            else:
                break

        cleaned = clean_facebook_url(current)

        # Requête supplémentaire uniquement pour /people/
        if urlparse(cleaned).path.lower().startswith("/people/"):

            fb_id = try_people_numeric(session, cleaned)

            if fb_id:
                cleaned = (
                    "https://www.facebook.com/profile.php?id="
                    + fb_id
                )

        # ---------------------------------------------
        # 3. ENREGISTREMENT
        # ---------------------------------------------

        if (
            facebook_url(cleaned)
            and not is_login_url(cleaned)
            and cleaned != original_url
        ):
            save_cache(original_url, cleaned)

        return jsonify(
            final_url=cleaned,
            cached=False,
            blocked=False
        )

    except requests.RequestException:

        return jsonify(
            error="Facebook n'a pas permis de résoudre ce lien.",
            blocked=True,
            original_url=original_url
        ), 502


@app.get("/api/cache-status")
def cache_status():

    try:
        with sqlite3.connect(DB_FILE) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM cache"
            ).fetchone()[0]

        return jsonify(
            cache="sqlite",
            entries=count,
            ttl_days=CACHE_DAYS
        )

    except sqlite3.Error:
        return jsonify(
            cache="sqlite",
            entries=0,
            error=True
        )


init_db()
