import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from sage import config, db, plugins, jobs

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        patcher=patch.multiple(config, DATA=root, DB_PATH=root/'test.db', VAULT=root/'vault', WORKSPACE=root/'workspace', PLUGIN_DIR=Path(__file__).resolve().parents[1]/'plugins')
        patcher.start(); self.addCleanup(patcher.stop)
        db.init()

    def test_memory_persists_with_provenance(self):
        mid=db.execute('INSERT INTO memories(content,kind,source,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?)',('Use concise answers','preference','user',1,db.now(),db.now()))
        self.assertEqual(db.rows('SELECT content,source FROM memories WHERE id=?',(mid,))[0],{'content':'Use concise answers','source':'user'})

    def test_assistant_code_is_formatted_source_and_saved_to_workspace(self):
        from sage import agent
        snippet='```python\ndef add(a, b):\n    return a + b\n```'
        with patch('sage.agent.llm.chat_message',return_value={'content':snippet}):
            answer=agent.respond('Write a Python function that adds two numbers.')
        self.assertIn('Saved code files:',answer)
        generated=list((config.WORKSPACE/'generated').glob('*.py'))
        self.assertEqual(len(generated),1)
        self.assertEqual(generated[0].read_text(encoding='utf-8'),'def add(a, b):\n    return a + b\n')
        self.assertIn(answer, [row['content'] for row in db.rows("SELECT content FROM messages WHERE role='assistant'")])

    def test_engineering_calculators_keep_units_and_assumptions(self):
        from sage.engineering import calculate
        result=calculate('ohms_law',{'voltage_v':'12','current_a':'2','resistance_ohm':''})
        self.assertEqual(result['results']['resistance_ohm'],6)
        self.assertEqual(calculate('convert',{'category':'length','value':'25.4','from_unit':'mm','to_unit':'in'})['result'],1)
        with self.assertRaises(ValueError): calculate('ohms_law',{'voltage_v':'12','current_a':'0','resistance_ohm':''})

    def test_engineering_files_extract_csv_parts_metadata(self):
        from sage.engineering import asset_metadata
        source=config.WORKSPACE/'parts.csv'; source.parent.mkdir(parents=True,exist_ok=True)
        source.write_text('Part,Quantity\nM3 bolt,8\n',encoding='utf-8')
        info=asset_metadata(source,'parts.csv','.csv')
        self.assertEqual(info['columns'],['Part','Quantity'])
        self.assertEqual(info['row_count'],1)
        self.assertEqual(len(info['sha256']),64)

    def test_engineering_report_generation_saves_review_document(self):
        job={'id':5,'title':'Motor mount design review','prompt':'Review the ROV motor mount','result':'{"project_id":null,"assets":[]}'}
        with patch('sage.jobs.llm.chat',return_value='## Assumptions\n- Dimensions need confirmation.'):
            jobs._write_engineering_doc(job,'design_review')
        doc=db.rows('SELECT kind,body,file_path FROM engineering_docs')[0]
        self.assertEqual(doc['kind'],'design_review')
        self.assertTrue(Path(doc['file_path']).exists())
        self.assertIn('Dimensions need confirmation',doc['body'])

    def test_code_test_runner_requires_approval_and_uses_no_shell(self):
        project_dir=(config.WORKSPACE/'engineering'/'code'/'unit_test_project').resolve(); project_dir.mkdir(parents=True)
        (project_dir/'test_demo.py').write_text('import unittest\nclass GeneratedTests(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n',encoding='utf-8')
        pid=db.execute('INSERT INTO code_projects(name,prompt,directory,summary,status,created_at) VALUES(?,?,?,?,?,?)',('Test','request',str(project_dir),'','ready',db.now()))
        db.execute('INSERT INTO code_files(project_id,filename,relative_path,created_at) VALUES(?,?,?,?)',(pid,'test_demo.py',str(project_dir/'test_demo.py'),db.now()))
        self.assertEqual(jobs.run_code_tests(pid)['status'],'pending_approval')
        result=jobs.run_code_tests(pid,approved=True)
        self.assertEqual(result['status'],'passed')
        self.assertIn('Ran 1 test',result['output'])

    def test_code_project_generator_creates_reviewable_openscad_files(self):
        response=json.dumps({'summary':'Parametric mount starter.','files':[{'filename':'mount.scad','content':'// review dimensions before printing\ncube([20,10,3]);'},{'filename':'README.md','content':'Review the geometry in OpenSCAD.'}]})
        with patch('sage.jobs.llm.chat',return_value=response) as model:
            jobs.run_code_project({'id':7,'title':'OpenSCAD mount','prompt':'Make an adjustable mount','result':'{}'})
        self.assertEqual(model.call_args.kwargs['model'],config.CODING_MODEL)
        self.assertEqual(model.call_args.kwargs['timeout'],config.PROJECT_WORK_TIMEOUT)
        project=db.rows('SELECT * FROM code_projects')[0]
        self.assertTrue((Path(project['directory'])/'mount.scad').exists())
        self.assertEqual({f['filename'] for f in db.rows('SELECT filename FROM code_files WHERE project_id=?',(project['id'],))},{'mount.scad','README.md'})

    def test_background_project_task_uses_extended_model_timeout(self):
        from sage import planner
        project_id=db.execute('INSERT INTO projects(name,created_at) VALUES(?,?)',('Long-running project',db.now()))
        task=planner.create_task('Draft project plan','Prepare the detailed digital deliverable.',assignee='sage',project_id=project_id,kind='doc')
        job_id=planner.queue_task(task['id'])
        job=db.rows('SELECT * FROM jobs WHERE id=?',(job_id,))[0]
        result=json.dumps({'status':'complete','summary':'Prepared the requested deliverable.','deliverable':'## Plan\n\nA complete project plan.','acceptance':[],'follow_ups':[],'needs_user':''})
        with patch('sage.jobs.is_idle',return_value=True),patch('sage.llm.chat',return_value=result) as model:
            planner.run_task(job)

        self.assertEqual(model.call_args.kwargs['timeout'],config.PROJECT_WORK_TIMEOUT)
        self.assertEqual(db.rows('SELECT status FROM tasks WHERE id=?',(task['id'],))[0]['status'],'done')

    def test_background_project_planning_uses_extended_model_timeout(self):
        from sage import planner
        project_id=db.execute('INSERT INTO projects(name,created_at) VALUES(?,?)',('Project plan timeout',db.now()))
        job_id=jobs.create('Plan: Project plan timeout','Create a practical project plan.','project_plan',json.dumps({'project_id':project_id}))
        job=db.rows('SELECT * FROM jobs WHERE id=?',(job_id,))[0]
        with patch('sage.llm.chat',side_effect=['not JSON','{}']) as model,patch('sage.planner.apply_plan',return_value='Plan saved'):
            planner.run_plan_job(job)

        self.assertEqual([call.kwargs['timeout'] for call in model.call_args_list],[config.PROJECT_WORK_TIMEOUT]*2)
        self.assertEqual(db.rows('SELECT status FROM jobs WHERE id=?',(job_id,))[0]['status'],'completed')

    def test_plugin_registry_and_safe_execution(self):
        names={x['name'] for x in plugins.discover()}
        self.assertIn('calculator',names)
        out=plugins.call('calculator',{'expression':'3*(8+2)'})
        self.assertEqual(out['result']['value'],30)

    def test_confirmation_plugin_waits_for_explicit_approval(self):
        manifest=config.PLUGIN_DIR/'danger-test.json'
        module=config.PLUGIN_DIR/'danger_test.py'
        try:
            manifest.write_text('{"name":"danger-test","description":"test","version":"1","permission":"confirm","entrypoint":"danger_test.py:run"}',encoding='utf8')
            module.write_text('def run(arguments, context): return {"ran": True}',encoding='utf8')
            out=plugins.call('danger-test',{'target':'x'})
            self.assertEqual(out['status'],'pending_approval')
            self.assertEqual(db.rows('SELECT status FROM action_log ORDER BY id DESC LIMIT 1')[0]['status'],'pending_approval')
            approved=plugins.call('danger-test',{'target':'x'},approved=True)
            self.assertEqual(approved['result']['ran'],True)
        finally:
            manifest.unlink(missing_ok=True); module.unlink(missing_ok=True)

    def test_jobs_start_queued_and_keep_checkpoint(self):
        jid=jobs.create('Plan','Plan a garden')
        row=db.rows('SELECT * FROM jobs WHERE id=?',(jid,))[0]
        self.assertEqual(row['status'],'queued')
        self.assertEqual(row['checkpoint'],'Waiting for idle worker')

    def test_workspace_access_stays_inside_workspace(self):
        from sage.config import WORKSPACE
        from sage import agent
        with patch('sage.agent.plugins.call') as call:
            call.return_value={'status':'completed','result':{'value':30}}
            self.assertIn('completed',agent.respond('/tool calculator {"expression":"3*10"}'))
            call.assert_called_once_with('calculator',{'expression':'3*10'})

    def test_workspace_file_plugin_blocks_path_escape(self):
        from plugins import workspace_files
        with self.assertRaises(ValueError):
            workspace_files.run({'action':'read','path':'../secret.txt'},{'workspace':str(config.WORKSPACE)})

    def test_idle_reflection_can_save_memory_plan_and_learning_request(self):
        response='''{"memories":[{"content":"Prefers concise technical answers","kind":"preference","confidence":0.92}],"project":{"name":"Home dashboard","description":"Build a personal home dashboard","tasks":[{"title":"Sketch layout","description":"Choose dashboard sections","priority":"high"}]},"learning_topics":["local-first dashboard security"]}'''
        job={'id':1,'prompt':'I prefer concise technical answers. Please build me a home dashboard. Also learn about local-first dashboard security.','result':'I will plan it.'}
        with patch('sage.jobs.llm.chat',return_value=response):
            outcome=jobs.reflect(job)
        self.assertIn('Project planned',outcome)
        self.assertEqual(db.rows('SELECT content FROM memories')[0]['content'],'Prefers concise technical answers')
        self.assertEqual(db.rows('SELECT name FROM projects')[0]['name'],'Home dashboard')
        self.assertEqual(db.rows('SELECT title FROM tasks')[0]['title'],'Sketch layout')
        self.assertEqual(db.rows("SELECT kind,prompt FROM jobs WHERE kind='learning'")[0],{'kind':'learning','prompt':'local-first dashboard security'})
        self.assertTrue((config.VAULT/'Memory 1.md').exists())
        self.assertTrue((config.VAULT/'Project 1.md').exists())

    def test_reflection_does_not_save_short_emotional_reactions(self):
        response='''{"memories":[{"content":"oh fuck","kind":"emotional expression","confidence":0.99}],"project":null,"learning_topics":[]}'''
        with patch('sage.jobs.llm.chat',return_value=response):
            jobs.reflect({'id':1,'prompt':'oh fuck','result':'You seem upset.'})
        self.assertEqual(db.rows('SELECT id FROM memories'),[])

    def test_reflection_expands_existing_project_without_duplicate_tasks(self):
        pid=db.execute('INSERT INTO projects(name,description,created_at) VALUES(?,?,?)',('ROV','Underwater vehicle',db.now()))
        db.execute("INSERT INTO tasks(project_id,title,created_at) VALUES(?,?,?)",(pid,'Research robotic platforms',db.now()))
        response='''{"memories":[],"project":{"name":"ROV Underwater Vehicle Project","description":"Underwater vehicle","tasks":[{"title":"Research robotic platforms","description":"Compare existing platforms"},{"title":"Define mission requirements","description":"Set depth and payload targets"},{"title":"Select propulsion and control","description":"Choose thrusters and control system","depends_on":["Define mission requirements"]}]},"learning_topics":["robotics"]}'''
        with patch('sage.jobs.llm.chat',return_value=response):
            jobs.reflect({'id':1,'prompt':'Please plan the ROV project','result':''})
        tasks=db.rows('SELECT title FROM tasks WHERE project_id=? ORDER BY id',(pid,))
        self.assertEqual(len(tasks),3)
        self.assertEqual(len(db.rows('SELECT id FROM projects')),1)
        self.assertEqual(db.rows("SELECT id FROM jobs WHERE kind='learning'"),[])
        self.assertEqual(db.rows('SELECT next_action FROM projects WHERE id=?',(pid,))[0]['next_action'],'Research robotic platforms')
        note=(config.VAULT/f'Project {pid}.md').read_text(encoding='utf-8')
        self.assertIn('[[Task 1]]',note)
        self.assertIn('[[Task 3]]',note)
        self.assertEqual(db.rows("SELECT depends_on FROM tasks WHERE title='Select propulsion and control'")[0]['depends_on'],'[2]')

    def test_learning_job_saves_sources_and_memory_note(self):
        jid=jobs.create('Learn: test topic','test topic','learning')
        result={'query':'test topic','summary':'Finding [1] is supported.','sources':[{'title':'Source One','url':'https://example.org/one'}]}
        with patch('sage.jobs.is_idle',return_value=True), patch('sage.jobs.research.search_with_config',return_value=result):
            jobs.run_learning(db.rows('SELECT * FROM jobs WHERE id=?',(jid,))[0])
        row=db.rows('SELECT status,result FROM jobs WHERE id=?',(jid,))[0]
        self.assertEqual(row['status'],'completed')
        self.assertIn('https://example.org/one', (config.VAULT/f'Learning {jid}.md').read_text(encoding='utf-8'))
        self.assertEqual(db.rows('SELECT kind FROM memories')[0]['kind'],'knowledge')

    def test_research_job_writes_markdown_report_to_vault(self):
        jid=jobs.create('Research: test topic','test topic','research')
        result={'query':'test topic','summary':'Finding [1] is supported.','sources':[{'title':'Source One','url':'https://example.org/one'}]}
        with patch('sage.jobs.is_idle',return_value=True), patch('sage.jobs.research.search_with_config',return_value=result):
            jobs.run_research(db.rows('SELECT * FROM jobs WHERE id=?',(jid,))[0])
        report=config.VAULT/f'Research {jid}.md'
        self.assertTrue(report.exists())
        self.assertIn('https://example.org/one',report.read_text(encoding='utf-8'))
        self.assertEqual(db.rows('SELECT status FROM jobs WHERE id=?',(jid,))[0]['status'],'completed')

    def test_focus_and_watchlist_tables_initialize(self):
        self.assertEqual(db.rows('SELECT enabled,goal FROM focus WHERE id=1')[0],{'enabled':0,'goal':''})
        db.execute('INSERT INTO watchlist(topic,interval_days,last_checked,enabled,created_at) VALUES(?,?,\'\',1,?)',('battery safety',1,db.now()))
        self.assertEqual(db.rows('SELECT topic FROM watchlist')[0]['topic'],'battery safety')

    def test_search_parser_collects_result_links(self):
        from sage.research import SearchParser, is_relevant
        parser=SearchParser()
        parser.feed('<a class="result__a" href="https://example.org/">Example Source</a><a class="result__snippet">Short evidence excerpt</a>')
        self.assertEqual(parser.results[0]['title'],'Example Source')
        self.assertTrue(is_relevant('robotics platforms','Underwater robotics systems for inspection'))
        self.assertFalse(is_relevant('underwater exploration','Netflix series recommendations'))

    def test_reflection_only_queues_for_durable_or_actionable_turns(self):
        from sage.agent import deserves_reflection, web_search_query
        self.assertFalse(deserves_reflection('How are you doing?'))
        self.assertTrue(deserves_reflection('I prefer concise answers.'))
        self.assertTrue(deserves_reflection('Please learn about home battery safety.'))
        self.assertEqual(web_search_query('What is the current price of bitcoin can you search it'),'What is the current price of bitcoin')
        self.assertEqual(web_search_query('Can you search online what the price of bitcoin is'),'what the price of bitcoin is')
        self.assertFalse(deserves_reflection('Can you search online what the price of bitcoin is?'))

    def test_explicit_chat_search_uses_plugin_and_shows_citations(self):
        from sage import agent
        result={'summary':'The source does not state a current price.','sources':[{'title':'Example','url':'https://example.org'}]}
        with patch('sage.plugins.call',return_value={'status':'completed','result':result}) as call:
            answer=agent.respond('What is the current price of bitcoin can you search it')
        call.assert_called_once_with('web_search',{'query':'What is the current price of bitcoin'})
        self.assertIn('https://example.org',answer)
        self.assertEqual(db.rows('SELECT kind FROM jobs'),[])

    def test_retry_of_unanswered_turn_does_not_duplicate_user_message(self):
        from sage import agent
        message='Hello there'
        db.execute('INSERT INTO messages(role,content,created_at) VALUES(?,?,?)',('user',message,db.now()))
        with patch('sage.agent.llm.chat_message',return_value={'content':'Hello!'}):
            answer=agent.respond(message)
        self.assertEqual(answer,'Hello!')
        self.assertEqual(len(db.rows("SELECT id FROM messages WHERE role='user' AND content=?",(message,))),1)
        self.assertEqual(len(db.rows("SELECT id FROM messages WHERE role='assistant' AND content='Hello!'")),1)

if __name__=='__main__': unittest.main()
