"""Release closure: offline user paths, not authenticated agent evidence."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from retryloom.main import app
from retryloom.models import Session, SessionEvent
from retryloom.storage.database import connect
from retryloom.storage.repository import save_session
from retryloom.replay.models import RunResult
from retryloom.replay.subprocess import execute_process
from retryloom.git.repository import repository_state

cli = CliRunner()


def repo(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    subprocess.run(['git', 'init', '--template=', str(root)], check=True, capture_output=True)
    (root / 'task.txt').write_text('base\n')
    subprocess.run(['git', '-C', str(root), 'add', 'task.txt'], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture base'], check=True, capture_output=True)
    return root


def init(root):
    result=cli.invoke(app,['init','--repository',str(root)])
    assert result.exit_code==0,result.output


def seed(root, *, corrected=True, other_repository=None):
    session=Session(id='synthetic-session',agent='codex',repository=str(other_repository or root),
                    started_at=datetime(2026,1,1,tzinfo=timezone.utc), task='Fix the failing check',
                    events=[SessionEvent(type='tool_result',command='pytest',exit_code=1,
                            metadata={'source_record':{'synthetic':True}})])
    if corrected:
        session.events += [SessionEvent(type='file_edit',content='+behavior correction'),
                           SessionEvent(type='tool_result',command='pytest',exit_code=0)]
    with connect(root/'.retryloom/retryloom.db') as db: save_session(db,session)
    return session


def lesson(root):
    init(root); seed(root)
    result=cli.invoke(app,['learn','--repository',str(root),'--session-id','synthetic-session'])
    assert result.exit_code==0,result.output
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        return db.execute('SELECT id,lesson_hash FROM lessons').fetchone()


def snapshot(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def plan(root, tmp_path, identity, digest, **changes):
    commit=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
    case={'id':'controlled-cli-v1','repository':str(root),'base_commit':commit,
          'task':'Inspect the fixture task and verify the check','checks':[{'name':'fixture-check','command':[sys.executable,'-B','-c','raise SystemExit(1)'],'required':True,'timeout_seconds':10}],
          'runs':3,'lesson_id':identity,'lesson_hash':digest,'timeout_seconds':10,'agent':'codex'}
    case.update(changes)
    p=tmp_path/'case.json'; p.write_text(json.dumps(case)); return p


def replay(root,p,*extra):
    return cli.invoke(app,['replay','--repository',str(root),'--case',str(p),*extra])


@pytest.mark.parametrize('name',['init','scan','learn','replay','apply','stats'])
def test_command_help(name):
    result=cli.invoke(app,[name,'--help']); assert result.exit_code==0,result.output
    assert 'not implemented yet' not in result.output


def test_init_idempotent_preserves_owned_and_user_content(tmp_path):
    root=repo(tmp_path); init(root)
    config=root/'.retryloom/config.toml'; config.write_text(config.read_text()+'\n# user comment\n')
    (root/'AGENTS.md').write_text('user instruction\n')
    before=snapshot(root); init(root)
    assert snapshot(root)==before
    for name in ['lessons','replays','reports','logs']: assert (root/'.retryloom'/name).is_dir()


@pytest.mark.parametrize('target',['.retryloom','.retryloom/config.toml','.retryloom/retryloom.db','.retryloom/lessons','.retryloom/retryloom.db-journal'])
def test_init_rejects_symlinks_without_external_writes(tmp_path,target):
    root=repo(tmp_path); external=tmp_path/'external'; external.mkdir()
    path=root/target; path.parent.mkdir(parents=True,exist_ok=True)
    path.symlink_to(external,target_is_directory=True)
    result=cli.invoke(app,['init','--repository',str(root)])
    assert result.exit_code!=0 and not list(external.iterdir())
    assert 'Traceback' not in result.output


def test_init_rejects_hardlinked_database(tmp_path):
    root=repo(tmp_path); init(root); db=root/'.retryloom/retryloom.db'; os.link(db,tmp_path/'alias.db')
    before=db.read_bytes(); result=cli.invoke(app,['init','--repository',str(root)])
    assert result.exit_code!=0 and db.read_bytes()==before


def test_init_invalid_repository_and_existing_invalid_config(tmp_path):
    result=cli.invoke(app,['init','--repository',str(tmp_path/'missing')]); assert result.exit_code!=0
    root=repo(tmp_path); (root/'.retryloom').mkdir(); config=root/'.retryloom/config.toml'; config.write_text('bad TOML !!')
    before=snapshot(root); result=cli.invoke(app,['init','--repository',str(root)])
    assert result.exit_code!=0 and snapshot(root)==before


def test_learn_preserves_provenance_idempotency_and_candidate_status(tmp_path):
    root=repo(tmp_path); init(root); seed(root)
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        before=[tuple(r) for r in db.execute('SELECT * FROM session_events')]
    args=['learn','--repository',str(root),'--session-id','synthetic-session']
    first=cli.invoke(app,args); second=cli.invoke(app,args)
    assert first.exit_code==second.exit_code==0,(first.output,second.output)
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        assert db.execute('SELECT COUNT(*) FROM lessons').fetchone()[0]==1
        assert db.execute('SELECT status FROM lessons').fetchone()[0]=='candidate'
        assert db.execute('SELECT COUNT(*) FROM failures').fetchone()[0]==1
        assert [tuple(r) for r in db.execute('SELECT * FROM session_events')]==before
        assert db.execute('SELECT COUNT(*) FROM lesson_evidence').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0]==0


@pytest.mark.parametrize('kind',['missing','no-correction','wrong-repository'])
def test_learn_expected_errors_are_clear_and_do_not_create_lessons(tmp_path,kind):
    root=repo(tmp_path); init(root)
    if kind!='missing': seed(root,corrected=kind!='no-correction',other_repository=tmp_path/'elsewhere' if kind=='wrong-repository' else None)
    result=cli.invoke(app,['learn','--repository',str(root),'--session-id','synthetic-session'])
    assert result.exit_code!=0 and 'Traceback' not in result.output
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db: assert db.execute('SELECT COUNT(*) FROM lessons').fetchone()[0]==0


def test_required_arguments_and_invalid_case_are_nonzero(tmp_path):
    root=repo(tmp_path)
    for args in [['learn','--repository',str(root)],['replay','--repository',str(root)],['replay','--repository',str(root),'--case',str(tmp_path/'absent.json')]]:
        result=cli.invoke(app,args); assert result.exit_code!=0 and 'Traceback' not in result.output


def test_replay_preview_is_nonmutating_and_does_not_start_provider(tmp_path,monkeypatch):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest)
    import retryloom.replay.runner as runners
    monkeypatch.setattr(runners.CodexRunner,'run',lambda *a,**k: pytest.fail('provider started'))
    before=snapshot(root); result=replay(root,p)
    assert result.exit_code==0,result.output
    assert snapshot(root)==before


@pytest.mark.parametrize('change',[{'runs':1},{'base_commit':'HEAD'},{'lesson_id':'missing'},{'lesson_hash':'0'*64},{'control_timeout_seconds':9},{'agent':'claude'}])
def test_replay_rejects_invalid_inputs_before_execution(tmp_path,change):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest,**change)
    before=snapshot(root); result=replay(root,p,'--execute')
    assert result.exit_code!=0 and 'Traceback' not in result.output
    assert snapshot(root)==before


def test_replay_rejects_other_source_repository(tmp_path):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest,repository=str(tmp_path/'other'))
    before=snapshot(root); result=replay(root,p,'--execute'); assert result.exit_code!=0
    assert snapshot(root)==before


class FixtureRunner:
    agent='codex'; agent_version=None; model=None; model_settings={}
    calls=[]
    check_success=False
    def __init__(self,*args,**kwargs): self.model=kwargs.get('model'); self.agent_version=kwargs.get('agent_version')
    def run(self,task,cwd,instructions,timeout_seconds,env):
        self.calls.append((task,cwd,list(instructions),timeout_seconds,dict(env)))
        result=execute_process([sys.executable,'-B','-c',"print('CONTROLLED_SYNTHETIC_ONLY')"],cwd,env,timeout_seconds)
        return RunResult(success=result.success,exit_code=result.exit_code,elapsed_seconds=result.elapsed_seconds,
                         stdout=result.stdout,stderr=result.stderr,agent='codex')


@pytest.mark.parametrize('baseline_ok',[True,False])
def test_controlled_cli_e2e_uses_engine_persistence_evaluation_stats_without_promotion(tmp_path,monkeypatch,baseline_ok):
    root=repo(tmp_path); identity,digest=lesson(root)
    changes={} if baseline_ok else {'checks':[{'name':'pass','command':[sys.executable,'-B','-c','pass'],'required':True,'timeout_seconds':10}]}
    p=plan(root,tmp_path,identity,digest,**changes)
    import retryloom.replay.runner as runners
    FixtureRunner.calls=[]; monkeypatch.setattr(runners,'CodexRunner',FixtureRunner)
    dbpath=root/'.retryloom/retryloom.db'; excluded=tuple(Path(str(dbpath)+s) for s in ['', '-wal','-shm','-journal'])
    before=repository_state(root,excluded_paths=excluded)
    result=replay(root,p,'--execute')
    assert result.exit_code==(0 if baseline_ok else 1),result.output
    assert ('COMPLETED' if baseline_ok else 'BASELINE_NOT_REPRODUCIBLE') in result.output
    assert repository_state(root,excluded_paths=excluded)==before
    calls=FixtureRunner.calls; assert len(calls)==(6 if baseline_ok else 3)
    assert len({str(c[1]) for c in calls})==len(calls)
    assert len({c[4]['HOME'] for c in calls})==len(calls)
    assert all(c[2]==[] for c in calls[:3])
    if baseline_ok:
        with sqlite3.connect(dbpath) as db: text=db.execute('SELECT rule FROM lessons').fetchone()[0]
        assert all(c[2]==[text] for c in calls[3:])
    with sqlite3.connect(dbpath) as db:
        assert db.execute('SELECT status FROM lessons').fetchone()[0]=='candidate'
        assert db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0]==1
        assert all(r[0] is None for r in db.execute('SELECT token_total FROM replay_runs'))
        assert all(r[0] is None for r in db.execute('SELECT tool_calls FROM replay_runs'))
    before=dbpath.read_bytes(); stats=cli.invoke(app,['stats','--repository',str(root)])
    assert stats.exit_code==0 and 'N/A' in stats.output
    assert dbpath.read_bytes()==before


def test_replay_validation_errors_do_not_echo_sensitive_input(tmp_path):
    root=repo(tmp_path); identity,digest=lesson(root)
    secret='SYNTHETIC_SENSITIVE_VALUE_DO_NOT_ECHO'
    p=plan(root,tmp_path,identity,digest,runs=secret)
    result=replay(root,p)
    assert result.exit_code!=0 and secret not in result.output


def test_missing_usage_and_order_preserved_by_session_reader(tmp_path):
    from retryloom.storage.repository import load_session
    root=repo(tmp_path); init(root); original=seed(root)
    with connect(root/'.retryloom/retryloom.db') as db:
        loaded=load_session(db,original.id)
    assert loaded.events==original.events
    assert loaded.token_input is loaded.token_output is loaded.token_total is None
    assert loaded.tool_call_count is None


def test_frozen_candidate_mutation_prevents_cli_replay(tmp_path):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest)
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        db.execute('UPDATE lessons SET rule=? WHERE id=?',('modified frozen guidance',identity))
    before=snapshot(root); result=replay(root,p,'--execute')
    assert result.exit_code!=0 and snapshot(root)==before


def test_agent_failure_is_preserved_and_returns_nonzero(tmp_path,monkeypatch):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest)
    import retryloom.replay.runner as runners
    class FailedRunner(FixtureRunner):
        def run(self,*args,**kwargs):
            return RunResult(success=False,exit_code=None,elapsed_seconds=0,stdout='',stderr='fixture unavailable',agent='codex',agent_error='fixture unavailable',efficiency_usable=False)
    monkeypatch.setattr(runners,'CodexRunner',FailedRunner)
    result=replay(root,p,'--execute')
    assert result.exit_code!=0 and 'AGENT_ERROR' in result.output
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        assert db.execute('SELECT status FROM lessons').fetchone()[0]=='candidate'
        assert db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0]==1


def test_full_controlled_cli_path_imports_canonical_synthetic_source(tmp_path,monkeypatch):
    root=repo(tmp_path); init(root)
    source=tmp_path/'sessions'; source.mkdir()
    records=[{'type':'session_meta','timestamp':'2026-01-01T00:00:00Z','payload':{'id':'synthetic-import','cwd':str(root),'timestamp':'2026-01-01T00:00:00Z'}},
             {'type':'event_msg','timestamp':'2026-01-01T00:00:01Z','payload':{'type':'user_message','message':'Fix the failing fixture test'}},
             {'type':'response_item','timestamp':'2026-01-01T00:00:02Z','payload':{'type':'function_call','name':'exec_command','call_id':'first','arguments':json.dumps({'cmd':'pytest'})}},
             {'type':'response_item','timestamp':'2026-01-01T00:00:03Z','payload':{'type':'function_call_output','call_id':'first','content':'synthetic failure','exit_code':1}},
             {'type':'response_item','timestamp':'2026-01-01T00:00:04Z','payload':{'type':'file_edit','content':'+synthetic bounded correction'}},
             {'type':'response_item','timestamp':'2026-01-01T00:00:05Z','payload':{'type':'function_call','name':'exec_command','call_id':'second','arguments':json.dumps({'cmd':'pytest'})}},
             {'type':'response_item','timestamp':'2026-01-01T00:00:06Z','payload':{'type':'function_call_output','call_id':'second','content':'synthetic success','exit_code':0}}]
    (source/'rollout-synthetic.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    args=['scan','--agent','codex','--repository',str(root),'--source-root',str(source)]
    first=cli.invoke(app,args); again=cli.invoke(app,args)
    assert first.exit_code==again.exit_code==0,(first.output,again.output)
    assert 'Imported: 1' in first.output and 'Imported: 0' in again.output
    learned=cli.invoke(app,['learn','--repository',str(root),'--session-id','synthetic-import'])
    assert learned.exit_code==0,learned.output
    with sqlite3.connect(root/'.retryloom/retryloom.db') as db:
        identity,digest=db.execute('SELECT id,lesson_hash FROM lessons').fetchone()
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM failures').fetchone()[0]==1
    p=plan(root,tmp_path,identity,digest)
    import retryloom.replay.runner as runners
    monkeypatch.setattr(runners,'CodexRunner',FixtureRunner)
    result=replay(root,p,'--execute'); assert result.exit_code==0,result.output
    stats=cli.invoke(app,['stats','--repository',str(root)])
    assert stats.exit_code==0 and 'CONTROL success: 0/3' in stats.output
    assert 'REFLEX success: 0/3' in stats.output and 'Lessons candidate: 1' in stats.output


@pytest.mark.parametrize('command',['init','learn','replay','stats','apply'])
def test_wal_database_is_rejected_without_creating_sidecars(tmp_path,command):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest)
    db=sqlite3.connect(root/'.retryloom/retryloom.db'); db.execute('PRAGMA journal_mode=WAL'); db.close()
    before=snapshot(root)
    args=[command,'--repository',str(root)]
    if command=='learn': args += ['--session-id','synthetic-session']
    if command=='replay': args += ['--case',str(p)]
    result=cli.invoke(app,args)
    assert result.exit_code!=0 and 'WAL' in result.output
    assert snapshot(root)==before


def test_scan_rejects_external_storage_symlink(tmp_path):
    root=repo(tmp_path); outside=tmp_path/'external'; outside.mkdir()
    (root/'.retryloom').symlink_to(outside,target_is_directory=True)
    source=tmp_path/'empty';source.mkdir()
    result=cli.invoke(app,['scan','--repository',str(root),'--source-root',str(source)])
    assert result.exit_code!=0 and not list(outside.iterdir())


def test_replay_case_id_reuse_must_preserve_original_case_inputs(tmp_path,monkeypatch):
    root=repo(tmp_path); identity,digest=lesson(root); p=plan(root,tmp_path,identity,digest)
    import retryloom.replay.runner as runners
    monkeypatch.setattr(runners,'CodexRunner',FixtureRunner)
    first=replay(root,p,'--execute'); assert first.exit_code==0,first.output
    before=snapshot(root); same=replay(root,p); assert same.exit_code==0,same.output
    assert snapshot(root)==before
    data=json.loads(p.read_text());data['task']='Materially changed task';p.write_text(json.dumps(data))
    result=replay(root,p,'--execute')
    assert result.exit_code!=0 and 'fresh case ID' in result.output
    assert snapshot(root)==before


def test_partial_repository_is_rejected_without_running_provider(tmp_path):
    root=repo(tmp_path); identity,digest=lesson(root);p=plan(root,tmp_path,identity,digest)
    subprocess.run(['git','-C',str(root),'config','remote.origin.promisor','true'],check=True,capture_output=True)
    before=snapshot(root); result=replay(root,p,'--execute')
    assert result.exit_code!=0 and 'promisor' in result.output
    assert snapshot(root)==before


def test_scan_bad_database_returns_clear_error_not_traceback(tmp_path):
    root=repo(tmp_path); (root/'.retryloom').mkdir(); (root/'.retryloom/retryloom.db').write_text('not a database')
    source=tmp_path/'empty'; source.mkdir()
    result=cli.invoke(app,['scan','--repository',str(root),'--source-root',str(source)])
    assert result.exit_code==1 and 'Scan failed:' in result.output and 'Traceback' not in result.output
