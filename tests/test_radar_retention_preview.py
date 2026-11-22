import hashlib
import importlib.util
import json
from pathlib import Path
import types

import pytest

ROOT=Path(__file__).parents[1]/'roles/radar_raw_relocation/files'


def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


@pytest.fixture
def preview(tmp_path):
    module=load('preview','aurora-cloud-radar-retention-preview.py')
    evidence=load('preview_evidence','radar_retention_evidence.py')
    day='Y2026/M05/D04';stem=day.replace('/','-')
    control=types.SimpleNamespace(DAYS={day},ORIGINALS=tmp_path/'originals',ARTIFACTS=tmp_path/'proofs',
        PROOFS=tmp_path/'historical',STATE=tmp_path/'state',IMAGES=tmp_path/'images',
        sha=evidence.sha,signature=evidence.signature,available=lambda:300*2**30)
    for p in (control.ARTIFACTS,control.PROOFS,control.STATE,control.IMAGES):p.mkdir()
    source=control.ORIGINALS/day;source.mkdir(parents=True)
    files={};historical={}
    for i in range(96):
        p=source/('observation-'+str(i));p.write_bytes(('science-'+str(i)).encode())
        row={'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        historical[p.name]=row;files[p.name]={**row,'signature':evidence.signature(p.stat())}
    proof=json.dumps({'day':day,'files':historical}).encode();(control.PROOFS/(stem+'.json')).write_bytes(proof)
    (control.ARTIFACTS/(stem+'-source-readback.json')).write_text(json.dumps({'day':day,'files':files,'proof_sha256':evidence.sha(proof)}))
    helper=types.SimpleNamespace(routes=lambda:({},None),control=lambda p:p.read_bytes(),guard=lambda *a:None)
    control.helper=lambda:helper;control.raw_gate=lambda:None
    def archive(*a):raise FileNotFoundError('archive readback must be prepared')
    control.archive_evidence=archive
    def forbidden(*a):raise AssertionError('preview attempted a mutation')
    for name in ('prepare','retire','image_route','remove_duplicates'):setattr(control,name,forbidden)
    return module,control,day,helper


def snapshot(base):
    return {str(p.relative_to(base)):(p.read_bytes(),p.stat().st_mtime_ns) for p in base.rglob('*') if p.is_file()}


def test_exact_candidate_preview_is_read_only_and_preparation_is_explicit(preview):
    m,c,day,_=preview;before=snapshot(c.ARTIFACTS.parent)
    result=m.build_plan(c,[day],1)
    assert result['dry_run'] and result['deletion_authorized'] is False
    row=result['candidates'][0]
    assert len(row['files'])==96 and all(f['current_version_matches'] for f in row['files'])
    assert row['preparation_required']=={'stage_and_fully_verify_image':True,'dual_archive_readback':True}
    assert row['net_bytes_after_existing_image'] is None
    assert 'full image hash and immutable seal' in row['mandatory_before_any_unlink']
    assert snapshot(c.ARTIFACTS.parent)==before


def test_source_drift_is_reported_and_the_changed_version_is_preserved(preview):
    m,c,day,_=preview;p=c.ORIGINALS/day/'observation-0';p.write_bytes(b'new scientific version')
    before=snapshot(c.ARTIFACTS.parent);result=m.build_plan(c,[day],1)
    assert any('source version differs' in b for b in result['candidates'][0]['blockers'])
    assert snapshot(c.ARTIFACTS.parent)==before


def test_retired_day_refill_is_not_silently_skipped(preview):
    m,c,day,helper=preview;helper.routes=lambda:({day:{}},'pin')
    (c.STATE/(day.replace('/','-')+'-result.json')).write_text('{}')
    before=snapshot(c.ARTIFACTS.parent)
    with pytest.raises(ValueError,match='refilled'):m.build_plan(c,[day],1)
    assert snapshot(c.ARTIFACTS.parent)==before


def test_stale_raw_evidence_cannot_make_the_preview_authorize_deletion(preview):
    m,c,day,_=preview
    def stale():raise ValueError('evidence stale')
    c.raw_gate=stale;result=m.build_plan(c,[day],1)
    assert result['raw_gate_ready'] is False and result['deletion_authorized'] is False
    assert result['raw_gate_reason']=='evidence stale'
