#!/usr/bin/env python3

import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("AX73_ROUTER_URL", "https://192.0.2.1").rstrip("/")
LOGIN_URL = f"{BASE_URL}/webpages/login.html"
BASE = Path(os.environ.get("AX73_BASE_DIR", str(Path(__file__).resolve().parent)))
PASSWORD_FILE = Path(os.environ.get("AX73_PASSWORD_FILE", str(BASE / "secrets/router_password")))
OUTPUT_FILE = Path(os.environ.get("AX73_OUTPUT_FILE", str(BASE / "data/ax73_clients.json")))

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def main():
    if not PASSWORD_FILE.exists():
        print(f"ERROR: password file not found: {PASSWORD_FILE}")
        sys.exit(1)

    password = PASSWORD_FILE.read_text().rstrip("\r\n")

    if not password:
        print("ERROR: password file is empty")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,
        )

        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            print("Opening AX73...")
            page.goto(
                LOGIN_URL,
                wait_until="networkidle",
                timeout=30000,
            )

            print("Entering stored password...")
            password_inputs = page.locator('input[type="password"]')
            password_inputs.first.fill(password)

            print("Submitting local login...")
            page.locator("#local-login-button").click()

            # Give the AX73 login transition time to initialize.
            page.wait_for_timeout(1500)

            # AX73 may report that another management session exists.
            conflict_ok = page.locator("#user-conflict-prompt-btn-ok")

            try:
                conflict_ok.wait_for(state="visible", timeout=5000)
                print("Existing AX73 session detected.")
                print("Taking over existing management session...")
                conflict_ok.click()
            except Exception:
                pass

            print("Waiting for AX73 application...")
            page.wait_for_url(
                "**/webpages/index.html**",
                timeout=15000,
            )

            # The AX73 frontend continues rendering asynchronously
            # after the application URL becomes available.
            page.wait_for_timeout(3000)

            print()
            print("===== AUTHENTICATED PAGE =====")
            print("AX73 application opened")

            # The AX73 only populates connectedClientsStore after
            # the Clients module is opened.
            print()
            print("===== OPENING CLIENTS MODULE =====")

            clients = page.get_by_text("Clients", exact=True)
            count = clients.count()

            print(f"Clients controls found: {count}")

            if count == 0:
                raise RuntimeError("Clients control was not found")

            clients.first.click()

            print("Clients clicked.")

            # Allow the Clients module to initialize its store.
            page.wait_for_timeout(3000)

            print()
            print("===== STORE STATE =====")

            # The AX73 creates connectedClientsStore before its
            # client data has necessarily populated. Poll the store
            # until client records actually appear.
            clients_data = []

            for attempt in range(1, 16):
                clients_data = page.evaluate("""
                    () => {
                        const s = $.su?.storeManager?.get("connectedClientsStore");
                        if (!s || !s.getData) return [];
                        return s.getData() || [];
                    }
                """)

                print(f"Attempt {attempt:02d}: {len(clients_data)} clients")

                if len(clients_data) > 0:
                    break

                page.wait_for_timeout(1000)

            if not clients_data:
                raise RuntimeError("AX73 returned zero clients after waiting")

            # Keep only the stable fields we actually want.
            stable_fields = [
                "ip",
                "mac",
                "deviceName",
                "deviceType",
                "deviceTag",
                "isGuest",
            ]

            clean_clients = []

            for raw in clients_data:
                item = {}

                for field in stable_fields:
                    value = raw.get(field)

                    if field == "mac" and value:
                        value = value.upper()

                    item[field] = value

                clean_clients.append(item)

            # Sort numerically by IPv4 address.
            clean_clients.sort(
                key=lambda x: tuple(
                    int(part)
                    for part in (x["ip"] or "0.0.0.0").split(".")
                )
            )

            result = {
                "source": "TP-Link Archer AX73",
                "router_ip": urlsplit(BASE_URL).hostname,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "client_count": len(clean_clients),
                "clients": clean_clients,
            }

            OUTPUT_FILE.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n"
            )
            OUTPUT_FILE.chmod(0o600)

            print()
            print("===== COLLECTION COMPLETE =====")
            print(f"Client count: {len(clean_clients)}")
            print(f"Output: {OUTPUT_FILE}")

            print("Private client details saved to local snapshot only.")

        finally:
            browser.close()


if __name__ == "__main__":
    main()
