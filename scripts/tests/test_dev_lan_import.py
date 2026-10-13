"""Verify LAN imports preserve the main installation and exclude cloud credentials."""

import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "dev_lan_import", Path(__file__).resolve().parents[1] / "dev-container/import_lan.py")
IMPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORTER)


class LanImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.main = self.root / "main"
        self.main.mkdir()
        self.source = self.main / "OrcaSlicer.conf"
        self.destination = self.root / "dev"
        self.document = {
            "app": {"cloud_token": "private-cloud-token", "legacy_networking": "true"},
            "access_code": {"legacy": "legacy-code"},
            "user_access_code": {"fallback": "fallback-code"},
            "local_machines": {
                "legacy": {"dev_name": "Printer", "dev_ip": "192.0.2.1", "printer_type": "N2S"},
                "fallback": {"dev_ip": "192.0.2.2"},
                "other": {"dev_ip": "192.0.2.3", "printer_agent_id": "moonraker", "access_code": "other-code"},
            },
        }

    def save_source(self):
        self.source.write_text(json.dumps(self.document))
        return self.source.read_bytes()

    def test_imports_legacy_codes_without_cloud_state_or_source_changes(self):
        original = self.save_source()
        self.assertEqual(IMPORTER.import_lan(self.source, self.destination), (2, 0))
        target = self.destination / "OrcaSlicer.conf"
        result = json.loads(target.read_text())
        self.assertEqual(set(result), {"local_machines", "app"})
        self.assertEqual(result["app"], {"network_plugin_version": "01.10.01.01"})
        self.assertEqual(set(result["local_machines"]), {"legacy", "fallback"})
        self.assertEqual(result["local_machines"]["legacy"]["access_code"], "legacy-code")
        self.assertEqual(result["local_machines"]["fallback"]["access_code"], "fallback-code")
        self.assertEqual(result["local_machines"]["legacy"]["printer_agent_id"], "bbl")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(self.source.read_bytes(), original)

    def test_keeps_existing_development_settings_and_prefers_scoped_code(self):
        self.document["local_machines"]["legacy"].update(printer_agent_id="bbl", access_code="scoped")
        self.save_source()
        self.destination.mkdir()
        target = self.destination / "OrcaSlicer.conf"
        target.write_text(json.dumps({"app": {"single_instance": "false"}}))
        IMPORTER.import_lan(self.source, self.destination)
        result = json.loads(target.read_text())
        self.assertEqual(result["app"]["single_instance"], "false")
        self.assertEqual(result["local_machines"]["legacy"]["access_code"], "scoped")

    def test_copies_only_networking_binaries(self):
        self.save_source()
        plugins = self.main / "plugins"
        plugins.mkdir()
        (plugins / "libbambu_networking.so").write_bytes(b"test library")
        (plugins / "tokens.json").write_text("private")
        self.assertEqual(IMPORTER.import_lan(self.source, self.destination), (2, 1))
        self.assertEqual(list((self.destination / "plugins").iterdir()),
                         [self.destination / "plugins/libbambu_networking.so"])
        self.assertEqual((plugins / "tokens.json").read_text(), "private")
        settings = json.loads((self.destination / "OrcaSlicer.conf").read_text())
        self.assertEqual(settings["app"]["installed_networking"], "true")
        self.assertEqual(settings["app"]["cloud_providers"], "orca")

    def test_copies_required_player_libraries_without_unrelated_files(self):
        self.save_source()
        plugins = self.main / "plugins"
        plugins.mkdir()
        for name in ("libbambu_networking.so", "libBambuSource.so", "liblive555.so"):
            (plugins / name).write_bytes(b"test library")
        (plugins / "unrelated.so").write_bytes(b"unrelated")
        self.assertEqual(IMPORTER.import_lan(self.source, self.destination), (2, 3))
        self.assertFalse((self.destination / "plugins/unrelated.so").exists())

    def test_refuses_the_main_directory_as_destination(self):
        original = self.save_source()
        with self.assertRaises(ValueError):
            IMPORTER.import_lan(self.source, self.main)
        self.assertEqual(self.source.read_bytes(), original)

    def test_missing_pairing_data_does_not_create_development_state(self):
        self.document["local_machines"] = {}
        self.save_source()
        with self.assertRaises(ValueError):
            IMPORTER.import_lan(self.source, self.destination)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
