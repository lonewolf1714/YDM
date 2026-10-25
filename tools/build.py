"""Build dist/YDM (the app folder) and dist/YDM-Setup.exe (the installer).

    pip install pyinstaller
    python tools/build.py
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST, WORK, PKG = ROOT / "dist", ROOT / "build", ROOT / "packaging"
sys.path.insert(0, str(ROOT))
from ydm.api import VERSION  # noqa: E402

VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}, 0), prodvers=({v}, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'YDM'), StringStruct('FileDescription', 'YDM Download Manager'),
    StringStruct('FileVersion', '{s}'), StringStruct('ProductName', 'YDM Download Manager'),
    StringStruct('ProductVersion', '{s}'), StringStruct('OriginalFilename', 'YDM.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def pyinstaller(*args):
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                    "--distpath", str(DIST), "--workpath", str(WORK), *args], check=True, cwd=ROOT)


def main():
    subprocess.run([sys.executable, str(ROOT / "tools" / "make_icons.py")], check=True)
    parts = (VERSION.split(".") + ["0", "0", "0"])[:3]
    (PKG / "version.txt").write_text(VERSION_INFO.format(v=", ".join(parts), s=VERSION), "utf-8")
    shutil.rmtree(DIST, ignore_errors=True)

    pyinstaller(str(PKG / "ydm.spec"))
    app = DIST / "YDM"
    shutil.copytree(ROOT / "extension", app / "extension")
    for f in ("README.md",):
        shutil.copy2(ROOT / f, app / f)

    pyinstaller("--onefile", "--windowed", "--name", f"YDM-Setup-{VERSION}", "--icon", str(PKG / "ydm.ico"),
                "--version-file", str(PKG / "version.txt"), "--paths", str(ROOT),
                "--add-data", f"{app}{';' if sys.platform == 'win32' else ':'}payload",
                "--specpath", str(WORK), str(ROOT / "setup_main.py"))
    setup = DIST / f"YDM-Setup-{VERSION}.exe"
    print(f"\nBuilt {app}\nBuilt {setup} ({setup.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
