"""Independent process-matching oracle; process operations remain host-specific."""
import re
EXCLUDED = (b'wineserver', b'winedevice', b'explorer.exe', b'services.exe', b'crashpad_handler.exe')


def excluded(name):
    return any(value in name.lower() for value in EXCLUDED)


def path_match(game, windows, candidate):
    if any(part in (b".", b"..") for part in candidate.split(b"/")):
        return False
    name = candidate.rsplit(b'/',1)[-1]
    return (not excluded(name) and candidate.endswith(b'.exe')
            and any(prefix and candidate.startswith(prefix) for prefix in (game, windows)))


def mapping_match(game, maps):
    if not game:
        return False
    for line in maps.splitlines():
        parts = line.split(None,5)
        if (len(parts)==6 and b'x' in parts[1] and parts[5].startswith(game)
                and re.search(rb'\.(?:exe|dll)(?: \(deleted\))?$',parts[5],re.I)):
            return True
    return False


def lifecycle(baseline, owned, current, alive, seen, empty_since, now):
    owned = dict(owned)
    for pid, start in current.items():
        if baseline.get(pid) != start:
            owned[pid] = start
            seen = True
    owned = {pid:start for pid,start in owned.items() if alive.get(pid) == start}
    if owned:
        empty_since = None
    elif seen and empty_since is None:
        empty_since = now
    return dict(owned=owned, seen=seen, empty_since=empty_since, running=bool(owned),
                finished=seen and not owned and empty_since is not None and now-empty_since >= 2)
