import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
RELEASE_NAME: str = "bearpond-server"
# what the server is made of, with the PostgreSQL driver, so that one release runs on SQLite and on PostgreSQL alike
PACKAGE_SPECS: list[str] = ["packages/protocol", "packages/auth", "packages/server[postgres]"]
LICENSE_NAMES: list[str] = ["LICENSE", "NOTICE"]
# what the release's Python says about itself, asked of that Python rather than of the one that runs this script
PYTHON_INFO_CODE: str = (
    "import platform, sys, sysconfig; "
    "print(platform.python_version()); "
    "print(f'cp{sys.version_info.major}{sys.version_info.minor}'); "
    "print(sysconfig.get_platform())"
)
INSTALL_TEXT: str = """\
Installing this release by hand, with the Python that BUILD-INFO names (YardDog's deploy.sh does the same):

    python3 -m venv venv
    venv/bin/pip install --no-index --find-links wheels --require-hashes -r requirements.txt
    venv/bin/bearpond-server serve --config-dir /path/to/config --port 8000

Nothing is downloaded: every wheel the server needs is in wheels/, and requirements.txt pins each one by its
hash.
"""


# ======================================================================================================================
class PythonInfo(NamedTuple):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    version: str
    tag: str
    platform: str


# ======================================================================================================================
class WheelFile(NamedTuple):

    # ==================================================================================================================
    # CLASS
    # ==================================================================================================================

    # ==================================================================================================================
    # INSTANCE
    # ==================================================================================================================
    project_name: str
    version: str
    path: Path


# ----------------------------------------------------------------------------------------------------------------------
def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Build the release of the BearPond server: one archive with every wheel it needs, for one Python "
        "on one platform"
    )
    parser.add_argument(
        "--python", type=Path, default=Path(sys.executable), help="the Python the release is for (default: this one)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "dist" / "release", help="where the archive is written"
    )
    parser.add_argument(
        "--package-repository",
        type=Path,
        default=None,
        help="a YardDog directory package repository that the archive is also put in, as "
        f"{RELEASE_NAME}/<version>/{RELEASE_NAME}-<version>.tgz",
    )
    args: argparse.Namespace = parser.parse_args()

    archive_path: Path = build_release(args.python, args.output_dir.resolve())
    print(f"Built {archive_path}")
    if args.package_repository is not None:
        published_path: Path = publish_release(archive_path, args.package_repository.resolve())
        print(f"Put it in the package repository as {published_path}")


# ----------------------------------------------------------------------------------------------------------------------
def build_release(python_path: Path, output_dir: Path) -> Path:
    # the version is the server package's, stamped with the time of the build, so that every build is a release of its
    # own; the commit is read before building, since building leaves files in the packages' folders
    server_version: str = read_server_version()
    stamp: str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    release_version: str = f"{server_version}-{stamp}"
    git_commit: str = run_git(["rev-parse", "HEAD"])
    is_dirty: bool = run_git(["status", "--porcelain"]) != ""
    if is_dirty:
        print("warning: the working tree has changes that are not committed; BUILD-INFO says so", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="bearpond-release-") as temporary_name:
        temporary_dir: Path = Path(temporary_name)
        stage_dir: Path = temporary_dir / RELEASE_NAME

        # the wheels are built and fetched by a pip of the release's Python, in a venv of its own, so that they are for
        # that Python and this machine, whatever runs this script
        build_python: Path = make_build_venv(python_path, temporary_dir / "buildenv")
        python_info: PythonInfo = read_python_info(build_python)
        wheels_dir: Path = stage_dir / "wheels"
        package_paths: list[str] = []
        for package_spec in PACKAGE_SPECS:
            package_paths.append(str(REPO_ROOT / package_spec))
        pip_command: list[str] = [str(build_python), "-m", "pip", "--disable-pip-version-check", "wheel", "--quiet"]
        run_command([*pip_command, "--wheel-dir", str(wheels_dir), *package_paths])

        # each wheel pinned by its hash, which is what lets the install take nothing else
        wheels: list[WheelFile] = find_wheels(wheels_dir)
        write_requirements(stage_dir / "requirements.txt", wheels)
        build_info: dict[str, str] = {
            "name": RELEASE_NAME,
            "version": release_version,
            "git_commit": git_commit,
            "git_dirty": "yes" if is_dirty else "no",
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "python": python_info.version,
            "python_tag": python_info.tag,
            "platform": python_info.platform,
        }
        write_build_info(stage_dir / "BUILD-INFO", build_info)
        (stage_dir / "INSTALL").write_text(INSTALL_TEXT, encoding="utf-8")
        for license_name in LICENSE_NAMES:
            shutil.copy2(REPO_ROOT / license_name, stage_dir / license_name)

        # one folder in the archive, named for the release, as deploy.sh unpacks it
        output_dir.mkdir(parents=True, exist_ok=True)
        archive_path: Path = output_dir / f"{RELEASE_NAME}-{release_version}.tgz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(stage_dir, arcname=RELEASE_NAME)
    return archive_path


