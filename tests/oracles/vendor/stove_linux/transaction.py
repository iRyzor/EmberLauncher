"""Experimental file transaction engine, exercised only on isolated copies.

Passing an installation enables process checks and transactional native metadata
commit. No live-installation CLI is exposed yet.
"""
import hashlib
import json
from .platform_fs import os, fcntl
from pathlib import Path
import shutil
import stat
import uuid

from .manifest import parse_manifest, repair_plan


def sync_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class FileTransaction:
    def __init__(self, game, prepared):
        self.game = Path(game)
        self.prepared = Path(prepared)
        self.root = os.open(self.game, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.lock = os.open('.stove-update.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.root)
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            if hasattr(self, "lock"):os.close(self.lock)
            os.close(self.root)
            raise
        self.journal = self.game / '.stove-update-transaction'
        self.state = None

    def close(self):
        os.close(self.lock)
        os.close(self.root)

    def parent(self, relative):
        parts = relative.split('/')
        if any(p in ('', '.', '..') or '\\' in p or ':' in p for p in parts):
            raise ValueError('Unsafe transaction path')
        fd = os.dup(self.root)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd);fd = child
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def record(self, relative, fd, name, directory=False):
        try:info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:info = None
        if info and not stat.S_ISREG(info.st_mode):
            raise ValueError('Transaction target is not a regular file')
        index = len(self.state['operations'])
        backup = str(index) if info else None
        if info:
            source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            with os.fdopen(source, 'rb') as stream, (self.journal/backup).open('xb') as target:
                shutil.copyfileobj(stream,target)
                os.fchmod(target.fileno(),stat.S_IMODE(info.st_mode))
                target.flush();os.fsync(target.fileno())
        backup_hash = None
        if backup is not None:
            with (self.journal/backup).open('rb') as stream:
                backup_hash = hashlib.file_digest(stream,'sha256').hexdigest()
        self.state['operations'].append({'path':relative,'backup':backup,'backup_sha256':backup_hash,'directory':directory,'temporary':None if directory else '.stove-new-'+str(index+1)})
        sync_json(self.journal/'journal.json',self.state)

    def load_journal(self):
        if self.journal.is_symlink():raise ValueError('Unsafe journal')
        with (self.journal/'journal.json').open('rb') as stream:
            raw=stream.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:raise ValueError('Journal too large')
        state=json.loads(raw)
        if not isinstance(state,dict) or state.get('phase') not in ('applying','files_verified','committed','rolled_back'):
            raise ValueError('Invalid journal')
        operations=state.get('operations')
        if not isinstance(operations,list) or len(operations)>100000:raise ValueError('Invalid journal operations')
        seen=set()
        for index,operation in enumerate(operations):
            relative=operation.get('path')
            if not isinstance(relative,str) or relative.startswith('.stove-') or relative in seen:raise ValueError('Invalid journal path')
            if any(p in ('', '.', '..') or '\\' in p or ':' in p for p in relative.split('/')):
                raise ValueError('Unsafe journal path')
            seen.add(relative)
            if type(operation.get('directory')) is not bool:raise ValueError('Invalid journal operation')
            backup=operation.get('backup')
            if backup is not None and backup != str(index):raise ValueError('Invalid backup index')
            temporary=operation.get('temporary')
            if temporary != (None if operation['directory'] else '.stove-new-'+str(index+1)):
                raise ValueError('Invalid journal temporary')
            if backup is not None:
                path=self.journal/backup
                if path.is_symlink() or not path.is_file():raise ValueError('Missing backup')
                with path.open('rb') as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest() != operation.get('backup_sha256'):
                        raise ValueError('Backup integrity mismatch')
        return state

    def finalize(self):
        """Atomically archive a completed journal, retaining its backups."""
        state=self.load_journal()
        if state['phase']=='applying':raise ValueError('Recover the unfinished update first')
        archive=self.game/('.stove-update-history-'+uuid.uuid4().hex)
        os.rename(self.journal,archive)
        os.fsync(self.root)
        return archive

    def rollback(self):
        self.state = self.load_journal()
        if self.state['phase'] != 'applying':raise ValueError('Transaction is not recoverable')
        for operation in reversed(self.state['operations']):
            try:fd,name = self.parent(operation['path'])
            except FileNotFoundError:
                if operation['backup'] is None:continue
                raise
            try:
                backup = operation['backup']
                if backup is not None:
                    if not backup.isdigit() or (self.journal/backup).is_symlink():raise ValueError('Unsafe backup')
                    # Keep backups intact so recovery can itself be repeated.
                    temporary = '.stove-restore-' + backup
                    output = os.open(temporary,os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW,0o600,dir_fd=fd)
                    with os.fdopen(output,'wb') as stream, (self.journal/backup).open('rb') as source:
                        shutil.copyfileobj(source,stream)
                        os.fchmod(stream.fileno(),stat.S_IMODE(os.fstat(source.fileno()).st_mode))
                        stream.flush();os.fsync(stream.fileno())
                    os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd)
                else:
                    try:
                        if operation['directory']:os.rmdir(name,dir_fd=fd)
                        else:os.unlink(name,dir_fd=fd)
                    except FileNotFoundError:pass
                temporary = operation.get('temporary')
                if temporary:
                    if not temporary.startswith('.stove-new-') or not temporary[len('.stove-new-'):].isdigit():raise ValueError('Unsafe temporary path')
                    try:os.unlink(temporary,dir_fd=fd)
                    except FileNotFoundError:pass
                os.fsync(fd)
            finally:os.close(fd)
        self.state['phase']='rolled_back'
        sync_json(self.journal/'journal.json',self.state)

    def apply(self, version, after_operation=None, installation=None):
        if installation is not None:
            from .update_guard import require_idle
            if installation.game_dir.resolve() != self.game.resolve():raise ValueError('Wrong installation')
            require_idle(installation)
        raw = (self.prepared/'manifest.json').read_bytes()
        parse_manifest(raw,version)
        receipt = json.loads((self.prepared/'prepared.json').read_text())
        if receipt['manifest_sha256'] != hashlib.sha256(raw).hexdigest():raise ValueError('Manifest changed')
        plan = repair_plan(self.game,raw,version)
        if plan['blocked']:raise ValueError('Blocked targets')
        staged = {item['path']:item['staged_file'] for item in receipt['staged']}
        sources = {}
        for item in plan['entries']:
            if item['kind']=='F' and item['status'] in ('missing','modified'):
                relative = staged[item['path']]
                source = self.prepared/relative
                if source.is_symlink() or self.prepared.resolve() not in source.resolve().parents:raise ValueError('Unsafe staged file')
                with source.open('rb') as stream:
                    if os.fstat(stream.fileno()).st_size != item['size'] or hashlib.file_digest(stream,'md5').hexdigest() != item['md5']:
                        raise ValueError('Staged file changed')
                sources[item['path']] = source
        self.journal.mkdir(mode=0o700) # An unfinished transaction is never overwritten.
        self.state={'phase':'applying','version':version,'operations':[]}
        sync_json(self.journal/'journal.json',self.state)
        try:
            metadata = []
            if installation is not None:
                from .update_metadata import build_metadata, checkpoint_inventory
                if receipt['published_version'] != version or version < receipt['distribution_version']:
                    raise ValueError('Invalid version transition')
                checkpoint_inventory(installation)
                metadata = build_metadata(self.game,self.journal,raw,version,receipt['distribution_version'])
            for item in sorted(plan['entries'],key=lambda x:(x['kind']!='D',x['path'].count('/'))):
                kind,status = item['kind'],item['status']
                if status not in ('missing','modified','remove'):continue
                if installation is not None:require_idle(installation)
                fd,name = self.parent(item['path'])
                try:
                    self.record(item['path'],fd,name,directory=kind=='D')
                    if kind=='D':os.mkdir(name,dir_fd=fd)
                    elif kind=='R':os.unlink(name,dir_fd=fd)
                    else:
                        temporary='.stove-new-'+str(len(self.state['operations']))
                        output=os.open(temporary,os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,0o644,dir_fd=fd)
                        try:
                            with os.fdopen(output,'wb') as stream:
                                if kind=='F':
                                    with sources[item['path']].open('rb') as source:shutil.copyfileobj(source,stream)
                                stream.flush();os.fsync(stream.fileno())
                            os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd)
                        finally:
                            try:os.unlink(temporary,dir_fd=fd)
                            except FileNotFoundError:pass
                    os.fsync(fd)
                finally:os.close(fd)
                if after_operation:after_operation(item)
            checked=repair_plan(self.game,raw,version)
            if checked['blocked'] or any(x['kind']=='F' and x['status']!='matching' for x in checked['entries']):
                raise ValueError('Post-install verification failed')
            for relative,source in metadata:
                require_idle(installation)
                fd,name = self.parent(relative)
                try:
                    self.record(relative,fd,name)
                    temporary='.stove-new-'+str(len(self.state['operations']))
                    output=os.open(temporary,os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,0o644,dir_fd=fd)
                    with os.fdopen(output,'wb') as target, source.open('rb') as stream:
                        shutil.copyfileobj(stream,target);target.flush();os.fsync(target.fileno())
                    os.replace(temporary,name,src_dir_fd=fd,dst_dir_fd=fd);os.fsync(fd)
                finally:os.close(fd)
                if after_operation:after_operation({'kind':'metadata','path':relative})
            self.state['phase']='committed' if installation is not None else 'files_verified'
            sync_json(self.journal/'journal.json',self.state)
            return checked
        except Exception:
            self.rollback()
            raise
