#!/usr/bin/env bash
# Оновлює сайт на PythonAnywhere і перезапускає його (Reload).
# Потрібні змінні оточення PA_API_TOKEN і PA_USERNAME, а також доступ до www.pythonanywhere.com.
#
# Спосіб 1: якщо на PythonAnywhere є запущена Bash-консоль — у ній виконується
#           git fetch + reset до гілки (git-копія на сервері лишається чистою).
# Спосіб 2: якщо консолі «сплять» (PythonAnywhere зупиняє їх, і через API їх не запустити),
#           файли сайту завантажуються напряму через Files API.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -I - "$(git rev-parse --abbrev-ref HEAD)" <<'EOF'
import json, os, subprocess, sys, time, urllib.request

branch = sys.argv[1]
user, token = os.environ["PA_USERNAME"], os.environ["PA_API_TOKEN"]
api = f"https://www.pythonanywhere.com/api/v0/user/{user}"
domain = f"{user.lower()}.pythonanywhere.com"
remote_dir = f"/home/{user}/TetsL"


def call(method, path, data=None, files=None, retries=4):
    body, headers = None, {"Authorization": f"Token {token}"}
    if files:
        boundary = "----tetsl" + str(time.time_ns())
        name, content = files
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"content\"; filename=\"{name}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(api + path, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw.decode(errors="replace")[:200]}
        except OSError:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


import urllib.parse

def via_console():
    _, consoles = call("GET", "/consoles/")
    for c in reversed([c for c in consoles if c["executable"] == "bash"]):
        status, out = call("GET", f"/consoles/{c['id']}/get_latest_output/")
        if status != 200 or "output" not in out:
            continue
        cmd = f"cd ~/TetsL && git fetch -q origin {branch} && git reset -q --hard origin/{branch} && git log --oneline -1\n"
        call("POST", f"/consoles/{c['id']}/send_input/", data={"input": cmd})
        time.sleep(8)
        _, out = call("GET", f"/consoles/{c['id']}/get_latest_output/")
        print("Консоль", c["id"], "→", out.get("output", "").strip().splitlines()[-2:])
        return True
    return False


def via_files():
    paths = subprocess.run(["git", "ls-files", "app.py", "requirements.txt", "templates", "static", "tests"],
                           capture_output=True, text=True, check=True).stdout.split()
    for p in paths:
        with open(p, "rb") as f:
            status, out = call("POST", f"/files/path{remote_dir}/{p}", files=(os.path.basename(p), f.read()))
        if status not in (200, 201):
            sys.exit(f"Не вдалося завантажити {p}: {status} {out}")
    print(f"Консолі неактивні — завантажено файлів через Files API: {len(paths)}")


if not via_console():
    via_files()
status, out = call("POST", f"/webapps/{domain}/reload/")
print("Reload:", status, out)
if status != 200:
    sys.exit(1)
EOF
