import hashlib,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'roles/radar_archive_recovery/files/coordinator'))
from per_file_queue import Queue,packed
from delegated_gws_attempt import retire,resolved_version,SCHEMA,MARKER


def attempt(tmp_path):
    prep=tmp_path/'prep';prep.mkdir();request=b'{"request":"original"}';(prep/'request.json').write_bytes(request)
    q=Queue(tmp_path/'queue.sqlite');expected={'relative_path':'Y2026/M11/D20/example.LV0','size':10,'mtime_ns':100,'sha256':'a'*64}
    q.add(expected);op=q.begin(expected,'gws-verified',{'unit':'failed.service','preparation':{'out':str(prep)}})
    receipt={'schema':SCHEMA,'operation_id':op['id'],'expected':expected,'unit':'failed.service',
             'intent_sha256':hashlib.sha256(packed(op['intent']).encode()).hexdigest(),
             'worker_exit_code':1,'verified':False,'transfer_started':True,'canonical_publication_by_attempt':False,
             'reason':'canonical-published-by-primary-dispatcher','retention_authorized':False,'eviction_authorized':False,
             'worker_invocation_id':'e'*32,'request_sha256':hashlib.sha256(request).hexdigest()}
    for k in ('worker_terminal','commit_journal_absent','fence_journal_absent','readback_journal_absent',
              'partial_preserved','primary_sha256_verified','gws_sha256_verified','object_get_sha256_verified','dispatcher_exact_dual_delivered'):receipt[k]=True
    for k in ('worker_log_sha256','transfer_journal_sha256','preflight_sha256'):receipt[k]='b'*64
    return q,op,receipt,prep


def test_superseded_attempt_closes_without_claiming_a_stage(tmp_path):
    q,op,r,prep=attempt(tmp_path);retire(q,op,r)
    assert q.outstanding() is None and q.next()==(op['expected'],'discovered')
    p=prep/MARKER;p.write_text(json.dumps(r));p.chmod(0o600)
    assert resolved_version(prep,tmp_path/'queue.sqlite')==op['expected']
    (prep/'request.json').write_bytes(b'changed')
    with pytest.raises(ValueError):resolved_version(prep,tmp_path/'queue.sqlite')
    q.close()


@pytest.mark.parametrize('field,value',[('object_get_sha256_verified',False),('partial_preserved',False),
    ('commit_journal_absent',False),('operation_id','wrong'),('retention_authorized',True)])
def test_ambiguous_or_unverified_supersession_preserves_outstanding(tmp_path,field,value):
    q,op,r,_=attempt(tmp_path);r[field]=value
    with pytest.raises(ValueError):retire(q,op,r)
    assert q.outstanding()==op;q.close()


def test_marker_cannot_resolve_unfinished_attempt(tmp_path):
    q,op,r,prep=attempt(tmp_path)
    p=prep/MARKER;p.write_text(json.dumps(r));p.chmod(0o600)
    with pytest.raises(ValueError):resolved_version(prep,tmp_path/'queue.sqlite')
    assert q.outstanding()==op;q.close()
