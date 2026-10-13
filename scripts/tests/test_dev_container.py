"""Exercise development-container isolation without starting real containers."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DevelopmentContainerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "checkout with spaces"
        self.repo.mkdir()
        for relative in ("build_linux.sh", "scripts/linux.d/debian", "scripts/dev-container.sh",
                         "scripts/dev-container/Containerfile", "scripts/dev-container/desktop.sh",
                         "scripts/dev-container/podman"):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        self.state = self.repo / "build-dev-state"
        self.log = self.root / "commands.jsonl"
        tools = self.root / "tools"
        tools.mkdir()
        podman = tools / "podman"
        podman.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
root = None
tmpdir = None
while args and args[0].startswith(("--root=", "--tmpdir=")):
    option, value = args.pop(0).split("=", 1)
    if option == "--root":
        root = value
    else:
        tmpdir = value
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps({"root": root, "tmpdir": tmpdir, "args": args}) + "\\n")
if args[0] == "info":
    print(os.environ.get("MOCK_ROOTLESS", "true"))
elif args[:2] == ["container", "exists"]:
    sys.exit(0 if os.environ.get("MOCK_CONTAINER_EXISTS") == "1" else 1)
elif args[:2] == ["image", "inspect"]:
    sys.exit(1)
elif args[0] == "inspect":
    print(os.environ.get("MOCK_RUNNING", "true"))
elif args[0] in ("run", "build"):
    if "-i" in args or "-" in args:
        sys.stdin.read()
    print("test-container")
''')
        podman.chmod(0o755)
        self.env = dict(os.environ, PATH=f"{tools}:{os.environ['PATH']}", COMMAND_LOG=str(self.log),
                        ORCA_DEV_MEMORY="4g")

    def run_launcher(self, *args, expected=0, **env):
        result = subprocess.run(["bash", str(self.repo / "scripts/dev-container.sh"), *args],
                                env=dict(self.env, **env), text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def command(self, name):
        return next(command for command in self.commands() if command["args"][0] == name)

    def package(self):
        binary = self.state / "workspace/build/package/bin/orca-slicer"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        launcher = binary.parent.parent / "orca-slicer"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)

    def test_refuses_rootful_podman(self):
        self.run_launcher("image", expected=1, MOCK_ROOTLESS="false")
        self.assertFalse(self.state.exists())

    def test_missing_binary_never_starts_a_container(self):
        self.run_launcher("start", expected=1)
        self.assertFalse(any(command["args"][0] == "run" for command in self.commands()))

    def test_missing_package_launcher_never_starts_a_container(self):
        self.package()
        (self.state / "workspace/build/package/orca-slicer").unlink()
        self.run_launcher("start", expected=1)
        self.assertFalse(any(command["args"][0] == "run" for command in self.commands()))

    def test_runtime_mounts_only_development_artifacts(self):
        self.package()
        self.run_launcher("start")
        run = self.command("run")["args"]
        self.assertEqual(self.command("run")["root"], str(self.state / "podman-storage"))
        self.assertEqual(self.command("run")["tmpdir"], str(self.state / "podman-tmp"))
        mounts = [run[index + 1] for index, arg in enumerate(run) if arg == "-v"]
        self.assertEqual(len(mounts), 5)
        for mount in mounts:
            self.assertTrue(mount.startswith(str(self.state) + "/"), mount)
        self.assertIn(f"{self.state}/workspace/build/package:/opt/orca:ro", mounts)
        for flag in ("--userns=keep-id", "--read-only", "--network=host", "--cap-drop=all"):
            self.assertIn(flag, run)
        self.assertNotIn("--privileged", run)
        self.assertNotIn("--pid=host", run)
        self.assertNotIn("--ipc=host", run)

    def test_failed_desktop_is_reported(self):
        self.package()
        result = self.run_launcher("start", expected=1, MOCK_RUNNING="false")
        self.assertIn("Development desktop exited", result.stderr)
        self.assertNotIn("Desktop: http", result.stdout)

    def test_start_cannot_race_a_build(self):
        self.package()
        with (self.state / "build.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.run_launcher("start", expected=1)
        self.assertFalse(any(command["args"][0] == "run" for command in self.commands()))

    def test_build_cannot_replace_a_running_application(self):
        self.run_launcher("build", expected=1, MOCK_CONTAINER_EXISTS="1")
        self.assertFalse((self.state / "workspace").exists())

    def test_build_resumes_dependencies_when_only_the_cache_exists(self):
        deps = self.state / "workspace/deps/build"
        deps.mkdir(parents=True)
        (deps / "CMakeCache.txt").write_text("incomplete dependency build")
        self.run_launcher("build")
        run = self.command("run")["args"]
        self.assertIn("-d", run)

    def test_source_sync_preserves_outputs_and_excludes_private_state(self):
        (self.repo / "version.inc").write_text("original version")
        (self.repo / ".env").write_text("private configuration")
        workspace = self.state / "workspace"
        (workspace / "build").mkdir(parents=True)
        (workspace / "build/object").write_text("cached build")
        (workspace / "deleted-source.cpp").write_text("obsolete")
        self.run_launcher("image")
        self.assertEqual((self.repo / "version.inc").read_text(), "original version")
        self.assertEqual((workspace / "build/object").read_text(), "cached build")
        self.assertFalse((workspace / "deleted-source.cpp").exists())
        self.assertFalse((workspace / ".env").exists())
        self.assertFalse((workspace / "build-dev-state").exists())
        run = self.command("run")["args"]
        self.assertEqual(self.command("run")["root"], str(self.state / "podman-storage"))
        self.assertEqual(self.command("run")["tmpdir"], str(self.state / "podman-tmp"))
        self.assertIn("--userns=keep-id", run)
        self.assertIn("--user=0", run)
        self.assertIn("--memory=4g", run)

    def test_stop_preserves_state_and_targets_only_this_checkout(self):
        self.state.mkdir()
        marker = self.state / "settings"
        marker.write_text("retained")
        self.run_launcher("stop", MOCK_CONTAINER_EXISTS="1")
        stop = self.command("stop")["args"]
        remove = self.command("rm")["args"]
        self.assertEqual(stop[1], remove[1])
        self.assertTrue(stop[1].startswith("orca-dev-"))
        self.assertEqual(marker.read_text(), "retained")


if __name__ == "__main__":
    unittest.main()
