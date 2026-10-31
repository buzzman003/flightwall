"""Build-time collection of dependency license notices and package inventory."""
from importlib import metadata
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig


output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
for dist in metadata.distributions():
    name = dist.metadata.get("Name", "unknown")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}-{dist.version}")
    package_dir = output / "python-packages" / safe_name
    package_dir.mkdir(parents=True, exist_ok=True)
    raw_metadata = dist.read_text("METADATA") or dist.read_text("PKG-INFO") or f"Name: {name}\nVersion: {dist.version}\n"
    (package_dir / "METADATA.txt").write_text(raw_metadata, encoding="utf-8")
    for item in dist.files or ():
        text = str(item).lower()
        basename = Path(text).name
        if not (basename.startswith(("license", "licence", "copying", "notice")) or "/licenses/" in text):
            continue
        source = Path(dist.locate_file(item))
        if source.is_file():
            destination = package_dir / re.sub(r"[^A-Za-z0-9_.-]", "_", str(item))
            shutil.copyfile(source, destination)

for source in (Path(sysconfig.get_path("stdlib")) / "LICENSE.txt", Path("/usr/local/LICENSE")):
    if source.is_file():
        shutil.copyfile(source, output / "PYTHON-LICENSE.txt")
        break
else:
    raise SystemExit("Python's license file was not found")

inventory = subprocess.check_output([sys.executable, "-m", "pip", "freeze", "--all"], text=True)
(output / "python-package-inventory.txt").write_text(inventory, encoding="utf-8")

# Retain the Debian copyright notices for system libraries copied by PyInstaller.
debian_packages = set()
for path in output.parent.rglob("*"):
    if not path.is_file() or output in path.parents:
        continue
    with path.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            continue
    result = subprocess.run(["dpkg-query", "-S", f"*/{path.name}"], capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if ": /" in line:
            debian_packages.add(line.split(": /", 1)[0])
for package in sorted(debian_packages):
    source = Path("/usr/share/doc") / package.split(":", 1)[0] / "copyright"
    if source.is_file():
        destination = output / "debian-packages" / package.replace(":", "_")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination / "copyright")
if debian_packages:
    result = subprocess.check_output(["dpkg-query", "-W", *sorted(debian_packages)], text=True)
    (output / "debian-package-inventory.txt").write_text(result, encoding="utf-8")
