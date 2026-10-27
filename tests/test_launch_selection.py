"""
Регрессионные тесты выбора «что скачать и что запустить» агентом.

Запуск без интернета и без pytest:
    .venv\\Scripts\\python.exe tests\\test_launch_selection.py

Если агент выбрал не тот файл для нового проекта — добавьте реальный список файлов его релиза
в tests/fixtures/release_assets.json с правильным ответом и поправьте правила в bot/runner_service.py.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Консоль Windows на серверах сборки — cp1252: без этого падает вывод русских названий проверок
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))

from bot import runner_service as rs  # noqa: E402

_results = []


def check(name, cond):
    _results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name)


def test_release_fixtures():
    """Реальные релизы популярных проектов: выбор совпадает с проверенным вручную."""
    data = json.loads((ROOT / "tests" / "fixtures" / "release_assets.json").read_text(encoding="utf-8"))
    for repo, case in data.items():
        ranked = rs.rank_release_assets(case["assets"])
        got = ranked[0] if ranked else None
        check(f"release {repo} -> {case['expected_first_choice']}", got == case["expected_first_choice"])


def test_asset_rules():
    s = rs.score_release_asset
    check("ARM через дефис отбрасывается на x64", (s("LocalSend-CLI-1.18.0-windows-arm-64.exe") or 0) <= 0)
    check("x86-64 через дефис распознаётся", s("LocalSend-1.18.2-windows-x86-64.exe") > (s("LocalSend-1.18.2-windows-arm-64.zip") or -999))
    check("Linux/macOS/Android не берутся", all(s(n) is None for n in ("app-linux-x64.zip", "app-macos.zip", "app-darwin-amd64.zip", "app.apk", "app-ubuntu-22.04-amd64.zip")))
    check("snapshot-сборка под Windows не путается со snap", s("app-snapshot-win64.zip") is not None)
    check("отладочные символы и исходники не берутся", all(s(n) is None for n in ("app-x64-pdb.zip", "app-symbols.zip", "app-src.zip")))
    check("portable важнее установщика", s("npp.8.9.portable.x64.zip") > s("npp.8.9.Installer.x64.exe"))
    check("версия с окном важнее CLI", s("LocalSend-1.18.2-windows-x86-64.exe") > s("LocalSend-CLI-1.18.2-windows-x86-64.exe"))
    check("32-битная через подчёркивание проигрывает", s("yt-dlp_x86.exe") < s("yt-dlp.exe"))
    check("самораспаковывающийся архив проигрывает установщику", s("PortableGit-2.56-64-bit.7z.exe") < s("Git-2.56-64-bit.exe"))
    check("китайская сборка проигрывает английской", s("Notepad4_zh-Hant_x64.zip") < s("Notepad4_en_x64.zip"))
    check("равные баллы: каноничное короткое имя", rs.rank_release_assets(["yt-dlp_extra_name.exe", "yt-dlp.exe"])[0] == "yt-dlp.exe")


def test_archives():
    import io
    import tarfile
    import zipfile
    from bot.platform_utils import extract_archive
    s = rs.score_release_asset
    check(".7z под Windows принимается", (s("tool-1.0-win-x64.7z") or 0) > 0)
    check("tar.gz без пометки Windows не берётся (это Linux/macOS)", s("tool-1.0-x86_64.tar.gz") is None)
    check("tar.gz с пометкой Windows принимается", (s("tool-1.0-windows-x86_64.tar.gz") or 0) > 0)
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        z = d / "ok.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("app/app.exe", "MZ")
        check("zip распаковывается", extract_archive(z, d / "out_zip") and (d / "out_zip" / "app" / "app.exe").exists())
        bad = d / "evil.zip"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.txt", "x")
        check("zip с путём ../ отклоняется", not extract_archive(bad, d / "out_bad") and not (d / "evil.txt").exists())
        t = d / "ok.tar.gz"
        with tarfile.open(t, "w:gz") as tf:
            data = b"MZ"
            info = tarfile.TarInfo("app.exe"); info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        check("tar.gz распаковывается", extract_archive(t, d / "out_tar") and (d / "out_tar" / "app.exe").exists())
        tb = d / "evil.tar"
        with tarfile.open(tb, "w") as tf:
            info = tarfile.TarInfo("../evil_tar.txt"); info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
        check("tar с путём ../ отклоняется", not extract_archive(tb, d / "out_tbad") and not (d / "evil_tar.txt").exists())
        try:
            import py7zr
            s7 = d / "ok.7z"
            with py7zr.SevenZipFile(s7, "w") as z7:
                z7.writestr("MZ", "app/app.exe")
            check("7z распаковывается", extract_archive(s7, d / "out_7z") and (d / "out_7z" / "app" / "app.exe").exists())
        except ImportError:
            check("py7zr установлен (pip install -r requirements.txt)", False)


def test_main_exe_ranking():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for n in ("BCUninstaller.exe", "BCU-console.exe"):
            (root / n).write_bytes(b"MZ" + b"\0" * 100)
        apps = [f for f in root.glob("*.exe") if not rs._has_marker(f.name, rs._JUNK_EXE_MARKERS + rs._INSTALLER_MARKERS, "Bulk-Crap-Uninstaller")]
        best = max(apps, key=lambda f: rs._rank_exe(f, root, "Bulk-Crap-Uninstaller"))
        check("BCU: главный BCUninstaller.exe, а не консольный и не «деинсталлятор»", best.name == "BCUninstaller.exe")
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        for n in ("createdump.exe", "pwsh.exe"):
            (root / n).write_bytes(b"MZ" + b"\0" * 100)
        (root / "pwsh.runtimeconfig.json").write_text("{}")
        cands = [f for f in root.glob("*.exe") if not rs._has_marker(f.name, rs._JUNK_EXE_MARKERS, "PowerShell")]
        best = max(cands, key=lambda f: rs._rank_exe(f, root, "PowerShell"))
        check("PowerShell: главный pwsh.exe (.NET runtimeconfig), а не createdump.exe", best.name == "pwsh.exe")
    check("служебное слово в названии проекта не делает файл мусором", not rs._has_marker("BCUninstaller.exe", rs._JUNK_EXE_MARKERS, "Bulk-Crap-Uninstaller"))
    check("настоящий деинсталлятор остаётся мусором", rs._has_marker("unins000.exe", rs._JUNK_EXE_MARKERS, "notepad4"))


def test_python_entry():
    def mk(files):
        d = Path(tempfile.mkdtemp())
        for rel, body in files.items():
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text(body, encoding="utf-8")
        return d
    lib = mk({"pyproject.toml": "[project]\nname='lib'\n", "docs/conf.py": "x=1", "tests/test_a.py": "x=1", "src/lib/__init__.py": ""})
    check("библиотека: docs/conf.py и тесты не запускаются", rs._python_entry(lib, "lib") is None)
    pkg = mk({"src/rich/__main__.py": "print(1)", "src/rich/__init__.py": ""})
    check("пакет с __main__.py -> python -m", rs._python_entry(pkg, "rich") == "-m rich")
    script = mk({"tool.py": "if __name__ == '__main__':\n    main()\n", "helpers.py": "def f(): pass\n"})
    check("единственный скрипт с __main__ в корне", rs._python_entry(script, "x") == "tool.py")
    cfg = mk({"setup.cfg": "[options.entry_points]\nconsole_scripts =\n    http = httpie.__main__:main\n"})
    check("консольная команда из setup.cfg", rs._pyproject_console_script(cfg, "cli") == "http")
    check("requires-python >=3.10,<3.13 не принимает 3.14", not rs.python_version_ok(">=3.10, <3.13", (3, 14)))
    check("requires-python >=3.10,<3.13 принимает 3.12", rs.python_version_ok(">=3.10, <3.13", (3, 12)))


def test_runtime_detection():
    d = rs.detect_required_runtime
    check("gradlew -> java", d(".\\gradlew.bat run") == "java")
    check("npm -> node", d("npm start") == "node")
    check("cargo -> rust", d("cargo run --release") == "rust")
    check("go -> go", d("go run ./cmd/gh") == "go")
    check("exe по пути с пробелом -> ничего", d("& 'C:\\Users\\Some User\\bat.exe'") is None)


def test_launch_funnel():
    launched = []
    orig = (rs.launch_in_terminal, rs.prepare_python_env, rs.is_runtime_available, rs.find_python_for_project)
    rs.launch_in_terminal = lambda cmd, cwd: launched.append(cmd) or True

    async def fake_prepare(path):
        return {"success": True}
    rs.prepare_python_env = fake_prepare
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "khoj"
            (p / "src" / "interface" / "android").mkdir(parents=True)
            (p / "src" / "interface" / "android" / "gradlew.bat").write_text("java -jar gradle.jar")
            (p / "pyproject.toml").write_text('[project]\nname="khoj"\nrequires-python=">=3.10,<3.13"\n[project.scripts]\nkhoj="khoj.main:run"\n')
            rs.find_python_for_project = lambda _p: None
            res = asyncio.run(rs.universal_windows_launch(p, "khoj"))
            check("khoj: сборщик Android не запускается, предлагается Python", not launched and res.get("runtime") == "python")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "gh"
            for c in ("gen-docs", "gh"):
                (p / "cmd" / c).mkdir(parents=True)
                (p / "cmd" / c / "main.go").write_text("package main")
            (p / "go.mod").write_text("module x")
            rs.is_runtime_available = lambda key: True
            launched.clear()
            asyncio.run(rs.universal_windows_launch(p, "cli"))
            check("go: главная команда cmd/gh, а не служебная cmd/gen-docs", launched == ["go run ./cmd/gh"])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "awesome"
            p.mkdir()
            (p / "readme.md").write_text("# list")
            launched.clear()
            res = asyncio.run(rs.universal_windows_launch(p, "awesome"))
            check("документация: честное «запускать нечего»", res.get("type") == "no_entrypoint")
    finally:
        rs.launch_in_terminal, rs.prepare_python_env, rs.is_runtime_available, rs.find_python_for_project = orig


if __name__ == "__main__":
    for fn in (test_release_fixtures, test_asset_rules, test_archives, test_main_exe_ranking, test_python_entry, test_runtime_detection, test_launch_funnel):
        fn()
    failed = [n for n, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    sys.exit(1 if failed else 0)
