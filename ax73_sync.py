#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

BASE = Path(os.environ.get("AX73_BASE_DIR", str(Path(__file__).resolve().parent)))
SOURCE = BASE / "data" / "ax73_clients.json"

CONTAINER = os.environ.get("NETBOX_CONTAINER", "netbox-docker-netbox-1")
CONTAINER_JSON = "/tmp/ax73_clients.json"
CONTAINER_SCRIPT = "/tmp/ax73_sync.py"

MAX_COLLECTION_AGE_SECONDS = 15 * 60

# Refresh AX73 last_seen telemetry at most once per hour.
# Actual discovery metadata changes are still reconciled immediately.
LAST_SEEN_REFRESH_SECONDS = 60 * 60


DJANGO_SCRIPT = r'''
import json
import os

from datetime import datetime, timedelta, timezone as dt_timezone
from ipaddress import IPv4Address, IPv4Network

from django.db import transaction
from django.utils import timezone

from ipam.models import IPAddress


DRY_RUN = os.environ.get("AX73_DRY_RUN", "1") != "0"

NETWORK = IPv4Network(os.environ["AX73_SUBNET"], strict=True)
DHCP_START = IPv4Address(os.environ["AX73_DHCP_START"])
DHCP_END = IPv4Address(os.environ["AX73_DHCP_END"])
# This query currently uses the first three octets for filtering.
# Reject /23 or broader networks to avoid missing records.
if NETWORK.prefixlen < 24 or not (
    DHCP_START in NETWORK and DHCP_END in NETWORK and DHCP_START <= DHCP_END
):
    raise RuntimeError("Expected subnet /24 or narrower with DHCP pool inside subnet")
NETBOX_PREFIX = str(NETWORK.network_address).rsplit(".", 1)[0] + "."

DELETE_AFTER = timedelta(minutes=int(os.environ.get("AX73_GRACE_MINUTES", "60")))

# Refresh AX73 last_seen telemetry at most once per hour.
# Actual discovery metadata changes are still reconciled immediately.
LAST_SEEN_REFRESH_SECONDS = 60 * 60


def in_dhcp_pool(ip):
    address = IPv4Address(ip)
    return DHCP_START <= address <= DHCP_END


def normalize_client(client, collected_at, managed):
    return {
        "managed_by_ax73": managed,
        "mac": client.get("mac"),
        "device_name": client.get("deviceName"),
        "device_type": client.get("deviceType"),
        "network": client.get("deviceTag"),
        "is_guest": client.get("isGuest"),
        "last_seen": collected_at,
        "present": True,
        "missing_since": None,
    }


# ============================================================
# LOAD AND VALIDATE AX73 COLLECTION
# ============================================================

with open("/tmp/ax73_clients.json") as f:
    source = json.load(f)

collected_at = source.get("collected_at")

if not collected_at:
    raise RuntimeError(
        "AX73 collection has no collected_at timestamp"
    )

try:
    collected_dt = timezone.datetime.fromisoformat(
        collected_at.replace("Z", "+00:00")
    )

    if timezone.is_naive(collected_dt):
        collected_dt = timezone.make_aware(collected_dt)

except Exception as exc:
    raise RuntimeError(
        f"Invalid AX73 collected_at timestamp: {collected_at}"
    ) from exc


now = timezone.now()


if collected_dt > now + timedelta(minutes=5):
    raise RuntimeError(
        f"AX73 collection timestamp is in the future: {collected_at}"
    )


if now - collected_dt > timedelta(minutes=15):
    raise RuntimeError(
        f"AX73 collection is too old: {collected_at}"
    )


raw_clients = source.get("clients")

if not isinstance(raw_clients, list):
    raise RuntimeError(
        "AX73 clients field is not a list"
    )


if not raw_clients:
    raise RuntimeError(
        "AX73 returned zero clients; refusing to reconcile"
    )


# ------------------------------------------------------------
# Only accept valid IPv4 addresses inside our /26.
# ------------------------------------------------------------

ax73 = {}

for client in raw_clients:

    ip = client.get("ip")

    if not ip:
        continue

    try:
        address = IPv4Address(ip)
    except Exception:
        continue

    if address not in NETWORK:
        continue

    # One IP represents one current AX73 client.
    ax73[str(address)] = client


if not ax73:
    raise RuntimeError(
        "AX73 collection contained no valid LAN clients; "
        "refusing to reconcile"
    )


# ============================================================
# RECONCILIATION
# ============================================================

with transaction.atomic():

    netbox = {}

    for obj in IPAddress.objects.filter(
        address__startswith=NETBOX_PREFIX
    ).order_by("address"):

        ip = str(obj.address).split("/")[0]

        try:
            address = IPv4Address(ip)
        except Exception:
            continue

        if address not in NETWORK:
            continue

        cf = dict(obj.custom_field_data or {})
        discovery = dict(cf.get("ax73_discovery") or {})

        netbox[ip] = {
            "object": obj,
            "cf": cf,
            "discovery": discovery,
            "managed": discovery.get(
                "managed_by_ax73",
                False,
            ),
        }


    created = []
    updated = []
    missing = []
    removed = []
    protected = []
    waiting = []
    unchanged = []


    # ========================================================
    # AX73 CURRENTLY SEES THESE CLIENTS
    # ========================================================

    for ip, client in sorted(
        ax73.items(),
        key=lambda item: IPv4Address(item[0]),
    ):

        existing = netbox.get(ip)


        # ----------------------------------------------------
        # NEW AX73 CLIENT
        # ----------------------------------------------------

        if existing is None:

            # AX73 may ONLY create inside DHCP pool.
            if not in_dhcp_pool(ip):

                protected.append(
                    f"{ip} AX73-only static/outside-DHCP client"
                )

                continue


            discovery = normalize_client(
                client,
                collected_at,
                True,
            )


            if DRY_RUN:

                created.append(ip)

                continue


            obj = IPAddress.objects.create(
                address=f"{ip}/{NETWORK.prefixlen}",
                status="active",
            )

            ax73_last_seen = None

            if discovery.get("last_seen"):
                try:
                    ax73_last_seen = datetime.fromisoformat(
                        discovery["last_seen"].replace("Z", "+00:00")
                    )

                    if ax73_last_seen.tzinfo is None:
                        ax73_last_seen = ax73_last_seen.replace(
                            tzinfo=dt_timezone.utc
                        )

                except Exception:
                    ax73_last_seen = None

            obj.custom_field_data = {
                "ax73_discovery": discovery,
                "ax73_last_seen": ax73_last_seen,
                "discovery_status": "Present",
            }

            obj.save()

            created.append(ip)

            continue


        # ----------------------------------------------------
        # EXISTING NETBOX IP
        # ----------------------------------------------------

        obj = existing["object"]

        old = dict(existing["discovery"])

        managed = existing["managed"]

        # AX73 is authoritative for the DHCP lease pool.
        # Any currently observed IP inside the configured DHCP range becomes
        # AX73-owned immediately, even if NetBox previously
        # marked the record as not AX73-owned.
        if in_dhcp_pool(ip):
            desired_managed = True
        else:
            desired_managed = managed

        desired = normalize_client(
            client,
            collected_at,
            desired_managed,
        )

        desired["managed_by_ax73"] = desired_managed

        old_compare = dict(old)
        new_compare = dict(desired)

        old_compare.pop("last_seen", None)
        new_compare.pop("last_seen", None)

        cf = dict(existing["cf"])

        cf["ax73_discovery"] = desired

        ax73_last_seen = None

        if desired.get("last_seen"):
            try:
                ax73_last_seen = datetime.fromisoformat(
                    desired["last_seen"].replace("Z", "+00:00")
                )

                if ax73_last_seen.tzinfo is None:
                    ax73_last_seen = ax73_last_seen.replace(
                        tzinfo=dt_timezone.utc
                    )

            except Exception:
                ax73_last_seen = None

        cf["ax73_last_seen"] = ax73_last_seen

        desired_status = (
            "Present"
            if desired.get("present") is True
            else "Missing - Grace Period"
        )

        cf["discovery_status"] = desired_status

        human_fields_need_refresh = (
            not existing["cf"].get("ax73_last_seen")
            or existing["cf"].get("discovery_status") != desired_status
        )

        # last_seen is telemetry, not discovery-state.
        # Do not rewrite every AX73 record on every collection.
        last_seen_needs_refresh = False

        old_last_seen = old.get("last_seen")

        if old_last_seen:
            try:
                old_last_seen_dt = datetime.fromisoformat(
                    old_last_seen.replace("Z", "+00:00")
                )

                if old_last_seen_dt.tzinfo is None:
                    old_last_seen_dt = old_last_seen_dt.replace(
                        tzinfo=dt_timezone.utc
                    )

                last_seen_needs_refresh = (
                    datetime.now(dt_timezone.utc) - old_last_seen_dt
                ).total_seconds() >= LAST_SEEN_REFRESH_SECONDS

            except Exception:
                # Invalid/missing telemetry should be repaired.
                last_seen_needs_refresh = True

        else:
            last_seen_needs_refresh = True

        needs_update = (
            old_compare != new_compare
            or old.get("present") is not True
            or last_seen_needs_refresh
            or human_fields_need_refresh
        )

        if needs_update:

            if not DRY_RUN:
                obj.custom_field_data = cf
                obj.save()

            updated.append(ip)

        else:
            unchanged.append(ip)


    # ========================================================
    # AX73 NO LONGER SEES THESE NETBOX CLIENTS
    # ========================================================

    for ip, existing in sorted(
        netbox.items(),
        key=lambda item: IPv4Address(item[0]),
    ):

        # Currently present — already handled above.
        if ip in ax73:
            continue


        obj = existing["object"]

        discovery = dict(existing["discovery"])

        managed = existing["managed"]


        # ====================================================
        # NOT CURRENTLY REPORTED BY AX73
        # ====================================================

        # DHCP pool is AX73-authoritative.
        #
        # An unclaimed DHCP-range record with no AX73 discovery
        # history is treated as stale NetBox data and is purged
        # immediately.
        #
        # A DHCP-range record with historical AX73 discovery is
        # considered AX73-owned territory. If it is not currently
        # reported, force AX73 ownership and continue into the
        # existing 60-minute missing/grace-period logic.
        #
        # Static/infrastructure addresses outside the DHCP pool
        # remain NetBox-authoritative.

        if in_dhcp_pool(ip):

            # No previous AX73 discovery.
            #
            # This is an unclaimed/stale NetBox DHCP record.
            # The DHCP pool is dynamically authoritative to AX73,
            # so do not leave this record waiting indefinitely.
            if not discovery:

                if not DRY_RUN:
                    obj.delete()

                removed.append(
                    f"{ip} stale DHCP record; "
                    f"no AX73 discovery history"
                )

                continue


            # Historical AX73 discovery exists.
            #
            # DHCP pool is AX73-authoritative, so this record must
            # be AX73-owned even if legacy state says otherwise.
            # Do NOT continue here: fall through into the existing
            # 60-minute Missing - Grace Period logic below.
            managed = True
            discovery["managed_by_ax73"] = True

        else:


            # Static/infrastructure range is NetBox-authoritative.
            # AX73 cannot claim or delete these records.

            protected.append(
                f"{ip} NetBox authoritative; "
                f"AX73 cannot claim/delete"
            )

            if not DRY_RUN:

                cf = dict(existing["cf"])
                cf["discovery_status"] = (
                    "Protected - NetBox Authoritative"
                )

                obj.custom_field_data = cf
                obj.save()

            continue


        # ====================================================
        # AX73-OWNED DHCP RECORD IS MISSING
        # ====================================================

        missing_since = discovery.get("missing_since")


        # ----------------------------------------------------
        # FIRST CONFIRMED ABSENCE
        # ----------------------------------------------------

        if not missing_since:

            discovery["present"] = False
            discovery["missing_since"] = now.isoformat()


            if not DRY_RUN:

                cf = dict(existing["cf"])
                cf["ax73_discovery"] = discovery

                # last_seen remains the last actual AX73 observation.
                cf["discovery_status"] = "Missing - Grace Period"

                obj.custom_field_data = cf
                obj.save()


            missing.append(ip)

            continue


        # ----------------------------------------------------
        # EXISTING ABSENCE TIMER
        # ----------------------------------------------------

        try:

            missing_dt = timezone.datetime.fromisoformat(
                missing_since.replace("Z", "+00:00")
            )

            if timezone.is_naive(missing_dt):
                missing_dt = timezone.make_aware(missing_dt)

        except Exception:

            # Never delete using a corrupt timestamp.
            # Reset the grace-period clock instead.

            discovery["present"] = False
            discovery["missing_since"] = now.isoformat()


            if not DRY_RUN:

                cf = dict(existing["cf"])
                cf["ax73_discovery"] = discovery

                # last_seen remains the last actual AX73 observation.
                cf["discovery_status"] = "Missing - Grace Period"

                obj.custom_field_data = cf
                obj.save()


            missing.append(ip)

            continue


        # ----------------------------------------------------
        # CLOCK SANITY
        # ----------------------------------------------------

        if missing_dt > now:

            # Future timestamp is suspicious.
            # Reset rather than deleting.

            discovery["present"] = False
            discovery["missing_since"] = now.isoformat()


            if not DRY_RUN:

                cf = dict(existing["cf"])
                cf["ax73_discovery"] = discovery

                # last_seen remains the last actual AX73 observation.
                cf["discovery_status"] = "Missing - Grace Period"

                obj.custom_field_data = cf
                obj.save()


            missing.append(ip)

            continue


        # ----------------------------------------------------
        # 60-MINUTE DELETION THRESHOLD
        # ----------------------------------------------------

        if now - missing_dt >= DELETE_AFTER:

            # Final safety check immediately before deletion.
            if (
                managed
                and in_dhcp_pool(ip)
                and ip not in ax73
            ):

                if DRY_RUN:

                    removed.append(ip)

                else:

                    obj.delete()
                    removed.append(ip)

            else:

                protected.append(
                    f"{ip} deletion safety check failed; retained"
                )

        else:

            if not DRY_RUN:

                cf = dict(existing["cf"])
                cf["ax73_discovery"] = discovery

                # last_seen remains the last actual AX73 observation.
                cf["discovery_status"] = "Missing - Grace Period"

                obj.custom_field_data = cf
                obj.save()

            missing.append(ip)


    # ========================================================
    # REPORT
    # ========================================================

    print()
    print("============================================================")
    print("        AX73 -> NETBOX SYNCHRONIZATION")
    print("============================================================")
    print()

    print(
        "MODE            :",
        "DRY-RUN" if DRY_RUN else "LIVE",
    )

    print("AX73 collection :", collected_at)
    print("AX73 clients    :", len(ax73))
    print("DHCP authority  : {DHCP_START} - {DHCP_END}")
    print("NetBox authority: static/infrastructure outside DHCP pool")
    print(f"Delete threshold: {DELETE_AFTER.total_seconds() / 60:g} minutes")
    print()


    print("===== CREATED / AX73 AUTHORITATIVE DHCP =====")

    for ip in created:
        print(ip)

    if not created:
        print("None")


    print()
    print("===== UPDATED =====")

    for ip in updated:
        print(ip)

    if not updated:
        print("None")


    print()
    print("===== WAITING / DHCP RANGE — AX73 AUTHORITATIVE =====")

    for item in waiting:
        print(item)

    if not waiting:
        print("None")


    print()
    print("===== MISSING / AX73-OWNED DHCP — 60 MIN GRACE =====")

    for ip in missing:
        print(ip)

    if not missing:
        print("None")


    print()
    print("===== REMOVED / AX73-OWNED DHCP =====")

    for ip in removed:
        print(ip)

    if not removed:
        print("None")


    print()
    print("===== PROTECTED / NETBOX AUTHORITATIVE =====")

    for item in protected:
        print(item)

    if not protected:
        print("None")


    print()
    print("===== UNCHANGED =====")

    for ip in unchanged:
        print(ip)

    if not unchanged:
        print("None")


    print()
    print("===== SUMMARY =====")

    print("Created  :", len(created))
    print("Updated  :", len(updated))
    print("Waiting  :", len(waiting))
    print("Missing  :", len(missing))
    print("Removed  :", len(removed))
    print("Protected:", len(protected))
    print("Same     :", len(unchanged))
'''


