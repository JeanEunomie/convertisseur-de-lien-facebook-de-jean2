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


# =========================================================
# CACHE SQLITE
# =========================================================

def init_db():
    try:
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
    except sqlite3.Error:
        pass


def is_share_url(url):
    try:
        return urlparse(url).path.lower().startswith("/share/")
    except Exception:
        return False


def is_login_url(url):
    try:
        return urlparse(url).path.lower().startswith("/login")
    except Exception:
        return False


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

            # Sécurité : suppression d'une éventuelle
            # ancienne mauvaise entrée.
            if is_share_url(final_url) or is_login_url(final_url):
                conn.execute(
                    "DELETE FROM cache WHERE original_url = ?",
                    (url,)
                )
                return None

            # Expiration après 30 jours
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
            """, (
                int(time.time()),
                url
            ))

            return final_url

    except sqlite3.Error:
        return None


def save_cache(original_url, final_url):
    # Ne jamais enregistrer /share/ ou /login/
    if is_share_url(final_url) or is_login_url(final_url):
        return

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


# =========================================================
# MOTEUR FACEBOOK V4
# =========================================================

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


def try_people_numeric(url):
    try:
        if not urlparse(url).path.lower().startswith("/people/"):
            return None

        r = requests.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0"
            },
            allow_redirects=True
        )

        fp = urlparse(r.url)
        fq = parse_qs(r.url)

        # Correction de parsing
        fq = parse_qs(urlparse(r.url).query)

        if (
            fp.path.rstrip("/").lower() == "/profile.php"
            and fq.get("id")
            and fq["id"][0].isdigit()
        ):
            return fq["id"][0]

        text = html.unescape(r.text)

        for pat in (
            r'"userID"\s*:\s*"(\d{8,25})"',
            r'"user_id"\s*:\s*"(\d{8,25})"',
            r'"profile_id"\s*:\s*"(\d{8,25})"',
            r'profile\.php\?id=(\d{8,25})'
        ):
            m = re.search(pat, text, re.I)

            if m:
                return m.group(1)

    except requests.RequestException:
        pass

    return None


# =========================================================
# PAGE PRINCIPALE
# =========================================================

@app.get("/")
def index():
    return send_from_directory(".", "index.html")


# =========================================================
# CONVERSION
# =========================================================

@app.post("/api/resolve")
def resolve():

    original_url = (
        request.get_json(silent=True) or {}
    ).get("url", "").strip()

    if not facebook_url(original_url):
        return jsonify(
            error="Colle un lien Facebook valide."
        ), 400


    # -----------------------------------------------------
    # 1. CACHE EN PREMIER
    # -----------------------------------------------------

    cached = get_cache(original_url)

    if cached:
        return jsonify(
            final_url=cached,
            cached=True
        )


    # -----------------------------------------------------
    # 2. PAS DANS LE CACHE :
    #    moteur V4 original
    # -----------------------------------------------------

    try:
        current = original_url

        for _ in range(10):

            r = requests.get(
                current,
                allow_redirects=False,
                timeout=12,
                headers={
                    "User-Agent": "Mozilla/5.0"
                }
            )

            if r.is_redirect or r.is_permanent_redirect:

                nxt = urljoin(
                    current,
                    r.headers.get("Location", "")
                )

                if not facebook_url(nxt):
                    return jsonify(
                        error="La redirection quitte Facebook."
                    ), 400

                # Si Facebook renvoie maintenant vers login,
                # inutile d'insister.
                if is_login_url(nxt):
                    return jsonify(
                        error=(
                            "Facebook demande une connexion "
                            "pour résoudre ce lien."
                        ),
                        blocked=True
                    ), 502

                current = nxt

            else:
                break


        # -------------------------------------------------
        # 3. NETTOYAGE
        # -------------------------------------------------

        cleaned = clean_facebook_url(current)


        # -------------------------------------------------
        # 4. CAS /people/
        # -------------------------------------------------

        if urlparse(cleaned).path.lower().startswith("/people/"):

            fb_id = try_people_numeric(cleaned)

            if fb_id:
                cleaned = (
                    "https://www.facebook.com/profile.php?id="
                    + fb_id
                )


        # -------------------------------------------------
        # 5. IMPORTANT :
        #    un /share/ restant n'est PAS un résultat.
        # -------------------------------------------------

        if is_share_url(cleaned):

            return jsonify(
                error=(
                    "Facebook n'a pas fourni le lien final "
                    "pour cette adresse."
                ),
                unresolved=True
            ), 422


        # -------------------------------------------------
        # 6. ENREGISTREMENT DANS LE CACHE
        # -------------------------------------------------

        if (
            facebook_url(cleaned)
            and not is_login_url(cleaned)
            and not is_share_url(cleaned)
        ):
            save_cache(
                original_url,
                cleaned
            )


        return jsonify(
            final_url=cleaned,
            cached=False
        )


    except requests.RequestException:

        return jsonify(
            error=(
                "Facebook n'a pas permis de résoudre "
                "ce lien sans connexion."
            )
        ), 502


# =========================================================
# INFORMATIONS SUR LE CACHE
# =========================================================

@app.get("/api/cache-status")
def cache_status():

    try:
        with sqlite3.connect(DB_FILE) as conn:

            count = conn.execute(
                "SELECT COUNT(*) FROM cache"
            ).fetchone()[0]

            hits = conn.execute(
                "SELECT COALESCE(SUM(hits), 0) FROM cache"
            ).fetchone()[0]

        return jsonify(
            cache="sqlite",
            entries=count,
            hits=hits,
            ttl_days=CACHE_DAYS
        )

    except sqlite3.Error:

        return jsonify(
            cache="sqlite",
            entries=0,
            hits=0,
            error=True
        )


# =========================================================
# INITIALISATION
# =========================================================

init_db()
