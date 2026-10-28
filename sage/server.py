import json
import mimetypes
import re
import uuid
import csv
import io
import hashlib
import shutil
import difflib
import zipfile
import tempfile
import subprocess
import hmac
import ipaddress
import time
import base64
import binascii
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs
from . import config, db, llm, agent, plugins, jobs, memory, engineering, outlook, tools as tool_registry, events, routes, scheduler, planner, reminders, system_control, computer_tasks, telegram

STREAMS={}  # active streaming replies by id, so the Stop button can cancel them
WALL_CACHE={'ollama_at':0.0,'ollama':{},'events_at':0.0,'events':[]}

def wall_snapshot():
    """Small, cheap-to-parse summary for wall displays (e.g. a Raspberry Pi Zero W). Read-only."""
    now=time.time()
    if now-WALL_CACHE['ollama_at']>20: WALL_CACHE.update(ollama_at=now,ollama=llm.status())
    if now-WALL_CACHE['events_at']>120:
        WALL_CACHE['events_at']=now
        try: WALL_CACHE['events']=[e for e in outlook.upcoming(2) if not e['cancelled']][:8]
        except Exception: WALL_CACHE['events']=[]
    today=date.today().isoformat(); approvals=[]
    for item in db.rows("SELECT id,tool_name,arguments_json,created_at FROM tool_approvals WHERE status='pending' ORDER BY id LIMIT 5"):
        try: args=json.loads(item['arguments_json'])
        except (ValueError,TypeError): args={}
        brief=', '.join(f'{k}: {str(v)[:40]}' for k,v in list(args.items())[:3]) if isinstance(args,dict) else ''
        approvals.append({'id':item['id'],'tool':item['tool_name'],'summary':brief,'created_at':item['created_at']})
    overdue=db.rows("SELECT t.title,t.due_date,p.name AS project FROM tasks t LEFT JOIN projects p ON p.id=t.project_id WHERE t.status!='done' AND t.due_date IS NOT NULL AND t.due_date!='' AND t.due_date<? ORDER BY t.due_date LIMIT 5",(today,))
    plan=planner.plan_view()
    next_tasks=[{'title':i['title'],'priority':i['priority'],'project':i['project'],'estimate_minutes':i['estimate_minutes'],'position':i.get('position')} for i in plan['queue'][:4]]
    timers=reminders.snapshot()
    working=db.rows("SELECT title,status,checkpoint FROM jobs WHERE status IN ('running','queued') ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,id LIMIT 4")
    queued=db.rows("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'")[0]['n']
    recent=db.rows("SELECT title,updated_at FROM jobs WHERE status='completed' ORDER BY id DESC LIMIT 3")
    last=db.rows('SELECT role,content,created_at FROM messages ORDER BY id DESC LIMIT 1')
    focus=db.rows('SELECT enabled,goal,ends_at FROM focus WHERE id=1')
    ollama=WALL_CACHE['ollama']
    return {'server_time':datetime.now().astimezone().isoformat(timespec='seconds'),'model':config.MODEL,'ollama_online':bool(ollama.get('online')),
        'approvals':approvals,'events':WALL_CACHE['events'],'overdue':overdue,'next_tasks':next_tasks,'working':working,'queued':queued,'recent_jobs':recent,
        'last_message':({'role':last[0]['role'],'content':str(last[0]['content'])[:600],'created_at':last[0]['created_at']} if last else None),
        'focus':({'enabled':bool(focus[0]['enabled']),'goal':focus[0]['goal'],'ends_at':focus[0]['ends_at']} if focus and focus[0]['enabled'] else None),
        'timers':timers['active'][:6],'alerts':timers['alerts'],'events_cursor':events.cursor(),
        'needs_input':[{'id':i['id'],'title':i['title'],'project':i['project'],'kind':i['kind'],'question':' '.join(i['question'].split())[:600]} for i in plan['needs_you'][:4]],
        'plan':{'autopilot':plan['autopilot'],'running':[{'title':i['title'],'project':i['project']} for i in plan['running'][:2]],'mine':len(plan['mine']),'blocked':len(plan['blocked']),'total_eta_minutes':plan['total_eta_minutes']}}

def payload(handler, limit=1_000_000):
    try: length = int(handler.headers.get('Content-Length','0') or 0)
    except ValueError: raise ValueError('Invalid request size')
    if length < 0 or length > limit: raise ValueError(f'Request is larger than {limit // 1_000_000} MB')
    return json.loads(handler.rfile.read(length) or b'{}')

def send_json(handler, value, status=200):
    data=json.dumps(value,ensure_ascii=False).encode()
    handler.send_response(status); handler.send_header('Content-Type','application/json; charset=utf-8')
    handler.send_header('Content-Length',str(len(data))); handler.send_header('Cache-Control','no-store'); handler.end_headers(); handler.wfile.write(data)

