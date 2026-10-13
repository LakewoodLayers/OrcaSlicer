#!/usr/bin/env python3
"""Import only saved Bambu LAN pairings into an isolated development data directory."""

import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile


def import_lan(source, destination):
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    target = destination / "OrcaSlicer.conf"
    if source == target.resolve() or source.parent == destination:
        raise ValueError("Source and development configuration must be separate.")

    original = source.read_bytes()
    settings = json.loads(original)
    machines = {}
    for device_id, saved in settings.get("local_machines", {}).items():
        if saved.get("printer_agent_id", "") not in ("", "bbl"):
            continue
        code = (saved.get("access_code") or settings.get("access_code", {}).get(device_id)
                or settings.get("user_access_code", {}).get(device_id))
        if not code or not saved.get("dev_ip"):
            continue
        machines[device_id] = {key: saved.get(key, "") for key in ("dev_name", "dev_ip", "printer_type")}
        machines[device_id].update(printer_agent_id="bbl", access_code=code)
    if not machines:
        raise ValueError("No saved Bambu LAN pairings with addresses and access codes were found.")

    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = json.loads(target.read_text()) if target.exists() else {}
    result.setdefault("local_machines", {}).update(machines)
    app = result.setdefault("app", {})
    source_app = settings.get("app", {})
    version = source_app.get("network_plugin_version", "")
    if not version and source_app.get("legacy_networking") in ("true", "1", True):
        version = "01.10.01.01"  # Linux legacy ABI, as declared in AppConfig.hpp.
    if version:
        app["network_plugin_version"] = version

    plugins = list((source.parent / "plugins").glob("libbambu_networking*.so"))
    # Orca's networking startup also checks the player entry point, even when
    # testing LAN communication. Include only the plugin's known support binaries.
    for name in ("libBambuSource.so", "liblive555.so", "libagora_rtc_sdk.so", "libagora-fdkaac.so"):
        support = source.parent / "plugins" / name
        if support.is_file():
            plugins.append(support)
    if plugins:
        plugin_dir = destination / "plugins"
        plugin_dir.mkdir(mode=0o700, exist_ok=True)
        for plugin in plugins:
            output = plugin_dir / plugin.name
            if output.resolve() == plugin.resolve():
                raise ValueError("Development plugin files must be separate from the source.")
            shutil.copyfile(plugin, output)
            output.chmod(0o600)
        app["installed_networking"] = "true"
        app.setdefault("cloud_providers", "orca")

    # Write only the development copy, atomically and with owner-only access.
    with tempfile.NamedTemporaryFile(mode="w", dir=destination, delete=False) as temporary:
        json.dump(result, temporary, indent=2)
        temporary.write("\n")
    os.replace(temporary.name, target)
    os.chmod(target, 0o600)

    if source.read_bytes() != original:
        raise RuntimeError("The source configuration changed during import; check the running main instance.")
    return len(machines), len(plugins)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Main OrcaSlicer.conf (read only)")
    parser.add_argument("destination", type=Path, help="Development --datadir directory")
    args = parser.parse_args()
    try:
        machines, plugins = import_lan(args.source, args.destination)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        parser.exit(1, "LAN import failed. Check the configuration format and separate development destination.\n")
    print(f"Imported {machines} LAN pairings and {plugins} networking libraries into development state.")


if __name__ == "__main__":
    main()
