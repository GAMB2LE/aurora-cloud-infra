import hashlib
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).parents[1]/'roles/radar_archive_recovery/files/coordinator'))
from per_file_queue import Queue,packed
from unstarted_gws_retirement import retire


def attempt(tmp_path):
    q=Queue(tmp_path/'q.sqlite')
    expected={'relative_path':'Y2026/M11/D20/example.LV1','size':10,'mtime_ns':100,'sha256':'a'*64}
    q.add(expected);op=q.begin(expected,'gws-verified',{'unit':'exact.service'})
    receipt={'schema':'radar-unstarted-gws-attempt-v1','operation_id':op['id'],'expected':expected,
             'unit':'exact.service','intent_sha256':hashlib.sha256(packed(op['intent']).encode()).hexdigest(),
             'reason':'expired-handoff-before-transfer','worker_exit_code':1,'verified':False,
             'transfer_started':False,'worker_terminal':True,'transfer_journal_absent':True,
             'gws_canonical_absent':True,'gws_partials_absent':True,'retention_authorized':False,
             'eviction_authorized':False,'worker_log_sha256':'b'*64,'request_sha256':'c'*64,
             'handoff_sha256':'d'*64,'worker_invocation_id':'e'*32}
    return q,op,receipt


def test_retirement_preserves_unverified_file_and_failed_receipt(tmp_path):
    q,op,receipt=attempt(tmp_path);retire(q,op,receipt)
    assert q.outstanding() is None and q.next()==(op['expected'],'discovered')
    assert q.finished_receipt(op['id'])==receipt
    replacement=q.begin(op['expected'],'gws-verified',{'unit':'new.service'})
    assert replacement['id']!=op['id'];q.close()


@pytest.mark.parametrize('field,value',[('transfer_started',True),('gws_canonical_absent',False),
                                         ('operation_id','wrong'),('retention_authorized',True)])
def test_ambiguous_or_different_attempt_cannot_be_retired(tmp_path,field,value):
    q,op,receipt=attempt(tmp_path);receipt[field]=value
    with pytest.raises(ValueError):retire(q,op,receipt)
    assert q.outstanding()==op and q.next()==(op['expected'],'discovered');q.close()
