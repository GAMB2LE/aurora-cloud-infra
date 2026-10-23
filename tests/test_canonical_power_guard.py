import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import signal
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / 'roles/standby_replication/files'


def render_mirror(enabled=True):
    import shlex
    import yaml
    from jinja2 import Environment, StrictUndefined
    role_vars = yaml.safe_load((ROOT/'roles/standby_replication/vars/main.yml').read_text())
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters['quote'] = lambda value: shlex.quote(str(value))
    env.filters['bool'] = bool
    return env.from_string((ROOT/'roles/standby_replication/templates/aurora-standby-pull.j2').read_text()).render(
        aurora_standby_canonical_power_guard_enabled=enabled,
        aurora_standby_replication_enabled=True, aurora_site_env='development',
        aurora_standby_primary_user='azimuth', aurora_standby_primary_host='130.246.212.116',
        aurora_standby_primary_proxy_jump='rrniii@login.jasmin.ac.uk',
        aurora_standby_replication_ssh_private_key='/home/aurora/.ssh/id_rsa_jasmin_20200514',
        aurora_service_user='aurora', aurora_service_group='aurora',
        aurora_standby_replication_state_root='/var/lib/aurora-cloud/dev-live-mirror',
        aurora_dev_live_mirror_stamp='/data/aurora/internal/dev-live-mirror/last_success.json',
        aurora_standby_replication_rsync_path='sudo rsync', aurora_standby_primary_ssh_extra_args=[],
        aurora_standby_replication_delete=True,
        aurora_standby_replication_effective_paths=role_vars['aurora_standby_raw_stages']+role_vars['aurora_standby_product_stages'])

spec = importlib.util.spec_from_file_location('capacity_guard', FILES / 'aurora-canonical-power-guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize('root_free,data_free,allowed', [
    (30, 15, True), (22, 10, True), (21.99, 20, False), (40, 9.99, False),
])
def test_reserve_thresholds(root_free, data_free, allowed):
    with patch.object(guard, 'validate_layout'), patch.object(guard, 'free', side_effect=[int(root_free*guard.GIB),int(data_free*guard.GIB)]):
        if allowed:
            assert guard.check()['dataBytes'] >= 10*guard.GIB
        else:
            with pytest.raises(RuntimeError, match='reserve hold'):
                guard.check()


def test_wrong_bind_is_rejected_before_capacity_or_transfer():
    source = Mock(); source.is_symlink.return_value=False;source.is_dir.return_value=True
    target = Mock(); target.is_symlink.return_value=False;target.is_dir.return_value=True;target.stat.return_value=SimpleNamespace(st_dev=1)
    data = Mock();data.stat.return_value=SimpleNamespace(st_dev=2)
    root = Mock();root.stat.return_value=SimpleNamespace(st_dev=1)
    with patch.multiple(guard, SOURCE=source,TARGET=target,DATA=data,ROOT=root), patch.object(guard.os.path,'ismount',return_value=True), patch.object(guard.os.path,'samefile',return_value=False):
        with pytest.raises(RuntimeError,match='missing or points elsewhere'):
            guard.validate_layout('mounted')


def test_missing_data_mount_fails_closed():
    with patch.object(guard.os.path,'ismount',return_value=False):
        with pytest.raises(RuntimeError,match='separate mounted device'):
            guard.validate_layout('mounted')


@pytest.mark.parametrize('code', [0, 23, 24, 75])
def test_child_result_is_preserved(code):
    child=Mock(pid=123);child.wait.return_value=code;child.poll.return_value=code
    with patch.object(guard,'check'), patch.object(guard.subprocess,'Popen',return_value=child) as spawn:
        assert guard.run(['rsync','--dry-run']) == code
        spawn.assert_called_once_with(['rsync','--dry-run'],start_new_session=True)


def test_pressure_during_transfer_terminates_private_process_group():
    child=Mock(pid=123);child.wait.side_effect=[subprocess.TimeoutExpired('rsync',2),0];child.poll.return_value=None
    with patch.object(guard,'check',side_effect=[{},RuntimeError('reserve hold')]), patch.object(guard.subprocess,'Popen',return_value=child), patch.object(guard.os,'killpg') as kill:
        with pytest.raises(RuntimeError,match='reserve hold'):
            guard.run(['rsync'])
    kill.assert_called_once_with(123,signal.SIGTERM)


def test_pressure_before_transfer_does_not_start_writer():
    with patch.object(guard,'check',side_effect=RuntimeError('reserve hold')),patch.object(guard.subprocess,'Popen') as spawn:
        with pytest.raises(RuntimeError):guard.run(['rsync'])
    spawn.assert_not_called()


@pytest.mark.parametrize('exit_code', [0, 24])
def test_post_transfer_pressure_prevents_success(exit_code):
    child=Mock(pid=123);child.wait.return_value=exit_code;child.poll.return_value=exit_code
    with patch.object(guard,'check',side_effect=[{},RuntimeError('reserve hold')]),patch.object(guard.subprocess,'Popen',return_value=child):
        with pytest.raises(RuntimeError):guard.run(['rsync'])