# ----------------------------------------------------------------------------------------------------------------------
def read_server_version() -> str:
    pyproject_path: Path = REPO_ROOT / "packages" / "server" / "pyproject.toml"
    pyproject: dict = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    return str(pyproject["project"]["version"])


# ----------------------------------------------------------------------------------------------------------------------
def run_git(git_args: list[str]) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", *git_args], cwd=REPO_ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


# ----------------------------------------------------------------------------------------------------------------------
def make_build_venv(python_path: Path, venv_dir: Path) -> Path:
    run_command([str(python_path), "-m", "venv", str(venv_dir)])
    bin_dir_name: str = "Scripts" if os.name == "nt" else "bin"
    return venv_dir / bin_dir_name / "python"


# ----------------------------------------------------------------------------------------------------------------------
def read_python_info(python_path: Path) -> PythonInfo:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [str(python_path), "-c", PYTHON_INFO_CODE], check=True, capture_output=True, text=True
    )
    lines: list[str] = completed.stdout.split()
    return PythonInfo(version=lines[0], tag=lines[1], platform=lines[2])


# ----------------------------------------------------------------------------------------------------------------------
def run_command(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO_ROOT, check=True)


# ----------------------------------------------------------------------------------------------------------------------
def find_wheels(wheels_dir: Path) -> list[WheelFile]:
    # a wheel's file name starts with its project name and version: name-version(-build)?-python-abi-platform.whl
    wheels: list[WheelFile] = []
    for wheel_path in sorted(wheels_dir.glob("*.whl")):
        name_parts: list[str] = wheel_path.name.split("-")
        wheel: WheelFile = WheelFile(project_name=name_parts[0], version=name_parts[1], path=wheel_path)
        wheels.append(wheel)
    return wheels


# ----------------------------------------------------------------------------------------------------------------------
def write_requirements(requirements_path: Path, wheels: list[WheelFile]) -> None:
    lines: list[str] = []
    for wheel in wheels:
        digest: str = hashlib.sha256(wheel.path.read_bytes()).hexdigest()
        lines.append(f"{wheel.project_name}=={wheel.version} --hash=sha256:{digest}")
    requirements_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------------------
def write_build_info(build_info_path: Path, build_info: dict[str, str]) -> None:
    # one "key=value" line each, which a shell script reads with grep and cut
    lines: list[str] = []
    for key, value in build_info.items():
        lines.append(f"{key}={value}")
    build_info_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------------------------------------------------------
def publish_release(archive_path: Path, repository_dir: Path) -> Path:
    # the layout of YardDog's directory package repository: <package>/<version>/<package>-<version>.tgz
    release_version: str = archive_path.name.removeprefix(f"{RELEASE_NAME}-").removesuffix(".tgz")
    version_dir: Path = repository_dir / RELEASE_NAME / release_version
    version_dir.mkdir(parents=True, exist_ok=True)
    published_path: Path = version_dir / archive_path.name
    shutil.copy2(archive_path, published_path)
    return published_path


# ----------------------------------------------------------------------------------------------------------------------
if __name__ == "__main__":
    main()
