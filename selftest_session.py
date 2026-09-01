"""服务端自测共用的登录步骤。"""

USERNAME = "selftest-admin"
PASSWORD = "selftest-password-2026"


def login(client, base_url: str = "") -> dict:
    response = client.post(f"{base_url}/api/auth/login", json={
        "username": USERNAME,
        "password": PASSWORD,
    })
    response.raise_for_status()
    data = response.json()
    client.headers.update({"X-CSRF-Token": data["csrf_token"]})
    return data