def run():

    dry_run = "--apply" not in sys.argv

    subnet = os.environ.get("AX73_SUBNET")
    pool_start = os.environ.get("AX73_DHCP_START")
    pool_end = os.environ.get("AX73_DHCP_END")
    if not all((subnet, pool_start, pool_end)):
        print("ERROR: set AX73_SUBNET, AX73_DHCP_START and AX73_DHCP_END")
        sys.exit(2)

    from ipaddress import IPv4Address, IPv4Network
    try:
        network = IPv4Network(subnet, strict=True)
        start = IPv4Address(pool_start)
        end = IPv4Address(pool_end)
        if network.prefixlen < 24 or not (
            start in network and end in network and start <= end
        ):
            raise ValueError("DHCP range must be ordered and within a /24 or narrower subnet")
        minutes = int(os.environ.get("AX73_GRACE_MINUTES", "60"))
        if minutes < 1:
            raise ValueError("AX73_GRACE_MINUTES must be >= 1")
    except (ValueError, TypeError) as exc:
        print(f"ERROR: invalid network settings: {exc}")
        sys.exit(2)


    # ---------------------------------------------------------
    # Source file must exist.
    # ---------------------------------------------------------

    if not SOURCE.exists():

        print(
            f"ERROR: source file not found: {SOURCE}"
        )

        sys.exit(1)


    # ---------------------------------------------------------
    # Read locally first.
    # ---------------------------------------------------------

    try:

        source_data = json.loads(
            SOURCE.read_text()
        )

    except Exception as exc:

        print(
            f"ERROR: cannot parse AX73 JSON: {exc}"
        )

        sys.exit(1)


    # ---------------------------------------------------------
    # Basic source validation.
    # ---------------------------------------------------------

    collected_at = source_data.get("collected_at")

    if not collected_at:

        print(
            "ERROR: AX73 JSON has no collected_at"
        )

        sys.exit(1)


    clients = source_data.get("clients")

    if not isinstance(clients, list):

        print(
            "ERROR: AX73 JSON clients field is invalid"
        )

        sys.exit(1)


    if not clients:

        print(
            "ERROR: AX73 JSON contains zero clients; "
            "refusing synchronization"
        )

        sys.exit(1)


    # ---------------------------------------------------------
    # Validate collection timestamp.
    # ---------------------------------------------------------

    try:

        collected_dt = datetime.fromisoformat(
            collected_at.replace("Z", "+00:00")
        )

        if collected_dt.tzinfo is None:

            collected_dt = collected_dt.replace(
                tzinfo=dt_timezone.utc
            )

    except Exception as exc:

        print(
            "ERROR: invalid AX73 collection timestamp: "
            f"{collected_at}"
        )

        print(exc)

        sys.exit(1)


    now = datetime.now(dt_timezone.utc)


    if collected_dt > now + timedelta(minutes=5):

        print(
            "ERROR: AX73 collection timestamp is in the future"
        )

        sys.exit(1)


    age_seconds = (
        now - collected_dt
    ).total_seconds()


    if age_seconds > MAX_COLLECTION_AGE_SECONDS:

        print(
            "ERROR: AX73 collection is too old: "
            f"{age_seconds / 60:.1f} minutes"
        )

        print(
            "Refusing synchronization to protect NetBox."
        )

        sys.exit(1)


    # ---------------------------------------------------------
    # Copy validated source into NetBox container.
    # ---------------------------------------------------------

    subprocess.run(
        [
            "docker",
            "cp",
            str(SOURCE),
            f"{CONTAINER}:{CONTAINER_JSON}",
        ],
        check=True,
    )

    # docker cp creates files owned by root inside the container.
    # Make them readable by the NetBox application user.
    subprocess.run(
        [
            "docker",
            "exec",
            "-u",
            "root",
            CONTAINER,
            "chmod",
            "644",
            CONTAINER_JSON,
        ],
        check=True,
    )


    # ---------------------------------------------------------
    # Copy Django reconciliation script.
    # ---------------------------------------------------------

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        delete=False,
    ) as f:

        f.write(DJANGO_SCRIPT)

        local_script = f.name


    try:

        subprocess.run(
            [
                "docker",
                "cp",
                local_script,
                f"{CONTAINER}:{CONTAINER_SCRIPT}",
            ],
            check=True,
        )

        # Make the Django script readable by the NetBox user.
        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "root",
                CONTAINER,
                "chmod",
                "644",
                CONTAINER_SCRIPT,
            ],
            check=True,
        )


        docker_exec = [
            "docker",
            "exec",
        ]


        docker_exec.extend(["-e", f"AX73_DRY_RUN={int(dry_run)}"])

        for var in ("AX73_SUBNET", "AX73_DHCP_START", "AX73_DHCP_END"):
            docker_exec.extend(["-e", f"{var}={os.environ[var]}"])

        docker_exec.extend([
            "-e",
            "AX73_GRACE_MINUTES=" + os.environ.get("AX73_GRACE_MINUTES", "60"),
        ])


        docker_exec.extend(
            [
                CONTAINER,
                "python",
                "manage.py",
                "shell",
                "-c",
                f'exec(open("{CONTAINER_SCRIPT}").read())',
            ]
        )


        subprocess.run(
            docker_exec,
            check=True,
        )


    finally:

        Path(local_script).unlink(
            missing_ok=True
        )


        subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "root",
                CONTAINER,
                "rm",
                "-f",
                CONTAINER_JSON,
                CONTAINER_SCRIPT,
            ],
            check=False,
        )


if __name__ == "__main__":
    run()
