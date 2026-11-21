#!/usr/bin/env python3
"""构建 Navop 使用的 macOS Apple Silicon 便携 zip；不依赖目标机安装 Homebrew。"""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = Path(__file__).with_name("navop-macos-bottles.json")
ICD_NAME = "kosmickrisp_mesa_icd.aarch64.json"
ROOT_PLACEHOLDER = "@@YSERVER_ROOT@@"
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")
LICENSE_PREFIXES = ("LICENSE", "COPYING", "COPYRIGHT", "NOTICE")


class PackagingError(RuntimeError):
    """可向使用者直接展示的打包错误。"""


def run(*args: str, cwd: Path | None = None) -> str:
    """运行工具并返回标准输出；失败时保留工具原有错误信息。"""
    return subprocess.check_output(args, text=True, cwd=cwd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_system_dependency(path: str) -> bool:
    return path.startswith(SYSTEM_PREFIXES)


def dependencies(binary: Path) -> list[str]:
    output = run("otool", "-L", str(binary))
    return [line.strip().split(" (", 1)[0] for line in output.splitlines()[1:] if line.strip()]


def copy_executable(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise PackagingError(f"找不到可执行文件：{source}")
    shutil.copy2(source, destination)
    destination.chmod(0o755)


def load_manifest(path: Path) -> list[dict]:
    with path.open("rb") as stream:
        data = json.load(stream)
    if data.get("schema") != 1 or data.get("architecture") != "aarch64":
        raise PackagingError("bottle 清单须为 schema=1、architecture=aarch64")
    formulae = data.get("formulae")
    if not isinstance(formulae, list) or not formulae:
        raise PackagingError("bottle 清单没有 formulae")
    names = set()
    for formula in formulae:
        for key in ("name", "version", "license", "tag", "url", "sha256"):
            if not isinstance(formula.get(key), str) or not formula[key]:
                raise PackagingError(f"bottle 项缺少 {key}：{formula}")
        name = formula["name"]
        if not re.fullmatch(r"[a-zA-Z0-9+_.-]+", name) or name in names:
            raise PackagingError(f"bottle 名称无效或重复：{name}")
        if not formula["url"].startswith(f"https://ghcr.io/v2/homebrew/core/{name}/blobs/sha256:"):
            raise PackagingError(f"{name} 不是官方 Homebrew GHCR bottle URL")
        if not re.fullmatch(r"[0-9a-f]{64}", formula["sha256"]):
            raise PackagingError(f"{name} 的 SHA-256 无效")
        names.add(name)
    required = {"mesa", "vulkan-loader", "xkeyboard-config"}
    if not required.issubset(names):
        raise PackagingError(f"bottle 清单缺少：{', '.join(sorted(required - names))}")
    return formulae


def ghcr_token(formula: str) -> str:
    """按 GHCR 的匿名拉取流程取 token，避免依赖本机 brew 配置或镜像。"""
    query = urllib.parse.urlencode(
        {
            "service": "ghcr.io",
            "scope": f"repository:homebrew/core/{formula}:pull",
        }
    )
    request = urllib.request.Request(
        f"https://ghcr.io/token?{query}", headers={"User-Agent": "yserver-navop-packager/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PackagingError(f"获取 {formula} 的 GHCR 匿名 token 失败：{error}") from error
    token = data.get("token") or data.get("access_token")
    if not token:
        raise PackagingError(f"GHCR 未返回 {formula} 的匿名 token")
    return token


def download_bottle(formula: dict, cache_dir: Path, offline: bool) -> Path:
    name = formula["name"]
    archive = cache_dir / (
        f"{name}-{formula['version']}-{formula['tag']}-{formula['sha256'][:12]}.tar.gz"
    )
    if archive.is_file():
        actual = sha256_file(archive)
        if actual != formula["sha256"]:
            raise PackagingError(f"缓存 bottle 校验失败，请删除后重试：{archive}")
        return archive
    if offline:
        raise PackagingError(f"离线模式缺少已校验 bottle：{archive}")

    print(f"下载官方 bottle：{name} {formula['version']} ({formula['tag']})")
    request = urllib.request.Request(
        formula["url"],
        headers={
            "Authorization": f"Bearer {ghcr_token(name)}",
            "User-Agent": "yserver-navop-packager/1",
        },
    )
    temporary = archive.with_name(archive.name + f".{os.getpid()}.part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != formula["sha256"]:
            raise PackagingError(f"下载的 {name} bottle SHA-256 与清单不符")
        os.replace(temporary, archive)
    except (urllib.error.URLError, TimeoutError) as error:
        raise PackagingError(f"下载 {name} bottle 失败：{error}") from error
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def extract_bottle(archive_path: Path, destination: Path, formula: dict) -> None:
    """剥掉 bottle 的 <formula>/<version> 两层目录，并拒绝危险归档成员。"""
    destination.mkdir()
    with tarfile.open(archive_path, "r:*") as archive:
        members = []
        for member in archive.getmembers():
            parts = PurePosixPath(member.name).parts
            if len(parts) < 3:
                continue
            if parts[0] != formula["name"]:
                raise PackagingError(f"{formula['name']} bottle 顶层目录异常：{member.name}")
            member.name = str(PurePosixPath(*parts[2:]))
            members.append(member)
        archive.extractall(destination, members=members, filter="data")


class DylibResolver:
    """把原始 Mach-O 依赖解析到固定 bottle 或显式 Homebrew prefix。"""

    def __init__(self, prefixes: dict[str, Path], homebrew_prefix: Path, lib_dir: Path):
        self.prefixes = prefixes
        self.homebrew_prefix = homebrew_prefix
        self.lib_dir = lib_dir
        self.bundled: dict[str, Path] = {}
        self.index: dict[str, list[Path]] = {}
        for prefix in prefixes.values():
            self._index_tree(prefix / "lib")
        opt = homebrew_prefix / "opt"
        if opt.is_dir():
            for formula_opt in sorted(opt.iterdir()):
                self._index_tree(formula_opt / "lib")

    def _index_tree(self, root: Path) -> None:
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*.dylib")):
            if path.is_file():
                self.index.setdefault(path.name, []).append(path)

    def register(self, source: Path, name: str | None = None) -> Path:
        name = name or source.name
        if not source.is_file():
            raise PackagingError(f"找不到运行时 dylib：{source}")
        destination = self.lib_dir / name
        if name in self.bundled:
            return self.bundled[name]
        shutil.copy2(source, destination)
        destination.chmod(0o755)
        self.bundled[name] = destination
        return destination

    def _from_homebrew_relative(self, relative: PurePosixPath) -> Path | None:
        parts = relative.parts
        if len(parts) >= 3 and parts[0] == "opt" and parts[1] in self.prefixes:
            return self.prefixes[parts[1]].joinpath(*parts[2:])
        if len(parts) >= 4 and parts[0] == "Cellar" and parts[1] in self.prefixes:
            return self.prefixes[parts[1]].joinpath(*parts[3:])
        return None

    def resolve(self, dependency: str, owner: Path) -> Path:
        name = PurePosixPath(dependency).name
        if name in self.bundled:
            return self.bundled[name]
        candidates: list[Path] = []
        for marker in ("@rpath/", "@loader_path/", "@executable_path/"):
            if dependency.startswith(marker):
                candidates.append(owner.parent / PurePosixPath(dependency[len(marker) :]))

        if dependency.startswith("@@HOMEBREW_PREFIX@@/"):
            relative = PurePosixPath(dependency.removeprefix("@@HOMEBREW_PREFIX@@/"))
            extracted = self._from_homebrew_relative(relative)
            if extracted is not None:
                candidates.append(extracted)
            candidates.append(self.homebrew_prefix.joinpath(*relative.parts))

        for prefix in {self.homebrew_prefix, Path("/opt/homebrew"), Path("/usr/local")}:
            prefix_text = str(prefix)
            if dependency.startswith(prefix_text + "/"):
                relative = PurePosixPath(dependency.removeprefix(prefix_text + "/"))
                extracted = self._from_homebrew_relative(relative)
                if extracted is not None:
                    candidates.append(extracted)
                candidates.append(Path(dependency))

        if dependency.startswith("/") and not dependency.startswith(
            ("@@HOMEBREW_PREFIX@@/", "/opt/homebrew/", "/usr/local/")
        ):
            candidates.append(Path(dependency))
        candidates.extend(self.index.get(name, []))

        source = next((path for path in candidates if path.is_file()), None)
        if source is None:
            raise PackagingError(f"{owner.name} 的非系统依赖无法解析：{dependency}")
        return self.register(source, name)


def rpaths(binary: Path) -> list[str]:
    lines = run("otool", "-l", str(binary)).splitlines()
    result = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for entry in lines[index + 1 : index + 6]:
            match = re.match(r"\s*path (.*) \(offset \d+\)$", entry)
            if match:
                result.append(match.group(1))
                break
    return result


def add_rpath(binary: Path, path: str) -> None:
    if path not in rpaths(binary):
        run("install_name_tool", "-add_rpath", path, str(binary))


def collect_and_rewrite(
    executables: list[Path], resolver: DylibResolver, lib_dir: Path
) -> set[Path]:
    """递归收集闭包，再把所有非系统引用统一改为 @rpath。"""
    queue = [*executables, *resolver.bundled.values()]
    seen: set[Path] = set()
    while queue:
        binary = queue.pop()
        if binary in seen:
            continue
        seen.add(binary)
        for dependency in dependencies(binary):
            if is_system_dependency(dependency):
                continue
            resolved = resolver.resolve(dependency, binary)
            if resolved not in seen:
                queue.append(resolved)

    for binary in sorted(seen, key=str):
        for dependency in dependencies(binary):
            if is_system_dependency(dependency):
                continue
            name = PurePosixPath(dependency).name
            bundled = resolver.bundled.get(name)
            if bundled is None:
                raise PackagingError(f"闭包中缺少 {name}（{binary}）")
            replacement = f"@rpath/{name}"
            if dependency != replacement:
                run("install_name_tool", "-change", dependency, replacement, str(binary))
        if binary in executables:
            add_rpath(binary, "@executable_path/lib")
        else:
            run("install_name_tool", "-id", f"@rpath/{binary.name}", str(binary))
            add_rpath(binary, "@loader_path")

    for binary in sorted(seen, key=str):
        for dependency in dependencies(binary):
            if not is_system_dependency(dependency):
                expected = f"@rpath/{PurePosixPath(dependency).name}"
                if dependency != expected or not (lib_dir / PurePosixPath(dependency).name).is_file():
                    raise PackagingError(f"改写后仍有非便携依赖：{binary}: {dependency}")
    return seen


def write_icd(prefixes: dict[str, Path], stage: Path) -> None:
    source = prefixes["mesa"] / "share/vulkan/icd.d" / ICD_NAME
    if not source.is_file():
        raise PackagingError(f"Mesa bottle 缺少 KosmicKrisp ICD：{source}")
    data = json.loads(source.read_text(encoding="utf-8"))
    data["ICD"]["library_path"] = (
        f"{ROOT_PLACEHOLDER}/lib/libvulkan_kosmickrisp.dylib"
    )
    destination = stage / "icd.d" / ICD_NAME
    destination.parent.mkdir()
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def copy_xkb(prefixes: dict[str, Path], stage: Path) -> None:
    source = prefixes["xkeyboard-config"] / "share/X11/xkb"
    if not (source / "rules/evdev").is_file():
        raise PackagingError(f"xkeyboard-config bottle 缺少 XKB 数据：{source}")
    shutil.copytree(source, stage / "xkb", symlinks=False)


def cargo_rust_licenses(stage: Path) -> None:
    """按 macOS 目标过滤 Cargo 正常依赖，不混入仅 Windows 使用的 crate。"""
    metadata = json.loads(
        run(
            "cargo",
            "metadata",
            "--format-version",
            "1",
            "--locked",
            "--filter-platform",
            "aarch64-apple-darwin",
            cwd=REPO_ROOT,
        )
    )
    packages = {package["id"]: package for package in metadata["packages"]}
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    roots = [
        package["id"]
        for package in metadata["packages"]
        if package["name"] in {"yserver", "yserver-xauth"}
    ]
    if len(roots) != 2:
        raise PackagingError("Cargo metadata 未同时找到 yserver 和 yserver-xauth")

    seen: set[str] = set()
    queue = [*roots]
    while queue:
        package_id = queue.pop()
        if package_id in seen:
            continue
        seen.add(package_id)
        for dependency in nodes[package_id]["deps"]:
            if any(kind["kind"] is None for kind in dependency["dep_kinds"]):
                queue.append(dependency["pkg"])

    lines = ["yserver 和 yserver-xauth 的 macOS Rust crate 许可证。", "目标：aarch64-apple-darwin"]
    members = set(metadata["workspace_members"])
    external = [packages[package_id] for package_id in seen - members]
    for package in sorted(external, key=lambda item: (item["name"], item["version"])):
        name = package["name"]
        version = package["version"]
        lines.extend(["", "=" * 78, f"{name} {version}: {package.get('license') or '未声明'}", "=" * 78])
        crate_dir = Path(package["manifest_path"]).parent
        sources = [
            path
            for path in sorted(crate_dir.iterdir())
            if path.is_file() and path.name.upper().startswith(LICENSE_PREFIXES)
        ]
        if package.get("license_file"):
            sources.append(crate_dir / package["license_file"])
        unique_sources = {source.resolve(): source for source in sources}
        if not unique_sources:
            lines.append(f"（crate 内未找到许可证文件；SPDX：{package.get('license') or '未声明'}）")
        for source in sorted(unique_sources.values(), key=lambda path: str(path)):
            if not source.is_file():
                raise PackagingError(f"Rust crate 许可证文件不存在：{source}")
            lines.extend([f"--- {source.name}", source.read_text(encoding="utf-8").rstrip()])
    (stage / "licenses" / "rust-crates.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_licenses(formulae: list[dict], prefixes: dict[str, Path], stage: Path) -> None:
    destination = stage / "licenses"
    destination.mkdir()
    receipts = {}
    for formula in formulae:
        name = formula["name"]
        receipts[name] = {
            key: formula[key] for key in ("version", "license", "tag", "url", "sha256")
        }
        prefix = prefixes[name]
        sources = [
            path
            for path in sorted(prefix.rglob("*"))
            if path.is_file() and path.name.upper().startswith(LICENSE_PREFIXES)
        ]
        for source in sources:
            target = destination / name / source.relative_to(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    (destination / "runtime.json").write_text(
        json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    cargo_rust_licenses(stage)
    shutil.copy2(REPO_ROOT / "LICENSE", stage / "LICENSE.txt")


def write_readme(stage: Path) -> None:
    (stage / "README.txt").write_text(
        """yserver macOS Apple Silicon 便携包（Navop）

内容：yserver、yserver-xauth、非系统 dylib、XKB 数据和 KosmicKrisp Vulkan ICD。
启动器必须把 icd.d/kosmickrisp_mesa_icd.aarch64.json 中的
@@YSERVER_ROOT@@ 替换为解压后目录的绝对路径，再启动 yserver。
用户显式设置的 VK_DRIVER_FILES 或 VK_ICD_FILENAMES 优先于包内 ICD。

KosmicKrisp 运行需要支持 Metal 的 Apple Silicon Mac 和 macOS 26 或更新版本。
本包只做 ad-hoc codesign，没有 Apple 开发者签名、公证或 x86_64 支持。
第三方 bottle 的版本、官方 URL、SHA-256 和许可证见 licenses/。
""",
        encoding="utf-8",
    )


def verify_archive(archive_path: Path, artifact_name: str) -> None:
    """从最终 zip 再检查目录、载荷和可执行权限，避免只验证 stage。"""
    required = {
        "yserver",
        "yserver-xauth",
        "lib/libvulkan.1.dylib",
        "lib/libvulkan_kosmickrisp.dylib",
        f"icd.d/{ICD_NAME}",
        "xkb/rules/evdev",
        "licenses/runtime.json",
        "licenses/rust-crates.txt",
        "README.txt",
        "LICENSE.txt",
    }
    with zipfile.ZipFile(archive_path) as archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise PackagingError(f"zip CRC 校验失败：{corrupt}")
        infos = archive.infolist()
        names = {info.filename for info in infos}
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != artifact_name:
                raise PackagingError(f"zip 含越界或多余顶层路径：{info.filename}")
        missing = [name for name in sorted(required) if f"{artifact_name}/{name}" not in names]
        if missing:
            raise PackagingError(f"zip 缺少：{', '.join(missing)}")
        for executable in ("yserver", "yserver-xauth"):
            info = archive.getinfo(f"{artifact_name}/{executable}")
            if info.is_dir() or not ((info.external_attr >> 16) & 0o111):
                raise PackagingError(f"zip 中 {executable} 没有可执行权限")
        icd = json.loads(archive.read(f"{artifact_name}/icd.d/{ICD_NAME}"))
        expected = f"{ROOT_PLACEHOLDER}/lib/libvulkan_kosmickrisp.dylib"
        if icd["ICD"]["library_path"] != expected:
            raise PackagingError("zip 中 ICD library_path 占位符不正确")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary-dir",
        type=Path,
        default=REPO_ROOT / "target/aarch64-apple-darwin/release",
        help="同时包含 yserver 和 xauth 的目录",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO_ROOT / "target/dist", help="stage 与 zip 输出目录"
    )
    parser.add_argument(
        "--build",
        action="store_true",
        help="先 cargo build --locked --release yserver 与 yserver-xauth",
    )
    parser.add_argument("--homebrew-prefix", type=Path, help="构建机的 brew --prefix；缺省时自动查询")
    parser.add_argument("--package-name", default="yserver", help="zip 的包名部分")
    parser.add_argument("--package-version", help="zip 的版本部分；缺省读 workspace 版本")
    parser.add_argument("--bottle-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPO_ROOT / "target/navop-macos-bottle-cache",
        help="已校验官方 bottle 的缓存目录",
    )
    parser.add_argument("--offline", action="store_true", help="只使用 --cache-dir，不访问网络")
    args = parser.parse_args()
    for value, label in ((args.package_name, "包名"), (args.package_version, "版本")):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
            parser.error(f"{label}含路径分隔符或无效字符：{value}")
    return args


def main() -> None:
    args = parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise PackagingError("此打包器只支持在 arm64 macOS 上运行，不生成 x86_64 包")
    for tool in ("otool", "install_name_tool", "codesign"):
        if shutil.which(tool) is None:
            raise PackagingError(f"缺少 Xcode 命令行工具：{tool}")

    if args.homebrew_prefix is None:
        try:
            homebrew_prefix = Path(run("brew", "--prefix").strip())
        except (OSError, subprocess.CalledProcessError) as error:
            raise PackagingError("无法查询 brew --prefix，请显式传 --homebrew-prefix") from error
    else:
        homebrew_prefix = args.homebrew_prefix.expanduser().resolve()
    if not homebrew_prefix.is_dir():
        raise PackagingError(f"Homebrew prefix 不存在：{homebrew_prefix}")

    version = args.package_version
    if version is None:
        with (REPO_ROOT / "Cargo.toml").open("rb") as stream:
            version = tomllib.load(stream)["workspace"]["package"]["version"]
    artifact_name = f"{args.package_name}-{version}-macos-aarch64"
    if args.build:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--release",
                "--target",
                "aarch64-apple-darwin",
                "-p",
                "yserver",
                "--bin",
                "yserver",
                "-p",
                "yserver-xauth",
                "--bin",
                "xauth",
            ],
            cwd=REPO_ROOT,
            check=True,
        )
    binary_dir = args.binary_dir.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()
    manifest_path = args.bottle_manifest.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    formulae = load_manifest(manifest_path)

    stage = output_dir / artifact_name
    archive_path = output_dir / f"{artifact_name}.zip"
    if stage.exists():
        shutil.rmtree(stage)
    (stage / "lib").mkdir(parents=True)
    executables = [stage / "yserver", stage / "yserver-xauth"]
    copy_executable(binary_dir / "yserver", executables[0])
    copy_executable(binary_dir / "xauth", executables[1])

    work_parent = output_dir / ".work"
    work_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="navop-macos-", dir=work_parent) as temporary:
        prefixes: dict[str, Path] = {}
        for formula in formulae:
            bottle = download_bottle(formula, cache_dir, args.offline)
            prefix = Path(temporary) / formula["name"]
            extract_bottle(bottle, prefix, formula)
            prefixes[formula["name"]] = prefix

        resolver = DylibResolver(prefixes, homebrew_prefix, stage / "lib")
        # Vulkan 在运行时 dlopen，otool 从 yserver 看不到，必须显式作为闭包根。
        resolver.register(prefixes["vulkan-loader"] / "lib/libvulkan.1.dylib")
        resolver.register(prefixes["mesa"] / "lib/libvulkan_kosmickrisp.dylib")
        binaries = collect_and_rewrite(executables, resolver, stage / "lib")
        write_icd(prefixes, stage)
        copy_xkb(prefixes, stage)
        copy_licenses(formulae, prefixes, stage)
        write_readme(stage)

        # 先签 dylib，再签两个可执行文件；修改 Mach-O 后必须全部重签。
        for binary in sorted(binaries, key=lambda path: (path in executables, str(path))):
            run("codesign", "--force", "--sign", "-", str(binary))
        for binary in sorted(binaries, key=str):
            run("codesign", "--verify", "--strict", str(binary))

    archive_path.unlink(missing_ok=True)
    shutil.make_archive(str(archive_path.with_suffix("")), "zip", output_dir, stage.name)
    verify_archive(archive_path, artifact_name)
    result = {
        "archive": str(archive_path),
        "stage": str(stage),
        "bytes": archive_path.stat().st_size,
        "sha256": sha256_file(archive_path),
        "dylibs": len(list((stage / "lib").glob("*.dylib"))),
        "uncompressed": sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()),
    }
    if os.environ.get("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"zip={archive_path}\nstage={stage}\nsha256={result['sha256']}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (PackagingError, OSError, subprocess.CalledProcessError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"打包失败：{error}")
