from flask import Flask, request, jsonify, send_from_directory
import os
import requests
import re
import html
import psycopg
from urllib.parse import urlparse, urljoin, parse_qs, urlencode

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")


# =========================================================
# CACHE POSTGRESQL / SUPABASE
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL absente")

    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=8
    )


def get_cache(url):
    """
    Recherche le lien dans Supabase.
    Si trouvé :
    - renvoie final_url
    - incrémente hits
    - actualise last_used_at
    """

    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT final_url
                    FROM facebook_cache
                    WHERE original_url = %s
                    LIMIT 1
                """, (url,))

                row = cur.fetchone()

                if not row:
                    return None

                final_url = row[0]

                # Sécurité contre une mauvaise ancienne entrée
                if is_share_url(final_url) or is_login_url(final_url):

                    cur.execute("""
                        DELETE FROM facebook_cache
                        WHERE original_url = %s
                    """, (url,))

                    return None

                cur.execute("""
                    UPDATE facebook_cache
                    SET
                        hits = COALESCE(hits, 0) + 1,
                        last_used_at = NOW()
                    WHERE original_url = %s
                """, (url,))

                return final_url

    except Exception as e:
        print("Erreur lecture cache :", e)
        return None


def save_cache(original_url, final_url):
    """
    Enregistre une conversion réussie.

    original_url est UNIQUE dans Supabase :
    si elle existe déjà, on actualise simplement final_url.
    """

    if is_share_url(final_url) or is_login_url(final_url):
        return

    try:
        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    INSERT INTO facebook_cache
                        (
                            original_url,
                            final_url,
                            last_used_at,
                            hits
                        )
                    VALUES
                        (%s, %s, NOW(), 0)

                    ON CONFLICT (original_url)
                    DO UPDATE SET
                        final_url = EXCLUDED.final_url,
                        last_used_at = NOW()
                """, (
                    original_url,
                    final_url
                ))

    except Exception as e:
        # Le convertisseur continue à fonctionner
        # même si Supabase est temporairement indisponible.
        print("Erreur écriture cache :", e)


# =========================================================
# FACEBOOK
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


def is_login_url(url):
    try:
        return urlparse(url).path.lower().startswith("/login")
    except Exception:
        return False


def is_share_url(url):
    try:
        return urlparse(url).path.lower().startswith("/share/")
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

            m = re.search(
                pat,
                text,
                re.I
            )

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
    # 1 - CACHE SUPABASE EN PREMIER
    # -----------------------------------------------------

    cached = get_cache(original_url)

    if cached:

        return jsonify(
            final_url=cached,
            cached=True
        )


    # -----------------------------------------------------
    # 2 - ABSENT DU CACHE :
    #     MOTEUR FACEBOOK V4
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

                # Facebook demande une connexion :
                # on s'arrête immédiatement.
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
        # NETTOYAGE
        # -------------------------------------------------

        cleaned = clean_facebook_url(current)


        # -------------------------------------------------
        # CAS /people/
        # -------------------------------------------------

        if urlparse(
            cleaned
        ).path.lower().startswith("/people/"):

            fb_id = try_people_numeric(cleaned)

            if fb_id:

                cleaned = (
                    "https://www.facebook.com/"
                    "profile.php?id="
                    + fb_id
                )


        # -------------------------------------------------
        # Un /share/ restant n'est pas un résultat
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
        # ENREGISTREMENT SUPABASE
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
# ÉTAT DU CACHE SUPABASE
# =========================================================

@app.get("/api/cache-status")
def cache_status():

    try:

        with get_db() as conn:
            with conn.cursor() as cur:

                cur.execute("""
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(hits), 0)
                    FROM facebook_cache
                """)

                row = cur.fetchone()

        return jsonify(
            cache="supabase-postgresql",
            connected=True,
            entries=row[0],
            hits=row[1]
        )

    except Exception as e:

        print("Erreur Supabase :", e)

        return jsonify(
            cache="supabase-postgresql",
            connected=False,
            entries=0,
            hits=0
        ), 503
