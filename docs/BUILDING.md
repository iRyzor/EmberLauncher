# Building Ember

## Prerequisites

- A current stable Rust toolchain.
- Node.js 22 and npm.
- Tauri 2's [platform prerequisites](https://v2.tauri.app/start/prerequisites/).
- Python 3 and `uv` for development tools and test oracles; they are not included in the shipped application.
- Wine on Linux for running the game. Building the interface does not require a STOVE account or a game installation.

The repository includes the native helper executables and their C sources. Changes to those helpers require an appropriate Windows cross compiler.

## Install and run

```sh
npm ci
npm run engine:build
npm run desktop
```

The engine build produces the platform's private Rust executable. Build it before launching or packaging the desktop.

## Checks

```sh
npm run build
npm run test:ui
uv sync --project tests/oracles --python 3.11 --locked
npm run test:engine
```

The optional credential-store diagnostic is ignored by default. It uses a separate synthetic entry, never an account credential:

```sh
cargo test --locked --manifest-path engine/rust/Cargo.toml --test credential_store -- --ignored
```

## Package

```sh
npm run engine:build
npm run package
```

Tauri writes packages under `src-tauri/target/release/bundle/`. To produce a Linux TAR or Windows ZIP portable archive after building:

```sh
python tools/package_portable.py
```

Use a native build host for each platform when possible. Windows cross compilation is experimental; follow Tauri's [Windows installer guide](https://v2.tauri.app/distribute/windows-installer/) if necessary. Cross compilation does not replace a real Windows desktop and game test.

## Automation

`.github/workflows/build.yml` builds Linux, Windows and both macOS architectures, runs checks and uploads artifacts, including Linux and Windows portable archives. It also configures isolated Linux AppImage checks. Published-release notes must describe the checks that actually ran, rather than assuming the full platform matrix passed.

## Wine network recovery check

On Linux with Wine and the MinGW x86_64 compiler installed, run:

```sh
python tools/check_wine_network.py
```

This opt-in check replays Win32 API failures against the production bridge code in a disposable Wine prefix. It verifies recovery before pipe creation, handling of transient service locks, refusal to proceed when recovery fails, and that native Windows startup skips Wine recovery. API responses are simulated; no services are stopped or reconfigured, and this does not replace a real game test. It never uses an existing prefix, account or game installation.

To rebuild the shipped bridge after changing its C source:

```sh
x86_64-w64-mingw32-gcc -std=c11 -O2 -Wall -Wextra -Werror -municode -static -s -Wl,--no-insert-timestamp engine/native/pipe_bridge.c -o engine/native/pipe-bridge.exe -liphlpapi -ladvapi32
```
