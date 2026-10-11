"""Web-App: Kontoauszüge (PDF/Screenshot) hochladen → CSV herunterladen."""

import asyncio
import csv
import hashlib
import hmac
import io
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from extract import ExtractionError, extract

STATIC = Path(__file__).parent / "static"
MAX_FILE_BYTES = 30 * 1024 * 1024
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

# APP_PASSWORD_HASH Format: scrypt$<salt-hex>$<hash-hex>  (erzeugt von setup.sh)
PASSWORD_HASH = os.environ["APP_PASSWORD_HASH"]
SESSION_SECRET = os.environ["SESSION_SECRET"]

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="ka_session",
    max_age=12 * 3600,
    same_site="strict",
    https_only=True,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Höchstens 3 gleichzeitige API-Aufrufe
_api_slots = asyncio.Semaphore(3)


def verify_password(pw: str) -> bool:
    try:
        _, salt, digest = PASSWORD_HASH.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), digest)
    except Exception:
        return False


def require_login(request: Request):
    if not request.session.get("ok"):
        raise HTTPException(401, "Nicht angemeldet")


# ---------------------------------------------------------------- Login

@app.get("/login", response_class=HTMLResponse)
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if verify_password(str(form.get("password", ""))):
        request.session["ok"] = True
        return RedirectResponse("/", status_code=303)
    await asyncio.sleep(2)  # Brute-Force bremsen
    return RedirectResponse("/login?fehler=1", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------- App

@app.get("/")
def index(request: Request):
    if not request.session.get("ok"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


@app.post("/api/extract")
async def api_extract(request: Request, file: UploadFile = File(...)):
    require_login(request)
    name = Path(file.filename or "datei").name
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        raise HTTPException(400, "Nur PDF, PNG, JPG, WEBP oder GIF erlaubt.")
    data = await file.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(413, "Datei größer als 30 MB.")

    async with _api_slots:
        try:
            result = await asyncio.to_thread(extract, name, data)
        except ExtractionError as e:
            raise HTTPException(422, str(e))
    return JSONResponse(result)


CSV_COLUMNS = [
    "Buchungsdatum", "Valutadatum", "Empfänger/Auftraggeber", "IBAN Gegenkonto",
    "Verwendungszweck", "Buchungsart", "Betrag", "Soll/Haben", "Währung",
    "Konto IBAN", "Bank", "Quelldatei",
]


def _de_date(iso: str | None) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y") if iso else ""
    except ValueError:
        return iso or ""


def _de_amount(value: str) -> str:
    return value.replace(",", "").replace(".", ",") if value else ""


@app.post("/api/csv")
async def api_csv(request: Request):
    require_login(request)
    results = await request.json()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(CSV_COLUMNS)
    for r in results:
        for b in r.get("buchungen", []):
            betrag = str(b.get("betrag", "")).strip()
            w.writerow([
                _de_date(b.get("buchungsdatum")),
                _de_date(b.get("valutadatum")),
                b.get("gegenpartei") or "",
                b.get("gegen_iban") or "",
                " ".join((b.get("verwendungszweck") or "").split()),
                b.get("buchungsart") or "",
                _de_amount(betrag.lstrip("+")),
                "S" if betrag.startswith("-") else "H",
                r.get("waehrung") or "EUR",
                r.get("iban") or "",
                r.get("bank") or "",
                r.get("datei") or "",
            ])
    # UTF-8 mit BOM, damit Excel Umlaute korrekt öffnet
    body = "﻿" + buf.getvalue()
    fname = f"kontoauszug_{date.today():%Y-%m-%d}.csv"
    return Response(
        body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
