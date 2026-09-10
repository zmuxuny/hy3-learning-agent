"""Source binding works in both ordinary checkouts and attached worktrees."""
import pytest
from learning_agent_eval import e3_io

@pytest.mark.parametrize('worktree,packed,detached',[(False,False,False),(True,False,False),(True,True,False),(True,False,True)])
def test_source_commit_from_shared_git_refs(tmp_path,monkeypatch,worktree,packed,detached):
    checkout=tmp_path/'checkout';checkout.mkdir();common=tmp_path/'shared-git' if worktree else checkout/'.git';common.mkdir()
    gitdir=common/'worktrees'/'attached' if worktree else common;gitdir.mkdir(parents=True,exist_ok=True)
    if worktree:
        (checkout/'.git').write_text(f'gitdir: {gitdir}\n');(gitdir/'commondir').write_text('../..\n')
    commit='1234567890abcdef1234567890abcdef12345678';ref='refs/heads/evaluation'
    (gitdir/'HEAD').write_text(commit+'\n' if detached else 'ref: '+ref+'\n')
    if packed:(common/'packed-refs').write_text('# pack-refs with: peeled\n'+commit+' '+ref+'\n')
    else:
        p=common/ref;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(commit+'\n')
    monkeypatch.setattr(e3_io,'PROJECT_ROOT',checkout)
    assert e3_io.current_git_commit()==commit
