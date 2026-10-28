import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from sage import config, db
from sage.server import Handler


class EngineeringApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p=patch.multiple(config,DATA=root,DB_PATH=root/'sage.db',VAULT=root/'vault',WORKSPACE=root/'workspace',PLUGIN_DIR=Path(__file__).resolve().parents[1]/'plugins')
        p.start(); self.addCleanup(p.stop); db.init()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.addCleanup(self.close_server)
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self,path,data=None,content_type='application/json'):
        body=data if isinstance(data,bytes) else json.dumps(data).encode() if data is not None else None
        req=Request(self.base+path,data=body,headers={'Content-Type':content_type} if body is not None else {})
        with urlopen(req,timeout=5) as response:
            raw=response.read()
            return json.loads(raw) if response.headers.get_content_type()=='application/json' else raw

    def test_home_assistant_command_requires_token_and_uses_fixed_command(self):
        from sage import server
        with patch.object(config,'HA_TOKEN','long-test-secret'), patch.object(server.agent,'respond',return_value='Your next action is review the motor mount.') as respond:
            denied=Request(self.base+'/api/ha/command',data=b'{"command":"next_action"}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as error:
                urlopen(denied,timeout=5)
            self.assertEqual(error.exception.code,401)
            request=Request(self.base+'/api/ha/command',data=b'{"command":"next_action"}',headers={'Content-Type':'application/json','Authorization':'Bearer long-test-secret'})
            with urlopen(request,timeout=5) as response:
                result=json.loads(response.read())
            self.assertEqual(result['answer'],'Your next action is review the motor mount.')
            self.assertEqual(result['command'],'next_action')
            self.assertIn('single best next action',respond.call_args.args[0])
            bad=Request(self.base+'/api/ha/command',data=b'{"command":"arbitrary"}',headers={'Content-Type':'application/json','Authorization':'Bearer long-test-secret'})
            with self.assertRaises(HTTPError) as error:
                urlopen(bad,timeout=5)
            self.assertEqual(error.exception.code,400)

    def test_iphone_awake_shortcut_requires_token_and_registers_then_returns_brief(self):
        from sage import events, scheduler
        with patch.object(config,'SHORTCUT_TOKEN','long-test-secret'), patch.object(scheduler,'build_brief',return_value='Good morning. Here is your brief.') as brief:
            denied=Request(self.base+'/api/shortcuts/awake',data=b'{}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as error:
                urlopen(denied,timeout=5)
            self.assertEqual(error.exception.code,401)

            request=Request(self.base+'/api/shortcuts/awake',data=b'{}',headers={'Content-Type':'application/json','Authorization':'Bearer long-test-secret'})
            with urlopen(request,timeout=5) as response:
                result=json.loads(response.read())

        self.assertEqual(result['answer'],'Good morning. Here is your brief.')
        self.assertTrue(result['awake_at'])
        brief.assert_called_once_with()
        self.assertEqual(db.rows("SELECT content FROM messages WHERE role='assistant'")[0]['content'],result['answer'])
        wake_events=events.recent(kinds=['user.awake'])
        self.assertEqual(len(wake_events),1)
        self.assertEqual(wake_events[0]['data']['awake_at'],result['awake_at'])

    def test_workshop_log_and_calculation_api_persist_files(self):
        logged=self.request('/api/workshop/log',{'note':'Measured thruster current','measurements':'2.1 A at 12 V'})
        self.assertEqual(logged['id'],1)
        entry=self.request('/api/workshop/log')[0]
        self.assertEqual(entry['measurements'],'2.1 A at 12 V')
        result=self.request('/api/engineering/calculate',{'operation':'power','values':{'voltage_v':'12','current_a':'2'}})
        self.assertEqual(result['result'],24)
        self.assertTrue((config.WORKSPACE/'engineering'/'calculations'/'calculation_1.md').exists())

    def test_chat_context_endpoint_lists_and_updates_persistent_pins(self):
        message_id=db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)',('user','Remember this motor detail.',db.now()))
        memory_id=db.execute('INSERT INTO memories(content,kind,source,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?)',('The motor uses a 12 V supply.','project','user',1,db.now(),db.now()))

        result=self.request('/api/chat/context/pins',{'action':'pin','item':{'type':'message','id':message_id}})

        self.assertEqual(result['pins'][0]['content'],'Remember this motor detail.')
        state=self.request('/api/chat/context')
        self.assertEqual(state['pins'][0]['id'],message_id)
        self.assertEqual(state['embedding_model'],'nomic-embed-text')
        removed=self.request('/api/chat/context/pins',{'action':'pin','item':{'type':'memory','id':memory_id}})
        self.assertEqual(len(removed['pins']),2)

    def test_semantic_memory_search_api_returns_local_matches_or_clear_model_error(self):
        from sage import server
        match={'id':1,'content':'I enjoy bicycle maintenance.','kind':'fact','similarity':0.82}
        with patch.object(server.memory,'semantic_search',return_value=[match]) as search:
            result=self.request('/api/memory-search?q=cycling+hobby')
        self.assertEqual(result['results'],[match])
        search.assert_called_once_with('cycling hobby',limit=10)

        with patch.object(server.memory,'semantic_search',side_effect=RuntimeError('Install the local embedding model')):
            request=Request(self.base+'/api/memory-search?q=cycling')
            with self.assertRaises(HTTPError) as error:
                urlopen(request,timeout=5)
            self.assertEqual(error.exception.code,503)
            self.assertIn('Install the local embedding model',json.loads(error.exception.read())['error'])

    def test_user_can_unblock_a_project_task_and_invalid_updates_are_rejected(self):
        project_id=db.execute('INSERT INTO projects(name,created_at) VALUES(?,?)',('Test rig',db.now()))
        task_id=db.execute("INSERT INTO tasks(project_id,title,status,created_at) VALUES(?,?,'blocked',?)",(project_id,'Install replacement motor',db.now()))

        result=self.request(f'/api/tasks/{task_id}',{'status':'todo'})

        self.assertEqual(result,{'ok':True,'id':task_id,'status':'todo'})
        self.assertEqual(db.rows('SELECT status FROM tasks WHERE id=?',(task_id,))[0]['status'],'todo')
        invalid=Request(self.base+f'/api/tasks/{task_id}',data=json.dumps({'status':'unblocked'}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(invalid,timeout=5)
        self.assertEqual(error.exception.code,400)
        missing=Request(self.base+'/api/tasks/9999',data=json.dumps({'status':'todo'}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(missing,timeout=5)
        self.assertEqual(error.exception.code,404)

    def test_bulk_approval_marks_blocked_tasks_done_and_requires_confirmation(self):
        project_id=db.execute('INSERT INTO projects(name,created_at) VALUES(?,?)',('Wall Sage',db.now()))
        blocked_ids=[
            db.execute("INSERT INTO tasks(project_id,title,status,created_at) VALUES(?,?,'blocked',?)",(project_id,title,db.now()))
            for title in ('Choose charging module','Design wall dock')
        ]
        todo_id=db.execute("INSERT INTO tasks(project_id,title,status,created_at) VALUES(?,?,'todo',?)",(project_id,'Review display options',db.now()))
        done_id=db.execute("INSERT INTO tasks(project_id,title,status,created_at) VALUES(?,?,'done',?)",(project_id,'Choose Raspberry Pi',db.now()))

        unconfirmed=Request(self.base+'/api/plan/approve-blocked',data=json.dumps({}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(unconfirmed,timeout=5)
        self.assertEqual(error.exception.code,400)

        result=self.request('/api/plan/approve-blocked',{'confirmed':True})

        self.assertEqual(result,{'ok':True,'updated':2})
        tasks={row['id']:row for row in db.rows('SELECT id,status,finished_at,blocked_reason,needs_input,user_input FROM tasks')}
        for task_id in blocked_ids:
            self.assertEqual(tasks[task_id]['status'],'done')
            self.assertTrue(tasks[task_id]['finished_at'])
            self.assertEqual(tasks[task_id]['blocked_reason'],'')
            self.assertEqual(tasks[task_id]['needs_input'],'')
            self.assertIn('User approved the completed work in Planner',tasks[task_id]['user_input'])
        self.assertEqual(tasks[todo_id]['status'],'todo')
        self.assertEqual(tasks[done_id]['status'],'done')
        self.assertEqual(self.request('/api/plan/approve-blocked',{'confirmed':True})['updated'],0)

    def test_cad_import_extracts_bom_metadata_and_download_is_contained(self):
        uploaded=self.request('/api/engineering/assets?name=parts.csv',b'Part,Quantity\nM3 screw,8\n','text/csv')
        self.assertEqual(uploaded['metadata']['row_count'],1)
        listing=self.request('/api/engineering/assets')
        self.assertEqual(listing[0]['metadata']['columns'],['Part','Quantity'])
        downloaded=self.request(f"/api/engineering/assets/{uploaded['id']}/download")
        self.assertIn(b'M3 screw',downloaded)

    def test_bom_builder_creates_downloadable_csv(self):
        made=self.request('/api/engineering/bom',{'title':'ROV parts','items':[{'reference':'M1','part_number':'ABC-1','description':'12V motor','quantity':2,'unit':'each','supplier':'Local shop','notes':''}]})
        self.assertEqual(made['parts'],1)
        content=self.request(made['url'])
        self.assertIn(b'12V motor',content)
        self.assertEqual(self.request('/api/engineering/assets')[0]['metadata']['row_count'],1)

    def test_code_test_endpoint_only_returns_approval_request_before_execution(self):
        project_dir=config.WORKSPACE/'engineering'/'code'/'api_project'; project_dir.mkdir(parents=True)
        test_file=project_dir/'test_safe.py'; test_file.write_text('def test_ok(): assert True\n',encoding='utf-8')
        pid=db.execute('INSERT INTO code_projects(name,prompt,directory,summary,status,created_at) VALUES(?,?,?,?,?,?)',('API test','',str(project_dir),'','ready',db.now()))
        db.execute('INSERT INTO code_files(project_id,filename,relative_path,created_at) VALUES(?,?,?,?)',(pid,'test_safe.py',str(test_file),db.now()))
        result=self.request(f'/api/engineering/code-projects/{pid}/tests',{'approved':False})
        self.assertEqual(result['status'],'pending_approval')

    def test_code_project_file_edit_is_confined_to_project(self):
        project_dir=config.WORKSPACE/'engineering'/'code'/'edit_project'; project_dir.mkdir(parents=True)
        source=project_dir/'main.py'; source.write_text('print("old")\n',encoding='utf-8')
        pid=db.execute('INSERT INTO code_projects(name,prompt,directory,summary,status,created_at) VALUES(?,?,?,?,?,?)',('Edit','',str(project_dir),'','ready',db.now()))
        db.execute('INSERT INTO code_files(project_id,filename,relative_path,created_at) VALUES(?,?,?,?)',(pid,'main.py',str(source),db.now()))
        self.request(f'/api/engineering/code-projects/{pid}/files/main.py',{'content':'print("new")\n'})
        self.assertEqual(source.read_text(encoding='utf-8'),'print("new")\n')

    def test_inventory_tracks_adjustments_and_rejects_negative_stock(self):
        item=self.request('/api/inventory',{'name':'M4 bolts','part_number':'M4-12','quantity':10,'unit':'each','reorder_level':3})
        self.assertEqual(item['id'],1)
        adjusted=self.request(f"/api/inventory/{item['id']}/move",{'delta':-4,'reason':'Used on test rig'})
        self.assertEqual(adjusted['quantity'],6)
        listing=self.request('/api/inventory')
        self.assertEqual(listing[0]['quantity'],6)
        self.assertEqual(listing[0]['movements'][0]['delta'],-4)
        req=Request(self.base+f"/api/inventory/{item['id']}/move",data=json.dumps({'delta':-99}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(Exception):urlopen(req,timeout=5)

    def test_troubleshooting_sessions_and_failure_lessons_are_searchable(self):
        tree=self.request('/api/troubleshooting',{'title':'No motor spin','symptom':'Motor controller has power but does not spin','steps':['Check fuse','Measure controller input','Inspect motor phase leads']})
        session=self.request(f"/api/troubleshooting/{tree['id']}/start",{})
        self.request(f"/api/troubleshooting/sessions/{session['id']}",{'action':'next','notes':'Fuse continuity good'})
        guide=self.request('/api/troubleshooting')[0]
        self.assertEqual(guide['sessions'][0]['step_index'],1)
        failure=self.request('/api/failures',{'title':'Connector overheated','symptom':'Connector reached 90 C under load','cause':'Loose crimp','fix':'Recrimped with correct die','lesson':'Pull-test every crimp before installation'})
        results=self.request('/api/workshop/search?q=crimp')
        self.assertTrue(any(x['id']==failure['id'] and x['type']=='failure' for x in results['results']))
        self.assertTrue((config.VAULT/f"Failure Lesson {failure['id']}.md").exists())

    def test_design_snapshot_is_immutable_and_project_handoff_is_saved(self):
        pid=self.request('/api/projects',{'name':'Pump controller','description':'Control and test the workshop coolant pump'})['id']
        self.request('/api/workshop/log',{'project_id':pid,'note':'Bench test completed','measurements':'Current 1.2 A'})
        uploaded=self.request('/api/engineering/assets?name=controller.csv',b'pin,value\nrelay,5V\n','text/csv')
        snapshot=self.request('/api/engineering/versions',{'asset_id':uploaded['id'],'notes':'Initial pin map'})
        download=self.request(f"/api/engineering/versions/{snapshot['id']}/download")
        self.assertIn(b'relay,5V',download)
        handoff=self.request(f'/api/projects/{pid}/handoff',{})
        self.assertIn('Bench test completed',handoff['content'])
        self.assertTrue((config.VAULT/f'Project {pid} Handoff.md').exists())
        self.assertTrue(self.request('/api/handoffs'))

    def test_project_parts_status_compares_bom_to_project_and_general_stock(self):
        pid=self.request('/api/projects',{'name':'Pump'})['id']
        self.request('/api/engineering/bom',{'title':'Pump BOM','project_id':pid,'items':[{'part_number':'P-1','description':'Pump motor','quantity':3}]})
        self.request('/api/inventory',{'name':'Pump motor','part_number':'P-1','quantity':2,'unit':'each'})
        status=self.request(f'/api/projects/{pid}/parts-status')
        self.assertEqual(status['parts'][0]['on_hand'],2)
        self.assertEqual(status['shortages'][0]['shortage'],1)

    def test_measurement_template_and_decision_log_persist(self):
        pid=self.request('/api/projects',{'name':'Calibration'})['id']
        template=self.request('/api/measurement-templates',{'project_id':pid,'title':'Motor check','fields':[{'name':'current','unit':'A','target':'2','tolerance':'0.2'}]})
        run=self.request(f"/api/measurement-templates/{template['id']}/runs",{'values':{'current':'2.1'},'notes':'Warm motor'})
        self.assertEqual(run['id'],1)
        self.assertEqual(self.request('/api/measurement-templates')[0]['fields'][0]['name'],'current')
        decision=self.request('/api/decisions',{'project_id':pid,'title':'Connector','decision':'Use locking plug','alternatives':'Screw terminal','rationale':'Vibration'})
        self.assertEqual(self.request('/api/decisions')[0]['id'],decision['id'])
        self.assertTrue((config.VAULT/f"Decision {decision['id']} - Connector.md").exists())

    def test_chat_passes_selected_project_to_agent(self):
        pid=self.request('/api/projects',{'name':'Selected'})['id']
        with patch('sage.agent.respond',return_value='ok') as respond:
            self.assertEqual(self.request('/api/chat',{'message':'What next?','project_id':pid})['answer'],'ok')
            respond.assert_called_once_with('What next?',project_id=pid)

    def test_version_comparison_reports_text_diff(self):
        uploaded=self.request('/api/engineering/assets?name=pinmap.csv',b'pin,value\nA,1\n','text/csv')
        first=self.request('/api/engineering/versions',{'asset_id':uploaded['id'],'notes':'first'})
        second=self.request('/api/engineering/versions',{'asset_id':uploaded['id'],'notes':'second'})
        root=config.DATA/'engineering'/'versions'; source=root/db.rows('SELECT stored_name FROM design_versions WHERE id=?',(second['id'],))[0]['stored_name'];source.write_text('pin,value\nA,2\n',encoding='utf-8')
        import hashlib
        db.execute('UPDATE design_versions SET sha256=? WHERE id=?',(hashlib.sha256(source.read_bytes()).hexdigest(),second['id']))
        result=self.request(f"/api/engineering/versions/{second['id']}/compare")
        self.assertTrue(result['changed']);self.assertIn('-A,1',result['diff']);self.assertIn('+A,2',result['diff'])

    def test_backup_endpoint_creates_local_archive(self):
        (config.VAULT/'Reference.md').write_text('known content',encoding='utf-8')
        made=self.request('/api/backups',{})
        self.assertTrue((config.DATA/'backups'/made['name']).is_file())
        (config.VAULT/'Reference.md').write_text('changed after backup',encoding='utf-8')
        restored=self.request(f"/api/backups/{made['name']}/restore",{'confirmed':True})
        self.assertTrue(restored['ok'])
        self.assertEqual((config.VAULT/'Reference.md').read_text(encoding='utf-8'),'known content')
