# Navop macOS 便携包

本文只描述 `feigeCode/yserver` 的 Navop Apple Silicon sidecar。原有 Windows
构建和 `.github/workflows/navop-windows.yml` 不受影响。

## 构建机要求

- Apple Silicon Mac；脚本拒绝生成或伪装 x86_64 包。
- Python 3.11 或更新版本、Xcode Command Line Tools（`otool`、
  `install_name_tool`、`codesign`）、Rust stable 和 Cargo。
- Homebrew 仅用于构建 yserver，以及解析构建机二进制中未列入固定清单的
  动态库；最终用户不需要安装 Homebrew。

```sh
brew install mesa vulkan-loader shaderc libxkbcommon fontconfig freetype pkgconf
python3 script/package-navop-macos.py \
  --build \
  --homebrew-prefix "$(brew --prefix)"
```

`--build` 执行等价于下面的显式目标构建，然后打包 `yserver` 和
`target/aarch64-apple-darwin/release/xauth`；后者在包中命名为
`yserver-xauth`，用于 SSH 的 `XAuthLocation`。

```sh
cargo build --locked --release --target aarch64-apple-darwin \
  -p yserver --bin yserver -p yserver-xauth --bin xauth
```

默认输出：

- `target/dist/yserver-<workspace-version>-macos-aarch64/`
- `target/dist/yserver-<workspace-version>-macos-aarch64.zip`

已有二进制可省略 `--build`，并用 `--binary-dir` 指定同时含有 `yserver` 和
`xauth` 的目录。`--output-dir`、`--package-name`、`--package-version` 分别控制
输出目录、包名和版本；本地发布版本可以显式追加 Navop 后缀。脚本把 zip 路径、
stage 路径和 SHA-256 写到 `GITHUB_OUTPUT`（如果存在），同时输出 JSON 结果。

## 固定运行时与离线重打

`script/navop-macos-bottles.json` 固定了 18 个 Homebrew formula 的版本、bottle
tag、官方 `ghcr.io/v2/homebrew/core/...` URL 和 SHA-256。Apple Silicon bottle
使用 `arm64_golden_gate`，XKB 数据使用 `all`。脚本自行完成 GHCR 匿名鉴权，不读取
`HOMEBREW_BOTTLE_DOMAIN`，因此不会把镜像 URL 当作可复现来源。

下载文件保存在 `target/navop-macos-bottle-cache`，每次使用都重新校验 SHA-256。
更新清单必须同时更新版本、URL 和摘要，不能只换版本号。已有完整缓存时可完全
离线重打：

```sh
python3 script/package-navop-macos.py \
  --homebrew-prefix /opt/homebrew \
  --cache-dir target/navop-macos-bottle-cache \
  --offline
```

也可以用 `--bottle-manifest` 和 `--cache-dir` 指定另一份清单与缓存。缓存损坏、
SHA-256 不符或离线缺文件都会直接失败，不会静默换用其他版本。

## 包结构与启动约定

```text
yserver-<version>-macos-aarch64/
├── yserver
├── yserver-xauth
├── lib/*.dylib
├── icd.d/kosmickrisp_mesa_icd.aarch64.json
├── xkb/
├── licenses/
└── README.txt
```

- 脚本递归读取两个可执行文件及全部已收集 dylib 的 `otool -L`，只保留
  `/usr/lib/` 和 `/System/Library/` 系统依赖，其余非系统依赖全部复制到 `lib/`。
  每个非系统引用改为 `@rpath/<name>`；可执行文件使用
  `@executable_path/lib`，dylib 使用 `@loader_path` 和 `@rpath` 安装名。
- Vulkan loader 和 KosmicKrisp 是运行时加载项，主程序的 `otool` 看不到，
  所以脚本显式把它们作为依赖闭包根。缺项或闭包改写后仍有绝对路径都会失败。
- `xkb/` 来自固定 `xkeyboard-config` bottle。已有 Rust 代码优先接受
  `XKB_CONFIG_ROOT`，否则查找可执行文件旁的 `xkb/`。
- ICD 保留原 bottle 的 `api_version` 和文件格式，只把 `library_path` 改为
  `@@YSERVER_ROOT@@/lib/libvulkan_kosmickrisp.dylib`。启动器必须把占位符替换为
  解压根目录的绝对路径，再启动 yserver。yserver 会自动设置包内 ICD 路径；用户
  显式设置的 `VK_DRIVER_FILES` 或 `VK_ICD_FILENAMES` 优先。
- `licenses/runtime.json` 记录 bottle 收据，并复制第三方许可证。
  `licenses/rust-crates.txt` 通过 `cargo metadata --locked --filter-platform
  aarch64-apple-darwin` 遍历 yserver/yserver-xauth 的正常依赖，包含 macOS 所需
  的 `objc2` 等 crate 的许可证正文，不复用 Windows 清单。
- 所有 Mach-O 修改后进行 ad-hoc `codesign`，逐文件执行严格验证。最终 zip 再做
  CRC、唯一顶层目录、必需文件、ICD 占位符和可执行权限检查。

## CI 与发布边界

独立 `.github/workflows/navop-macos.yml` 在 `macos-15` 上安装构建依赖、构建两个
二进制、下载固定 bottle、打包并上传 `navop-yserver-macos-aarch64` artifact。
smoke 只执行 `yserver --version`、`otool`、签名校验和 zip/许可证结构检查；不启动
GUI。release 由维护者本地重打、重签并上传。

KosmicKrisp 运行仍要求 **macOS 26 或更新版本、Apple Silicon 和可用 Metal
设备**。`macos-15` 构建成功不等于 GUI 运行验证。当前方案没有 Apple Developer ID
签名、公证、x86_64 或 universal binary，也没有证明在未安装开发工具的干净机器
上运行；这些必须由目标环境实测单独确认。