def test_copy_requires_whole_copy_plus_reserve(tmp_path):
    src=tmp_path/'source';src.mkdir();(src/'one').write_bytes(b'1')
    with patch.object(guard,'SOURCE',src),patch.object(guard,'check'),patch.object(guard,'free',return_value=24*guard.GIB),patch.object(guard.subprocess,'Popen') as spawn:
        with pytest.raises(RuntimeError,match='full logical size'):guard.run(['rsync'],'copy')
    spawn.assert_not_called()


def test_copy_rejects_symlink_inventory(tmp_path):
    src=tmp_path/'source';src.mkdir();(src/'link').symlink_to('/etc/passwd')
    with patch.object(guard,'SOURCE',src),patch.object(guard,'check'),patch.object(guard.subprocess,'Popen') as spawn:
        with pytest.raises(RuntimeError,match='symlinks'):guard.run(['rsync'],'copy')
    spawn.assert_not_called()


def test_shared_script_limits_new_guard_to_power_and_preserves_other_branches():
    old=render_mirror(False);new=render_mirror(True)
    assert '--exclude=\'/ecmwf_solar_planning/***\'' in new
    assert 'guarded_command=(/usr/local/lib/aurora-canonical-power-guard.py -- rsync --bwlimit=8192)' in new
    assert '"$name" == product-power-components || "$name" == product-power-planning-cache' in new
    for name in ['raw-asfs','raw-hatprog5','product-hatprog5','product-vaisalamet','product-wxcam','product-rpgfmcw94','product-asfs-logger']:
        def branch(text):
            start=text.index('if [[ "$requested_stage" == "all" || "$requested_stage" == '+name+' ]]')
            return text[start:text.index('\nfi',start)+3]
        assert branch(old)==branch(new)
    assert 'exec 8>/run/aurora-canonical-power-mirror.lock' in new
    assert new.index('replicate_path product-power-planning-cache') < new.index("root / 'product-power.json'")
    assert new.index('if [[ "$power_mirror_held" == true ]]') < new.index('success_utc=$(date')
    unit=(FILES/'90-canonical-planning-cache.conf').read_text()
    assert 'RequiresMountsFor=/data/aurora/products/power/ecmwf_solar_planning' in unit


def test_power_unit_guard_is_failure_visible():
    unit=(FILES/'90-canonical-planning-cache.conf').read_text()
    assert 'ExecStartPre=' in unit
    assert 'ExecCondition=' not in unit


def test_opt_in_preserves_all_non_power_stages_and_default_transport():
    import re
    off, on = render_mirror(False), render_mirror(True)
    branch = re.compile(r'^if \[\[ "\$requested_stage" == "all" \|\| "\$requested_stage" == ([\w-]+) \]\]; then\n(.*?)^fi$', re.M | re.S)
    before = dict(branch.findall(off))
    after = dict(branch.findall(on))
    assert before.keys() == after.keys()
    for name in before:
        if name != 'product-power':
            assert before[name] == after[name], name
    assert 'product-power-components' not in before['product-power']
    assert "replicate_path product-power /data/aurora/products/power/ /data/aurora/products/power/ '' --exclude='*.lock' --exclude='*.tmp'" in before['product-power']
    assert "--exclude='Y2026/**' --exclude='.radar-partials/**'" in before['raw-rpgfmcw94']
    assert '/data/aurora/products/blesensors/blesensors.lock' in after['product-blesensors']
    for script in (off, on):
        subprocess.run(['bash', '-n'], input=script, text=True, check=True)


def test_role_fails_closed_before_persistent_writes_and_scopes_unit_to_power():
    import yaml
    defaults = yaml.safe_load((ROOT/'roles/standby_replication/defaults/main.yml').read_text())
    assert defaults['aurora_standby_canonical_power_guard_enabled'] is False
    opted_in = []
    for path in (ROOT/'inventory/host_vars').glob('*.yml'):
        if 'aurora_standby_canonical_power_guard_enabled: true' in path.read_text():
            opted_in.append(path.stem)
    assert opted_in == ['aurora-cloud-droplet']
    tasks = yaml.safe_load((ROOT/'roles/standby_replication/tasks/main.yml').read_text())
    guard_index = next(i for i,t in enumerate(tasks) if t.get('ansible.builtin.include_tasks') == 'canonical_power_guard.yml')
    mirror_index = next(i for i,t in enumerate(tasks) if t.get('ansible.builtin.template', {}).get('src') == 'aurora-standby-pull.j2')
    assert guard_index < mirror_index
    assert 'aurora_standby_canonical_power_guard_enabled | default(false) | bool' in tasks[guard_index]['when']
    scoped = yaml.safe_load((ROOT/'roles/standby_replication/tasks/canonical_power_guard.yml').read_text())
    assert scoped[0]['ansible.builtin.assert']['that'][0] == "inventory_hostname == 'aurora-cloud-droplet'"
    assert scoped[1]['ansible.builtin.script']['cmd'] == 'aurora-canonical-power-guard.py --check'
    assert scoped[1]['check_mode'] is False
    assert scoped[1]['changed_when'] is False
    assert all('ansible.builtin.systemd' not in t and 'ansible.builtin.mount' not in t for t in scoped)
    destinations = [t['ansible.builtin.copy']['dest'] for t in scoped if 'ansible.builtin.copy' in t]
    assert destinations == ['/usr/local/lib/aurora-canonical-power-guard.py', '/etc/systemd/system/aurora-dev-live-pull@product-power.service.d/90-canonical-planning-cache.conf']






