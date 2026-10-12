"""Extract and smoke-test a packaged Linux AppImage with isolated user settings."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time


def check(image, desktop=False):
    image = Path(image).resolve(strict=True)
    image.chmod(image.stat().st_mode | 0o100)
    with tempfile.TemporaryDirectory(prefix='ember-appimage-check-') as directory:
        temporary = Path(directory)
        env = dict(os.environ)
        for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_DATA_HOME'):
            env[name] = str(temporary / name.lower())
        env['EMBER_GAME_ROOT'] = str(temporary / 'absent-library')
        extraction = subprocess.run([str(image), '--appimage-extract'], cwd=temporary,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=60, env=env)
        if extraction.returncode:
            raise RuntimeError('AppImage extraction failed')
        bundle = temporary / 'squashfs-root'
        engines = list(bundle.rglob('engine/stove-engine'))
        if len(engines) != 1:
            raise RuntimeError('Expected one packaged private engine')
        engine = engines[0]
        native = engine.parent.parent / 'native'
        for name in ('pipe-bridge.exe', 'webview-probe.exe'):
            if not (native / name).is_file():
                raise RuntimeError(f'Missing packaged resource: {name}')
        engine.chmod(engine.stat().st_mode | 0o100)
        result = subprocess.run([str(engine), '--root', str(temporary / 'absent-library'),
                                 '--helper', str(native / 'pipe-bridge.exe')],
                                input='{"action":"state"}\n{"action":"shutdown"}\n',
                                text=True, capture_output=True, timeout=30, env=env)
        if result.returncode:
            raise RuntimeError('Extracted private engine failed to start')
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        if len(responses) != 2 or responses[0]['connected'] or responses[0]['game_installed']:
            raise RuntimeError('Unexpected isolated engine state')
        if (temporary / 'absent-library').exists():
            raise RuntimeError('Smoke check created a game library')
        if desktop:
            if not env.get('DISPLAY'):
                raise RuntimeError('Desktop check requires DISPLAY, e.g. xvfb-run')
            process = subprocess.Popen([str(bundle / 'AppRun')], cwd=temporary, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 8
                engine_seen = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Packaged desktop exited during startup')
                    for status in Path('/proc').glob('[0-9]*/status'):
                        try:
                            fields = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
                            if int(fields['PPid']) == process.pid:
                                cmd = (status.parent / 'cmdline').read_bytes().split(b'\0')[0]
                                engine_seen |= Path(os.fsdecode(cmd)).name == 'stove-engine'
                        except (FileNotFoundError, PermissionError, ProcessLookupError):
                            continue
                    time.sleep(.2)
                if not engine_seen:
                    raise RuntimeError('Desktop did not start its private engine')
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        print(json.dumps({'appimage': image.name, 'engine_ready': True,
                          'desktop_startup': desktop, 'account_accessed': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    images = list(args.artifact.rglob('*.AppImage')) if args.artifact.is_dir() else [args.artifact]
    if len(images) != 1:
        raise SystemExit('Expected exactly one AppImage artifact')
    check(images[0], args.desktop)
