"""Build a source-only release archive from an explicit allowlist."""
from pathlib import Path
import hashlib
import zipfile

root = Path(__file__).resolve().parents[1]
version = (root / "VERSION").read_text(encoding="utf-8").strip()
if version != "2.0.0":
    raise SystemExit("Review the release version before building")
files = [".gitignore", ".gitattributes", "VERSION", "README.md", "CHANGELOG.md", "UPGRADING.md",
         "VALIDATION.md", "HOST_LEARNING_POLICY.md", "PROACTIVE_WORKFLOW.md", "RELEASE_NOTES.md",
         "requirements.txt", "requirements.lock.txt", "server.py", "models.py", "db_manager.py", "learning.py",
         "tools/build_release.py", ".github/workflows/release.yml"]
files += sorted(path.relative_to(root).as_posix() for path in (root / "tests").glob("test_*.py"))
output = root / "dist"
output.mkdir(exist_ok=True)
archive = output / f"personal-knowledge-mcp-v{version}.zip"
with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
    for name in files:
        data = (root / name).read_bytes()
        info = zipfile.ZipInfo(f"personal-knowledge-mcp-v{version}/{name}", date_time=(2026, 11, 6, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, data)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(output / f"personal-knowledge-mcp-v{version}-SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
print(f"Built {archive.name}: {len(files)} source files, sha256={digest}")