def shell_fixture(tmp_path):
    import os
    import json
    bindir=tmp_path/'bin';bindir.mkdir()
    def executable(name,body):
        path=bindir/name;path.write_text(body);path.chmod(0o755);return path
    executable('chown','#!/bin/sh\nexit 0\n')
    executable('flock','''#!/usr/bin/env python3
import fcntl,os,sys
fd=int(sys.argv[-1])
if fd==8 and os.environ.get('POWER_HELD')=='1' and '-u' not in sys.argv:raise SystemExit(1)
fcntl.flock(fd,fcntl.LOCK_UN if '-u' in sys.argv else fcntl.LOCK_EX|fcntl.LOCK_NB)
''')
    executable('rsync','''#!/usr/bin/env python3
import json,os,sys
with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
size=200 if any(a.endswith('/ecmwf_solar_planning/') for a in sys.argv) else 100
print('Total transferred file size: '+str(size))
''')
    fake_guard=executable('guard','''#!/usr/bin/env python3
import os,sys
if os.environ.get('DENY_GUARD')=='1':raise SystemExit(75)
if os.environ.get('DENY_PLANNING')=='1' and any(a.endswith('/ecmwf_solar_planning/') for a in sys.argv):raise SystemExit(75)
os.execvp(sys.argv[2],sys.argv[2:])
''')
    script=render_mirror(True)
    for prefix in ['/data/aurora','/project/aurora','/var/lib/aurora-cloud','/home/aurora','/run/aurora']:
        script=script.replace(prefix,str(tmp_path/prefix.lstrip('/')))
    script=script.replace('/usr/local/lib/aurora-canonical-power-guard.py',str(fake_guard))
    path=tmp_path/'mirror';path.write_text(script)
    (tmp_path/'run').mkdir()
    key=tmp_path/'home/aurora/.ssh/id_rsa_jasmin_20200514';key.parent.mkdir(parents=True);key.write_text('test-fixture-not-a-key')
    state=tmp_path/'var/lib/aurora-cloud/dev-live-mirror';state.mkdir(parents=True)
    (state/'last_success_utc').write_text('previous-global')
    (state/'product-power.json').write_text('{"previous": true}')
    public=tmp_path/'data/aurora/internal/dev-live-mirror/last_success.json';public.parent.mkdir(parents=True);public.write_text('previous-public')
    env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CALLS':str(tmp_path/'calls.jsonl')}
    return path,state,public,env


@pytest.mark.parametrize('held,denied,planning_denied', [(True,False,False),(False,True,False),(False,False,True)])
def test_actual_shell_power_hold_or_failure_never_stamps_success(tmp_path,held,denied,planning_denied):
    import json
    path,state,public,env=shell_fixture(tmp_path)
    env.update(POWER_HELD=str(int(held)),DENY_GUARD=str(int(denied)),DENY_PLANNING=str(int(planning_denied)))
    result=subprocess.run(['bash',str(path),'product-power'],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==75,result.stderr
    assert json.loads((state/'product-power.json').read_text())=={'previous':True}
    assert (state/'last_success_utc').read_text()=='previous-global'
    assert public.read_text()=='previous-public'


def test_actual_shell_success_aggregates_both_guarded_transfers(tmp_path):
    import json
    path,state,public,env=shell_fixture(tmp_path)
    result=subprocess.run(['bash',str(path),'product-power'],env=env,capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    stamp=json.loads((state/'product-power.json').read_text())
    assert stamp['transferred_bytes']==300
    assert stamp['stage']=='product-power'
    calls=[json.loads(line) for line in Path(env['CALLS']).read_text().splitlines()]
    assert len(calls)==2
    assert '--exclude=/ecmwf_solar_planning/***' in calls[0]
    assert '--bwlimit=8192' in calls[0] and '--bwlimit=8192' in calls[1]
    assert calls[1][-1].endswith('/ecmwf_solar_planning/')


def test_actual_all_stage_hold_continues_other_stages_without_global_success(tmp_path):
    import json
    path,state,public,env=shell_fixture(tmp_path);env['POWER_HELD']='1'
    result=subprocess.run(['bash',str(path),'all'],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==75,result.stderr
    calls=[json.loads(line) for line in Path(env['CALLS']).read_text().splitlines()]
    assert any(args[-1].endswith('/products/quicklooks/') for args in calls)
    assert not any(args[-1].endswith('/products/power/') for args in calls)
    assert json.loads((state/'product-power.json').read_text())=={'previous':True}
    assert (state/'last_success_utc').read_text()=='previous-global'
    assert public.read_text()=='previous-public'


