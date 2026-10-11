#!/bin/bash
# Setzt Passwort und Anthropic-API-Key für die Kontoauszug-App.
# Aufruf auf dem Server:  sudo kontoauszug-setup
set -euo pipefail
ENV=/etc/kontoauszug.env

read -rsp "Anthropic API-Key (leer = unverändert): " KEY; echo
read -rsp "Neues App-Passwort (leer = unverändert): " PW; echo
if [[ -n "$PW" ]]; then
  read -rsp "Passwort wiederholen: " PW2; echo
  [[ "$PW" == "$PW2" ]] || { echo "Passwörter stimmen nicht überein."; exit 1; }
fi

touch "$ENV"; chmod 600 "$ENV"
get() { grep -E "^$1=" "$ENV" | cut -d= -f2- || true; }

[[ -n "$KEY" ]] || KEY="$(get ANTHROPIC_API_KEY)"
if [[ -n "$PW" ]]; then
  HASH="$(PW="$PW" python3 -c 'import os,hashlib;s=os.urandom(16);print("scrypt$"+s.hex()+"$"+hashlib.scrypt(os.environ["PW"].encode(),salt=s,n=2**14,r=8,p=1).hex())')"
else
  HASH="$(get APP_PASSWORD_HASH)"
fi
SECRET="$(get SESSION_SECRET)"
[[ -n "$SECRET" && -z "$PW" ]] || SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"  # neues PW → alle abmelden

[[ -n "$KEY" && -n "$HASH" ]] || { echo "API-Key und Passwort müssen beim ersten Mal gesetzt werden."; exit 1; }

cat > "$ENV" <<EOV
ANTHROPIC_API_KEY=$KEY
APP_PASSWORD_HASH=$HASH
SESSION_SECRET=$SECRET
EOV
chmod 600 "$ENV"
systemctl restart kontoauszug
echo "Gespeichert, App neu gestartet."
