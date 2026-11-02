#!/usr/bin/env python3
"""Credential helper for Goblin on Linux.

Speaks the same JSON protocol as the bundled macOS helper: one request object on
stdin, one response object on stdout. Secrets live in the desktop keyring through
secret-tool (libsecret), never in a Goblin file.
"""

import json
import shutil
import subprocess
import sys

SERVICE = "service"
ACCOUNT = "account"


def fail(message, code="error"):
    return {"state": "error", "code": code, "message": message}


def secret_tool():
    executable = shutil.which("secret-tool")
    if not executable:
        return ""
    return executable


def attributes(service, account):
    return [SERVICE, service, ACCOUNT, account]


def get(service, account):
    executable = secret_tool()
    if not executable:
        return fail("secret-tool is not installed")
    result = subprocess.run(
        [executable, "lookup"] + attributes(service, account),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"state": "missing"}
    value = result.stdout
    if value.endswith("\n"):
        value = value[:-1]
    return {"state": "ok", "value": value}


def put(service, account, value):
    executable = secret_tool()
    if not executable:
        return fail("secret-tool is not installed")
    result = subprocess.run(
        [executable, "store", "--label", service] + attributes(service, account),
        input=value,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return fail(result.stderr.strip() or "The keyring refused the secret")
    return {"state": "ok"}


def delete(service, account):
    executable = secret_tool()
    if not executable:
        return fail("secret-tool is not installed")
    result = subprocess.run(
        [executable, "clear"] + attributes(service, account),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {"state": "missing"}
    return {"state": "ok"}


def handle(request):
    operation = request.get("operation")
    service = request.get("service")
    account = request.get("account") or "default"
    if not service:
        return fail("A keychain service is required")
    if operation == "get":
        return get(service, account)
    if operation == "put":
        value = request.get("value")
        if not isinstance(value, str) or not value:
            return fail("A secret value is required")
        return put(service, account, value)
    if operation == "delete":
        return delete(service, account)
    if operation == "exists":
        found = get(service, account)
        if found.get("state") == "ok":
            return {"state": "ok", "value": "yes"}
        if found.get("state") == "missing":
            return {"state": "ok", "value": "no"}
        return found
    if operation == "authorize":
        # libsecret asks the desktop keyring itself on first use.
        return {"state": "ok"}
    return fail("Unknown keychain operation")


def main():
    try:
        request = json.load(sys.stdin)
    except ValueError:
        json.dump(fail("The credential request was not valid JSON"), sys.stdout)
        return 0
    if not isinstance(request, dict):
        json.dump(fail("The credential request must be an object"), sys.stdout)
        return 0
    json.dump(handle(request), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
