"""Focused checks for the release source path and bundled runtime inputs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "Runtime"))

import release_build
from goblin_core.cua import MacHelper


def run_git(root, *arguments):
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True)


def make_repository(base):
    repo = base / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Test")
    (repo / "Runtime").mkdir()
    (repo / "Runtime" / "goblin_bridge.py").write_text('print("bridge")\n', encoding="utf-8")
    (repo / "product.json").write_text('{"name": "Goblin", "version": "0.1.0"}\n', encoding="utf-8")
    (repo / ".gitignore").write_text(".build/\ndist/\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    return repo


def make_fake_python_runtime(root):
    runtime = root / "Python"
    keep = runtime / "lib" / "python3.14" / "site-packages" / "keep.txt"
    keep.parent.mkdir(parents=True)
    keep.write_text("keep\n", encoding="utf-8")
    for relative in (
        "bin/pip",
        "bin/idle3",
        "lib/libtcl9.0.dylib",
        "lib/libtcl9tk9.0.dylib",
        "lib/tcl9.0",
        "lib/tk9.0",
        "lib/itcl4.3.8",
        "lib/thread3.0.6",
        "lib/python3.14/site-packages/pip",
        "lib/python3.14/site-packages/pip-26.2.1.dist-info",
        "lib/python3.14/site-packages/README.txt",
        "lib/python3.14/tkinter",
        "lib/python3.14/idlelib",
        "lib/python3.14/config-3.14-darwin",
        "lib/python3.14/lib-dynload/_tkinter.cpython-314-darwin.so",
    ):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("artifact\n", encoding="utf-8")
    return runtime


class ReleaseSourceTests(unittest.TestCase):
    def test_dirty_development_build_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = make_repository(Path(temp))
            (repo / "Runtime" / "goblin_bridge.py").write_text('print("changed")\n', encoding="utf-8")
            (repo / "new-file.txt").write_text("untracked\n", encoding="utf-8")
            source = release_build.prepare_build_source(repo, False)
            self.assertFalse(source.release)
            self.assertEqual(source.root, repo.resolve())
            self.assertIsNone(source.archive)

    def test_release_refuses_a_dirty_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = make_repository(base)
            (repo / "Runtime" / "goblin_bridge.py").write_text('print("changed")\n', encoding="utf-8")
            with self.assertRaises(release_build.ReleaseBuildError) as caught:
                release_build.prepare_build_source(repo, True, base / "source.tar.gz")
            self.assertIn("without --release", str(caught.exception))

    def test_release_exports_only_tracked_files_and_verifies_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = make_repository(base)
            archive = base / "dist" / "goblin-source.tar.gz"
            source = release_build.prepare_build_source(repo, True, archive)
            (repo / "untracked-secret.txt").write_text("do not ship\n", encoding="utf-8")
            release_build.export_tracked_source(repo, source.root, archive)
            self.assertTrue(source.release)
            self.assertEqual(len(source.commit), 40)
            self.assertTrue(source.archive.is_file())
            self.assertTrue((source.root / "Runtime" / "goblin_bridge.py").is_file())
            self.assertFalse((source.root / "untracked-secret.txt").exists())
            self.assertFalse((source.root / ".git").exists())
            count = release_build.verify_export_identity(repo, source.root)
            self.assertEqual(count, 3)
            (source.root / "Runtime" / "goblin_bridge.py").write_text('print("tampered")\n', encoding="utf-8")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.verify_export_identity(repo, source.root)


class RuntimeInputTests(unittest.TestCase):
    def make_runtime_root(self, base):
        root = base / "source"
        runtime = root / "Runtime"
        (runtime / "goblin_core" / "native" / "mac").mkdir(parents=True)
        (runtime / "goblin_core" / "__pycache__").mkdir()
        (runtime / "goblin_bridge.py").write_text("bridge\n", encoding="utf-8")
        (runtime / "goblin_core" / "__init__.py").write_text("core\n", encoding="utf-8")
        (runtime / "goblin_core" / "native" / "desk_input.py").write_text("desk\n", encoding="utf-8")
        (runtime / "goblin_core" / "native" / "mac" / "GoblinCUA.swift").write_text("// helper\n", encoding="utf-8")
        (runtime / "goblin_core" / "__pycache__" / "cache.pyc").write_text("cache\n", encoding="utf-8")
        (runtime / "BRIDGE.md").write_text("protocol\n", encoding="utf-8")
        baseline = runtime / "goblin_core" / "baseline_skills" / "fixture"
        (baseline / "references").mkdir(parents=True)
        (baseline / "scripts").mkdir()
        (baseline / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        (baseline.parent / "SOURCE.json").write_text(json.dumps({"baseline_core": ["fixture"]}), encoding="utf-8")
        (baseline / "references" / "guide.md").write_text("linked reference\n", encoding="utf-8")
        (baseline / "scripts" / "helper.py").write_text("helper\n", encoding="utf-8")
        (baseline / "scripts" / "helper.mjs").write_text("javascript helper\n", encoding="utf-8")
        (baseline / ".DS_Store").write_bytes(b"cache")
        for name in release_build.RUNTIME_DOCUMENTS:
            (runtime / name).write_text(name + "\n", encoding="utf-8")
        license_root = runtime / "THIRD_PARTY_LICENSES"
        license_root.mkdir()
        (license_root / "component.txt").write_text("component license\n", encoding="utf-8")
        return root

    def test_runtime_plan_excludes_developer_and_cache_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            relative = {path.relative_to(root).as_posix() for path in release_build.runtime_input_paths(root)}
            self.assertIn("Runtime/goblin_bridge.py", relative)
            self.assertIn("Runtime/goblin_core/native/desk_input.py", relative)
            self.assertIn("Runtime/THIRD_PARTY_NOTICES", relative)
            self.assertIn("Runtime/THIRD_PARTY_LICENSES/component.txt", relative)
            self.assertNotIn("Runtime/BRIDGE.md", relative)
            self.assertNotIn("Runtime/goblin_core/native/mac/GoblinCUA.swift", relative)
            self.assertFalse(any("__pycache__" in name for name in relative))
            resources = Path(temp) / "app" / "Contents" / "Resources"
            copied = release_build.copy_runtime_inputs(root, resources)
            self.assertEqual(len(copied), len(relative))
            self.assertTrue((resources / "Runtime" / "goblin_bridge.py").is_file())
            self.assertFalse((resources / "Runtime" / "BRIDGE.md").exists())
            self.assertFalse((resources / "Runtime" / "goblin_core" / "native" / "mac").exists())

    def test_bundle_keeps_skill_data_and_linked_scripts_exactly_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            paths = release_build.runtime_input_paths(root)
            self.assertEqual(len(paths), len(set(paths)))
            resources = Path(temp) / "app" / "Contents" / "Resources"
            release_build.copy_runtime_inputs(root, resources)
            baseline = Path("Runtime/goblin_core/baseline_skills/fixture")
            for name in ("SKILL.md", "references/guide.md", "scripts/helper.py", "scripts/helper.mjs"):
                relative = baseline / name
                self.assertEqual((resources / relative).read_bytes(), (root / relative).read_bytes())
            self.assertFalse((resources / baseline / ".DS_Store").exists())

    def test_missing_bundled_skills_fails_the_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            import shutil
            shutil.rmtree(root / "Runtime" / "goblin_core" / "baseline_skills")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.runtime_input_paths(root)

    def test_every_bundled_procedure_and_reference_reaches_fresh_runtime_unchanged(self):
        baseline = ROOT / "Runtime" / "goblin_core" / "baseline_skills"
        skills = list(baseline.glob("*/SKILL.md"))
        self.assertEqual(len(skills), 53)
        with tempfile.TemporaryDirectory() as temp:
            resources = Path(temp) / "Resources"
            copied = release_build.copy_runtime_inputs(ROOT, resources)
            self.assertEqual(len(copied), len(set(copied)))
            for path in baseline.rglob("*"):
                if path.is_file():
                    relative = path.relative_to(ROOT)
                    self.assertEqual(path.read_bytes(), (resources / relative).read_bytes(), str(relative))

    def test_deleted_declared_core_skill_cannot_ship(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            skill = root / "Runtime/goblin_core/baseline_skills/fixture/SKILL.md"
            skill.unlink()
            with self.assertRaisesRegex(release_build.ReleaseBuildError, "Required baseline skill is missing"):
                release_build.runtime_input_paths(root)

    def test_missing_core_manifest_cannot_ship(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            manifest = root / "Runtime/goblin_core/baseline_skills/SOURCE.json"
            manifest.unlink()
            with self.assertRaisesRegex(release_build.ReleaseBuildError, "Bundled skill manifest is unavailable"):
                release_build.runtime_input_paths(root)

    def test_missing_license_directory_fails_the_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            licenses = root / "Runtime" / "THIRD_PARTY_LICENSES"
            (licenses / "component.txt").unlink()
            licenses.rmdir()
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.runtime_input_paths(root)

    def test_missing_notice_fails_the_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_runtime_root(Path(temp))
            (root / "Runtime" / "THIRD_PARTY_NOTICES").unlink()
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.runtime_input_paths(root)


class PythonRuntimeTests(unittest.TestCase):
    def test_prune_removes_pip_tk_and_idle_but_keeps_stdlib(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = make_fake_python_runtime(Path(temp))
            removed = release_build.prune_python_runtime(runtime)
            self.assertIn("lib/python3.14/site-packages/pip", removed)
            self.assertIn("lib/python3.14/tkinter", removed)
            self.assertTrue((runtime / "lib" / "python3.14" / "site-packages" / "keep.txt").is_file())
            self.assertFalse((runtime / "lib" / "libtcl9.0.dylib").exists())
            self.assertFalse((runtime / "lib" / "python3.14" / "lib-dynload" / "_tkinter.cpython-314-darwin.so").exists())

    def test_scrub_rewrites_sysconfig_prefix_to_runtime_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "Python"
            data_file = runtime / "lib" / "python3.14" / "_sysconfigdata__darwin_darwin.py"
            data_file.parent.mkdir(parents=True)
            build_prefix = os.path.join("/", "Users", "example", "build", "python")
            upstream_source = os.path.join("/", "private", "tmp", "upstream", "Python")
            data_file.write_text(
                "# system configuration generated and used by the sysconfig module\n"
                "build_time_vars = {\n"
                '    "BINDIR": "' + build_prefix + '/bin",\n'
                '    "CONFIG_ARGS": "--prefix=' + build_prefix + ' --with-openssl=' + upstream_source + '",\n'
                '    "prefix": "' + build_prefix + '",\n'
                "}\n",
                encoding="utf-8",
            )
            release_build.scrub_python_relocation(runtime, Path(build_prefix))
            text = data_file.read_text(encoding="utf-8")
            self.assertNotIn(build_prefix, text)
            self.assertNotIn(upstream_source, text)
            self.assertIn("_INSTALL_PREFIX = _sys.base_prefix", text)
            namespace = {}
            exec(text, namespace)
            variables = namespace["build_time_vars"]
            self.assertEqual(variables["BINDIR"], sys.base_prefix + "/bin")
            self.assertNotIn("@PREFIX@", variables["CONFIG_ARGS"])
            self.assertIn("/install", variables["CONFIG_ARGS"])

    def test_private_path_gate_detects_source_machine_path(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "Python"
            data_file = runtime / "lib" / "python3.14" / "data.py"
            data_file.parent.mkdir(parents=True)
            data_file.write_text("root=" + os.path.join("/", "Users", "example") + "\n", encoding="utf-8")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.verify_no_private_paths(runtime, Path(temp), runtime)
            data_file.write_text("root=/install\n", encoding="utf-8")
            release_build.verify_no_private_paths(runtime, Path(temp), runtime)


    def test_private_path_gate_checks_opaque_binary_content(self):
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "app"
            artifact.mkdir()
            binary = artifact / "executable"
            private_path = os.path.join("/", "Users", "example", "build").encode("utf-8")
            binary.write_bytes(b"\xcf\xfa\xed\xfe" + private_path + b"\x00")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.verify_no_private_paths(artifact, Path(temp) / "source", Path(temp) / "python")
            binary.write_bytes(b"\xcf\xfa\xed\xfe./Sources/Companion.swift\x00")
            release_build.verify_no_private_paths(artifact, Path(temp) / "source", Path(temp) / "python")


class NoticeTests(unittest.TestCase):
    def test_notice_versions_match_the_runtime_contract(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        release_build.verify_notice_versions(notice, {
            "python": "3.14.8",
            "openssl": "OpenSSL 3.5.9 29 Sep 2026",
            "sqlite": "3.53.1",
        })
        with self.assertRaises(release_build.ReleaseBuildError):
            release_build.verify_notice_versions(notice, {
                "python": "3.14.8",
                "openssl": "OpenSSL 3.4.0 1 Jan 2026",
                "sqlite": "3.53.1",
            })
        with self.assertRaises(release_build.ReleaseBuildError):
            release_build.verify_notice_versions(notice, {
                "python": "3.14.7",
                "openssl": "OpenSSL 3.5.9 29 Sep 2026",
                "sqlite": "3.53.1",
            })

    def test_notice_names_the_bundled_components(self):
        notice = (ROOT / "Runtime" / "THIRD_PARTY_NOTICES").read_text(encoding="utf-8")
        for name in ("CPython 3.14.8", "OpenSSL 3.5.9", "SQLite 3.53.1", "Tcl/Tk", "pip", "idle"):
            self.assertIn(name, notice)


class MacHelperTests(unittest.TestCase):
    def fake_config(self, app):
        settings = SimpleNamespace(mac_helper_app=str(app), mac_bundle_id="com.example.look",
                                   timeout=5.0, max_image_px=100, sips="/usr/bin/sips")
        return SimpleNamespace(home=app.parent, cua=settings)

    def test_missing_helper_is_reported_without_writing_the_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "GoblinMacLook.app"
            helper = MacHelper(self.fake_config(app))
            with self.assertRaises(RuntimeError) as caught:
                helper.require_bundled()
            self.assertIn("never compiled at runtime", str(caught.exception))
            self.assertFalse((app / "Contents").exists())

    def test_prebuilt_helper_runs_without_modifying_the_app_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "GoblinMacLook.app"
            executable = app / "Contents" / "MacOS" / "GoblinMacLook"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"prebuilt")
            executable.chmod(0o755)
            info = app / "Contents" / "Info.plist"
            info.write_bytes(b"manifest-owned")
            before = {path: path.read_bytes() for path in app.rglob("*") if path.is_file()}
            commands = []

            def fake_run(arguments, **kwargs):
                commands.append(arguments)
                output = Path(arguments[arguments.index("--args") + 1])
                output.write_text(json.dumps({"ok": True, "status": "ready"}), encoding="utf-8")
                return subprocess.CompletedProcess(arguments, 0, "", "")

            helper = MacHelper(self.fake_config(app))
            with mock.patch("goblin_core.cua.subprocess.run", side_effect=fake_run):
                result = helper.run("status")
            self.assertEqual(result, {"ok": True, "status": "ready"})
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0][0], "/usr/bin/open")
            after = {path: path.read_bytes() for path in app.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertFalse(hasattr(helper, "ensure_built"))

    def test_cua_source_has_no_runtime_compiler_path(self):
        source = (ROOT / "Runtime" / "goblin_core" / "cua.py").read_text(encoding="utf-8")
        for word in ("swiftc", "codesign", "ensure_built"):
            self.assertNotIn(word, source)


class NamingTests(unittest.TestCase):
    def test_no_tessera_cua_user_strings_remain(self):
        paths = [
            ROOT / "Runtime" / "goblin_core" / "cua.py",
            ROOT / "Runtime" / "goblin_core" / "native_cua.py",
            ROOT / "Runtime" / "goblin_core" / "host_cua.py",
            ROOT / "Runtime" / "goblin_core" / "native" / "mac" / "GoblinCUA.swift",
            ROOT / "Runtime" / "goblin_core" / "native" / "mac" / "GoblinMacLook.swift",
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("TesseraCUA", text, str(path))
            self.assertNotIn("tessera-cua", text, str(path))

    def test_manifest_points_to_the_renamed_helpers(self):
        product = json.loads((ROOT / "product.json").read_text(encoding="utf-8"))
        computer = product["native_helpers"]["computer_use"]["source"]
        look = product["native_helpers"]["mac_look"]["source"]
        self.assertEqual(computer, "goblin_core/native/mac/GoblinCUA.swift")
        self.assertEqual(look, "goblin_core/native/mac/GoblinMacLook.swift")
        self.assertTrue((ROOT / "Runtime" / computer).is_file())
        self.assertTrue((ROOT / "Runtime" / look).is_file())

    def test_no_upstream_project_naming_remains(self):
        for root in (ROOT / "Runtime", ROOT / "Sources", ROOT / "Resources"):
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in (".py", ".swift", ".md", ".json", ".txt", ""):
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("Tessera", text, str(path))
                self.assertNotIn("tessera", text, str(path))


class BundleIdentityTests(unittest.TestCase):
    def make_source_and_bundle(self, base):
        source = base / "source"
        runtime = source / "Runtime"
        runtime.mkdir(parents=True)
        (runtime / "goblin_bridge.py").write_text("bridge\n", encoding="utf-8")
        (runtime / "goblin_core").mkdir()
        (runtime / "goblin_core" / "__init__.py").write_text("core\n", encoding="utf-8")
        baseline = runtime / "goblin_core" / "baseline_skills" / "fixture"
        baseline.mkdir(parents=True)
        (baseline / "SKILL.md").write_text("skill body\n", encoding="utf-8")
        (baseline.parent / "SOURCE.json").write_text(json.dumps({"baseline_core": ["fixture"]}), encoding="utf-8")
        for name in release_build.RUNTIME_DOCUMENTS:
            (runtime / name).write_text(name + "\n", encoding="utf-8")
        license_root = runtime / "THIRD_PARTY_LICENSES"
        license_root.mkdir()
        (license_root / "component.txt").write_text("component license\n", encoding="utf-8")
        resources = source / "Resources" / "Avatars"
        resources.mkdir(parents=True)
        (resources / "rookie.png").write_bytes(b"png")
        (source / "product.json").write_text('{"name": "Goblin"}\n', encoding="utf-8")
        app = base / "Goblin.app"
        target_runtime = app / "Contents" / "Resources" / "Runtime"
        target_runtime.mkdir(parents=True)
        for path in runtime.rglob("*"):
            if path.is_file():
                target = target_runtime / path.relative_to(runtime)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        target_art = app / "Contents" / "Resources" / "Avatars"
        target_art.mkdir(parents=True)
        (target_art / "rookie.png").write_bytes(b"png")
        (app / "Contents" / "Resources" / "product.json").write_text('{"name": "Goblin"}\n', encoding="utf-8")
        return source, app

    def test_bundle_source_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            source, app = self.make_source_and_bundle(Path(temp))
            count = release_build.verify_bundle_sources(source, app)
            self.assertGreater(count, 0)
            (app / "Contents" / "Resources" / "Runtime" / "goblin_bridge.py").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(release_build.ReleaseBuildError):
                release_build.verify_bundle_sources(source, app)


if __name__ == "__main__":
    unittest.main()
