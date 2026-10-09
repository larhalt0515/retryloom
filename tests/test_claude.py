import json
import pytest
from retryloom.adapters.claude import ClaudeAdapter


def parse(tmp_path, records, suffix='.jsonl'):
    path = tmp_path / ('session' + suffix)
    path.write_text('\n'.join(json.dumps(x) for x in records) if suffix == '.jsonl' else json.dumps(records))
    return ClaudeAdapter(tmp_path).parse(path)


def rec(kind='user', **kwargs):
    return dict(type=kind, timestamp='2026-01-01T00:00:00Z', sessionId='synthetic', **kwargs)


@pytest.mark.parametrize('suffix', ['.json', '.jsonl'])
def test_format_and_metrics(tmp_path, suffix):
    records = [rec(message={'content':'Build a parser.'}), rec('assistant', message={'id':'m1','model':'claude-future','content':[{'type':'text','text':'Working'}],'usage':{'input_tokens':10,'output_tokens':2}}), rec('assistant',message={'id':'m1','usage':{'input_tokens':10,'output_tokens':5}}),rec('result',subtype='success')]
    s=parse(tmp_path,records,suffix)
    assert (s.agent,s.model,s.token_input,s.token_output,s.result)==('Claude','claude-future',10,5,'success')
    assert s.repository == '' and s.base_commit is None
    assert s.task=='Build a parser.'


def test_cumulative_not_added_to_messages(tmp_path):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('assistant',message={'usage':{'input_tokens':7,'output_tokens':2}}),rec('cost-state',modelUsage={'a':{'inputTokens':11,'outputTokens':4},'b':{'inputTokens':3,'outputTokens':1}})])
    assert (s.token_input,s.token_output)==(14,5)


def test_malformed_and_noise(tmp_path):
    p=tmp_path/'a.jsonl';p.write_text(json.dumps(rec(message={'content':'approved'}))+'\n{broken\n'+json.dumps(rec(message={'content':'Actual task'},unexpected=42)))
    s=ClaudeAdapter(tmp_path).parse(p)
    assert s.task=='Actual task'
    assert s.events[-1].type=='parse_warning'
    assert 'broken' not in s.events[-1].model_dump_json()


def test_no_timestamp_rejected(tmp_path):
    with pytest.raises(ValueError):parse(tmp_path,[{'type':'user','message':{'content':'Task'}}])


def test_subagent_identity(tmp_path):
    root=tmp_path/'subagents';root.mkdir();p=root/'agent-demo.jsonl';p.write_text(json.dumps(rec(message={'content':'Task'})))
    assert ClaudeAdapter(tmp_path).parse(p).id=='synthetic:agent-demo'


def test_unknown_records_and_partial_usage(tmp_path):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('assistant', message={'usage':{'input_tokens':4}}),rec('new-format',future={'anything':1})])
    assert s.token_output is None and s.token_input==4
    assert s.result is None and s.ended_at is None
    assert s.events[-1].metadata['source_record']['future']=={'anything':1}

@pytest.mark.parametrize('bad', [rec([],message={'content':'Bad'}),rec('assistant',message={'id':{},'usage':{'input_tokens':1}}),rec('assistant',message={'content':[{'type':'tool_use','id':[],'name':[],'input':{}}]}),rec('user',message={'content':[{'type':'tool_result','tool_use_id':[],'content':'Bad'}]})])
def test_malformed_optional_fields(tmp_path,bad):
    s=parse(tmp_path,[rec(message={'content':'Task'}),bad])
    assert any(e.type=='parse_warning' for e in s.events)


def test_not_shell_output_cannot_set_exit(tmp_path):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('assistant',message={'content':[{'type':'tool_use','id':'read','name':'Read','input':{}}]}),rec(message={'content':[{'type':'tool_result','tool_use_id':'read','content':'Exit code 1\nThis is a text file.'}]})])
    assert next(e for e in s.events if e.type=='tool_result').exit_code is None


def test_json_wrapper_malformed_record(tmp_path):
    p=tmp_path/'a.json';p.write_text(json.dumps({'messages':[rec(message={'content':'Task'}),17]}))
    assert ClaudeAdapter(tmp_path).parse(p).events[-1].type=='parse_warning'

@pytest.mark.parametrize('outcome', ['success','failure','error'])
def test_explicit_outcome(tmp_path,outcome):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('result',result=outcome)])
    assert s.result==outcome


def test_storage_uses_same_tables(tmp_path):
    from retryloom.storage.database import connect
    from retryloom.storage.repository import save_session
    s=parse(tmp_path,[rec(cwd='/workspace/example',version='1.2',git={'commit_hash':'a'*40},message={'content':'Task'}),rec('assistant',message={'model':'claude-new','usage':{'input_tokens':9,'output_tokens':3,'cache_read_input_tokens':50}})])
    with connect(tmp_path/'retryloom.db') as db:
        save_session(db,s)
        save_session(db,s)
        row=db.execute('SELECT * FROM sessions').fetchone()
        assert row['agent']=='Claude' and row['model']=='claude-new'
        assert row['token_input']==9 and row['base_commit']=='a'*40
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM session_events').fetchone()[0]==len(s.events)


