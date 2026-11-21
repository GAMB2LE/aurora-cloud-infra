#!/usr/bin/env python3
"""Reserve and observe one read-only native radar probe in the Windows session.

One invocation is one observation, not a finalization receipt. The durable
intent prevents a lost SSH response from silently launching another probe.
The interactive task remains available for reconciliation until its result is
durably captured and its exact action has been authenticated.
"""
import argparse
import base64
from datetime import datetime
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from uuid import uuid4

NAME = re.compile(r'Y(\d{4})/M(\d{2})/D(\d{2})/rpgfmcw94-aurora_(\d{6})_(\d{6})_(P\d+_ZEN)\.(LV[01])\Z')
TASK_PREFIX = 'aurora-radar-probe-'
WINDOWS_ROOT = 'D:\\rpgfmcw94\\'


def validate_pair(source, successor):
    parsed = []
    for value in (source, successor):
        match = NAME.fullmatch(value) if isinstance(value, str) else None
        if match is None:
            raise ValueError('invalid native radar path')
        year, month, day, datepart, clock, programme, level = match.groups()
        timestamp = datetime.strptime(datepart + clock, '%y%m%d%H%M%S')
        if (timestamp.year, timestamp.month, timestamp.day) != (int(year), int(month), int(day)):
            raise ValueError('native filename and directory date differ')
        parsed.append((timestamp, programme, level))
    if parsed[0][1:] != parsed[1][1:] or parsed[1][0] <= parsed[0][0]:
        raise ValueError('successor is not a later file in the same programme and level')


def ps_wrapper(text):
    data = bytearray(gzip.compress(text.encode('utf-8'), mtime=0))
    data[9] = 255  # Python 3.12 and 3.13 otherwise encode different gzip OS bytes.
    compressed = base64.b64encode(data).decode('ascii')
    return ("$b=[Convert]::FromBase64String('" + compressed + "'); "
            "$m=[IO.MemoryStream]::new($b); "
            "$g=[IO.Compression.GzipStream]::new($m,[IO.Compression.CompressionMode]::Decompress); "
            "$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8); Invoke-Expression $r.ReadToEnd()")


def encode_ps(text):
    result = base64.b64encode(ps_wrapper(text).encode('utf-16le')).decode('ascii')
    if len(result) > 7000:
        raise ValueError('Windows command exceeds the bounded transport length')
    return result


def job_script(operation, source, successor):
    validate_pair(source, successor)
    if not re.fullmatch('[0-9a-f]{32}', operation):
        raise ValueError('invalid probe operation')
    path = WINDOWS_ROOT + source.replace('/', '\\')
    newer = WINDOWS_ROOT + successor.replace('/', '\\')
    receipt = 'C:\\Users\\aurora\\AppData\\Local\\Temp\\' + TASK_PREFIX + operation + '.json'
    # All interpolated strings have already passed the native-path/UUID grammar.
    return f'''$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$r=@{{schema='radar-windows-observation-v1'; operation='{operation}'; source='{source}'; successor='{successor}'}}
try {{
  $p='{path}'; $n='{newer}'
  $a=Get-Item -LiteralPath $p; $s=Get-Item -LiteralPath $n
  if($a.PSIsContainer -or $s.PSIsContainer -or ($a.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($s.Attributes -band [IO.FileAttributes]::ReparsePoint)) {{ throw 'not regular files' }}
  $hash=(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()
  $b=Get-Item -LiteralPath $p; $t=Get-Item -LiteralPath $n
  if($a.Length -ne $b.Length -or $a.LastWriteTimeUtc.Ticks -ne $b.LastWriteTimeUtc.Ticks -or $s.Length -ne $t.Length -or $s.LastWriteTimeUtc.Ticks -ne $t.LastWriteTimeUtc.Ticks) {{ throw 'source or successor changed during observation' }}
  $r.size=[long]$b.Length; $r.mtime_ticks=[long]$b.LastWriteTimeUtc.Ticks; $r.sha256=$hash
  $r.successor_size=[long]$t.Length; $r.successor_mtime_ticks=[long]$t.LastWriteTimeUtc.Ticks
  $r.observed_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds(); $r.ok=$true
}} catch {{ $r.ok=$false; $r.error=$_.Exception.Message }}
$raw=[Text.Encoding]::UTF8.GetBytes(($r | ConvertTo-Json -Compress))
$f=[IO.File]::Open('{receipt}',[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try {{ $f.Write($raw,0,$raw.Length); $f.Flush($true) }} finally {{ $f.Dispose() }}
'''


