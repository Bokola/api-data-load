"""Sync locally-written CSV exports directly into a Microsoft Fabric
Lakehouse in OneLake, via the ADLS Gen2 REST API - authenticated, not a
local file copy (an earlier version of this module assumed a local
OneLake File Explorer mount; switched to this per an explicit request to
authenticate directly against ADLS/OneLake instead).

A separate, standalone step from extraction itself - main.py's run()
calls sync_to_lakehouse() after all countries' CSVs are written, but this
module is also independently runnable:

    uv run python -m gfpvan_pipeline.lakehouse_sync
    uv run python -m gfpvan_pipeline.lakehouse_sync --csv-dir path/to/csvs

Requires azure-storage-file-datalake and azure-identity (both listed in
pyproject.toml's dependencies - `uv sync` installs them automatically).

LAKEHOUSE_PATH is an abfss:// URI identifying the workspace and
Lakehouse, e.g.:

    abfss://MyWorkspace@onelake.dfs.fabric.microsoft.com/MyLakehouse.Lakehouse

Authentication uses azure-identity's DefaultAzureCredential, which tries,
in order: environment variables (AZURE_CLIENT_ID / AZURE_CLIENT_SECRET /
AZURE_TENANT_ID for a service principal), a managed identity (if running
on Azure infrastructure), then a locally logged-in Azure CLI session
(`az login`), among other supported sources - see
https://learn.microsoft.com/python/api/overview/azure/identity-readme
for the full chain. This module doesn't hardcode any ONE mechanism -
whichever DefaultAzureCredential source is configured in the environment
is what gets used; set up whichever one fits (most commonly the three
AZURE_* service-principal env vars, or `az login` for local development).

IMPORTANT - NOT verified against a real environment: there is no live
Azure/Fabric workspace or real credentials available to test this
against. The ADLS Gen2 API calls below follow the SDK's documented usage
pattern, but this has not actually been run against OneLake. Please
validate against a real (ideally non-production) Lakehouse before
relying on this for anything important, and report back anything that
doesn't work as expected - a wrong assumption about the exact directory-
creation or file-upload call shape is realistic here and would need a
real error message to diagnose properly.

Destination layout - YYYY/MM are always the CURRENT date at write time,
not anything derived from the data's own period:

    {LAKEHOUSE_PATH}/Files/supply_data/raw/VAN_data/YYYY/MM/<filename>.csv
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import Config, ConfigError
from .logger import get_logger

log = get_logger(__name__)

# Fixed per the confirmed required layout - not config-driven, since the
# request was for exactly this path shape.
LAKEHOUSE_SUBPATH = "Files/supply_data/raw/VAN_data"


class LakehouseSyncError(RuntimeError):
    pass


def _parse_lakehouse_uri(uri: str) -> tuple[str, str, str]:
    """Parse an abfss:// OneLake URI into (account_url, file_system,
    base_path).

    e.g. abfss://MyWorkspace@onelake.dfs.fabric.microsoft.com/MyLakehouse.Lakehouse
      -> ("https://onelake.dfs.fabric.microsoft.com", "MyWorkspace", "MyLakehouse.Lakehouse")

    file_system is the ADLS Gen2 "container" - for OneLake this is the
    workspace name (or GUID); base_path is everything after it (typically
    the Lakehouse name, e.g. "MyLakehouse.Lakehouse").
    """
    parsed = urlparse(uri)
    if parsed.scheme != "abfss" or not parsed.hostname or not parsed.username:
        raise LakehouseSyncError(
            f"LAKEHOUSE_PATH must be an abfss:// URI like "
            f"'abfss://<workspace>@onelake.dfs.fabric.microsoft.com/<lakehouse>.Lakehouse', "
            f"got: {uri!r}"
        )
    account_url = f"https://{parsed.hostname}"
    file_system = parsed.username
    base_path = parsed.path.strip("/")
    return account_url, file_system, base_path


def _lakehouse_destination_path(base_path: str, when: datetime) -> str:
    """Build <base_path>/Files/supply_data/raw/VAN_data/YYYY/MM - a remote
    ADLS Gen2 directory path (forward-slash-joined string), not a local
    Path object. YYYY/MM come from `when` (the current date at write
    time, computed once by the caller) - not any date embedded in the
    CSV's own contents - per an explicit request.
    """
    parts = [p for p in (base_path, LAKEHOUSE_SUBPATH, when.strftime("%Y"), when.strftime("%m")) if p]
    return "/".join(parts)


def _get_directory_client(lakehouse_path: str, when: datetime):
    """Resolve (and create if needed) the destination directory client for
    this run's date. Imports azure-identity/azure-storage-file-datalake
    lazily so importing this module doesn't require them unless a sync is
    actually attempted.
    """
    try:
        from azure.core.exceptions import ResourceExistsError
        from azure.identity import DefaultAzureCredential
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as e:
        raise LakehouseSyncError(
            "azure-storage-file-datalake and azure-identity are required "
            "for Lakehouse sync - run: uv sync (they're in pyproject.toml's "
            "dependencies) or: uv add azure-storage-file-datalake azure-identity"
        ) from e

    account_url, file_system, base_path = _parse_lakehouse_uri(lakehouse_path)
    credential = DefaultAzureCredential()
    service_client = DataLakeServiceClient(account_url=account_url, credential=credential)
    file_system_client = service_client.get_file_system_client(file_system=file_system)
    dest_path = _lakehouse_destination_path(base_path, when)
    directory_client = file_system_client.get_directory_client(dest_path)
    try:
        directory_client.create_directory()
    except ResourceExistsError:
        pass
    return directory_client


def sync_to_lakehouse(csv_paths: list[Path], lakehouse_path: str | None = None) -> list[str]:
    """Upload each given CSV into the Lakehouse's dated Files folder in
    OneLake, via the ADLS Gen2 REST API, creating the folder if it doesn't
    exist yet. Returns the list of remote abfss:// paths actually written
    (skips - with a warning, not an error - any input path that doesn't
    exist locally).

    lakehouse_path defaults to the LAKEHOUSE_PATH environment variable (an
    abfss:// URI - see this module's docstring). Raises LakehouseSyncError
    if that's not set/usable, if authentication fails, or if the upload
    itself fails - this only ever supplements the local CSV export (which
    has already succeeded by the time this runs), so a caller that wants
    the overall pipeline to keep succeeding even when the Lakehouse is
    unreachable should catch this rather than let it propagate - see
    run()'s own handling in main.py for the pattern.
    """
    lakehouse_path = lakehouse_path or os.environ.get("LAKEHOUSE_PATH")
    if not lakehouse_path:
        raise LakehouseSyncError(
            "LAKEHOUSE_PATH environment variable is not set - cannot sync to the Lakehouse"
        )

    when = datetime.now()  # computed once - kept consistent across directory
    # resolution and every file's logged remote path this call, so a sync
    # can never straddle a month/year boundary mid-run and disagree with
    # itself about which folder it actually wrote to.

    try:
        directory_client = _get_directory_client(lakehouse_path, when)
    except LakehouseSyncError:
        raise
    except Exception as e:  # noqa: BLE001 - surface any auth/connection failure clearly
        raise LakehouseSyncError(
            f"Could not connect to the Lakehouse at {lakehouse_path}: {e}"
        ) from e

    written: list[str] = []
    for csv_path in csv_paths:
        csv_path = Path(csv_path)
        if not csv_path.exists():
            log.warning("Skipping %s - file does not exist", csv_path)
            continue
        try:
            file_client = directory_client.create_file(csv_path.name)
            data = csv_path.read_bytes()
            file_client.append_data(data, offset=0, length=len(data))
            file_client.flush_data(len(data))
        except Exception as e:  # noqa: BLE001 - surface any upload failure clearly
            raise LakehouseSyncError(
                f"Failed to upload {csv_path} to the Lakehouse: {e}"
            ) from e
        remote_path = f"{lakehouse_path.rstrip('/')}/{LAKEHOUSE_SUBPATH}/{when.strftime('%Y')}/{when.strftime('%m')}/{csv_path.name}"
        log.info("Synced %s to %s", csv_path, remote_path)
        written.append(remote_path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync CSV exports into a Microsoft Fabric Lakehouse in OneLake (ADLS Gen2)."
    )
    parser.add_argument(
        "--csv-dir",
        default=None,
        help="Directory of CSVs to sync (default: config.yaml's output.csv_dir)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (used only to resolve the default --csv-dir)",
    )
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir) if args.csv_dir else None
    if csv_dir is None:
        try:
            cfg = Config.load(args.config)
            csv_dir = Path(cfg.get("output.csv_dir", "./run_data/csv_exports"))
        except ConfigError:
            csv_dir = Path("./run_data/csv_exports")

    if not csv_dir.exists():
        log.error("CSV directory does not exist: %s", csv_dir)
        return 1

    csv_paths = sorted(csv_dir.glob("*.csv"))
    if not csv_paths:
        log.warning("No CSV files found in %s - nothing to sync", csv_dir)
        return 0

    try:
        written = sync_to_lakehouse(csv_paths)
    except LakehouseSyncError as e:
        log.error("Lakehouse sync failed: %s", e)
        return 1

    log.info("Synced %d file(s) to the Lakehouse", len(written))
    return 0


if __name__ == "__main__":
    sys.exit(main())
