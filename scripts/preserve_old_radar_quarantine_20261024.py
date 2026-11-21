#!/usr/bin/env python3
"""Preserve the closed August radar quarantine on the existing root filesystem.

This one-off command keeps every observation and its logical path. Default mode
only inspects. Copy, publication and duplicate release use a pinned request and a
durable receipt; no service, timer, mount or archive policy is changed.
"""
if not __debug__:
    raise SystemExit('Run without Python optimization')

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

SOURCE = Path('/data/quarantine/aurora-root-relocation/ass-mirror-20260829/rpgfmcw94')
TARGET = Path('/var/lib/aurora-recovery/ass-mirror-20260829/rpgfmcw94')
BACKUP_PARENT = Path('/data/quarantine/aurora-root-relocation/.radar-preservation-20261024')
RECEIPT = Path('/var/lib/aurora-recovery/old-radar-preservation-20261024.json')
OWNER = 0
GIB = 1024 ** 3


def require(value, message):
    if not value:
        raise RuntimeError(message)


def present(path):
    return os.path.lexists(path)


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_gid, info.st_mode,
            info.st_nlink, info.st_blocks]


def directory_id(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode), 'Expected a real directory: ' + str(path))
    return signature(info)[:2]


def safe_path(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'Invalid absolute path')
    for parent in [path, *path.parents]:
        require(not parent.is_symlink(), 'Unexpected symlink: ' + str(parent))
    return path


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def attributes(entry):
    info = entry['identity']
    return [info[3], info[5], info[6], info[7], entry['xattrs']]


def walk_error(error):
    raise error


def inventory(root):
    safe_path(root)
    device, inode = directory_id(root)
    result = {}
    for base, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
        for path in [Path(base), *(Path(base) / n for n in files)]:
            before = path.lstat()
            require(before.st_dev == device, 'Unexpected device: ' + str(path))
            require(stat.S_ISDIR(before.st_mode) or
                    (stat.S_ISREG(before.st_mode) and before.st_nlink == 1),
                    'Unexpected link or file type: ' + str(path))
            result[str(path.relative_to(root))] = {
                'identity': signature(before),
                'xattrs': {name: os.getxattr(path, name, follow_symlinks=False).hex()
                          for name in os.listxattr(path, follow_symlinks=False)}}
            require(signature(path.lstat()) == signature(before), 'Changed during inventory: ' + str(path))
        for name in directories:
            require(not (Path(base) / name).is_symlink(), 'Directory symlink in inventory')
    require(directory_id(root) == [device, inode], 'Inventory root replaced')
    return result


def hash_file(path):
    before = signature(path.lstat())
    require(stat.S_ISREG(before[7]) and before[8] == 1, 'Expected a single-link regular file')
    sha = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        require(signature(os.fstat(stream.fileno())) == before, 'Hash source replaced')
        while data := stream.read(8 * 1024**2):
            sha.update(data)
        require(signature(os.fstat(stream.fileno())) == before, 'File changed while hashing')
    require(signature(path.lstat()) == before, 'File replaced after hashing')
    return sha.hexdigest()


def read_control(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            before.st_uid == OWNER and not before.st_mode & 0o022, 'Unsafe control file: ' + str(path))
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        value = stream.read()
        require(signature(os.fstat(stream.fileno())) == signature(before), 'Control file changed')
    require(signature(path.lstat()) == signature(before), 'Control file replaced')
    return value


def token(path):
    return [read_control(path), signature(path.lstat())]


