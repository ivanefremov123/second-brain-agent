# Сборка агента в папку SecondBrainAgent/ (onedir: меньше ложных срабатываний антивирусов, чем onefile)
#   pyinstaller --noconfirm packaging/SecondBrainAgent.spec
from PyInstaller.utils.hooks import collect_submodules

ROOT = SPECPATH + "/.."

a = Analysis(
    [ROOT + "/win_agent.py"],
    pathex=[ROOT],
    # py7zr подключается только при распаковке .7z, поэтому PyInstaller не видит его сам
    hiddenimports=collect_submodules("py7zr"),
    excludes=["google", "aiogram", "apscheduler", "aiosqlite", "pydantic", "qrcode", "PIL", "tkinter.test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SecondBrainAgent",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SecondBrainAgent")
