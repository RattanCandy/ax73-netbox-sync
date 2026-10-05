# TP-Link AX73 → NetBox IPAM sync

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](ax73_sync.py)
![NetBox](https://img.shields.io/badge/NetBox-IPAM-2463AB)
![Safety](https://img.shields.io/badge/default-dry--run-orange)
![Status](https://img.shields.io/badge/status-experimental-yellow)

**Discover devices connected to a TP-Link Archer AX73 router and reconcile its DHCP-authoritative address pool with NetBox.**

> [!WARNING]
> **Experimental. Dry-run is the default.** In apply mode the reconciler can **create, modify, and delete NetBox IPAddress objects**. Always test with a non-production NetBox or take a verified database backup before running `--apply`. This is an example of a tested private integration, **not an official TP-Link or NetBox integration**.

## What it does

- Reads the AX73's authenticated **Clients** page using Playwright/Chromium.
- Writes a timestamped JSON snapshot of currently connected clients.
- Synchronizes a **configurable IPv4 subnet and DHCP range** to NetBox.
- Treats the DHCP pool as AX73-authoritative; addresses outside the DHCP pool remain NetBox-authoritative.
- Records `ax73_discovery`, `ax73_last_seen`, and `discovery_status` custom fields.
- Marks missing previously seen DHCP clients with a grace period (default **60 minutes**) before deleting.
- Prevents destructive reconciliation when the snapshot is missing, empty, stale, or future-dated.
- Supports explicit apply/dry-run execution and a scheduled systemd timer.

### Reconciliation rules

| Condition | Action |
|---|---|
| AX73 sees new DHCP-pool client | Create corresponding NetBox IP |
| AX73 sees an existing DHCP-pool IP | Update discovery and mark AX73-owned |
| AX73 sees a static/out-of-pool IP | Do not create or claim a new record |
| Known AX73-managed DHCP client disappears | Mark missing; allow configurable grace period |
| Previously seen DHCP client still absent past grace | Delete **only after ownership and absence checks** |
| Existing DHCP record has no discovery history and isn't seen | **Delete immediately** in apply mode (AX73-authoritative pool policy) |
| Out-of-pool NetBox record | Protect from AX73-driven deletion |

**Important:** The immediate stale-record deletion rule is intentional and potentially destructive. Audit your DHCP pool ownership before enabling apply mode. The source was originally tested with a particular AX73 UI; firmware changes may break collection.

## Requirements

- Python 3.11+ and `playwright`; Chromium installed for Playwright.
- Local network access to a TP-Link Archer AX73 web administration page.
- Running **NetBox Docker** container with Django's `manage.py` available.
- Host account allowed to run `docker cp` and `docker exec`.
- Three NetBox IPAddress custom fields (names/types):
  - `ax73_discovery`: **JSON**
  - `ax73_last_seen`: **Date and time**
  - `discovery_status`: **Text**
- Reliable host clock, IPv4 subnet with **/24 or longer prefix**, and a separately defined inclusive DHCP pool.

## Setup

```bash
git clone https://github.com/RattanCandy/ax73-netbox-sync.git
cd ax73-netbox-sync
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Edit `.env` to match your **own** router, LAN subnet, DHCP start/end, Docker container name, and the location of a password file. The sample addresses use the documentation-only `192.0.2.0/24` block. **Never commit real .env files or passwords.**

Create `secrets/router_password` with your AX73 administration password, readable only by the service account:

```bash
mkdir -p -m 700 secrets data
chmod 700 secrets data
# Create secrets/router_password securely in your editor; do not paste passwords into shell history.
chmod 600 secrets/router_password
```

Load the environment, then collect and preview. An example way to load a trusted local .env file into your own shell is:

```bash
set -a
. ./.env
set +a
python ax73_collect_clients.py
python ax73_sync.py --dry-run
```

Check the reported **created / updated / missing / removed / protected** lists carefully. `--dry-run` makes no NetBox changes. To enable changes **only after backups and verification**:

```bash
python ax73_sync.py --apply
```

`ax73_cycle.sh` runs the collector and the dry-run reconciler with a lock by default. Set `AX73_APPLY=1` in the service environment **only** when ready for periodic live reconciliation.

### Optional systemd scheduler

Templates are included in `systemd/`. Adjust service account, installation directory and environment-file location to suit the host. The example timer runs approximately every **15 minutes**. In practice allow enough time that the snapshot remains fresh (15-minute maximum age); verify scheduling and observe several dry-runs before setting `AX73_APPLY=1`.

## Security & privacy

- The **local snapshot** contains IP addresses, MAC addresses and hostnames; treat it as private inventory data.
- The **password file is local** and must never be committed.
- Browser automation uses `ignore_https_errors=True` for router self-signed certificates; run only on your trusted local network.
- The router UI may require taking over a previous admin session.
- No device snapshots, session cookies, router credentials, or production network details are included in this repo.

## Validation

`python -m unittest discover -s tests -v` covers non-destructive source validation and Docker invocation behavior with mocks. It does not replace testing against a disposable NetBox and the correct AX73 firmware.

## Limitations and provenance

The browser collector relies on TP-Link's internal JavaScript `connectedClientsStore`, which is undocumented and firmware-dependent. This project originated from a personal home-lab implementation and is provided **as-is**, without affiliation or vendor support. The **copyright holder has not selected an open-source license yet**; public visibility alone does not grant permission to redistribute or modify.
