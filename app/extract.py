"""Extraktion von Buchungen aus Kontoauszügen (PDF / Bild) via Claude."""

import base64
import io
import json
from decimal import Decimal, InvalidOperation

import anthropic
import pdfplumber
from PIL import Image

MODEL = "claude-opus-5-5"

# Ab so vielen Zeichen Text pro Seite gilt ein PDF als "echtes" Text-PDF
MIN_TEXT_CHARS_PER_PAGE = 80

# Grenzen für Bilder (API: max. 5 MB pro Bild, lange Kante wird ohnehin verkleinert)
MAX_IMAGE_BYTES = 4_500_000
TILE_HEIGHT_RATIO = 1.4  # sehr hohe Screenshots werden in Kacheln (Breite x 1.4*Breite) zerlegt
TILE_OVERLAP = 0.12

SYSTEM_PROMPT = """Du extrahierst Buchungen aus deutschen (oder anderen) Kontoauszügen, \
Umsatzlisten und Screenshots aus Online-Banking-Apps. Das Ergebnis wird an einen \
Buchhalter weitergegeben, Genauigkeit ist daher wichtiger als alles andere.

Regeln:
- Erfasse JEDE einzelne Buchung genau einmal, in der Reihenfolge des Dokuments.
- Mehrzeilige Verwendungszwecke gehören zu EINER Buchung; Zeilen zusammenfügen (mit Leerzeichen).
- Betrag mit Vorzeichen: Abbuchungen / Soll / Belastungen negativ, Gutschriften / Haben positiv. \
Achte auf Kennzeichen wie "S"/"H", "-"/"+", Soll/Haben-Spalten, rote Schrift.
- Beträge als Dezimalzahl-String mit Punkt, ohne Tausendertrennzeichen, z. B. "-1234.56" oder "89.90".
- Datumsangaben als YYYY-MM-DD. Fehlt beim Buchungsdatum das Jahr, leite es aus dem \
Auszugszeitraum ab.
- Zwischensummen, Überträge, Saldenzeilen und Werbetexte sind KEINE Buchungen.
- Anfangs- und Endsaldo (alter/neuer Kontostand) nur angeben, wenn sie im Dokument stehen, sonst null.
- Wenn ein Bild in mehrere überlappende Teile zerlegt wurde, gehören die Teile zu einem \
einzigen Screenshot: Buchungen in Überlappungsbereichen NICHT doppelt erfassen.
- Unleserliches nicht raten: setze stattdessen einen Hinweis in "hinweise".
- Erfinde keine Daten. Unbekannte Felder sind null bzw. leerer String."""

USER_PROMPT = "Extrahiere alle Buchungen aus diesem Kontoauszug."

NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}

SCHEMA = {
    "type": "object",
    "properties": {
        "bank": NULLABLE_STR,
        "kontoinhaber": NULLABLE_STR,
        "iban": NULLABLE_STR,
        "waehrung": {"type": "string", "description": "ISO-Code, z. B. EUR"},
        "zeitraum_von": NULLABLE_STR,
        "zeitraum_bis": NULLABLE_STR,
        "anfangssaldo": NULLABLE_STR,
        "endsaldo": NULLABLE_STR,
        "buchungen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "buchungsdatum": {"type": "string"},
                    "valutadatum": NULLABLE_STR,
                    "gegenpartei": {"type": "string", "description": "Empfänger bzw. Auftraggeber"},
                    "gegen_iban": NULLABLE_STR,
                    "verwendungszweck": {"type": "string"},
                    "buchungsart": {"type": "string", "description": "z. B. Lastschrift, Überweisung, Kartenzahlung"},
                    "betrag": {"type": "string"},
                },
                "required": [
                    "buchungsdatum", "valutadatum", "gegenpartei", "gegen_iban",
                    "verwendungszweck", "buchungsart", "betrag",
                ],
                "additionalProperties": False,
            },
        },
        "hinweise": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "bank", "kontoinhaber", "iban", "waehrung", "zeitraum_von", "zeitraum_bis",
        "anfangssaldo", "endsaldo", "buchungen", "hinweise",
    ],
    "additionalProperties": False,
}

client = anthropic.Anthropic(max_retries=3)


class ExtractionError(Exception):
    pass


# ---------------------------------------------------------------- Eingabe aufbereiten