def test_scan_repository_scope_and_duplicate(tmp_path):
    import subprocess
    from typer.testing import CliRunner
    from retryloom.main import app
    from retryloom.storage.database import connect
    a=tmp_path/'a';b=tmp_path/'b';a.mkdir();b.mkdir()
    for root in [a,b]:subprocess.run(['git','init',str(root)],check=True,capture_output=True)
    source=tmp_path/'transcripts';source.mkdir()
    for sid,cwd in [('first',a),('other',b)]:
        (source/(sid+'.jsonl')).write_text(json.dumps(dict(rec(cwd=str(cwd),message={'content':'Invented task'}),sessionId=sid)))
    args=['scan','--agent','claude','--source-root',str(source),'--repository',str(a)]
    first=CliRunner().invoke(app,args);assert first.exit_code==0,first.output
    assert 'Imported: 1' in first.output
    second=CliRunner().invoke(app,args);assert second.exit_code==0
    assert 'Imported: 0' in second.output
    with connect(a/'.retryloom/retryloom.db') as db:
        assert db.execute('SELECT id FROM sessions').fetchall()[0][0]=='first'
        assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1


def test_cross_adapter_equivalent_task_tools_metrics(tmp_path):
    from retryloom.adapters.codex import CodexAdapter
    from retryloom.analysis.failures import detect_failure
    task='Repair a parser.'
    claude=parse(tmp_path,[rec(cwd='/workspace/example',message={'content':task}),rec('assistant',message={'id':'api1','usage':{'input_tokens':10,'output_tokens':5},'content':[{'type':'tool_use','id':'call','name':'Bash','input':{'command':'pytest'}}]}),rec(message={'content':[{'type':'tool_result','tool_use_id':'call','content':'Exit code 1\nfailed','is_error':True,'exit_code':1}]}),rec(message={'content':'Please fix the validation instead of ignoring it.'})])
    records=[{'type':'session_meta','payload':{'id':'codex-invented','cwd':'/workspace/example','timestamp':'2026-01-01T00:00:00Z'}},{'type':'event_msg','payload':{'type':'user_message','message':task}},{'type':'response_item','payload':{'type':'function_call','call_id':'call','name':'shell','arguments':json.dumps({'cmd':'pytest'})}},{'type':'response_item','payload':{'type':'function_call_output','call_id':'call','exit_code':1,'content':'failed'}},{'type':'event_msg','payload':{'type':'user_message','message':'Please fix the validation instead of ignoring it.'}},{'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':10,'output_tokens':5}}}}]
    p=tmp_path/'codex.jsonl';p.write_text('\n'.join(json.dumps(x) for x in records));codex=CodexAdapter(tmp_path).parse(p)
    assert (claude.task,claude.result,claude.token_input,claude.token_output,claude.tool_call_count)==(codex.task,codex.result,codex.token_input,codex.token_output,codex.tool_call_count)
    assert set(detect_failure(claude).signals)==set(detect_failure(codex).signals)


def test_missing_api_usage_is_not_partial_total(tmp_path):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('assistant',message={'id':'a','usage':{'input_tokens':5,'output_tokens':2}}),rec('assistant',message={'id':'b','content':'Done'})])
    assert s.token_input is None and s.token_output is None


def test_stale_cost_state_does_not_override_later_usage(tmp_path):
    s=parse(tmp_path,[rec(message={'content':'Task'}),rec('assistant',message={'id':'a','usage':{'input_tokens':5,'output_tokens':2}}),rec('cost-state',modelUsage={'a':{'inputTokens':5,'outputTokens':2}}),rec('assistant',message={'id':'b','usage':{'input_tokens':7,'output_tokens':3}})])
    assert (s.token_input,s.token_output)==(12,5)


def test_scan_preserves_existing_agent_and_source(tmp_path):
    import subprocess
    from typer.testing import CliRunner
    from retryloom.main import app
    from retryloom.storage.database import connect
    from retryloom.storage.repository import save_session
    repo=tmp_path/'repo';repo.mkdir();subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    sub=repo/'nested';sub.mkdir();source=tmp_path/'source';source.mkdir()
    path=source/'one.jsonl';path.write_text(json.dumps(rec(cwd=str(sub),message={'content':'Synthetic task'})))
    before=path.read_bytes()
    original=ClaudeAdapter(source).parse(path).model_copy(update={'agent':'codex'})
    with connect(repo/'.retryloom/retryloom.db') as db:save_session(db,original)
    result=CliRunner().invoke(app,['scan','--agent','claude','--source-root',str(source),'--repository',str(repo)])
    assert result.exit_code==0 and 'Imported: 0' in result.output
    with connect(repo/'.retryloom/retryloom.db') as db:assert db.execute('SELECT agent FROM sessions').fetchone()[0]=='codex'
    assert path.read_bytes()==before


def test_committed_fixture_is_synthetic_and_parseable():
    from pathlib import Path
    p=Path(__file__).parent/'fixtures/claude/session.jsonl'
    text=p.read_text()
    assert '/Users/' not in text and 'API_KEY' not in text and 'synthetic-claude-fixture' in text
    s=ClaudeAdapter().parse(p)
    assert s.result=='error' and s.tool_call_count==1
    assert [e.timestamp for e in s.events]==sorted(e.timestamp for e in s.events)
