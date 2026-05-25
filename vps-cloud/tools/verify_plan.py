from pathlib import Path
import sys

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import app, init_db


def main() -> int:
    init_db()

    retained = [
        "/",
        "/anon",
        "/links",
        "/handler",
        "/drool",
        "/drool.html",
        "/spotify",
        "/spotify.html",
        "/admin",
        "/admin.html",
    ]
    retired = [
        "/store",
        "/store.html",
        "/checkout",
        "/checkout.html",
        "/vault",
        "/vault.html",
        "/vods",
        "/vods.html",
        "/chat",
        "/chat.html",
    ]

    with TestClient(app, raise_server_exceptions=False) as client:
        print("=== ROUTE MATRIX: RETAINED ===")
        retained_failures = []
        for path in retained:
            resp = client.get(path, follow_redirects=False)
            location = resp.headers.get("location", "")
            print(f"{path}\t{resp.status_code}\t{location}")
            if resp.status_code >= 400:
                retained_failures.append((path, resp.status_code))

        print("=== ROUTE MATRIX: RETIRED ===")
        retired_failures = []
        for path in retired:
            resp = client.get(path, follow_redirects=False)
            location = resp.headers.get("location", "")
            print(f"{path}\t{resp.status_code}\t{location}")
            if resp.status_code not in (301, 302, 307, 308) or location != "/":
                retired_failures.append((path, resp.status_code, location))

        print("=== API SMOKE ===")
        api_checks = [
            "/api/questions/public",
            "/api/public/status",
        ]
        api_failures = []
        for path in api_checks:
            resp = client.get(path, follow_redirects=False)
            print(f"{path}\t{resp.status_code}")
            if resp.status_code >= 500:
                api_failures.append((path, resp.status_code))

    print("=== SUMMARY ===")
    print(f"retained_failures={len(retained_failures)}")
    print(f"retired_failures={len(retired_failures)}")
    print(f"api_failures={len(api_failures)}")

    if retained_failures:
        print("retained_failure_details:")
        for row in retained_failures:
            print(row)
    if retired_failures:
        print("retired_failure_details:")
        for row in retired_failures:
            print(row)
    if api_failures:
        print("api_failure_details:")
        for row in api_failures:
            print(row)

    return 1 if (retained_failures or retired_failures or api_failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