class Handler(BaseHTTPRequestHandler):
    server_version=f'Sage/{config.VERSION}'
    def log_message(self, fmt, *args): pass
    def chat_stream(self, body):
        """Stream a chat reply as newline-delimited JSON events: start, token, status, reset, done, error."""
        jobs.note_interaction(); project_id=body.get('project_id') or None
        if project_id:
            try: project_id=int(project_id)
            except (TypeError,ValueError): return send_json(self,{'error':'Invalid project selection'},400)
            if not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
        message=str(body.get('message',''))
        if not message.strip(): return send_json(self,{'error':'Message is empty'},400)
        computer_task_id=body.get('computer_task_id')
        if computer_task_id is not None:
            try:
                computer_task_id=int(computer_task_id)
                task=computer_tasks.get(computer_task_id)
                if not task or task['status']!='interrupted':return send_json(self,{'error':'This saved PC task is no longer available to resume.'},409)
            except (TypeError,ValueError):return send_json(self,{'error':'Invalid saved PC task id.'},400)
        stream_id=re.sub(r'[^\w-]','',str(body.get('id') or ''))[:64] or uuid.uuid4().hex
        cancel=llm.StreamCancel(); STREAMS[stream_id]=cancel
        self.send_response(200); self.send_header('Content-Type','application/x-ndjson; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Accel-Buffering','no'); self.end_headers()
        alive={'ok':True}
        def emit(event):
            if not alive['ok']: return
            try:
                self.wfile.write((json.dumps(event,ensure_ascii=False)+'\n').encode()); self.wfile.flush()
            except OSError:
                # The browser went away: stop generating instead of finishing a reply nobody will see.
                alive['ok']=False; cancel.cancel()
        emit({'type':'start','id':stream_id})
        try:
            answer=agent.respond(message,project_id=project_id,emit=emit,cancel=cancel,computer_task_id=computer_task_id)
            emit({'type':'done','answer':answer,'stopped':cancel.cancelled})
        except Exception as exc:
            emit({'type':'error','error':str(exc)})
        finally:
            STREAMS.pop(stream_id,None); jobs.touch()
    def client_is_allowed(self):
        try: address=ipaddress.ip_address(self.client_address[0])
        except ValueError: return False
        if address.is_loopback: return True
        return str(address) in config.ALLOWED_CLIENT_IPS
    def upload_engineering_asset(self):
        params=parse_qs(urlparse(self.path).query)
        original=Path(params.get('name',[''])[0]).name
        extension=Path(original).suffix.lower()
        images={'.jpg','.jpeg','.png','.webp'}
        cad={'.step','.stp','.stl','.dxf','.kicad_pcb','.kicad_sch','.csv','.bom','.json'}
        if not original or extension not in images|cad: return send_json(self,{'error':'Supported files: STEP, STL, DXF, KiCad, CSV/BOM, JSON, JPG, PNG, WEBP'},400)
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: length=0
        limit=8_000_000 if extension in images else 40_000_000
        if length<=0 or length>limit: return send_json(self,{'error':f'File size must be between 1 byte and {limit//1_000_000} MB'},413)
        project_id=None
        if params.get('project_id',[''])[0]:
            project_id=int(params['project_id'][0])
            if not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
        content=self.rfile.read(length)
        category='photos' if extension in images else 'assets'
        directory=(config.DATA/'engineering'/category).resolve(); directory.mkdir(parents=True,exist_ok=True)
        stored=f'{uuid.uuid4().hex}{extension}'; destination=(directory/stored).resolve(); destination.relative_to(directory)
        destination.write_bytes(content)
        metadata={'type':extension.lstrip('.').upper(),'size_bytes':length}
        if category=='assets': metadata=engineering.asset_metadata(destination,original,extension)
        else:
            import hashlib
            metadata['sha256']=hashlib.sha256(content).hexdigest()
        asset_id=db.execute('INSERT INTO engineering_assets(project_id,name,stored_name,extension,size_bytes,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',(project_id,original,stored,extension,length,metadata.get('sha256',''),json.dumps(metadata,ensure_ascii=False),db.now()))
        return send_json(self,{'id':asset_id,'name':original,'stored_name':stored,'metadata':metadata,'url':f'/api/workshop/photos/{stored}' if category=='photos' else ''})
    def events_stream(self, since):
        """Server-sent events: every new event is pushed to the browser the moment it happens."""
        self.send_response(200); self.send_header('Content-Type','text/event-stream; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('X-Accel-Buffering','no'); self.end_headers()
        try:
            self.wfile.write(b'retry: 3000\n\n'); self.wfile.flush()
            while True:
                items=events.wait_for(since,15)
                if not items:
                    self.wfile.write(b': keepalive\n\n'); self.wfile.flush(); continue
                for item in items:
                    since=max(since,item['id'])
                    self.wfile.write(f"id: {item['id']}\ndata: {json.dumps(item,ensure_ascii=False)}\n\n".encode()); self.wfile.flush()
        except OSError:
            return
    def do_GET(self):
        if not self.client_is_allowed(): return send_json(self,{'error':'This client IP is not allowed to access Sage'},403)
        path=urlparse(self.path).path
        if path=='/api/events/stream':
            raw=parse_qs(urlparse(self.path).query).get('since',[''])[0]
            since=int(raw) if raw.isdigit() else events.cursor()
            last=self.headers.get('Last-Event-ID','')
            if last.isdigit(): since=max(since,int(last)) if not raw else since
            return self.events_stream(since)
        try:
            handled,value=routes.get(path,parse_qs(urlparse(self.path).query))
        except ValueError as exc: return send_json(self,{'error':str(exc)},400)
        if handled: return send_json(self,value)
        if path=='/': return self.serve_file(config.STATIC_DIR/'index.html')
        if path.startswith('/static/'): return self.serve_file(config.STATIC_DIR/unquote(path[len('/static/'):]))
        if path.startswith('/api/code-files/'):
            filename=unquote(path.rsplit('/',1)[1])
            if not re.fullmatch(r'sage_\d{8}_\d{6}_\d+_\d+\.[a-z0-9]+',filename): return send_json(self,{'error':'Invalid code filename'},400)
            try:
                workspace=config.WORKSPACE.resolve(); source=(workspace/'generated'/filename).resolve(); source.relative_to(workspace)
                if not source.is_file(): return send_json(self,{'error':'Code file not found'},404)
                data=source.read_bytes(); self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.send_header('Content-Disposition',f'attachment; filename="{filename}"'); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data); return
            except ValueError: return send_json(self,{'error':'Invalid code path'},400)
        if path.startswith('/api/project-task-results/') and path.endswith('/download'):
            try: job_id=int(path.split('/')[-2])
            except ValueError: return send_json(self,{'error':'Invalid task result id'},400)
            rows=db.rows("SELECT result FROM jobs WHERE id=? AND kind='project_task'",(job_id,))
            if not rows:return send_json(self,{'error':'Task result not found'},404)
            try:
                details=json.loads(rows[0]['result']); workspace=config.WORKSPACE.resolve(); artifact=Path(details['artifact']).resolve(); artifact.relative_to(workspace)
                if not artifact.is_file():return send_json(self,{'error':'Task result file not found'},404)
                data=artifact.read_bytes()
            except (ValueError,KeyError,TypeError):return send_json(self,{'error':'Invalid task result file'},400)
            download_name='task-'+str(details.get('task_id','result'))+'-result.md'
            self.send_response(200);self.send_header('Content-Type','text/markdown; charset=utf-8');self.send_header('Content-Length',str(len(data)));self.send_header('Content-Disposition',f'attachment; filename="{download_name}"');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data);return
        if path=='/api/workshop/log': return send_json(self,db.rows('SELECT * FROM workshop_entries ORDER BY id DESC LIMIT 100'))
        if path=='/api/workshop/search':
            q=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip()[:200]
            if len(q)<2: return send_json(self,{'results':[]})
            like='%'+q+'%'; found=[]
            searches=[('workshop_entry','SELECT id,note AS title,measurements AS body,created_at FROM workshop_entries WHERE note LIKE ? OR measurements LIKE ? ORDER BY id DESC LIMIT 30'),('failure','SELECT id,title,lesson AS body,created_at FROM failure_lessons WHERE title LIKE ? OR symptom LIKE ? OR cause LIKE ? OR fix LIKE ? OR lesson LIKE ? ORDER BY id DESC LIMIT 30'),('engineering_document','SELECT id,title,body,created_at FROM engineering_docs WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT 20'),('memory','SELECT id,kind AS title,content AS body,updated_at AS created_at FROM memories WHERE content LIKE ? OR kind LIKE ? ORDER BY id DESC LIMIT 20'),('calculation','SELECT id,operation AS title,result_json AS body,created_at FROM engineering_calculations WHERE operation LIKE ? OR inputs_json LIKE ? OR result_json LIKE ? ORDER BY id DESC LIMIT 20'),('project','SELECT id,name AS title,description AS body,created_at FROM projects WHERE name LIKE ? OR description LIKE ? ORDER BY id DESC LIMIT 20')]
            for kind,sql in searches:
                args=tuple([like]*sql.count('?'))
                for item in db.rows(sql,args): item['type']=kind; found.append(item)
            found.sort(key=lambda r:r.get('created_at',''),reverse=True)
            return send_json(self,{'results':found[:60]})
        if path=='/api/inventory':
            items=db.rows('SELECT * FROM inventory_items ORDER BY name COLLATE NOCASE')
            for item in items: item['movements']=db.rows('SELECT delta,reason,created_at FROM inventory_movements WHERE item_id=? ORDER BY id DESC LIMIT 8',(item['id'],))
            return send_json(self,items)
        if path=='/api/engineering/versions':
            return send_json(self,db.rows('SELECT v.*,p.name AS project_name FROM design_versions v LEFT JOIN projects p ON p.id=v.project_id ORDER BY v.project_id,v.name,v.version DESC'))
        if path.startswith('/api/engineering/versions/') and path.endswith('/download'):
            try: vid=int(path.split('/')[-2])
            except ValueError: return send_json(self,{'error':'Invalid version id'},400)
            rows=db.rows('SELECT stored_name FROM design_versions WHERE id=?',(vid,))
            if not rows:return send_json(self,{'error':'Design version not found'},404)
            root=(config.DATA/'engineering'/'versions').resolve(); source=(root/rows[0]['stored_name']).resolve()
            try: source.relative_to(root)
            except ValueError:return send_json(self,{'error':'Invalid version path'},400)
            if not source.is_file():return send_json(self,{'error':'Version file is missing'},404)
            data=source.read_bytes(); self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition',f'attachment; filename="{source.name}"'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(data); return
        if path=='/api/troubleshooting':
            trees=db.rows('SELECT * FROM troubleshooting_trees ORDER BY id DESC')
            for tree in trees:
                tree['steps']=json.loads(tree.pop('steps_json')); tree['sessions']=db.rows("SELECT * FROM troubleshooting_sessions WHERE tree_id=? AND status='active' ORDER BY id DESC",(tree['id'],))
            return send_json(self,trees)
        if path=='/api/failures': return send_json(self,db.rows('SELECT f.*,p.name AS project_name FROM failure_lessons f LEFT JOIN projects p ON p.id=f.project_id ORDER BY id DESC'))
        if path=='/api/tailscale':
            executable=shutil.which('tailscale') or shutil.which('tailscale.exe'); out={'installed':bool(executable),'running':False,'hostname':'','serve_status':'','command':f'tailscale serve localhost:{config.PORT}'}
            if executable:
                try:
                    result=subprocess.run([executable,'status','--json'],capture_output=True,text=True,timeout=1.25,check=False); data=json.loads(result.stdout or '{}'); selfinfo=data.get('Self') or {}; out.update({'running':data.get('BackendState')=='Running','hostname':selfinfo.get('DNSName','').rstrip('.'),'ips':selfinfo.get('TailscaleIPs',[])})
                except Exception: pass
                try:
                    result=subprocess.run([executable,'serve','status'],capture_output=True,text=True,timeout=1.25,check=False); out['serve_status']=(result.stdout or result.stderr).strip()[:2000]
                except Exception: pass
            return send_json(self,out)
        if path=='/api/handoffs': return send_json(self,db.rows('SELECT id,project_id,title,file_path,created_at FROM engineering_docs WHERE kind=\'handoff\' ORDER BY id DESC'))
        if path=='/api/engineering/assets':
            assets=db.rows('SELECT id,project_id,name,stored_name,extension,size_bytes,sha256,metadata_json,created_at FROM engineering_assets ORDER BY id DESC')
            for item in assets:
                try: item['metadata']=json.loads(item.pop('metadata_json'))
                except ValueError: item['metadata']={}
            return send_json(self,assets)
        if path.startswith('/api/engineering/assets/') and path.endswith('/download'):
            try: asset_id=int(path.split('/')[-2])
            except ValueError: return send_json(self,{'error':'Invalid file id'},400)
            rows=db.rows('SELECT stored_name,extension FROM engineering_assets WHERE id=?',(asset_id,))
            if not rows: return send_json(self,{'error':'File not found'},404)
            item=rows[0]; category='photos' if item['extension'] in ('.jpg','.jpeg','.png','.webp') else 'assets'
            root=(config.DATA/'engineering'/category).resolve(); source=(root/item['stored_name']).resolve()
            try: source.relative_to(root)
            except ValueError: return send_json(self,{'error':'Invalid file path'},400)
            data=source.read_bytes(); self.send_response(200); self.send_header('Content-Type','application/octet-stream'); self.send_header('Content-Disposition',f'attachment; filename="{item["stored_name"]}"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path=='/api/engineering/documents':
            docs=db.rows('SELECT * FROM engineering_docs ORDER BY id DESC')
            for doc in docs:
                try: doc['metadata']=json.loads(doc.pop('metadata_json'))
                except ValueError: doc['metadata']={}
                doc['results']=db.rows('SELECT outcome,notes,measurements,created_at FROM engineering_doc_results WHERE doc_id=? ORDER BY id DESC',(doc['id'],))
            return send_json(self,docs)
        if path.startswith('/api/engineering/documents/') and path.endswith('/download'):
            try: doc_id=int(path.split('/')[-2])
            except ValueError: return send_json(self,{'error':'Invalid document id'},400)
            rows=db.rows('SELECT title,file_path FROM engineering_docs WHERE id=?',(doc_id,))
            if not rows: return send_json(self,{'error':'Document not found'},404)
            source=Path(rows[0]['file_path']).resolve()
            try: source.relative_to(config.WORKSPACE.resolve())
            except ValueError: return send_json(self,{'error':'Invalid document path'},400)
            data=source.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/markdown; charset=utf-8'); self.send_header('Content-Disposition',f'attachment; filename="engineering_{doc_id}.md"'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        if path=='/api/engineering/code-projects':
            projects=db.rows('SELECT id,project_id,name,prompt,directory,summary,status,created_at FROM code_projects ORDER BY id DESC')
            for project in projects:
                project['files']=db.rows('SELECT id,filename FROM code_files WHERE project_id=? ORDER BY id',(project['id'],))
            return send_json(self,projects)
        if path.startswith('/api/engineering/code-projects/') and path.endswith('/files'):
            try: project_id=int(path.split('/')[-2])
            except ValueError: return send_json(self,{'error':'Invalid project id'},400)
            files=db.rows('SELECT filename,relative_path FROM code_files WHERE project_id=? ORDER BY id',(project_id,)); out=[]
            for item in files:
                file=Path(item['relative_path']).resolve(); root=(config.WORKSPACE/'engineering'/'code').resolve()
                try: file.relative_to(root)
                except ValueError: continue
                if file.is_file(): out.append({'filename':item['filename'],'content':file.read_text(encoding='utf-8',errors='replace')[:50000]})
            return send_json(self,out)
        if path.startswith('/api/workshop/photos/'):
            filename=unquote(path.rsplit('/',1)[1])
            row=db.rows("SELECT stored_name,extension FROM engineering_assets WHERE stored_name=? AND extension IN ('.jpg','.jpeg','.png','.webp')",(filename,))
            if not row: return send_json(self,{'error':'Photo not found'},404)
            source=(config.DATA/'engineering'/'photos'/row[0]['stored_name']).resolve()
            try: source.relative_to((config.DATA/'engineering'/'photos').resolve())
            except ValueError: return send_json(self,{'error':'Invalid photo path'},400)
            data=source.read_bytes(); self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(filename)[0] or 'application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data); return
        if path=='/api/status': return send_json(self, {"ollama":llm.status(),"model":config.MODEL,"wake_word":config.WAKE_WORD,"web_search":config.WEB_SEARCH_ENABLED,"counts":{"memories":len(db.rows('SELECT id FROM memories')),"projects":len(db.rows('SELECT id FROM projects')),"jobs":len(db.rows('SELECT id FROM jobs'))}})
        if path=='/api/system/health': return send_json(self,{'host':system_control.system_health(),'ollama':llm.status(),'chat_model':config.MODEL,'vision_model':config.VISION_MODEL})
        if path=='/api/telegram/status': return send_json(self,telegram.status())
        if path=='/api/wall': return send_json(self,wall_snapshot())
        if path=='/api/messages': return send_json(self,db.rows('SELECT * FROM messages ORDER BY id DESC LIMIT 80'))
        if path=='/api/computer/tasks': return send_json(self,computer_tasks.get_recoverable())
        if path=='/api/chat/context': return send_json(self,agent.context_controls())
        if path=='/api/memory-search':
            query=parse_qs(urlparse(self.path).query).get('q',[''])[0].strip()
            if not query:return send_json(self,{'error':'Enter a search query'},400)
            try:return send_json(self,{'query':query,'results':memory.semantic_search(query,limit=10),'model':config.EMBED_MODEL})
            except RuntimeError as exc:return send_json(self,{'error':str(exc)},503)
        if path=='/api/memories':
            params=parse_qs(urlparse(self.path).query)
            query=params.get('q',[''])[0].strip()
            status=params.get('status',['all'])[0].strip() or 'all'
            return send_json(self,memory.list_memories(status=status,query=query,limit=2000))
        if path=='/api/projects':
            projects=db.rows('SELECT * FROM projects ORDER BY id DESC')
            project_task_jobs=db.rows("SELECT id,status,result FROM jobs WHERE kind='project_task' ORDER BY id DESC")
            work_by_task={}
            for job in project_task_jobs:
                try: details=json.loads(job.get('result') or '{}')
                except (ValueError,TypeError): continue
                if details.get('task_id') and details['task_id'] not in work_by_task:
                    details['job_id']=job['id']; details['job_status']=job['status']; work_by_task[details['task_id']]=details
            for p in projects:
                p['tasks']=db.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY id',(p['id'],))
                for task in p['tasks']: task['ai_work']=work_by_task.get(task['id'])
            return send_json(self,projects)
        if path.startswith('/api/projects/') and path.endswith('/parts-status'):
            try: pid=int(path.split('/')[-2])
            except ValueError:return send_json(self,{'error':'Invalid project id'},400)
            if not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):return send_json(self,{'error':'Project not found'},404)
            rows=db.rows('SELECT * FROM engineering_assets WHERE project_id=? AND extension IN (\'.csv\',\'.bom\')',(pid,)); required=[]
            for asset in rows:
                root=(config.DATA/'engineering'/'assets').resolve(); file=(root/asset['stored_name']).resolve()
                try:file.relative_to(root)
                except ValueError:continue
                if not file.is_file():continue
                try:
                    with file.open(encoding='utf-8-sig',newline='') as stream:
                        reader=csv.DictReader(stream)
                        for row in reader:
                            lower={str(k or '').strip().lower():str(v or '').strip() for k,v in row.items()}
                            part=lower.get('part number') or lower.get('part_number') or ''; name=lower.get('description') or lower.get('name') or part
                            try:qty=float(lower.get('quantity','1') or 1)
                            except ValueError:continue
                            found=db.rows('SELECT quantity,unit FROM inventory_items WHERE (project_id=? OR project_id IS NULL) AND ((?<>\'\' AND lower(part_number)=lower(?)) OR lower(name)=lower(?)) ORDER BY project_id DESC LIMIT 1',(pid,part,part,name))
                            stock=float(found[0]['quantity']) if found else 0
                            required.append({'part_number':part,'name':name,'required':qty,'on_hand':stock,'shortage':max(0,qty-stock),'unit':found[0]['unit'] if found else (lower.get('unit') or 'each'),'source':asset['name']})
                except (OSError,csv.Error):continue
            return send_json(self,{'project_id':pid,'parts':required,'shortages':[x for x in required if x['shortage']>0]})
        if path=='/api/measurement-templates':
            templates=db.rows('SELECT * FROM measurement_templates ORDER BY id DESC')
            for item in templates:
                item['fields']=json.loads(item.pop('fields_json'));item['runs']=db.rows('SELECT * FROM measurement_runs WHERE template_id=? ORDER BY id DESC LIMIT 10',(item['id'],))
                for run in item['runs']:run['values']={value['field_name']:{'value':value['value'],'unit':value['unit'],'target':value['target'],'tolerance':value['tolerance']} for value in db.rows('SELECT field_name,value,unit,target,tolerance FROM measurement_values WHERE run_id=?',(run['id'],))}
            return send_json(self,templates)
        if path=='/api/decisions':return send_json(self,db.rows('SELECT d.*,p.name AS project_name FROM decision_log d LEFT JOIN projects p ON p.id=d.project_id ORDER BY d.id DESC'))
        if path=='/api/session-summaries':return send_json(self,db.rows('SELECT * FROM session_summaries ORDER BY id DESC LIMIT 50'))
        if path=='/api/backups':
            root=config.DATA/'backups';root.mkdir(parents=True,exist_ok=True);items=[]
            for file in sorted(root.glob('sage-backup-*.zip'),key=lambda x:x.stat().st_mtime,reverse=True):items.append({'name':file.name,'size':file.stat().st_size,'created_at':datetime.fromtimestamp(file.stat().st_mtime).isoformat(timespec='seconds')})
            return send_json(self,items)
        if path.startswith('/api/engineering/versions/') and path.endswith('/compare'):
            try:vid=int(path.split('/')[-2])
            except ValueError:return send_json(self,{'error':'Invalid version id'},400)
            current=db.rows('SELECT * FROM design_versions WHERE id=?',(vid,))
            if not current:return send_json(self,{'error':'Design version not found'},404)
            cur=current[0]; prior=db.rows('SELECT * FROM design_versions WHERE project_id IS ? AND name=? AND version<? ORDER BY version DESC LIMIT 1',(cur['project_id'],cur['name'],cur['version']))
            if not prior:return send_json(self,{'current':cur,'previous':None,'summary':'This is the first saved version.'})
            old=prior[0];root=(config.DATA/'engineering'/'versions').resolve()
            def read_version(item):
                p=(root/item['stored_name']).resolve();p.relative_to(root);return p.read_bytes() if p.is_file() and p.stat().st_size<=1_000_000 else None
            a,b=read_version(old),read_version(cur);diff='';
            if a is not None and b is not None:
                try:diff=''.join(list(difflib.unified_diff(a.decode('utf-8').splitlines(True),b.decode('utf-8').splitlines(True),fromfile=f"v{old['version']}",tofile=f"v{cur['version']}",n=2))[:400])
                except UnicodeDecodeError:pass
            return send_json(self,{'previous':old,'current':cur,'changed':old['sha256']!=cur['sha256'],'diff':diff,'summary':'Files are identical.' if old['sha256']==cur['sha256'] else ('Text diff available.' if diff else 'Binary file changed; compare hashes and file size.')})
        if path.startswith('/api/engineering/documents/') and path.endswith('/compare'):
            try:did=int(path.split('/')[-2])
            except ValueError:return send_json(self,{'error':'Invalid document id'},400)
            docs=db.rows('SELECT * FROM engineering_docs WHERE id=? AND kind=\'test_plan\'',(did,))
            if not docs:return send_json(self,{'error':'Test plan not found'},404)
            runs=db.rows('SELECT * FROM engineering_doc_results WHERE doc_id=? ORDER BY id DESC LIMIT 2',(did,))
            if len(runs)<2:return send_json(self,{'runs':runs,'summary':'Record two test runs to compare results.'})
            return send_json(self,{'latest':runs[0],'previous':runs[1],'changed':{key:{'from':runs[1][key],'to':runs[0][key]} for key in ('outcome','notes','measurements') if runs[0][key]!=runs[1][key]}})
        if path=='/api/jobs': return send_json(self,db.rows('SELECT * FROM jobs ORDER BY id DESC'))
        if path=='/api/plugins': return send_json(self,plugins.discover())
        if path=='/api/tools':
            return send_json(self,[{'name':x['function']['name'],'description':x['function'].get('description',''),'schema':x['function'].get('parameters',{}),'permission':tool_registry.permission(x['function']['name'])} for x in tool_registry.definitions()])
        if path=='/api/outlook/status': return send_json(self,{**outlook.status(),'pending':outlook.pending_flow()})
        if path=='/api/outlook/events':
            days=parse_qs(urlparse(self.path).query).get('days',['7'])[0]
            try:return send_json(self,{'events':outlook.upcoming(days)})
            except (ValueError,PermissionError,ConnectionError) as exc:return send_json(self,{'error':str(exc)},503)
        if path=='/api/tool-approvals':
            pending=db.rows("SELECT id,tool_name,arguments_json,created_at FROM tool_approvals WHERE status='pending' ORDER BY id")
            for item in pending:
                try:item['arguments']=json.loads(item.pop('arguments_json'))
                except ValueError:item['arguments']={}
            return send_json(self,pending)
        if path=='/api/actions': return send_json(self,db.rows('SELECT * FROM action_log ORDER BY id DESC LIMIT 100'))
        if path=='/api/notes':
            config.VAULT.mkdir(parents=True,exist_ok=True)
            return send_json(self,[{"name":p.stem,"content":p.read_text(encoding='utf-8')} for p in config.VAULT.glob('*.md')])
        if path=='/api/briefing':
            today=date.today().isoformat()
            projects=db.rows("SELECT * FROM projects WHERE status='active' ORDER BY priority DESC,id DESC")
            for p in projects: p['tasks']=db.rows("SELECT * FROM tasks WHERE project_id=? AND status!='done' ORDER BY id LIMIT 4",(p['id'],))
            overdue=db.rows("SELECT title,due_date FROM tasks WHERE status!='done' AND due_date IS NOT NULL AND due_date!='' AND due_date<? ORDER BY due_date",(today,))
            recent=db.rows("SELECT title,result,updated_at FROM jobs WHERE status='completed' ORDER BY id DESC LIMIT 5")
            learning=db.rows("SELECT content,updated_at FROM memories WHERE kind='knowledge' ORDER BY updated_at DESC LIMIT 4")
            return send_json(self,{'date':today,'projects':projects,'overdue':overdue,'recent_jobs':recent,'recent_learning':learning})
        if path=='/api/checkins':
            today=date.today().isoformat(); result=[]
            for p in db.rows("SELECT * FROM projects WHERE status='active' ORDER BY id DESC"):
                tasks=db.rows("SELECT id,title,status,due_date FROM tasks WHERE project_id=? ORDER BY id",(p['id'],))
                reasons=[]
                if p.get('due_date') and p['due_date']<today: reasons.append('Project deadline has passed')
                if tasks and all(t['status']=='done' for t in tasks): reasons.append('All listed tasks are complete')
                if not tasks and not p.get('next_action'): reasons.append('No next action is set')
                if any(t.get('due_date') and t['due_date']<today and t['status']!='done' for t in tasks): reasons.append('A task is overdue')
                if reasons: result.append({'project':p,'tasks':tasks,'reasons':reasons})
            return send_json(self,result)
        if path=='/api/focus':
            row=db.rows('SELECT * FROM focus WHERE id=1')[0]
            if row['enabled'] and row.get('ends_at'):
                try:
                    if datetime.fromisoformat(row['ends_at'])<datetime.now(timezone.utc):
                        db.execute("UPDATE focus SET enabled=0,updated_at=? WHERE id=1",(db.now(),)); row=db.rows('SELECT * FROM focus WHERE id=1')[0]
                except ValueError: pass
            return send_json(self,row)
        if path in ('/api/research','/api/learner'):
            kind='research' if path.endswith('research') else 'learning'
            return send_json(self,db.rows('SELECT * FROM jobs WHERE kind=? ORDER BY id DESC',(kind,)))
        if path=='/api/watchlist': return send_json(self,db.rows('SELECT * FROM watchlist ORDER BY id DESC'))
        if path=='/api/plugin-requests': return send_json(self,db.rows("SELECT * FROM plugin_requests WHERE status='proposed' ORDER BY id DESC"))
        return send_json(self,{"error":"Not found"},404)
    def serve_file(self,path):
        try:
            resolved=path.resolve(); resolved.relative_to(config.STATIC_DIR.resolve())
            data=resolved.read_bytes()
        except Exception: return send_json(self,{"error":"Not found"},404)
        self.send_response(200); self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream'); self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        if not self.client_is_allowed(): return send_json(self,{'error':'This client IP is not allowed to access Sage'},403)
        path=urlparse(self.path).path
        try:
            jobs.note_interaction()
            if path=='/api/engineering/assets': return self.upload_engineering_asset()
            if path=='/api/ha/command':
                if not config.HA_TOKEN: return send_json(self,{'error':'Home Assistant bridge is disabled; configure SAGE_HA_TOKEN'},503)
                supplied=self.headers.get('Authorization','')
                if not hmac.compare_digest(supplied,'Bearer '+config.HA_TOKEN): return send_json(self,{'error':'Home Assistant bridge token is invalid'},401)
            if path=='/api/shortcuts/awake':
                if not config.SHORTCUT_TOKEN: return send_json(self,{'error':'iPhone Shortcut endpoint is disabled; configure SAGE_SHORTCUT_TOKEN'},503)
                supplied=self.headers.get('Authorization','')
                if not hmac.compare_digest(supplied,'Bearer '+config.SHORTCUT_TOKEN): return send_json(self,{'error':'iPhone Shortcut token is invalid'},401)
            body=payload(self, limit=7_000_000 if path=='/api/screen/describe' else 1_000_000)
            handled,value=routes.post(path,body)
            if handled: return send_json(self,value)
            if path=='/api/system/screen':
                question=str(body.get('question') or 'Describe the current desktop, including the visible app and any dialog or buttons.')[:1000]
                try:return send_json(self,system_control.inspect_screen(question,include_preview=True))
                except llm.OllamaError as exc:return send_json(self,{'error':str(exc)},503)
                except (RuntimeError,PermissionError,ValueError) as exc:return send_json(self,{'error':str(exc)},503)
            if path=='/api/screen/describe':
                image=str(body.get('image',''))
                question=str(body.get('question') or 'Briefly describe what is visible on this screen.')[:1000]
                if not image or len(image)>6_500_000: return send_json(self,{'error':'Capture image is missing or too large (maximum 4.8 MB encoded).'},413)
                try: raw=base64.b64decode(image,validate=True)
                except (binascii.Error,ValueError): return send_json(self,{'error':'Capture image must be valid base64 JPEG data.'},400)
                if len(raw)>4_800_000: return send_json(self,{'error':'Capture image is too large (maximum 4.8 MB).'},413)
                if not raw.startswith(b'\xff\xd8\xff'): return send_json(self,{'error':'Capture must be a JPEG image.'},400)
                if not question.strip(): question='Briefly describe what is visible on this screen.'
                try: answer=llm.describe_image(question,image)
                except llm.OllamaError as exc: return send_json(self,{'error':str(exc)},503)
                return send_json(self,{'answer':answer,'model':config.VISION_MODEL})
            if path=='/api/chat/context/pins':
                return send_json(self,agent.update_context_pins(body.get('action'),body.get('item')))
            if path=='/api/outlook/connect':
                try:return send_json(self,outlook.begin_device_flow(body.get('client_id',''),body.get('tenant','common')))
                except (ValueError,ConnectionError) as exc:return send_json(self,{'error':str(exc)},400)
            if path=='/api/outlook/poll':
                try:return send_json(self,outlook.poll_device_flow())
                except (ValueError,PermissionError,ConnectionError) as exc:return send_json(self,{'error':str(exc)},502)
            if path=='/api/outlook/disconnect': return send_json(self,outlook.disconnect())
            if path=='/api/outlook/ics/connect':
                try:return send_json(self,outlook.connect_ics(body.get('url','')))
                except ValueError as exc:return send_json(self,{'error':str(exc)},400)
            if path=='/api/outlook/ics/disconnect': return send_json(self,outlook.disconnect_ics())
            if path=='/api/ha/command':
                prompts={
                    'briefing':'Give me a short spoken daily briefing using saved Sage project and task records. Mention urgent overdue work and the best next action. Keep it under 120 words.',
                    'next_action':'What is the single best next action across my active Sage projects? Use recorded priorities and deadlines, and keep the answer under 80 words.',
                    'project_status':'Give me a concise status update on my active Sage projects using saved Sage records only. Keep it under 120 words.'
                }
                command=str(body.get('command','')).strip()
                if command not in prompts: return send_json(self,{'error':'Choose briefing, next_action, or project_status'},400)
                answer=agent.respond(prompts[command]); return send_json(self,{'answer':answer,'command':command})
            if path=='/api/chat/cancel':
                handle=STREAMS.get(str(body.get('id','')))
                if handle: handle.cancel()
                return send_json(self,{'ok':bool(handle)})
            if path=='/api/chat/stream': return self.chat_stream(body)
            if path=='/api/telegram/pair-code':
                try:return send_json(self,telegram.create_pair_code())
                except telegram.TelegramError as exc:return send_json(self,{'error':str(exc)},503)
            if path=='/api/telegram/unlink': return send_json(self,telegram.unlink())
            if path.startswith('/api/computer/tasks/') and path.endswith('/dismiss'):
                try:
                    task_id=int(path.split('/')[-2]);computer_tasks.dismiss(task_id)
                    return send_json(self,{'ok':True})
                except (ValueError,TypeError) as exc:return send_json(self,{'error':str(exc)},400)
            if path=='/api/chat':
                jobs.note_interaction(); project_id=body.get('project_id') or None
                if project_id:
                    try: project_id=int(project_id)
                    except (TypeError,ValueError): return send_json(self,{'error':'Invalid project selection'},400)
                    if not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
                try: answer=agent.respond(str(body.get('message','')),project_id=project_id)
                finally: jobs.touch()
                return send_json(self,{"answer":answer})
            if path=='/api/measurement-templates':
                title=str(body.get('title','')).strip()[:160];fields=body.get('fields',[]);pid=body.get('project_id') or None
                if not title or not isinstance(fields,list) or not fields or len(fields)>40:return send_json(self,{'error':'Add a title and 1–40 measurement fields'},400)
                clean=[]
                for field in fields:
                    if not isinstance(field,dict) or not str(field.get('name','')).strip():continue
                    clean.append({k:str(field.get(k,'')).strip()[:120] for k in ('name','unit','target','tolerance')})
                if not clean:return send_json(self,{'error':'Each field needs a name'},400)
                if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):return send_json(self,{'error':'Project not found'},404)
                tid=db.execute('INSERT INTO measurement_templates(project_id,title,description,fields_json,created_at) VALUES(?,?,?,?,?)',(pid,title,str(body.get('description',''))[:1000],json.dumps(clean,ensure_ascii=False),db.now()))
                return send_json(self,{'id':tid})
            if path.startswith('/api/measurement-templates/') and path.endswith('/runs'):
                try:tid=int(path.split('/')[-2])
                except ValueError:return send_json(self,{'error':'Invalid template id'},400)
                template=db.rows('SELECT * FROM measurement_templates WHERE id=?',(tid,))
                if not template:return send_json(self,{'error':'Template not found'},404)
                values=body.get('values',{}); fields=json.loads(template[0]['fields_json']);pid=body.get('project_id') or template[0]['project_id']
                rid=db.execute('INSERT INTO measurement_runs(template_id,project_id,notes,created_at) VALUES(?,?,?,?)',(tid,pid,str(body.get('notes',''))[:2000],db.now()))
                for f in fields:
                    val=values.get(f['name'],'') if isinstance(values,dict) else ''
                    db.execute('INSERT INTO measurement_values(run_id,field_name,value,unit,target,tolerance,created_at) VALUES(?,?,?,?,?,?,?)',(rid,f['name'],str(val)[:200],f['unit'],f['target'],f['tolerance'],db.now()))
                return send_json(self,{'id':rid})
            if path=='/api/decisions':
                title=str(body.get('title','')).strip()[:200];decision=str(body.get('decision','')).strip()[:4000];pid=body.get('project_id') or None
                if not title or not decision:return send_json(self,{'error':'Decision title and chosen option are required'},400)
                if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):return send_json(self,{'error':'Project not found'},404)
                did=db.execute('INSERT INTO decision_log(project_id,title,decision,alternatives,rationale,source,created_at) VALUES(?,?,?,?,?,?,?)',(pid,title,decision,str(body.get('alternatives',''))[:3000],str(body.get('rationale',''))[:3000],str(body.get('source',''))[:500],db.now()))
                (config.VAULT/f'Decision {did} - {re.sub(r"[^\w -]", "", title)[:100]}.md').write_text(f'# Decision: {title}\n\n## Chosen option\n{decision}\n\n## Alternatives\n{body.get("alternatives", "Not recorded")}\n\n## Rationale\n{body.get("rationale", "Not recorded")}\n\nSource: {body.get("source", "Not recorded")}\n',encoding='utf-8')
                return send_json(self,{'id':did})
            if path=='/api/session-summaries/draft':
                pid=body.get('project_id') or None
                if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):return send_json(self,{'error':'Project not found'},404)
                context={'project':db.rows('SELECT * FROM projects WHERE id=?',(pid,))[0] if pid else None,
                    'messages':list(reversed(db.rows('SELECT role,content,created_at FROM messages ORDER BY id DESC LIMIT 30'))),
                    'entries':db.rows('SELECT note,measurements,created_at FROM workshop_entries WHERE (? IS NULL OR project_id=?) ORDER BY id DESC LIMIT 12',(pid,pid)),
                    'tests':db.rows('SELECT d.title,r.outcome,r.notes,r.measurements,r.created_at FROM engineering_doc_results r JOIN engineering_docs d ON d.id=r.doc_id WHERE (? IS NULL OR d.project_id=?) ORDER BY r.id DESC LIMIT 12',(pid,pid)),
                    'recent_jobs':db.rows("SELECT title,status,checkpoint,result FROM jobs WHERE status IN ('completed','failed') ORDER BY id DESC LIMIT 8")}
                title=str(body.get('title','Work session summary')).strip()[:180];sid=db.execute('INSERT INTO session_summaries(project_id,title,content,status,created_at,updated_at) VALUES(?,?,?,\'drafting\',?,?)',(pid,title,'',db.now(),db.now()))
                jid=jobs.create(title,'Draft a factual work-session summary from captured evidence. Separate changes, verification, issues, and next steps. Never imply unrecorded work happened. Return Markdown only. Evidence: '+json.dumps(context,ensure_ascii=False,default=str)[:16000],'session_summary',json.dumps({'summary_id':sid},ensure_ascii=False))
                db.execute('UPDATE session_summaries SET job_id=? WHERE id=?',(jid,sid));return send_json(self,{'id':sid,'job_id':jid,'status':'queued'})
            if path.startswith('/api/session-summaries/'):
                try:sid=int(path.rsplit('/',1)[1])
                except ValueError:return send_json(self,{'error':'Invalid summary id'},400)
                row=db.rows("SELECT * FROM session_summaries WHERE id=? AND status='pending_review'",(sid,))
                if not row:return send_json(self,{'error':'Summary is not ready for review'},404)
                if body.get('action')=='discard':db.execute("UPDATE session_summaries SET status='discarded',updated_at=? WHERE id=?",(db.now(),sid));return send_json(self,{'ok':True})
                if body.get('action')!='approve':return send_json(self,{'error':'Choose approve or discard'},400)
                safe=re.sub(r'[^\w -]','',row[0]['title']).strip()[:120] or f'Session {sid}';dest=(config.VAULT/f'{safe} {sid}.md').resolve();dest.relative_to(config.VAULT.resolve());dest.write_text(row[0]['content']+'\n',encoding='utf-8');db.execute("UPDATE session_summaries SET status='saved',updated_at=? WHERE id=?",(db.now(),sid));return send_json(self,{'ok':True,'name':dest.name})
            if path=='/api/backups':
                root=config.DATA/'backups';root.mkdir(parents=True,exist_ok=True);stamp=datetime.now().strftime('%Y%m%d-%H%M%S');dest=root/f'sage-backup-{stamp}.zip'
                dbcopy=root/f'.{uuid.uuid4().hex}.db';source=db.connect();target=__import__('sqlite3').connect(dbcopy)
                try:source.backup(target)
                finally:target.close();source.close()
                manifest={'created_at':db.now(),'includes':['database','vault','workspace','engineering/import files']}
                try:
                    with zipfile.ZipFile(dest,'w',zipfile.ZIP_DEFLATED) as archive:
                        archive.write(dbcopy,'sage.db');
                        for folder in ('vault','workspace','engineering','imports'):
                            base=config.DATA/folder
                            if base.exists():
                                for file in base.rglob('*'):
                                    if file.is_file():archive.write(file,Path(folder)/file.relative_to(base))
                        archive.writestr('manifest.json',json.dumps(manifest,indent=2))
                finally:dbcopy.unlink(missing_ok=True)
                return send_json(self,{'name':dest.name,'created_at':manifest['created_at'],'size':dest.stat().st_size})
            if path.startswith('/api/backups/') and path.endswith('/restore'):
                name=path.split('/')[-2]
                if not re.fullmatch(r'sage-backup-\d{8}-\d{6}\.zip',name):return send_json(self,{'error':'Invalid backup name'},400)
                if body.get('confirmed') is not True:return send_json(self,{'error':'Explicit confirmation is required'},400)
                if db.rows("SELECT id FROM jobs WHERE status='running'"):return send_json(self,{'error':'Pause or wait for active background jobs before restoring'},409)
                root=(config.DATA/'backups').resolve();archive=(root/name).resolve();archive.relative_to(root)
                if not archive.is_file():return send_json(self,{'error':'Backup not found'},404)
                stage=Path(tempfile.mkdtemp(prefix='sage-restore-',dir=root));backup_root=config.DATA/'.restore-previous';backup_root.mkdir(exist_ok=True)
                moved=[]; restored=False
                try:
                    with zipfile.ZipFile(archive) as z:
                        members=z.infolist()
                        if sum(item.file_size for item in members)>1_000_000_000:raise ValueError('Backup expands beyond the 1 GB restore limit')
                        for member in members:
                            p=Path(member.filename)
                            if p.is_absolute() or '..' in p.parts or (member.external_attr >> 16) & 0o170000 == 0o120000:raise ValueError('Backup contains an unsafe path')
                        if 'sage.db' not in z.namelist():raise ValueError('Backup is missing its database')
                        z.extractall(stage)
                    check=__import__('sqlite3').connect(stage/'sage.db')
                    try:
                        if check.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Backup database failed its integrity check')
                    finally:check.close()
                    for folder in ('vault','workspace','engineering','imports'):
                        src=stage/folder
                        if src.exists():
                            dest=config.DATA/folder;old=backup_root/(folder+'-'+uuid.uuid4().hex)
                            moved.append((dest,old,None))
                            if dest.exists():shutil.move(str(dest),str(old));moved[-1]=(dest,old,True)
                            shutil.move(str(src),str(dest));moved[-1]=(dest,old,True)
                    import os
                    live=config.DB_PATH;temp=live.with_name(live.name+'.restore');shutil.copy2(stage/'sage.db',temp);os.replace(temp,live)
                    Path(str(live)+'-wal').unlink(missing_ok=True);Path(str(live)+'-shm').unlink(missing_ok=True)
                    restored=True
                    return send_json(self,{'ok':True,'message':'Backup restored. Restart Sage to reload the restored database.'})
                except Exception:
                    for dest,old,was_moved in reversed(moved):
                        if dest.exists():shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
                        if old.exists():shutil.move(str(old),str(dest))
                    raise
                finally:shutil.rmtree(stage,ignore_errors=True)
            if path=='/api/memories':
                content=str(body.get('content','')).strip()
                if not content: return send_json(self,{"error":"Memory cannot be empty"},400)
                mid=memory.save(content,body.get('kind','preference'),'user',1.0)
                return send_json(self,{"id":mid})
            if path.startswith('/api/memories/') and not path.startswith('/api/memories/delete/'):
                mid=int(path.rsplit('/',1)[1])
                return send_json(self,memory.update_memory(mid,**{k:v for k,v in body.items() if k in ('status','pinned','importance','content')}))
            if path.startswith('/api/memories/delete/'):
                mid=int(path.rsplit('/',1)[1]);db.execute('DELETE FROM memory_embeddings WHERE memory_id=?',(mid,));db.execute('DELETE FROM memories WHERE id=?',(mid,));return send_json(self,{"ok":True})
            if path=='/api/projects':
                name=str(body.get('name','')).strip()
                if not name: return send_json(self,{"error":"Project name required"},400)
                pid=db.execute('INSERT INTO projects(name,description,status,priority,due_date,next_action,created_at) VALUES(?,?,?,?,?,?,?)',(name,body.get('description',''),'active',body.get('priority','medium'),body.get('due_date'),body.get('next_action',''),db.now()))
                return send_json(self,{"id":pid})
            if path=='/api/tasks':
                title=str(body.get('title','')).strip()
                if not title: return send_json(self,{"error":"Task title required"},400)
                tid=db.execute('INSERT INTO tasks(project_id,title,description,status,priority,due_date,depends_on,created_at) VALUES(?,?,?,\'todo\',?,?,?,?)',(body.get('project_id') or None,title,body.get('description',''),body.get('priority','medium'),body.get('due_date'),json.dumps(body.get('depends_on',[])),db.now()))
                return send_json(self,{"id":tid})
            if path.startswith('/api/tasks/'):
                try: tid=int(path.rsplit('/',1)[1])
                except ValueError: return send_json(self,{'error':'Invalid task id'},400)
                status=body.get('status','todo')
                if status not in ('todo','doing','blocked','done'): return send_json(self,{'error':'Choose todo, doing, blocked, or done'},400)
                if not db.rows('SELECT id FROM tasks WHERE id=?',(tid,)): return send_json(self,{'error':'Task not found'},404)
                db.execute('UPDATE tasks SET status=? WHERE id=?',(status,tid))
                return send_json(self,{'ok':True,'id':tid,'status':status})
            if path=='/api/jobs':
                title=str(body.get('title','')).strip(); prompt=str(body.get('prompt','')).strip()
                if not title or not prompt: return send_json(self,{"error":"Title and assignment required"},400)
                return send_json(self,{"id":jobs.create(title,prompt,body.get('kind','planning'))})
            if path.startswith('/api/jobs/'):
                jid=int(path.rsplit('/',1)[1]); action=body.get('action')
                if action=='pause': jobs.set_status(jid,'paused')
                elif action=='resume': jobs.set_status(jid,'queued')
                elif action=='cancel': jobs.set_status(jid,'cancelled')
                else: return send_json(self,{"error":"Unknown action"},400)
                return send_json(self,{"ok":True})
            if path=='/api/plugins/run':
                return send_json(self,plugins.call(body.get('name',''),body.get('arguments',{}),approved=body.get('approved') is True,dry_run=body.get('dry_run') is True))
            if path.startswith('/api/tool-approvals/'):
                try:approval_id=int(path.rsplit('/',1)[1])
                except ValueError:return send_json(self,{'error':'Invalid approval id'},400)
                if 'approved' not in body or not isinstance(body['approved'],bool):return send_json(self,{'error':'Choose approve or decline'},400)
                try:return send_json(self,{'answer':agent.resolve_tool_approval(approval_id,body['approved'])})
                except ValueError as exc:return send_json(self,{'error':str(exc)},404)
            if path=='/api/notes':
                title=re.sub(r'[^\w -]','',str(body.get('title','')).strip()).strip()
                if not title: return send_json(self,{"error":"Note title required"},400)
                dest=(config.VAULT/(title+'.md')).resolve(); dest.relative_to(config.VAULT.resolve())
                text=str(body.get('content',''))
                if body.get('links'): text+='\n\nRelated: '+' '.join('[['+str(x)+']]' for x in body['links'])
                dest.write_text(text+'\n',encoding='utf-8'); return send_json(self,{"ok":True,"name":title})
            if path=='/api/workshop/log':
                note=str(body.get('note','')).strip(); image_name=str(body.get('image_name','')).strip()
                if not note: return send_json(self,{'error':'Add a note about the build or test'},400)
                project_id=body.get('project_id') or None
                if project_id and not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
                if image_name and not db.rows("SELECT id FROM engineering_assets WHERE stored_name=? AND extension IN ('.jpg','.jpeg','.png','.webp')",(image_name,)): return send_json(self,{'error':'Workshop photo not found'},404)
                measurements=str(body.get('measurements','')).strip()[:4000]
                entry_id=db.execute('INSERT INTO workshop_entries(project_id,note,measurements,image_name,created_at) VALUES(?,?,?,?,?)',(project_id,note,measurements,image_name,db.now()))
                project=db.rows('SELECT name FROM projects WHERE id=?',(project_id,)) if project_id else []
                parts=[f'# Workshop entry {entry_id}',f'\n{note}']
                if project: parts.append(f"\nProject: [[Project {project_id}]] — {project[0]['name']}")
                if measurements: parts.append('\n## Measurements\n'+measurements)
                if image_name: parts.append(f'\n![Workshop photo](../engineering/photos/{image_name})')
                (config.VAULT/f'Workshop Entry {entry_id}.md').write_text('\n'.join(parts)+'\n',encoding='utf-8')
                return send_json(self,{'id':entry_id})
            if path=='/api/inventory':
                name=str(body.get('name','')).strip()[:200]
                if not name:return send_json(self,{'error':'Part name is required'},400)
                try: quantity=float(body.get('quantity',0)); reorder=float(body.get('reorder_level',0))
                except (ValueError,TypeError):return send_json(self,{'error':'Quantity and reorder level must be numbers'},400)
                if quantity<0 or reorder<0:return send_json(self,{'error':'Stock values cannot be negative'},400)
                pid=body.get('project_id') or None
                iid=db.execute('INSERT INTO inventory_items(project_id,part_number,name,description,quantity,unit,location,supplier,reorder_level,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,str(body.get('part_number',''))[:120],name,str(body.get('description',''))[:1000],quantity,str(body.get('unit','each'))[:40],str(body.get('location',''))[:200],str(body.get('supplier',''))[:200],reorder,db.now(),db.now()))
                if quantity:db.execute('INSERT INTO inventory_movements(item_id,delta,reason,created_at) VALUES(?,?,?,?)',(iid,quantity,'Initial stock',db.now()))
                return send_json(self,{'id':iid})
            if path.startswith('/api/inventory/') and path.endswith('/move'):
                try:iid=int(path.split('/')[-2]); delta=float(body.get('delta'))
                except (ValueError,TypeError):return send_json(self,{'error':'Enter a valid stock adjustment'},400)
                item=db.rows('SELECT quantity FROM inventory_items WHERE id=?',(iid,))
                if not item:return send_json(self,{'error':'Inventory item not found'},404)
                updated=item[0]['quantity']+delta
                if updated<0:return send_json(self,{'error':'Adjustment would make stock negative'},400)
                reason=str(body.get('reason','Stock adjustment')).strip()[:500]
                db.execute('UPDATE inventory_items SET quantity=?,updated_at=? WHERE id=?',(updated,db.now(),iid)); db.execute('INSERT INTO inventory_movements(item_id,delta,reason,created_at) VALUES(?,?,?,?)',(iid,delta,reason,db.now()))
                return send_json(self,{'ok':True,'quantity':updated})
            if path=='/api/engineering/versions':
                try: asset_id=int(body.get('asset_id'))
                except (ValueError,TypeError):return send_json(self,{'error':'Choose an imported design file'},400)
                rows=db.rows('SELECT * FROM engineering_assets WHERE id=?',(asset_id,))
                if not rows:return send_json(self,{'error':'Design asset not found'},404)
                asset=rows[0]; category='photos' if asset['extension'] in ('.jpg','.jpeg','.png','.webp') else 'assets'; srcroot=(config.DATA/'engineering'/category).resolve(); src=(srcroot/asset['stored_name']).resolve()
                try:src.relative_to(srcroot)
                except ValueError:return send_json(self,{'error':'Invalid source path'},400)
                if not src.is_file():return send_json(self,{'error':'Source design file missing'},404)
                group=f"project:{asset['project_id']}" if asset['project_id'] else 'unlinked'; prior=db.rows('SELECT MAX(version) AS v FROM design_versions WHERE project_id IS ? AND name=?',(asset['project_id'],asset['name'])); version=(prior[0]['v'] or 0)+1
                root=(config.DATA/'engineering'/'versions').resolve(); root.mkdir(parents=True,exist_ok=True); stored=f'{uuid.uuid4().hex}{asset["extension"]}'; dest=(root/stored).resolve(); dest.relative_to(root); shutil.copyfile(src,dest); sha=hashlib.sha256(dest.read_bytes()).hexdigest()
                vid=db.execute('INSERT INTO design_versions(project_id,asset_id,version,name,stored_name,sha256,notes,created_at) VALUES(?,?,?,?,?,?,?,?)',(asset['project_id'],asset_id,version,asset['name'],stored,sha,str(body.get('notes',''))[:2000],db.now()))
                (config.VAULT/f'Design {asset["name"]} v{version}.md').write_text(f'# Design version {version}: {asset["name"]}\n\nOriginal asset: {asset_id}\n\nSHA-256: `{sha}`\n\nNotes: {str(body.get("notes",""))}\n',encoding='utf-8')
                return send_json(self,{'id':vid,'version':version,'sha256':sha})
            if path=='/api/troubleshooting':
                title=str(body.get('title','')).strip()[:200]; symptom=str(body.get('symptom','')).strip()[:2000]; steps=body.get('steps',[])
                steps=[str(x).strip()[:500] for x in steps if str(x).strip()] if isinstance(steps,list) else []
                if not title or not symptom or not steps:return send_json(self,{'error':'Add a title, symptom, and at least one diagnostic step'},400)
                tree_id=db.execute('INSERT INTO troubleshooting_trees(project_id,title,symptom,steps_json,created_at) VALUES(?,?,?,?,?)',(body.get('project_id') or None,title,symptom,json.dumps(steps,ensure_ascii=False),db.now()))
                return send_json(self,{'id':tree_id})
            if path.startswith('/api/troubleshooting/') and path.endswith('/start'):
                try:tree_id=int(path.split('/')[-2])
                except ValueError:return send_json(self,{'error':'Invalid tree id'},400)
                if not db.rows('SELECT id FROM troubleshooting_trees WHERE id=?',(tree_id,)):return send_json(self,{'error':'Troubleshooting tree not found'},404)
                sid=db.execute("INSERT INTO troubleshooting_sessions(tree_id,updated_at) VALUES(?,?)",(tree_id,db.now()));return send_json(self,{'id':sid})
            if path.startswith('/api/troubleshooting/sessions/'):
                try:sid=int(path.split('/')[-1])
                except ValueError:return send_json(self,{'error':'Invalid session id'},400)
                session=db.rows('SELECT * FROM troubleshooting_sessions WHERE id=?',(sid,))
                if not session:return send_json(self,{'error':'Session not found'},404)
                action=body.get('action'); status='active' if action=='next' else 'resolved' if action=='resolved' else 'unresolved' if action=='unresolved' else None
                if not status:return send_json(self,{'error':'Choose next, resolved, or unresolved'},400)
                tree=db.rows('SELECT steps_json FROM troubleshooting_trees WHERE id=?',(session[0]['tree_id'],))[0]; steps=json.loads(tree['steps_json']); index=session[0]['step_index']+(1 if action=='next' else 0); index=min(index,max(0,len(steps)-1))
                db.execute('UPDATE troubleshooting_sessions SET step_index=?,status=?,notes=?,updated_at=? WHERE id=?',(index,status,str(body.get('notes',session[0]['notes']))[:2000],db.now(),sid));return send_json(self,{'ok':True,'step_index':index,'status':status})
            if path=='/api/failures':
                title=str(body.get('title','')).strip()[:200]; symptom=str(body.get('symptom','')).strip()[:2000]
                if not title or not symptom:return send_json(self,{'error':'Add a title and symptom'},400)
                pid=body.get('project_id') or None; cause=str(body.get('cause','')).strip()[:2000]; fix=str(body.get('fix','')).strip()[:2000]; lesson=str(body.get('lesson','')).strip()[:2000];stamp=db.now()
                fid=db.execute('INSERT INTO failure_lessons(project_id,title,symptom,cause,fix,lesson,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(pid,title,symptom,cause,fix,lesson,stamp,stamp))
                project=db.rows('SELECT name FROM projects WHERE id=?',(pid,)) if pid else []; link=f"\n\nProject: [[Project {pid}]] — {project[0]['name']}" if project else ''
                (config.VAULT/f'Failure Lesson {fid}.md').write_text(f'# Failure lesson: {title}\n\n## Symptom\n{symptom}\n\n## Cause\n{cause or "Not determined"}\n\n## Fix\n{fix or "Not recorded"}\n\n## Lesson\n{lesson or "Not recorded"}{link}\n',encoding='utf-8')
                return send_json(self,{'id':fid})
            if path.startswith('/api/projects/') and path.endswith('/handoff'):
                try:pid=int(path.split('/')[-2])
                except ValueError:return send_json(self,{'error':'Invalid project id'},400)
                projects=db.rows('SELECT * FROM projects WHERE id=?',(pid,))
                if not projects:return send_json(self,{'error':'Project not found'},404)
                p=projects[0]; tasks=db.rows("SELECT title,status,priority,due_date FROM tasks WHERE project_id=? ORDER BY status,due_date",(pid,)); entries=db.rows('SELECT note,measurements,created_at FROM workshop_entries WHERE project_id=? ORDER BY id DESC LIMIT 12',(pid,)); parts=db.rows('SELECT name,part_number,quantity,unit,location FROM inventory_items WHERE project_id=? OR project_id IS NULL ORDER BY name',(pid,)); failures=db.rows('SELECT title,symptom,cause,fix,lesson FROM failure_lessons WHERE project_id=? ORDER BY id DESC LIMIT 12',(pid,)); versions=db.rows('SELECT name,version,sha256,notes,created_at FROM design_versions WHERE project_id=? ORDER BY created_at DESC LIMIT 20',(pid,))
                lines=[f'# Project handoff: {p["name"]}',f'\nGenerated: {db.now()}','\n## Goal\n'+(p['description'] or 'Not recorded'),'\n## Current status\n'+f"{p['status']} · priority {p['priority']} · due {p['due_date'] or 'not set'}",'\n## Next action\n'+(p['next_action'] or 'Not set'),'\n## Tasks']+[f'- [{"x" if t["status"]=="done" else " "}] {t["title"]} — {t["status"]}, {t["priority"]}, due {t["due_date"] or "not set"}' for t in tasks]
                lines+=['\n## Workshop log']+[f'- {e["created_at"]}: {e["note"]}'+(f'\n  Measurements: {e["measurements"]}' if e['measurements'] else '') for e in entries]
                lines+=['\n## Relevant inventory']+[f'- {i["name"]} ({i["part_number"] or "no part number"}): {i["quantity"]} {i["unit"]}, {i["location"] or "location unknown"}' for i in parts]
                lines+=['\n## Design versions']+[f'- {v["name"]} v{v["version"]} · {v["created_at"]} · SHA-256 `{v["sha256"]}` · {v["notes"]}' for v in versions]
                lines+=['\n## Failures and lessons']+[f'- {f["title"]}: {f["symptom"]}; cause: {f["cause"] or "unknown"}; fix: {f["fix"] or "not recorded"}; lesson: {f["lesson"] or "not recorded"}' for f in failures]
                content='\n'.join(lines)+'\n'; folder=(config.WORKSPACE/'engineering'/'handoffs').resolve();folder.mkdir(parents=True,exist_ok=True);folder.relative_to(config.WORKSPACE.resolve());dest=folder/f'project_{pid}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.md';dest.write_text(content,encoding='utf-8');docid=db.execute('INSERT INTO engineering_docs(project_id,kind,title,prompt,body,metadata_json,status,file_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(pid,'handoff',f'{p["name"]} handoff','Generated project handoff',content,'{}','ready',str(dest),db.now()));(config.VAULT/f'Project {pid} Handoff.md').write_text(content,encoding='utf-8');return send_json(self,{'id':docid,'title':f'{p["name"]} handoff','content':content,'url':f'/api/engineering/documents/{docid}/download'})
            if path=='/api/engineering/calculate':
                operation=str(body.get('operation','')); values=body.get('values',{})
                result=engineering.calculate(operation,values); project_id=body.get('project_id') or None
                if project_id and not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
                calculation_id=db.execute('INSERT INTO engineering_calculations(project_id,operation,inputs_json,result_json,file_path,created_at) VALUES(?,?,?,?,?,?)',(project_id,operation,json.dumps(values,ensure_ascii=False),json.dumps(result,ensure_ascii=False),'',db.now()))
                content=f"# Engineering calculation {calculation_id}\n\nOperation: {operation}\n\nInputs:\n```json\n{json.dumps(values,indent=2,ensure_ascii=False)}\n```\n\nFormula: {result.get('formula','')}\n\nResult: {json.dumps(result,ensure_ascii=False)}\n\nAssumptions:\n"+'\n'.join('- '+x for x in result.get('assumptions',[]))+'\n'
                folder=(config.WORKSPACE/'engineering'/'calculations').resolve(); folder.mkdir(parents=True,exist_ok=True); folder.relative_to(config.WORKSPACE.resolve())
                dest=folder/f'calculation_{calculation_id}.md'; dest.write_text(content,encoding='utf-8')
                db.execute('UPDATE engineering_calculations SET file_path=? WHERE id=?',(str(dest),calculation_id))
                (config.VAULT/f'Calculation {calculation_id}.md').write_text(content,encoding='utf-8')
                result.update({'id':calculation_id,'file':f'workspace/engineering/calculations/calculation_{calculation_id}.md'})
                return send_json(self,result)
            if path=='/api/engineering/bom':
                title=re.sub(r'[^\w -]','',str(body.get('title','')).strip())[:100] or 'Workshop BOM'
                items=body.get('items',[]); project_id=body.get('project_id') or None
                if not isinstance(items,list) or not items or len(items)>200: return send_json(self,{'error':'Add between 1 and 200 parts'},400)
                if project_id and not db.rows('SELECT id FROM projects WHERE id=?',(project_id,)): return send_json(self,{'error':'Project not found'},404)
                columns=('reference','part_number','description','quantity','unit','supplier','notes')
                def cell(value):
                    value=str(value).strip()[:500]
                    return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
                output=io.StringIO(newline=''); writer=csv.writer(output); writer.writerow(('Reference','Part number','Description','Quantity','Unit','Supplier','Notes'))
                clean=[]
                for item in items:
                    if not isinstance(item,dict): continue
                    try: quantity=float(item.get('quantity',1))
                    except (TypeError,ValueError): continue
                    if quantity<=0 or not __import__('math').isfinite(quantity): continue
                    part={key:cell(item.get(key,'')) for key in columns}; part['quantity']=quantity
                    writer.writerow([part[key] for key in columns]); clean.append(part)
                if not clean: return send_json(self,{'error':'No valid parts with positive quantities were supplied'},400)
                root=(config.DATA/'engineering'/'assets').resolve(); root.mkdir(parents=True,exist_ok=True); stored=f'sage-bom-{uuid.uuid4().hex}.csv'; dest=(root/stored).resolve(); dest.relative_to(root); dest.write_text(output.getvalue(),encoding='utf-8',newline='')
                meta=engineering.asset_metadata(dest,title+'.csv','.csv')
                asset_id=db.execute('INSERT INTO engineering_assets(project_id,name,stored_name,extension,size_bytes,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',(project_id,title+'.csv',stored,'.csv',dest.stat().st_size,meta['sha256'],json.dumps(meta,ensure_ascii=False),db.now()))
                (config.VAULT/f'{title} BOM.md').write_text(f"# {title} BOM\n\nGenerated parts list: [{title}.csv](../engineering/assets/{stored})\n\nParts: {len(clean)}\n",encoding='utf-8')
                return send_json(self,{'asset_id':asset_id,'name':title+'.csv','parts':len(clean),'url':f'/api/engineering/assets/{asset_id}/download'})
            if path=='/api/engineering/generate':
                kind=str(body.get('kind','')); title=str(body.get('title','')).strip()[:200]; prompt=str(body.get('prompt','')).strip()[:6000]
                if kind not in ('test_plan','design_review') or not title or not prompt: return send_json(self,{'error':'Choose a document type, title, and design or test description'},400)
                project_id=body.get('project_id') or None
                project=db.rows('SELECT id,name,description FROM projects WHERE id=?',(project_id,)) if project_id else []
                asset_context=[]
                for asset_id in body.get('asset_ids',[])[:12]:
                    rows=db.rows('SELECT id,name,extension,metadata_json FROM engineering_assets WHERE id=?',(asset_id,))
                    if rows:
                        row=rows[0]; asset_context.append({'id':row['id'],'name':row['name'],'type':row['extension'],'metadata':json.loads(row['metadata_json'])})
                context={'project_id':project_id,'project':project[0] if project else None,'assets':asset_context}
                job_id=jobs.create(title,prompt,kind,json.dumps(context,ensure_ascii=False))
                return send_json(self,{'job_id':job_id,'status':'queued'})
            if path.startswith('/api/engineering/documents/') and path.endswith('/results'):
                try: doc_id=int(path.split('/')[-2])
                except ValueError: return send_json(self,{'error':'Invalid document id'},400)
                doc=db.rows('SELECT * FROM engineering_docs WHERE id=?',(doc_id,))
                if not doc or doc[0]['kind']!='test_plan': return send_json(self,{'error':'Test plan not found'},404)
                outcome=str(body.get('outcome','')).lower()
                if outcome not in ('passed','failed','blocked','not_run'): return send_json(self,{'error':'Choose passed, failed, blocked, or not_run'},400)
                notes=str(body.get('notes','')).strip()[:4000]; measurements=str(body.get('measurements','')).strip()[:4000]
                db.execute('INSERT INTO engineering_doc_results(doc_id,outcome,notes,measurements,created_at) VALUES(?,?,?,?,?)',(doc_id,outcome,notes,measurements,db.now()))
                path=Path(doc[0]['file_path']).resolve(); path.relative_to(config.WORKSPACE.resolve())
                with path.open('a',encoding='utf-8') as stream: stream.write(f"\n\n## Test run — {db.now()}\n\nOutcome: {outcome}\n\nMeasurements:\n{measurements or 'Not recorded'}\n\nNotes:\n{notes or 'None'}\n")
                return send_json(self,{'ok':True})
            if path=='/api/engineering/code-projects/generate':
                title=str(body.get('title','')).strip()[:160]; prompt=str(body.get('prompt','')).strip()[:5000]
                if not title or not prompt: return send_json(self,{'error':'Enter a project name and describe what to build'},400)
                project_id=body.get('project_id') or None
                project=db.rows('SELECT id,name,description FROM projects WHERE id=?',(project_id,)) if project_id else []
                if project_id and not project: return send_json(self,{'error':'Project not found'},404)
                context={'project_id':project_id,'project':project[0] if project else None}
                jid=jobs.create('Code project: '+title,prompt,'code_project',json.dumps(context,ensure_ascii=False)); return send_json(self,{'job_id':jid,'status':'queued'})
            if path.startswith('/api/engineering/code-projects/') and '/files/' in path:
                pieces=path.split('/')
                try: project_id=int(pieces[4]); filename=unquote(pieces[6])
                except (ValueError,IndexError): return send_json(self,{'error':'Invalid project file'},400)
                if Path(filename).name!=filename: return send_json(self,{'error':'Invalid filename'},400)
                files=db.rows('SELECT relative_path FROM code_files WHERE project_id=? AND filename=?',(project_id,filename))
                if not files: return send_json(self,{'error':'Project file not found'},404)
                source=Path(files[0]['relative_path']).resolve(); root=(config.WORKSPACE/'engineering'/'code').resolve()
                try: source.relative_to(root)
                except ValueError: return send_json(self,{'error':'Invalid project path'},400)
                content=str(body.get('content',''))
                if len(content)>50000: return send_json(self,{'error':'File is larger than 50 KB'},413)
                source.write_text(content,encoding='utf-8'); return send_json(self,{'ok':True,'filename':filename})
            if path.startswith('/api/engineering/code-projects/') and path.endswith('/tests'):
                try: project_id=int(path.split('/')[-2])
                except ValueError: return send_json(self,{'error':'Invalid project id'},400)
                return send_json(self,jobs.run_code_tests(project_id,approved=body.get('approved') is True))
            if path=='/api/import':
                files=body.get('files',[])
                if not isinstance(files,list) or not files or len(files)>20: return send_json(self,{'error':'Choose between 1 and 20 files'},400)
                import_dir=config.DATA/'imports'; import_dir.mkdir(parents=True,exist_ok=True); imported=[]
                for item in files:
                    original=str(item.get('name',''))
                    suffix=Path(original).suffix.lower()
                    if suffix not in ('.txt','.md','.markdown','.csv','.json'): continue
                    content=str(item.get('content',''))
                    if len(content)>200_000: continue
                    stem=re.sub(r'[^\w -]','',Path(original).stem).strip()[:80] or 'Imported file'
                    name=stem; number=2
                    while (import_dir/(name+suffix)).exists(): name=f'{stem} {number}'; number+=1
                    (import_dir/(name+suffix)).write_text(content,encoding='utf-8')
                    note_title=f'Import {name}'
                    note=(config.VAULT/(note_title+'.md')).resolve(); note.relative_to(config.VAULT.resolve())
                    note.write_text(f"# {note_title}\n\nImported from `{original}`.\n\n{content[:200000]}\n",encoding='utf-8')
                    imported.append({'name':name+suffix,'note':note_title})
                if not imported: return send_json(self,{'error':'Supported formats are .txt, .md, .markdown, .csv, and .json'},400)
                return send_json(self,{'imported':imported})
            if path=='/api/watchlist':
                topic=str(body.get('topic','')).strip()[:300]
                if not topic: return send_json(self,{'error':'Enter a topic'},400)
                interval=max(1,min(30,int(body.get('interval_days',1))))
                db.execute('INSERT OR IGNORE INTO watchlist(topic,interval_days,last_checked,enabled,created_at) VALUES(?,?,\'\',1,?)',(topic,interval,db.now()))
                return send_json(self,{'ok':True})
            if path.startswith('/api/watchlist/delete/'):
                db.execute('DELETE FROM watchlist WHERE id=?',(int(path.rsplit('/',1)[1]),)); return send_json(self,{'ok':True})
            if path=='/api/focus':
                enabled=bool(body.get('enabled'))
                hours=max(1,min(8,int(body.get('hours',1))))
                end=(datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat(timespec='seconds') if enabled else ''
                project_id=body.get('project_id') or None
                db.execute('UPDATE focus SET enabled=?,project_id=?,goal=?,ends_at=?,updated_at=? WHERE id=1',(int(enabled),project_id,str(body.get('goal',''))[:300],end,db.now()))
                return send_json(self,db.rows('SELECT * FROM focus WHERE id=1')[0])
            if path.startswith('/api/plugin-requests/'):
                rid=int(path.rsplit('/',1)[1]); request=db.rows('SELECT * FROM plugin_requests WHERE id=?',(rid,))
                if not request: return send_json(self,{'error':'Plugin request not found'},404)
                if body.get('action')=='dismiss':
                    db.execute("UPDATE plugin_requests SET status='dismissed' WHERE id=?",(rid,)); return send_json(self,{'ok':True})
                if body.get('action')=='create-starter':
                    item=request[0]; slug=re.sub(r'[^a-z0-9]+','_',item['capability'].lower()).strip('_')[:48] or f'plugin_{rid}'
                    py=config.PLUGIN_DIR/(slug+'.py'); manifest=config.PLUGIN_DIR/(slug+'.json')
                    if py.exists() or manifest.exists(): return send_json(self,{'error':'A plugin with this name already exists'},409)
                    config.PLUGIN_DIR.mkdir(parents=True,exist_ok=True)
                    py.write_text('"""Starter plugin generated from a Sage capability request."""\n\ndef run(arguments, context):\n    # Implement the requested capability here. Keep inputs validated and scope access narrowly.\n    return {"status": "not_implemented", "message": "Complete this plugin before relying on it."}\n',encoding='utf-8')
                    metadata={'name':slug,'description':item['capability']+' — '+item['reason'],'version':'0.1.0','permission':'confirm','input_schema':{'type':'object','properties':{}},'entrypoint':slug+'.py:run'}
                    manifest.write_text(json.dumps(metadata,indent=2),encoding='utf-8')
                    db.execute("UPDATE plugin_requests SET status='starter_created' WHERE id=?",(rid,))
                    return send_json(self,{'ok':True,'name':slug})
                return send_json(self,{'error':'Unknown action'},400)
            return send_json(self,{"error":"Not found"},404)
        except PermissionError as exc: return send_json(self,{"error":str(exc)},403)
        except ValueError as exc: return send_json(self,{"error":str(exc)},400)
        except Exception as exc: return send_json(self,{"error":str(exc)},500)

def main():
    db.init(); computer_tasks.recover_on_startup(); jobs.start(); scheduler.start()
    try:
        from . import voice
        voice.start()
    except Exception as exc: print(f"Sage voice gateway not started: {exc}")
    try:
        if telegram.start(): print('Sage Telegram bridge started')
    except Exception as exc: print(f"Sage Telegram bridge not started: {exc}")
    print(f"Sage listening at http://{config.HOST}:{config.PORT} (model: {config.MODEL})")
    ThreadingHTTPServer((config.HOST,config.PORT),Handler).serve_forever()
