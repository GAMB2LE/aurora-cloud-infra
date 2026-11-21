"""Observe an exact GWS destination and any incomplete stages read-only."""
import json
import os
from pathlib import Path
import re
import sys

ROOT=Path('/gws/ssde/j25b/gamb2le/data/incoming/aurora-cloud/raw/rpgfmcw94')
PATH=re.compile(r'Y[0-9]{4}/M[0-9]{2}/D[0-9]{2}/[A-Za-z0-9_.-]+\.(?:LV0|LV0\.NC|LV1|LV1\.NC)\Z')


def observe(root,relative):
    root=Path(root)
    if not root.is_absolute() or '..' in root.parts or not isinstance(relative,str) or not PATH.fullmatch(relative):
        raise ValueError('unsafe GWS target')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    fds=[os.open('/',flags)]
    try:
        parts=(*root.parts[1:],*Path(relative).parts[:-1])
        for part in parts:
            fds.append(os.open(part,flags,dir_fd=fds[-1]))
        directory=fds[-1];name=Path(relative).name
        try:
            os.stat(name,dir_fd=directory,follow_symlinks=False);canonical=True
        except FileNotFoundError:
            canonical=False
        prefix='.'+name+'.'
        partials=sorted(n for n in os.listdir(directory) if n.startswith(prefix) and n.endswith('.partial'))
        # Directory identity is held open; a later publisher must recheck.
        st=os.fstat(directory)
        return {'relative_path':relative,'canonical_present':canonical,'partials':partials,
                'directory_identity':[st.st_dev,st.st_ino], 'eviction_authorized':False,
                'retention_authorized':False}
    finally:
        for fd in reversed(fds):os.close(fd)


if __name__=='__main__':
    print(json.dumps(observe(ROOT,sys.argv[1]),sort_keys=True))