def _pdf_text(data: bytes) -> str | None:
    """Text eines Text-PDFs (layouterhaltend) oder None bei gescannten PDFs."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [p.extract_text(layout=True) or "" for p in pdf.pages]
    except Exception:
        return None
    if not pages:
        return None
    if sum(len(p.strip()) for p in pages) < MIN_TEXT_CHARS_PER_PAGE * len(pages):
        return None
    return "\n\n".join(f"=== Seite {i} ===\n{p}" for i, p in enumerate(pages, 1))


def _encode_image(img: Image.Image) -> dict:
    """Bild als PNG (oder JPEG, falls zu groß) in einen Content-Block packen."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    media_type = "image/png"
    if buf.tell() > MAX_IMAGE_BYTES:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        media_type = "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(buf.getvalue()).decode(),
        },
    }


def _image_blocks(data: bytes) -> list[dict]:
    """Bild laden; sehr hohe Screenshots in überlappende Kacheln zerlegen."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ExtractionError(f"Bild konnte nicht gelesen werden: {e}")

    w, h = img.size
    tile_h = int(w * TILE_HEIGHT_RATIO)
    if h <= tile_h * 1.2:
        return [_encode_image(img)]

    step = int(tile_h * (1 - TILE_OVERLAP))
    tops = list(range(0, max(h - tile_h, 0) + 1, step))
    if tops[-1] + tile_h < h:
        tops.append(h - tile_h)
    blocks = []
    for i, top in enumerate(tops, 1):
        blocks.append({"type": "text", "text": f"Teil {i} von {len(tops)}:"})
        blocks.append(_encode_image(img.crop((0, top, w, top + tile_h))))
    return blocks


def build_content(filename: str, data: bytes) -> tuple[list[dict], str]:
    """Content-Blöcke für die API + Beschreibung der verwendeten Methode."""
    name = filename.lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        text = _pdf_text(data)
        if text:
            return [{"type": "text", "text": f"Kontoauszug (Text aus PDF extrahiert):\n\n{text}"}], "PDF-Text"
        doc = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(data).decode(),
            },
        }
        return [doc], "PDF-Scan"
    return _image_blocks(data), "Bild"


# ---------------------------------------------------------------- Claude-Aufruf

def _call_claude(content: list[dict]) -> dict:
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content + [{"type": "text", "text": USER_PROMPT}]}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": SCHEMA},
            },
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            response = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise ExtractionError("API-Key ungültig – bitte Server-Konfiguration prüfen.")
    except anthropic.RateLimitError:
        raise ExtractionError("API-Limit erreicht – bitte kurz warten und erneut versuchen.")
    except anthropic.BadRequestError as e:
        raise ExtractionError(f"Datei wurde von der API abgelehnt: {e.message}")
    except anthropic.APIStatusError as e:
        raise ExtractionError(f"API-Fehler {e.status_code} – bitte später erneut versuchen.")
    except anthropic.APIConnectionError:
        raise ExtractionError("Keine Verbindung zur API.")

    if response.stop_reason == "refusal":
        raise ExtractionError("Die Verarbeitung wurde vom Modell abgelehnt.")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("Auszug zu lang für einen Durchgang – bitte in kleinere Dateien aufteilen.")

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ExtractionError("Leere Antwort vom Modell.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ExtractionError("Antwort des Modells war kein gültiges JSON.")


# ---------------------------------------------------------------- Prüfung

def _dec(value) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace("+", ""))
    except InvalidOperation:
        return None


def check_balance(result: dict) -> dict:
    """Anfangssaldo + Summe der Buchungen == Endsaldo?"""
    bad = [b for b in result["buchungen"] if _dec(b["betrag"]) is None]
    summe = sum((_dec(b["betrag"]) or Decimal(0)) for b in result["buchungen"])
    start, end = _dec(result.get("anfangssaldo")), _dec(result.get("endsaldo"))

    check = {"summe": f"{summe:.2f}", "status": "unbekannt", "differenz": None}
    if bad:
        check["status"] = "fehler"
        check["text"] = f"{len(bad)} Buchung(en) mit unlesbarem Betrag."
    elif start is None or end is None:
        check["text"] = "Kein Anfangs- und/oder Endsaldo im Dokument – Summe konnte nicht geprüft werden."
    else:
        diff = start + summe - end
        check["differenz"] = f"{diff:.2f}"
        if diff == 0:
            check["status"] = "ok"
            check["text"] = "Saldo stimmt: Anfangssaldo + Buchungen = Endsaldo."
        else:
            check["status"] = "fehler"
            check["text"] = f"Saldo weicht um {diff:.2f} ab – bitte Buchungen prüfen."
    return check


def extract(filename: str, data: bytes) -> dict:
    content, method = build_content(filename, data)
    result = _call_claude(content)
    result["datei"] = filename
    result["methode"] = method
    result["pruefung"] = check_balance(result)
    return result