def durable_write(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(directory)
    finally: os.close(directory)


def read_state(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or before.st_mode & 0o077 or before.st_size > 1024 * 1024):
            raise ValueError('unsafe probe state file')
        raw = stream.read(1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('probe state changed while reading')
    return json.loads(raw)


def powershell(script, *, jump):
    inner = ['ssh', '-oBatchMode=yes', '-oIdentitiesOnly=yes', '-oConnectTimeout=10',
             '-oStrictHostKeyChecking=yes', '-oUserKnownHostsFile=/home/aurora/.ssh/known_hosts',
             '-i', '/home/aurora/.ssh/id_ed_gamb2le', 'aurora@100.121.25.32',
             'powershell', '-NoProfile', '-NonInteractive', '-Command', '-']
    command = (['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', 'ass-proxmox-linux', shlex.join(inner)]
               if jump else inner)
    result = subprocess.run(command, input=ps_wrapper(script) + '\n', capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError('Windows task control failed; reconcile the recorded operation')
    try:
        return json.loads(result.stdout.strip())
    except ValueError:
        raise RuntimeError('Windows task returned an invalid response; reconcile the recorded operation') from None


def task_parts(record):
    if (record.get('schema') not in {'radar-windows-probe-intent-v1', 'radar-windows-probe-intent-v2'}
            or record.get('retention_authorized') is not False or record.get('eviction_authorized') is not False):
        raise ValueError('probe intent schema or authority differs')
    if record['schema'] == 'radar-windows-probe-intent-v2' and record.get('execution_limit_seconds') != 1200:
        raise ValueError('probe execution limit differs')
    operation = record['operation']
    script = job_script(operation, record['source'], record['successor'])
    digest = hashlib.sha256(script.encode()).hexdigest()
    if digest != record['job_sha256']:
        raise ValueError('probe action differs from durable intent')
    name = TASK_PREFIX + operation
    arguments = '-NoProfile -NonInteractive -EncodedCommand ' + encode_ps(script)
    receipt = 'C:\\Users\\aurora\\AppData\\Local\\Temp\\' + name + '.json'
    return name, arguments, receipt


def reserve(root, source, successor):
    validate_pair(source, successor)
    for previous in root.iterdir():
        if not re.fullmatch('[0-9a-f]{32}', previous.name):
            continue
        info = previous.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unsafe prior probe directory')
        try:
            complete = read_state(previous / 'complete.json')
        except FileNotFoundError:
            raise ValueError('a previous probe requires reconciliation') from None
        if complete != {'cleanup_confirmed': True}:
            raise ValueError('prior probe cleanup evidence differs')
    operation = uuid4().hex
    script = job_script(operation, source, successor)
    record = {'schema': 'radar-windows-probe-intent-v2', 'operation': operation,
              'source': source, 'successor': successor,
              'job_sha256': hashlib.sha256(script.encode()).hexdigest(),
              'execution_limit_seconds': 1200,
              'retention_authorized': False, 'eviction_authorized': False}
    task_parts(record)  # Refuse locally before recording a launch if action transport cannot be encoded.
    directory = root / operation
    directory.mkdir(mode=0o700)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(parent)
    finally: os.close(parent)
    durable_write(directory / 'intent.json', (json.dumps(record, sort_keys=True) + '\n').encode())
    return directory, record


def launch(record, *, jump):
    name, arguments, receipt = task_parts(record)
    settings = ("$s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew; "
                if record['schema'] == 'radar-windows-probe-intent-v2' else '')
    settings_arg = ' -Settings $s' if settings else ''
    script = f'''$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
if ((Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath '{receipt}')) {{ throw 'operation already exists' }}
$a=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '{arguments}'
$p=New-ScheduledTaskPrincipal -UserId ($env:COMPUTERNAME+'\\aurora') -LogonType Interactive -RunLevel Limited
{settings}Register-ScheduledTask -TaskName '{name}' -Action $a -Principal $p{settings_arg} | Out-Null
Start-ScheduledTask -TaskName '{name}'
@{{started=$true}} | ConvertTo-Json -Compress'''
    result = powershell(script, jump=jump)
    if result != {'started': True}:
        raise RuntimeError('probe launch result differs; reconcile the recorded operation')


def validate_proof(record, proof):
    if (not isinstance(proof, dict) or proof.get('schema') != 'radar-windows-observation-v1'
            or proof.get('operation') != record['operation'] or proof.get('source') != record['source']
            or proof.get('successor') != record['successor'] or proof.get('ok') is not True
            or type(proof.get('size')) is not int or proof['size'] <= 0
            or type(proof.get('mtime_ticks')) is not int or proof['mtime_ticks'] <= 0
            or type(proof.get('successor_size')) is not int or proof['successor_size'] <= 0
            or type(proof.get('successor_mtime_ticks')) is not int or proof['successor_mtime_ticks'] <= 0
            or type(proof.get('observed_at')) is not int
            or not isinstance(proof.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', proof['sha256'])):
        raise RuntimeError('probe receipt did not verify the exact source and successor')
    return proof


def validate_attestation(record, proof, attestation):
    name, arguments, _ = task_parts(record)
    if (not isinstance(attestation, dict) or attestation.get('schema') != 'radar-windows-task-attestation-v1'
            or attestation.get('operation') != record['operation'] or attestation.get('task') != name
            or attestation.get('job_sha256') != record['job_sha256']
            or attestation.get('arguments_sha256') != hashlib.sha256(arguments.encode()).hexdigest()
            or attestation.get('logon_type') != 'Interactive' or attestation.get('run_level') != 'Limited'
            or not isinstance(attestation.get('principal'), str)
            or not re.fullmatch(r'(?:[A-Za-z0-9-]+\\)?aurora', attestation['principal'], re.IGNORECASE)
            or type(attestation.get('last_result')) is not int or attestation['last_result'] != 0
            or not isinstance(attestation.get('last_run_utc'), str)
            or not attestation['last_run_utc']
            or attestation.get('receipt_sha256') != hashlib.sha256(
                json.dumps(proof, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()):
        raise ValueError('Windows task attestation differs from the captured observation')
    try:
        started = datetime.fromisoformat(attestation['last_run_utc'].replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Windows task start time is invalid') from None
    if started.tzinfo is None or started.timestamp() >= proof['observed_at'] + 1:
        raise ValueError('Windows task start time differs from observation')
    if record['schema'] == 'radar-windows-probe-intent-v2' and (
            attestation.get('execution_time_limit') != 'PT20M' or attestation.get('multiple_instances') != 'IgnoreNew'):
        raise ValueError('Windows task execution bounds differ')
    return attestation


def poll(record, *, jump):
    name, arguments, receipt = task_parts(record)
    settings_guard = ("if($t.Settings.ExecutionTimeLimit -ne 'PT20M' -or $t.Settings.MultipleInstances.ToString() -ne 'IgnoreNew') { throw 'task execution bounds differ' }"
                      if record['schema'] == 'radar-windows-probe-intent-v2' else '')
    script = f'''$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$t=Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue
if(-not $t) {{ @{{task_exists=$false}} | ConvertTo-Json -Compress; exit }}
if($t.Actions.Count -ne 1 -or $t.Actions[0].Execute -ne 'powershell.exe' -or $t.Actions[0].Arguments -cne '{arguments}' -or $t.Principal.LogonType -ne 'Interactive' -or $t.Principal.RunLevel -ne 'Limited' -or ($t.Principal.UserId -ine 'aurora' -and $t.Principal.UserId -ine ($env:COMPUTERNAME+'\\aurora'))) {{ throw 'task action or principal differs' }}
{settings_guard}
$r=$null; if(Test-Path -LiteralPath '{receipt}') {{ $r=[IO.File]::ReadAllText('{receipt}') }}
$i=Get-ScheduledTaskInfo -TaskName '{name}'
$u=Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop; $j=Get-ScheduledTaskInfo -TaskName '{name}'
if($u.Actions.Count -ne 1 -or $u.Actions[0].Execute -ne 'powershell.exe' -or $u.Actions[0].Arguments -cne '{arguments}' -or $u.Principal.UserId -ine $t.Principal.UserId -or $u.Principal.LogonType -ne $t.Principal.LogonType -or $u.Principal.RunLevel -ne $t.Principal.RunLevel -or $u.Settings.ExecutionTimeLimit -ne $t.Settings.ExecutionTimeLimit -or $u.Settings.MultipleInstances -ne $t.Settings.MultipleInstances -or $u.State -ne $t.State -or $j.LastRunTime -ne $i.LastRunTime -or $j.LastTaskResult -ne $i.LastTaskResult) {{ throw 'task changed during observation' }}
@{{task_exists=$true; state=$t.State.ToString(); receipt=$r; principal=$t.Principal.UserId;
  logon_type=$t.Principal.LogonType.ToString(); run_level=$t.Principal.RunLevel.ToString();
  last_result=[int64]$i.LastTaskResult; last_run_utc=$i.LastRunTime.ToUniversalTime().ToString('o');
  execution_time_limit=$t.Settings.ExecutionTimeLimit; multiple_instances=$t.Settings.MultipleInstances.ToString()}} | ConvertTo-Json -Compress'''
    result = powershell(script, jump=jump)
    if not isinstance(result, dict) or result.get('task_exists') is not True:
        raise RuntimeError('recorded task is missing; outcome is unresolved')
    if result.get('state') == 'Running':
        return None
    if result.get('state') != 'Ready':
        raise RuntimeError('probe task state is not recognized')
    if result.get('receipt') is None:
        if result.get('last_result') in (267009, 267011):
            return None  # Scheduling has not yet resolved; never relaunch.
        raise RuntimeError('probe finished without a receipt; outcome is unresolved')
    try:
        proof = json.loads(result['receipt'])
    except (ValueError, TypeError):
        raise RuntimeError('probe receipt is invalid') from None
    proof = validate_proof(record, proof)
    attestation = {'schema': 'radar-windows-task-attestation-v1', 'operation': record['operation'],
                   'task': name, 'job_sha256': record['job_sha256'],
                   'arguments_sha256': hashlib.sha256(arguments.encode()).hexdigest(),
                   'principal': result.get('principal'), 'logon_type': result.get('logon_type'),
                   'run_level': result.get('run_level'), 'last_result': result.get('last_result'),
                   'last_run_utc': result.get('last_run_utc'),
                   'receipt_sha256': hashlib.sha256(json.dumps(proof, sort_keys=True,
                       separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()}
    if record['schema'] == 'radar-windows-probe-intent-v2':
        attestation['execution_time_limit'] = result.get('execution_time_limit')
        attestation['multiple_instances'] = result.get('multiple_instances')
    return proof, validate_attestation(record, proof, attestation)


def cleanup(record, *, jump):
    name, arguments, receipt = task_parts(record)
    settings_guard = ("if($t.Settings.ExecutionTimeLimit -ne 'PT20M' -or $t.Settings.MultipleInstances.ToString() -ne 'IgnoreNew') { throw 'task execution bounds differ' }"
                      if record['schema'] == 'radar-windows-probe-intent-v2' else '')
    script = f'''$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
$t=Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue
if($t) {{
  if($t.State.ToString() -eq 'Running' -or $t.Actions.Count -ne 1 -or $t.Actions[0].Execute -ne 'powershell.exe' -or $t.Actions[0].Arguments -cne '{arguments}' -or $t.Principal.LogonType -ne 'Interactive' -or $t.Principal.RunLevel -ne 'Limited' -or ($t.Principal.UserId -ine 'aurora' -and $t.Principal.UserId -ine ($env:COMPUTERNAME+'\\aurora'))) {{ throw 'task identity differs or is running' }}
  {settings_guard}
  Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false
}} elseif(Test-Path -LiteralPath '{receipt}') {{ throw 'receipt remains but recorded task is absent' }}
Remove-Item -LiteralPath '{receipt}' -Force -ErrorAction SilentlyContinue
@{{cleaned=$true}} | ConvertTo-Json -Compress'''
    if powershell(script, jump=jump) != {'cleaned': True}:
        raise RuntimeError('probe cleanup was not confirmed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-root', required=True, type=Path)
    parser.add_argument('--jump', action='store_true', help='reach Windows through ass-proxmox-linux')
    parser.add_argument('--resume', help='resume a recorded operation ID')
    parser.add_argument('--source')
    parser.add_argument('--successor')
    args = parser.parse_args()
    root = args.state_root
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('unsafe probe state directory')
    fd = os.open(root / 'probe.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.resume:
            if not re.fullmatch('[0-9a-f]{32}', args.resume): parser.error('invalid operation ID')
            directory = root / args.resume
            d = directory.lstat()
            if not stat.S_ISDIR(d.st_mode) or d.st_uid != os.getuid() or d.st_mode & 0o077:
                raise ValueError('unsafe probe operation directory')
            record = read_state(directory / 'intent.json')
            if record.get('operation') != args.resume: raise ValueError('probe intent differs')
        else:
            if not args.source or not args.successor: parser.error('source and successor are required')
            directory, record = reserve(root, args.source, args.successor)
            durable_write(directory / 'launch-intent.json', b'{"launch_requested":true}\n')
            print('probe operation ' + record['operation'], file=sys.stderr, flush=True)
            launch(record, jump=args.jump)
        proof_path = directory / 'observation.json'
        attestation_path = directory / 'attestation.json'
        if proof_path.exists():
            proof = validate_proof(record, read_state(proof_path))
            validate_attestation(record, proof, read_state(attestation_path))
        else:
            result = poll(record, jump=args.jump)
            if result is None:
                print(json.dumps({'state': 'awaiting-observation', 'operation': record['operation']}))
                return
            proof, attestation = result
            if attestation_path.exists():
                if read_state(attestation_path) != attestation:
                    raise ValueError('prior task attestation changed')
            else:
                durable_write(attestation_path, (json.dumps(attestation, sort_keys=True) + '\n').encode())
            durable_write(proof_path, (json.dumps(proof, sort_keys=True) + '\n').encode())
        cleanup(record, jump=args.jump)
        if not (directory / 'complete.json').exists():
            durable_write(directory / 'complete.json', b'{"cleanup_confirmed":true}\n')
        print(json.dumps({'state': 'observed', 'proof': proof}, sort_keys=True))
    finally:
        os.close(fd)


if __name__ == '__main__':
    main()
