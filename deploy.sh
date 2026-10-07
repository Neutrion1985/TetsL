#!/usr/bin/env bash
# Оновлює сайт на PythonAnywhere: git pull у Bash-консолі + Reload вебзастосунку.
# Потрібні змінні оточення PA_API_TOKEN і PA_USERNAME, а також доступ до www.pythonanywhere.com.
set -euo pipefail
API="https://www.pythonanywhere.com/api/v0/user/$PA_USERNAME"
H="Authorization: Token $PA_API_TOKEN"
DOMAIN="${PA_USERNAME,,}.pythonanywhere.com"

# Береться перша запущена Bash-консоль (нові консолі через API не стартують,
# доки їх не відкрити в браузері).
CONSOLE=$(curl -sS --retry 3 -H "$H" "$API/consoles/" |
  python3 -I -c "import json,sys; c=[x['id'] for x in json.load(sys.stdin) if x['executable']=='bash']; print(c[-1] if c else '')")
if [ -z "$CONSOLE" ]; then
  echo "Немає Bash-консолі. Відкрийте Consoles → Bash на pythonanywhere.com і повторіть." >&2
  exit 1
fi

curl -sS --retry 3 -H "$H" -X POST --data-urlencode $'input=cd ~/TetsL && git pull\n' \
  "$API/consoles/$CONSOLE/send_input/" > /dev/null
sleep 10
curl -sS --retry 3 -H "$H" "$API/consoles/$CONSOLE/get_latest_output/" |
  python3 -I -c "import json,sys,re; print(re.sub(r'\x1b\[[0-9;?]*[a-zA-Z=>]','',json.load(sys.stdin)['output'])[-800:])"

curl -sS --retry 3 -H "$H" -X POST "$API/webapps/$DOMAIN/reload/"
echo
