# Kontoauszug → CSV

Interne Web-App: Kontoauszüge (PDF, Scans, Screenshots) hochladen, Buchungen per Claude extrahieren,
per Saldo-Abgleich prüfen und als CSV für die Buchhaltung herunterladen.

- **App:** FastAPI (`app/main.py`), Extraktion in `app/extract.py`, Oberfläche in `app/static/`
- **Server:** Ubuntu, Caddy (HTTPS, `deploy/Caddyfile`), systemd (`deploy/kontoauszug.service`)
- **Zugangsdaten:** liegen nur auf dem Server in `/etc/kontoauszug.env`, gesetzt über
  `ssh -t root@<server> kontoauszug-setup` (`deploy/setup-secrets.sh`)

## Deployment

```bash
rsync -a --delete --exclude __pycache__ app requirements.txt root@<server>:/opt/kontoauszug/
ssh root@<server> 'chmod -R a+rX /opt/kontoauszug && systemctl restart kontoauszug'
```

Neue Abhängigkeiten: `ssh root@<server> /opt/kontoauszug/venv/bin/pip install -r /opt/kontoauszug/requirements.txt`

## CSV-Format

Semikolon-getrennt, UTF-8 mit BOM, Datum `TT.MM.JJJJ`, Betrag `-1234,50`. Spalten:
Buchungsdatum, Valutadatum, Empfänger/Auftraggeber, IBAN Gegenkonto, Verwendungszweck,
Buchungsart, Betrag, Soll/Haben, Währung, Konto IBAN, Bank, Quelldatei.