def durable(path, value, old=None):
    data = (json.dumps(value, indent=2, sort_keys=True) + '\n').encode()
    if old is None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    else:
        require(token(path) == old, 'Receipt changed')
        fd, temporary = tempfile.mkstemp(prefix='.radar-preservation-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            require(token(path) == old, 'Receipt replaced before publication')
            os.replace(temporary, path)
        finally:
            if present(temporary):
                os.unlink(temporary)
    sync_dir(path.parent)
    return token(path)


def metadata(path, expected):
    info = expected['identity']
    os.chown(path, info[5], info[6], follow_symlinks=False)
    os.chmod(path, stat.S_IMODE(info[7]), follow_symlinks=False)
    for name in os.listxattr(path, follow_symlinks=False):
        if name not in expected['xattrs']:
            os.removexattr(path, name, follow_symlinks=False)
    for name, value in expected['xattrs'].items():
        os.setxattr(path, name, bytes.fromhex(value), follow_symlinks=False)
    os.utime(path, ns=(info[3], info[3]), follow_symlinks=False)


def mount_records(path):
    result = []
    for line in path.read_text().splitlines():
        parts = line.split()
        require(len(parts) >= 10 and '-' in parts, 'Incomplete mount inventory')
        major, minor = (int(x) for x in parts[2].split(':'))
        decode = lambda value: Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value))
        result.append({'device': os.makedev(major, minor), 'root': decode(parts[3]), 'path': decode(parts[4])})
    require(result, 'Empty mount inventory')
    return result


def closure(roots, identities):
    """Check paths/inode aliases and all visible mount namespaces; errors propagate."""
    def under(path):
        return any(path == str(root) or path.startswith(str(root) + '/') for root in roots)
    namespaces = {}
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            namespaces.setdefault((proc / 'ns/mnt').stat().st_ino, []).append(proc / 'mountinfo')
            handles = list((proc / 'fd').iterdir()) + [proc / 'cwd', proc / 'root']
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(proc.name) != os.getpid():
            for handle in handles:
                try:
                    target, info = os.readlink(handle), handle.stat()
                    require(not under(target) and (info.st_dev, info.st_ino) not in identities,
                            'Open selected-tree user: ' + str(handle))
                except (FileNotFoundError, ProcessLookupError):
                    continue
            try:
                for line in (proc / 'maps').read_text().splitlines():
                    fields = line.split(maxsplit=5)
                    major, minor = (int(x, 16) for x in fields[3].split(':'))
                    require((os.makedev(major, minor), int(fields[4])) not in identities and
                            (len(fields) != 6 or not under(fields[5])), 'Mapped selected-tree user: ' + str(proc))
            except (FileNotFoundError, ProcessLookupError):
                pass
    host = mount_records(Path('/proc/self/mountinfo'))
    for representatives in namespaces.values():
        reject_mount_aliases(roots, host, namespace_records(representatives))


def namespace_records(representatives):
    for path in representatives:
        try:
            return mount_records(path)
        except (FileNotFoundError, ProcessLookupError):
            continue
    # A later retry can establish that the namespace disappeared. This scan
    # cannot claim closure merely because its representative exited.
    raise RuntimeError('Mount namespace disappeared before it could be checked')


def reject_mount_aliases(roots, host, records):
    for root in roots:
        base_path = Path('/data') if Path('/data') in root.parents else Path('/')
        bases = [r for r in host if r['path'] == base_path]
        require(len(bases) == 1, 'Expected one canonical filesystem mount')
        base = bases[0]
        selected_root = base['root'] / root.relative_to(base['path'])
        for record in records:
            require(not (record['path'] == root or root in record['path'].parents),
                    'Nested mount in selected tree')
            if record['device'] != base['device'] or (record['path'] == base['path'] and record['root'] == base['root']):
                continue
            other = record['root']
            require(other != selected_root and other not in selected_root.parents and selected_root not in other.parents,
                    'Selected tree has a mount alias: ' + str(record['path']))


def installed_references(source, target):
    """Only configuration and installed local scripts; never inspect credentials."""
    candidates = [Path('/etc/fstab')]
    for root in ('/etc/systemd/system', '/etc/tmpfiles.d', '/etc/cron.d'):
        candidates.extend(p for p in Path(root).rglob('*') if p.is_file() and not p.is_symlink())
    for root in ('/usr/local/bin', '/usr/local/lib'):
        candidates.extend(p for p in Path(root).iterdir() if p.is_file() and not p.is_symlink())
    needles = [str(source).encode(), str(target).encode(), b'ass-mirror-20260829', b'aurora-root-relocation']
    for path in candidates:
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            if path.stat().st_size > 4 * 1024**2:
                continue  # Executable binaries are not the scoped configuration audit.
            require(not any(needle in path.read_bytes() for needle in needles),
                    'Installed reference requires review: ' + str(path))
        except FileNotFoundError:
            continue


