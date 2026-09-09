import copy,json,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from summarize_decisionbench_final import verify_attempts,verify_digest
from learning_agent_eval.canonical import sha256_digest
from learning_agent_eval.learning_quality import request_for,validate_rating
from test_learning_quality import sample,rating


def recorded_attempt():
    e=sample();request=request_for(e);payload=rating()
    request_hash=sha256_digest({**request,'_budget_call_id':'example:rating:1'})
    attempt={'number':1,'request_sha256':request_hash,'status':'complete','payload':payload,
        'reply':{'status':'completed','finish_reason':'stop','content':json.dumps(payload)}}
    return e,request,attempt


def test_recompute_requires_original_request_and_response_not_just_score():
    e,req,a=recorded_attempt()
    assert verify_attempts([a],req,e,validate_rating,'example','rating')==rating()
    changed=copy.deepcopy(a);changed['reply']['content']='{}'
    with pytest.raises(ValueError):verify_attempts([changed],req,e,validate_rating,'example','rating')
    with pytest.raises(AssertionError,match='request mismatch'):
        verify_attempts([a],{**req,'extra':'changed'},e,validate_rating,'example','rating')


def test_recompute_rejects_score_selection_after_first_valid_response():
    e,req,a=recorded_attempt()
    with pytest.raises(AssertionError):verify_attempts([a,a],req,e,validate_rating,'example','rating')


def test_changed_result_cannot_reuse_original_digest():
    row={'status':'complete','score':80};row['result_sha256']=sha256_digest(row)
    verify_digest(row);row['score']=100
    with pytest.raises(AssertionError,match='digest mismatch'):verify_digest(row)