def protected_parent(path):
    for parent in [path, *path.parents]:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and not info.st_mode & 0o022,
                'Preservation ancestor is not root protected: ' + str(parent))


class Preserver:
    def __init__(self, request, baseline, request_sha, tool_sha, receipt=RECEIPT):
        self.request, self.baseline, self.receipt = request, baseline, receipt
        self.source, self.target = Path(request['source']), Path(request['target'])
        self.parent = self.target.parent
        self.stage = self.parent / '.rpgfmcw94-copying'
        self.backup_parent = Path(request['backup_parent'])
        self.backup = self.backup_parent / 'rpgfmcw94'
        self.files = sorted(n for n, v in baseline.items() if stat.S_ISREG(v['identity'][7]))
        self.dirs = sorted((n for n, v in baseline.items() if stat.S_ISDIR(v['identity'][7])), key=lambda n: (len(Path(n).parts), n))
        self.pins = {'request_sha256': request_sha, 'tool_sha256': tool_sha, 'request': request}
        self.record, self.previous = None, None

    def save(self):
        self.previous = durable(self.receipt, self.record, self.previous)

    def private(self):
        protected_parent(self.parent.parent); protected_parent(self.backup_parent.parent)
        for path, key in ((self.parent, 'target_parent'), (self.backup_parent, 'backup_parent')):
            info = path.lstat()
            require(directory_id(path) == self.record[key] and info.st_uid == OWNER and stat.S_IMODE(info.st_mode) == 0o700,
                    'Private preservation ancestor changed: ' + str(path))

    def original(self, releasing=False):
        renamed = self.record.get('source_renamed') or present(self.backup)
        root = self.backup if renamed else self.source
        removed = set(self.record['released']) | set(self.record['removed_dirs'])
        pending = self.record['release_pending'] or self.record['dir_pending']
        if not present(root):
            require(set(self.baseline) - removed <= {'.'} and ('.' in removed or pending == '.'),
                    'Original root disappeared')
            return {}
        current = inventory(root)
        expected_names = set(self.baseline) - removed
        require(set(current) == expected_names or
                (pending is not None and set(current) == expected_names - {pending}),
                'Original file membership changed')
        for name, entry in current.items():
            expected = self.baseline[name]
            if name in self.files:
                require(entry == expected, 'Original observation changed: ' + name)
            else:
                require(entry['identity'][:2] == expected['identity'][:2], 'Original directory replaced')
                require(entry['identity'][5:8] == expected['identity'][5:8] and entry['xattrs'] == expected['xattrs'],
                        'Original directory attributes changed')
                if not releasing:
                    require(entry['identity'][3] == expected['identity'][3], 'Original directory mtime changed')
                if not renamed:
                    require(entry == expected, 'Original directory changed before preservation')
        return current

    def capacities(self, remaining):
        require(Path('/').stat().st_dev == self.request['root_device'], 'Root filesystem changed')
        require(Path('/data').stat().st_dev == self.request['data_device'], 'Data filesystem changed')
        require(self.request['root_device'] != self.request['data_device'], 'Filesystems must differ')
        for path, device in ((self.parent, self.request['root_device']),
                             (self.backup_parent, self.request['data_device'])):
            safe_path(path)
            require((path if path.exists() else path.parent).stat().st_dev == device,
                    'Preservation path is on the wrong filesystem')
        info = os.statvfs(self.parent if self.parent.exists() else self.parent.parent)
        available = info.f_bavail * info.f_frsize
        require(available >= remaining + self.request['root_reserve_bytes'], 'Root reserve would be crossed')
        return available

    def check_references(self):
        installed_references(self.source, self.target)

    def check_closure(self):
        identities = {tuple(v['identity'][:2]) for v in self.baseline.values()}
        for root in (self.stage, self.target, self.backup):
            if present(root):
                identities.update(tuple(v['identity'][:2]) for v in inventory(root).values())
        closure([self.source, self.stage, self.target, self.backup], identities)

    def event(self, name):
        """Failure injection boundary for disposable tests; no production hook."""

    def load(self):
        self.previous = token(self.receipt)
        self.record = json.loads(self.previous[0])
        require(self.record['pins'] == self.pins and self.record['baseline'] == self.baseline,
                'Receipt belongs to another request/tool/inventory')
        require(self.record['schema'] == 1, 'Unknown receipt schema')
        for key in ('released', 'removed_dirs'):
            require(len(self.record[key]) == len(set(self.record[key])), 'Duplicate progress entry')
        require(set(self.record['released']) <= set(self.files) and
                set(self.record['removed_dirs']) <= set(self.dirs), 'Foreign progress entry')
        require(self.record['release_pending'] in [None, *self.files] and
                self.record['dir_pending'] in [None, *self.dirs], 'Foreign pending entry')

    def create_dir(self, path, key):
        safe_path(path)
        if key in self.record['directories']:
            require(directory_id(path) == self.record['directories'][key], 'Owned directory replaced')
            return
        pending = self.record['mkdir_pending']
        if pending is None:
            require(not present(path), 'Foreign directory exists: ' + str(path))
            self.record['mkdir_pending'] = {'path': str(path), 'key': key}
            self.save()
        else:
            require(pending == {'path': str(path), 'key': key}, 'Foreign directory intent')
        if not present(path):
            os.mkdir(path, 0o700)
            sync_dir(path.parent)
            self.event('mkdir')
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == OWNER and
                stat.S_IMODE(info.st_mode) == 0o700 and not list(path.iterdir()), 'Unproven directory creation')
        self.record['directories'][key] = directory_id(path)
        self.record['mkdir_pending'] = None
        self.save()

    def preflight(self):
        require(inventory(self.source) == self.baseline, 'Pinned source inventory changed')
        for path in (self.parent, self.backup_parent):
            safe_path(path); protected_parent(path.parent)
            require(not present(path), 'Pre-existing preservation container: ' + str(path))
        size = sum(self.baseline[n]['identity'][2] for n in self.files)
        available = self.capacities(size + 16 * 1024**2)
        self.check_closure(); self.check_references()
        require(inventory(self.source) == self.baseline, 'Source changed during preflight')
        return {'files': len(self.files), 'directories': len(self.dirs),
                'logical_bytes': size, 'root_available': available, 'mode': 'observe-only'}

    def begin(self):
        self.preflight()
        self.record = {'schema': 1, 'pins': self.pins, 'baseline': self.baseline,
                       'phase': 'copying', 'started_at': time.time(), 'directories': {},
                       'mkdir_pending': None, 'copied': {}, 'copy_pending': None,
                       'released': [], 'release_pending': None, 'removed_dirs': [], 'dir_pending': None}
        self.save()

    def copied(self, complete=False):
        root = self.target if self.record.get('target_published') else self.stage
        current = inventory(root)
        allowed = set(self.dirs) | set(self.record['copied'])
        pending = self.record['copy_pending']
        if pending:
            allowed.update([pending['name'], pending['temporary']])
        require(set(current) <= allowed, 'Foreign staged entry')
        require(set(self.record['copied']) <= set(current), 'Completed copy missing')
        for name, proof in self.record['copied'].items():
            require(current[name] == proof['entry'], 'Verified copy changed: ' + name)
        for name in set(self.dirs) & set(current):
            require(current[name]['identity'][:2] == self.record['directories']['tree:' + name],
                    'Copied directory replaced')
        if complete:
            require(set(current) == set(self.baseline), 'Incomplete copied inventory')
            for name in self.dirs:
                require(attributes(current[name]) == attributes(self.baseline[name]), 'Copied directory attributes changed')
        return current

    def copy_file(self, name):
        self.private(); self.original(); self.copied()
        source, destination = self.source / name, self.stage / name
        temporary_name = str(Path(name).with_name('.preserving-' + Path(name).name))
        temporary = self.stage / temporary_name
        pending = self.record['copy_pending']
        if pending is None:
            require(not present(destination) and not present(temporary), 'Foreign copy destination')
            self.record['copy_pending'] = pending = {'name': name, 'temporary': temporary_name, 'identity': None}
            self.save()
        require(pending['name'] == name and pending['temporary'] == temporary_name, 'Wrong pending copy')
        if present(destination):
            require(not present(temporary) and pending['identity'] == signature(destination.lstat())[:2],
                    'Unproven promoted copy')
            require(hash_file(source) == hash_file(destination), 'Interrupted promotion byte mismatch')
            require(attributes(inventory(destination.parent)[destination.name]) == attributes(self.baseline[name]),
                    'Interrupted promotion metadata mismatch')
            sha = hash_file(destination)
        else:
            if not present(temporary):
                require(pending['identity'] is None, 'Pending temporary disappeared')
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd); sync_dir(temporary.parent)
                self.event('temporary-created')
            info = temporary.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Unsafe temporary copy')
            if pending['identity'] is None:
                require(info.st_uid == OWNER and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == 0,
                        'Unproven temporary creation')
                pending['identity'] = signature(info)[:2]; self.save()
            require(signature(info)[:2] == pending['identity'], 'Temporary copy replaced')
            self.capacities(self.baseline[name]['identity'][2])
            sha = hashlib.sha256()
            with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as src, \
                    os.fdopen(os.open(temporary, os.O_WRONLY | os.O_NOFOLLOW), 'wb') as dst:
                require(signature(os.fstat(src.fileno())) == self.baseline[name]['identity'], 'Source changed before copy')
                require(signature(os.fstat(dst.fileno()))[:2] == pending['identity'], 'Temporary replaced before copy')
                dst.truncate(0)
                while chunk := src.read(8 * 1024**2):
                    dst.write(chunk); sha.update(chunk)
                    self.event('copy-chunk')
                dst.flush(); os.fsync(dst.fileno())
                require(signature(os.fstat(src.fileno())) == self.baseline[name]['identity'], 'Source changed during copy')
            metadata(temporary, self.baseline[name])
            with open(temporary, 'rb') as stream:
                os.fsync(stream.fileno())
            self.original()
            require(not present(destination), 'Copy destination appeared')
            os.rename(temporary, destination); sync_dir(destination.parent)
            self.event('copy-promoted')
            sha = sha.hexdigest()
        entry = inventory(destination.parent)[destination.name]
        require(attributes(entry) == attributes(self.baseline[name]), 'Copied metadata differs')
        self.record['copied'][name] = {'sha256': sha, 'entry': entry}
        self.record['copy_pending'] = None; self.save()

    def copy(self):
        self.create_dir(self.parent, 'target_parent')
        self.record['target_parent'] = self.record['directories']['target_parent']; self.save()
        self.create_dir(self.backup_parent, 'backup_parent')
        self.record['backup_parent'] = self.record['directories']['backup_parent']; self.save()
        self.private()
        for name in self.dirs:
            self.create_dir(self.stage / name, 'tree:' + name)
        self.original(); self.copied()
        pending = self.record['copy_pending']
        if pending:
            self.copy_file(pending['name'])
        for name in self.files:
            if name not in self.record['copied']:
                self.copy_file(name)
        for name in reversed(self.dirs):
            metadata(self.stage / name, self.baseline[name]); sync_dir(self.stage / name)
        self.original(); self.private()
        before = self.copied(complete=True)
        for name in self.files:
            require(hash_file(self.stage / name) == self.record['copied'][name]['sha256'], 'Independent target SHA mismatch')
            self.event('target-hashed')
        require(self.copied(complete=True) == before, 'Destination changed during full verification')
        self.original()
        # Persist the successful full byte proof before publication's fresh
        # closure scan, so a disappearing process namespace never forces an
        # unnecessary complete rehash on resume. Publication still fails closed.
        self.record['copy_proof'] = {'at': time.time(), 'inventory': before}
        self.record['phase'] = 'publishing'; self.save()

    def target_proof(self):
        require(self.record.get('copy_proof'), 'Full independent target proof missing')
        current = self.copied(complete=True)
        proof = self.record['copy_proof']['inventory']
        for name in self.files:
            require(current[name] == proof[name], 'Hashed target identity changed')
        for name in self.dirs:
            require(current[name]['identity'][:2] == proof[name]['identity'][:2] and
                    attributes(current[name]) == attributes(proof[name]), 'Hashed target directory changed')
        return current

    def publish(self):
        self.private(); self.original(); self.capacities(0); self.check_closure(); self.check_references()
        if not self.record.get('target_published'):
            self.record['target_intent'] = True; self.save()
            if present(self.stage):
                self.target_proof(); require(not present(self.target), 'Foreign final target')
                os.rename(self.stage, self.target); sync_dir(self.parent)
                self.event('target-renamed')
            else:
                require(directory_id(self.target) == self.record['directories']['tree:.'], 'Unproven target rename')
            self.record['target_published'] = True; self.save()
        self.target_proof()
        if not self.record.get('source_renamed'):
            self.record['source_intent'] = True; self.save()
            if not present(self.backup):
                require(inventory(self.source) == self.baseline, 'Source changed before preservation')
                os.rename(self.source, self.backup); sync_dir(self.source.parent); sync_dir(self.backup_parent)
                self.event('source-renamed')
            else:
                require(not present(self.source) and directory_id(self.backup) == self.baseline['.']['identity'][:2],
                        'Unproven original rename')
            self.record['source_renamed'] = True; self.save()
        self.original()
        if not self.record.get('logical_link'):
            self.record['link_intent'] = True; self.save()
            if not present(self.source):
                os.symlink(self.target, self.source); sync_dir(self.source.parent)
                self.event('symlink-created')
            require(self.source.is_symlink() and os.readlink(self.source) == str(self.target), 'Wrong logical link')
            info = self.source.lstat()
            require(info.st_uid == OWNER, 'Unproven symlink owner')
            self.record['logical_link'] = signature(info); self.save()
        self.logical_proof()
        self.record['logical_proof_at'] = time.time()
        self.record['phase'] = 'releasing'; self.save()

    def logical_proof(self):
        require(self.record.get('target_published') and self.record.get('logical_link'), 'Published logical proof missing')
        link = signature(self.source.lstat())
        require(link == self.record['logical_link'] and self.source.is_symlink() and
                os.readlink(self.source) == str(self.target), 'Logical link changed')
        before = self.target_proof()
        for name in self.files:
            with os.fdopen(os.open(self.source / name, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                require(signature(os.fstat(stream.fileno())) == before[name]['identity'], 'Logical read resolves to wrong inode')
        require(self.target_proof() == before and signature(self.source.lstat()) == link,
                'Logical target changed during read-through')

    def remove_original(self, name, directory=False):
        self.private(); self.logical_proof(); self.original(releasing=True)
        key, done = ('dir_pending', 'removed_dirs') if directory else ('release_pending', 'released')
        require(self.record[key] in (None, name), 'Other removal pending')
        self.record[key] = name; self.save()
        self.event('release-intent' if not directory else 'rmdir-intent')
        self.private(); self.logical_proof(); self.original(releasing=True)
        path = self.backup / name
        if present(path):
            expected = self.baseline[name]
            info = path.lstat()
            require((signature(info)[:2] == expected['identity'][:2]) if directory else
                    (signature(info) == expected['identity']), 'Original replaced before release')
            parent = path.parent if name != '.' else self.backup_parent
            leaf = path.name if name != '.' else self.backup.name
            pinned_parent = directory_id(parent)
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                require(signature(os.fstat(fd))[:2] == pinned_parent and directory_id(parent) == pinned_parent,
                        'Removal parent changed')
                actual = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
                require(signature(actual) == signature(info), 'Original entry changed before unlink')
                require(signature(self.source.lstat()) == self.record['logical_link'] and
                        os.readlink(self.source) == str(self.target), 'Logical link changed before unlink')
                if not directory:
                    with os.fdopen(os.open(self.source / name, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as logical:
                        require(signature(os.fstat(logical.fileno())) == self.record['copied'][name]['entry']['identity'],
                                'Logical target changed before unlink')
                # No recursive removal: only a manifest name in its pinned backup directory.
                (os.rmdir if directory else os.unlink)(leaf, dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(fd)
            self.event('rmdir-done' if directory else 'release-unlinked')
        self.record[done].append(name); self.record[key] = None; self.save()

    def release(self):
        require(self.record.get('logical_proof_at'), 'Staging-only receipt cannot release originals')
        self.private(); self.capacities(0); self.check_closure(); self.check_references(); self.logical_proof()
        self.original(releasing=True)
        for name in self.files:
            if name not in self.record['released']:
                self.remove_original(name)
        for name in reversed(self.dirs):
            if name not in self.record['removed_dirs']:
                self.remove_original(name, directory=True)
        self.logical_proof()
        self.record['phase'] = 'released'; self.save()

    def finish(self):
        require(set(self.record['released']) == set(self.files) and
                set(self.record['removed_dirs']) == set(self.dirs) and not present(self.backup),
                'Original duplicates remain')
        self.logical_proof(); self.capacities(0)
        # Restore ordinary traversal only after there is no longer a hash-to-unlink window.
        info = self.parent.lstat()
        require(directory_id(self.parent) == self.record['target_parent'] and info.st_uid == OWNER and
                stat.S_IMODE(info.st_mode) in (0o700, 0o755), 'Target ancestor changed')
        os.chmod(self.parent, 0o755); sync_dir(self.parent)
        self.event('access-restored')
        self.record['phase'] = 'complete'; self.record['completed_at'] = time.time()
        self.record['actual_available'] = {str(p): os.statvfs(p).f_bavail * os.statvfs(p).f_frsize
                                           for p in (self.parent, self.backup_parent)}
        self.save()

    def run(self, resume=False):
        if resume:
            self.load()
        else:
            require(not present(self.receipt), 'Receipt exists; explicit resume required')
            self.begin()
        if self.record['phase'] == 'copying':
            self.copy()
        if self.record['phase'] == 'publishing':
            self.publish()
        if self.record['phase'] == 'releasing':
            self.release()
        if self.record['phase'] == 'released':
            self.finish()
        require(self.record['phase'] == 'complete', 'Unknown preservation phase')
        self.logical_proof()
        parent_info = self.parent.lstat()
        require(directory_id(self.parent) == self.record['target_parent'] and parent_info.st_uid == OWNER and
                stat.S_IMODE(parent_info.st_mode) == 0o755, 'Completed target traversal changed')
        require(not present(self.backup), 'Unexpected original backup after completion')
        return {'phase': self.record['phase'], 'files_preserved': len(self.files),
                'receipt': str(self.receipt), 'target': str(self.target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', type=Path, required=True)
    parser.add_argument('--request-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Run as root for complete closure and metadata checks')
    raw = read_control(safe_path(args.request))
    require(hashlib.sha256(raw).hexdigest() == args.request_sha256, 'Request SHA mismatch')
    request = json.loads(raw)
    require(request['source'] == str(SOURCE) and request['target'] == str(TARGET) and
            request['backup_parent'] == str(BACKUP_PARENT), 'Request exceeds the specific reviewed paths')
    require(request['root_reserve_bytes'] >= 100 * GIB, 'Root reserve below reviewed minimum')
    tool_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    require(request['tool_sha256'] == tool_sha, 'Tool does not match reviewed request')
    baseline = request['metadata']
    for name in baseline:
        require(name == '.' or (not Path(name).is_absolute() and '..' not in Path(name).parts and
                               str(Path(name)) == name and not any(p.startswith('.preserving-') for p in Path(name).parts)),
                'Invalid manifest name')
    runner = Preserver(request, baseline, args.request_sha256, tool_sha)
    require(len(runner.files) == 166 and len(runner.dirs) == 61 and
            sum(baseline[n]['identity'][2] for n in runner.files) == 141618292399,
            'Request differs from the reviewed 166-file historical family')
    require(not args.resume or args.apply, '--resume requires --apply')
    safe_path(RECEIPT.parent); protected_parent(RECEIPT.parent)
    if not args.apply:
        print(json.dumps(runner.preflight(), indent=2, sort_keys=True))
        return
    lock = RECEIPT.with_suffix('.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                not info.st_mode & 0o077, 'Unsafe preservation lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.run(resume=args.resume)
        print(json.dumps(result, indent=2, sort_keys=True))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
