"""Model-callable built-in tools plus the user's installed plugin tools."""
import ast
import hashlib
import csv
import io
import json
import shutil
import uuid
import re
from math import log
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, engineering, jobs, memory, plugins, outlook, llm, extra_tools, trust, system_control, octoprint, file_search


BUILTINS = [
    ("get_outlook_calendar", "Read the connected Outlook calendar for a specified number of days (1–90). Use this for agenda, availability, upcoming meetings, or schedule planning. Calendar data is private; fetch only the needed range.", {"type":"object","properties":{"days":{"type":"integer","minimum":1,"maximum":90}}}),
    ("search_calendar_events", "Find upcoming connected Outlook calendar events by words in the title or location (up to 365 days). Use for requests to find a named event, trip, appointment, or event at a location.", {"type":"object","properties":{"query":{"type":"string"},"days":{"type":"integer","minimum":1,"maximum":365}},"required":["query"]}),
    ("create_outlook_event", "Create an event in the connected Outlook calendar after the user approves the confirmation. Provide ISO 8601 start and end times with timezone offsets. Sage checks for conflicts first.", {"type":"object","properties":{"title":{"type":"string"},"start":{"type":"string"},"end":{"type":"string"},"location":{"type":"string"},"notes":{"type":"string"}},"required":["title","start","end"]}, 'confirm'),
    ("search_emails", "Search the connected Outlook mailbox for a sender, subject, or phrase. Returns sender, date, subject, and a short message preview so Sage can summarize relevant results. Email contents are private; never save them as memory.", {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":25}},"required":["query"]}),
    ("inbox_brief", "Summarize a bounded list of the newest unread messages in the connected Outlook inbox. Returns only short previews; email contents are private and must not be saved as memory.", {"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":25}}}),
    ("latest_emails", "Read a bounded list of the newest inbox messages whether read or unread, for requests about the latest or most recent email. Returns short previews only; never save email contents as memory.", {"type":"object","properties":{"limit":{"type":"integer","minimum":1,"maximum":15}}}),
    ("draft_email_reply", "Create an Outlook reply draft to a message returned by search_emails or inbox_brief. Saves in Drafts and never sends. Use only when the user asks for a draft/reply; do not invent personal commitments or facts.", {"type":"object","properties":{"message_id":{"type":"string"},"reply_text":{"type":"string"}},"required":["message_id","reply_text"]}),
    ("search_user_files", "Search names and readable text in Sage's configured local document folders. Returns matching paths and short snippets only; it does not open or change files. Never search credential-like files.", {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":50}},"required":["query"]}),
    ("list_projects", "List Sage projects with their tasks and current status.", {"type":"object","properties":{}}),
    ("create_project", "Create a project with a goal, next action, and optional first tasks.", {"type":"object","properties":{"name":{"type":"string"},"description":{"type":"string"},"next_action":{"type":"string"},"tasks":{"type":"array","items":{"type":"string"}}},"required":["name"]}),
    ("add_project_task", "Add a task to an existing Sage project.", {"type":"object","properties":{"project_id":{"type":"integer"},"title":{"type":"string"},"description":{"type":"string"},"priority":{"type":"string","enum":["low","medium","high"]}},"required":["project_id","title"]}),
    ("complete_project_task", "Update an existing task to todo, doing, blocked, or done. If the user explicitly resolves a blocked task's blocker or recorded project evidence confirms it is now actionable, set its status to todo. Never unblock based on assumptions.", {"type":"object","properties":{"task_id":{"type":"integer"},"status":{"type":"string","enum":["todo","doing","blocked","done"]}},"required":["task_id","status"]}),
    ("search_memory", "Search saved Sage memories by semantic similarity, including related ideas that use different words. Uses the local Ollama embedding model.", {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}),
    ("save_memory", "Save a durable user preference, fact, project detail, or learned knowledge to Sage memory and the Obsidian vault.", {"type":"object","properties":{"content":{"type":"string"},"kind":{"type":"string","enum":["preference","fact","project","knowledge"]}},"required":["content"]}),
    ("search_workshop_notebook", "Search workshop logs, measurements, reports, failures, calculations, projects, and saved memories.", {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}),
    ("add_workshop_entry", "Record a workshop observation or build step, optionally with measurements and project id.", {"type":"object","properties":{"note":{"type":"string"},"measurements":{"type":"string"},"project_id":{"type":"integer"}},"required":["note"]}),
    ("get_printer_status", "Read OctoPrint printer connection, operational/printing/paused state, and available temperatures.", {"type":"object","properties":{}}),
    ("list_printer_library", "List existing G-code files in the configured OctoPrint local or SD-card library. This does not upload or slice models.", {"type":"object","properties":{"storage":{"type":"string","enum":["local","sdcard"]},"limit":{"type":"integer","minimum":1,"maximum":200}}}),
    ("print_library_file", "Start an existing G-code file from the OctoPrint library. Use only after the user directly commands Sage to print that named file; no upload, slicing, or model selection.", {"type":"object","properties":{"path":{"type":"string"},"storage":{"type":"string","enum":["local","sdcard"]}},"required":["path"]}),
    ("calculate_engineering", "Calculate a supported engineering formula. Supported operations: ohms_law, power, moment, pressure_force, density, convert. Include all required values and units.", {"type":"object","properties":{"operation":{"type":"string","enum":["ohms_law","power","moment","pressure_force","density","convert"]},"values":{"type":"object"}},"required":["operation","values"]}),
    ("list_inventory", "List workshop parts inventory, quantities, locations, and reorder levels.", {"type":"object","properties":{"query":{"type":"string"}}}),
    ("add_inventory_item", "Add a part to Sage's inventory with quantity, unit, location, supplier, threshold, and optional project.", {"type":"object","properties":{"name":{"type":"string"},"part_number":{"type":"string"},"description":{"type":"string"},"quantity":{"type":"number"},"unit":{"type":"string"},"location":{"type":"string"},"supplier":{"type":"string"},"reorder_level":{"type":"number"},"project_id":{"type":"integer"}},"required":["name"]}),
    ("adjust_inventory_stock", "Record stock received or used; the adjustment is rejected if it would make stock negative.", {"type":"object","properties":{"item_id":{"type":"integer"},"delta":{"type":"number"},"reason":{"type":"string"}},"required":["item_id","delta","reason"]}),
    ("list_failures_and_lessons", "Search recorded engineering failures, root causes, fixes, and lessons.", {"type":"object","properties":{"query":{"type":"string"}}}),
    ("record_failure_lesson", "Record a failure symptom, known or unknown cause, fix, and lesson in Sage and Obsidian.", {"type":"object","properties":{"title":{"type":"string"},"symptom":{"type":"string"},"cause":{"type":"string"},"fix":{"type":"string"},"lesson":{"type":"string"},"project_id":{"type":"integer"}},"required":["title","symptom"]}),
    ("list_troubleshooting_guides", "Find saved troubleshooting guides and their diagnostic steps.", {"type":"object","properties":{"query":{"type":"string"}}}),
    ("create_troubleshooting_guide", "Save a repeatable diagnostic guide from a symptom and ordered steps.", {"type":"object","properties":{"title":{"type":"string"},"symptom":{"type":"string"},"steps":{"type":"array","items":{"type":"string"}},"project_id":{"type":"integer"}},"required":["title","symptom","steps"]}),
    ("queue_research", "Queue a sourced web research report to run in Sage's background worker when idle.", {"type":"object","properties":{"title":{"type":"string"},"question":{"type":"string"}},"required":["title","question"]}),
    ("queue_engineering_review", "Queue a test plan or design review draft for background generation when Sage is idle.", {"type":"object","properties":{"kind":{"type":"string","enum":["test_plan","design_review"]},"title":{"type":"string"},"prompt":{"type":"string"},"project_id":{"type":"integer"}},"required":["kind","title","prompt"]}),
    ("create_project_handoff", "Generate a Markdown handoff sheet with the project's tasks, workshop notes, stock, design versions, and lessons.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("create_knowledge_note", "Create a Markdown note in the Obsidian-compatible knowledge vault, with optional links to other notes.", {"type":"object","properties":{"title":{"type":"string"},"content":{"type":"string"},"links":{"type":"array","items":{"type":"string"}}},"required":["title","content"]}),
    ("create_plugin", "Generate a Sage plugin for a capability Sage does not yet have. This changes Sage's own source files and therefore requires user approval before writing it. Generated plugin execution also requires approval.", {"type":"object","properties":{"name":{"type":"string","description":"Short machine-readable name"},"purpose":{"type":"string","description":"What it should do, including inputs and boundaries"}},"required":["name","purpose"]}, "confirm"),
    ("list_engineering_files", "List imported workshop CAD, electronics, BOM, and image files with their metadata.", {"type":"object","properties":{"project_id":{"type":"integer"}}}),
    ("create_bom_csv", "Create a BOM spreadsheet from structured parts and save it to the engineering library.", {"type":"object","properties":{"title":{"type":"string"},"parts":{"type":"array","items":{"type":"object"}},"project_id":{"type":"integer"}},"required":["title","parts"]}),
    ("create_engineering_code_project", "Queue a code or OpenSCAD project for Sage to generate in its isolated workspace when idle. Generated code is not run automatically.", {"type":"object","properties":{"title":{"type":"string"},"description":{"type":"string"},"project_id":{"type":"integer"}},"required":["title","description"]}),
    ("add_research_watch", "Add a topic to Sage's idle research watchlist.", {"type":"object","properties":{"topic":{"type":"string"},"interval_days":{"type":"integer","enum":[1,3,7,30]}},"required":["topic"]}),
    ("check_project_parts", "Compare a project's imported BOM files against linked and general workshop stock, returning shortages.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("create_measurement_template", "Create a repeatable measurement template for a project.", {"type":"object","properties":{"title":{"type":"string"},"fields":{"type":"array","items":{"type":"object"}},"project_id":{"type":"integer"}},"required":["title","fields"]}),
    ("record_engineering_decision", "Record a chosen engineering option, alternatives, rationale, and optional project link.", {"type":"object","properties":{"title":{"type":"string"},"decision":{"type":"string"},"alternatives":{"type":"string"},"rationale":{"type":"string"},"project_id":{"type":"integer"}},"required":["title","decision"]}),
    ("draft_session_summary", "Queue an evidence-based work-session summary for user review. It is not saved to Obsidian until approved.", {"type":"object","properties":{"project_id":{"type":"integer"},"title":{"type":"string"}}}),
    ("get_project_brief", "Build a concise evidence-based brief for a project: next action, open and overdue tasks, blockers, recent verification, decisions, and BOM shortages.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("search_related_knowledge", "Find the most relevant related memories, workshop records, failures, decisions, engineering reports, and notes using local keyword relevance ranking.", {"type":"object","properties":{"query":{"type":"string"},"project_id":{"type":"integer"}},"required":["query"]}),
    ("check_task_dependencies", "Check which project tasks are ready, waiting on dependencies, or have invalid dependency references.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("review_project_readiness", "Review recorded project data for overdue or blocked tasks, failed/blocked tests, missing next action, and BOM stock shortages. Reports evidence and gaps; it does not certify safety or compliance.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("inspect_engineering_asset", "Read metadata and a bounded text preview of an imported engineering asset. Select by asset id, or by project/name when unambiguous. Binary files return metadata only; no design validity is inferred.", {"type":"object","properties":{"asset_id":{"type":"integer"},"project_id":{"type":"integer"},"name":{"type":"string"}}}),
    ("audit_bom_file", "Check an imported CSV/BOM for blank rows, duplicate part numbers, missing identifiers, and invalid quantities. Select by asset id or a project with one unambiguous BOM. It does not determine whether the design itself is correct.", {"type":"object","properties":{"asset_id":{"type":"integer"},"project_id":{"type":"integer"}}}),
    ("analyze_measurement_run", "Compare a saved measurement run to its template targets and tolerances. Use run id or the latest run for a project. Numeric values are evaluated only when units/targets are understandable; ambiguous fields are reported as not comparable.", {"type":"object","properties":{"run_id":{"type":"integer"},"project_id":{"type":"integer"}}}),
    ("compare_measurement_runs", "Compare the two latest runs of a measurement template and report numeric changes without claiming a cause. Select by template id or project id when unambiguous.", {"type":"object","properties":{"template_id":{"type":"integer"},"project_id":{"type":"integer"}}}),
    ("get_project_timeline", "Retrieve a chronological project history of recorded tasks, workshop entries, test outcomes, design versions, decisions, and failure lessons.", {"type":"object","properties":{"project_id":{"type":"integer"},"days":{"type":"integer","minimum":1,"maximum":3650}},"required":["project_id"]}),
    ("analyze_task_graph", "Analyze project task dependencies for ready tasks, waiting chains, invalid references, and dependency cycles.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}),
    ("compare_bom_revisions", "Compare two imported BOM/CSV assets by part number, quantities, and units; select by asset ids or filenames and report added, removed, and changed line items.", {"type":"object","properties":{"older_asset_id":{"type":"integer"},"newer_asset_id":{"type":"integer"},"older_name":{"type":"string"},"newer_name":{"type":"string"},"project_id":{"type":"integer"}}}),
    ("explain_memory_provenance", "Show where a saved memory came from, its confidence, and when it was saved or updated. Select by memory id or a remembered phrase.", {"type":"object","properties":{"memory_id":{"type":"integer"},"query":{"type":"string"}}}),
    ("save_design_version", "Create an immutable local snapshot of an imported CAD, electronics, or BOM file. Requires user approval.", {"type":"object","properties":{"asset_id":{"type":"integer"},"notes":{"type":"string"}},"required":["asset_id","notes"]}, "confirm"),
    ("run_engineering_tests", "Run the unit tests for an existing generated code project. Executes code with Sage's OS account permissions and requires user approval.", {"type":"object","properties":{"project_id":{"type":"integer"}},"required":["project_id"]}, "confirm"),
    ("run_linux_command", "Run one Linux shell command on the Kali Sage computer as Sage's normal user. Routine commands run directly. Software installation commands and commands that may modify Sage's own files require approval first. Sage's source directory is mounted read-only for ordinary commands. Do not put passwords or tokens in commands.", {"type":"object","properties":{"command":{"type":"string"},"cwd":{"type":"string"},"timeout_seconds":{"type":"integer","minimum":1,"maximum":900}},"required":["command"]}),
    ("install_system_packages", "Install named packages from Kali's configured APT repositories. Sage shows the package names for approval first, then the local Polkit administrator prompt must also be completed. Sage never asks for or stores your administrator password. Use only when the user explicitly requested installation.", {"type":"object","properties":{"packages":{"type":"array","items":{"type":"string"}}},"required":["packages"]}, "confirm"),
    ("linux_desktop_action", "Perform exactly one X11 desktop action on the Kali Sage computer: click pixel coordinates, type text, press a key/chord, or scroll. Inspect the screen immediately before choosing coordinates. Runs without a Sage approval prompt. Only works when Sage has access to the active X11 desktop; not supported on Wayland.", {"type":"object","properties":{"action":{"type":"string","enum":["click","type_text","press_key","scroll"]},"x":{"type":"integer"},"y":{"type":"integer"},"text":{"type":"string"},"key":{"type":"string"},"direction":{"type":"string","enum":["up","down"]},"clicks":{"type":"integer","minimum":1,"maximum":10}},"required":["action"]}),
    ("inspect_linux_screen", "Capture and describe one temporary screenshot of Sage's own Kali desktop using local Ollama vision. Use this before desktop actions and again afterwards to verify what changed. The image is discarded and not saved.", {"type":"object","properties":{"question":{"type":"string"}}}),
    ("open_browser_url", "Open a user-requested http/https web page in the Kali desktop's default browser. Never submit forms, send messages, or make purchases without the user explicitly asking for that exact action.", {"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}),
    ("linux_file_action", "List, read, open, create, copy, move, or delete files within the current user's home folder. File content is bounded; recursive folder deletion and opening executable/installer files are disabled. Changes to Sage's own source files require approval.", {"type":"object","properties":{"action":{"type":"string","enum":["list","read","open","write","mkdir","copy","move","delete"]},"path":{"type":"string"},"destination":{"type":"string"},"content":{"type":"string"}},"required":["action","path"]}),
]

BUILTINS += extra_tools.BUILTINS


def definitions():
    result=[]; names=set()
    for name,description,schema,*rest in BUILTINS:
        names.add(name)
        result.append({"type":"function","function":{"name":name,"description":description,"parameters":schema}})
    for meta in plugins.discover():
        name=meta.get('name','')
        if name in names or meta.get('permission','blocked')=='blocked': continue
        result.append({"type":"function","function":{"name":name,"description":meta.get('description',''),'parameters':meta.get('input_schema') or {"type":"object","properties":{}}}})
    return result

_GENERIC_WORDS={'list','create','add','get','check','save','run','queue','record','analyze','compare','explain','search','review','draft','inspect','audit','find','make','write','read','show','file','files','tool','plugin','user','sage','generation','generate','item','items','data','test','tests','calculate','complete','adjust','build','save','status'}

def _validate_schema_value(value,schema,path):
    expected=schema.get('type')
    types={
        'object':lambda item:isinstance(item,dict),
        'array':lambda item:isinstance(item,list),
        'string':lambda item:isinstance(item,str),
        'integer':lambda item:isinstance(item,int) and not isinstance(item,bool),
        'number':lambda item:isinstance(item,(int,float)) and not isinstance(item,bool),
        'boolean':lambda item:isinstance(item,bool),
    }
    if expected in types and not types[expected](value):
        raise ValueError(f"{path} must be {expected}")
    if 'enum' in schema and value not in schema['enum']:
        raise ValueError(f"{path} must be one of {', '.join(map(str,schema['enum']))}")
    if expected=='object':
        properties=schema.get('properties',{})
        for key in schema.get('required',[]):
            if key not in value:raise ValueError(f"{path}.{key} is required")
        for key,item in value.items():
            if key in properties:_validate_schema_value(item,properties[key],f'{path}.{key}')
            elif schema.get('additionalProperties') is False:raise ValueError(f"{path}.{key} is not an allowed argument")
    elif expected=='array':
        item_schema=schema.get('items')
        if item_schema:
            for index,item in enumerate(value):_validate_schema_value(item,item_schema,f'{path}[{index}]')
    elif expected in ('integer','number'):
        if 'minimum' in schema and value<schema['minimum']:raise ValueError(f"{path} must be at least {schema['minimum']}")
        if 'maximum' in schema and value>schema['maximum']:raise ValueError(f"{path} must be at most {schema['maximum']}")

def validate_arguments(name,args):
    """Reject malformed model-generated arguments before any built-in or plugin can run."""
    if not isinstance(args,dict):raise ValueError('Tool arguments must be a JSON object')
    definition=next((item for item in definitions() if item['function']['name']==name),None)
    if definition is None:raise ValueError(f"Tool {name!r} is not available")
    schema=definition['function'].get('parameters',{})
    _validate_schema_value(args,schema,f'arguments for {name}')

def _name_terms(name):
    """Distinctive words of a tool/plugin name, e.g. password_generation -> {'password'}."""
    words=re.findall(r'[a-z]+',str(name).replace('_',' ').lower())
    return {w.rstrip('s') for w in words if w not in _GENERIC_WORDS and len(w.rstrip('s'))>=4}

def _mentions(terms,text):
    words=re.findall(r'[a-z]+',str(text).lower())
    return any(word.startswith(term) for term in terms for word in words)

def explicit_tool_names(query):
    """Installed plugins the user clearly asked for, by name word or manifest keyword.
    This works for every plugin automatically, including ones added later."""
    text=str(query).lower(); found=[]
    for meta in plugins.discover():
        name=meta.get('name','')
        if not name or meta.get('permission','blocked')=='blocked': continue
        keywords=[str(k).lower() for k in meta.get('keywords',[]) if str(k).strip()]
        if _mentions(_name_terms(name),text) or any(k in text for k in keywords): found.append(name)
    return found

def definitions_for_query(query, definitions_list=None, strict=False):
    """Keep the fallback prompt compact while still surfacing relevant tools."""
    catalog=definitions_list if definitions_list is not None else definitions()
    text=str(query).lower()
    triggers={
        'get_outlook_calendar':('outlook','calendar','agenda','availability','free time','meeting','appointment','my day','plan my day','what do i have','schedule'),
        'search_calendar_events':('find an event','search calendar','find on my calendar','when is the','when are the','which day is','calendar event called','appointment called'),
        'create_outlook_event':('put that in my calendar','add to my calendar','schedule a meeting','book a time','create a calendar event'),
        'search_emails':('search email','search emails','find email','find emails','look through my email','email about','mail from','message from','search my inbox'),
        'inbox_brief':('inbox brief','summarize my inbox','summarize my emails','unread emails','unread mail','new emails','check my inbox','important emails'),
        'latest_emails':('latest email','latest emails','most recent email','last email','newest email'),
        'draft_email_reply':('draft a reply','write a reply','reply to this email','reply to that email','prepare a reply','respond to this email'),
        'search_user_files':('find a file','find files','search files','search my files','search documents','find a document','look for a document','find in my documents'),
        'calculator':('calculate','compute','math','plus','minus','times','voltage','current','resistance','force','pressure','density','convert'),
        'web_search':('search online','search the web','look up','browse','latest','current information','find online','research'),
        'workspace_files':('workspace file','read file','list files','show file'),
        'workspace_write':('write file','save code','create file','edit file'),
        'launch_app':('open app','launch app','start app','open browser'),
        'list_projects':('project','projects','task','tasks','plan'),
        'create_project':('create a project','new project','plan a project','start a project'),
        'add_project_task':('add task','new task','task to','next task'),
        'complete_project_task':('complete task','mark task','task done','finish task','unblock task','unblock the task','clear task blocker','resolve task blocker','mark task todo'),
        'search_memory':('remember','memory','what do you know','recall'),
        'save_memory':('remember this','save this','learn this','store in memory'),
        'search_workshop_notebook':('search notes','notebook','workshop history','when did','find in my notes'),
        'add_workshop_entry':('workshop log','log this','record measurement','build note','workshop note'),
        'get_printer_status':('printer status','octoprint status','printer ready','printer connection','check the printer'),
        'list_printer_library':('printer library','printer files','octoprint files','what can i print','list gcode','g-code library'),
        'print_library_file':('print ','print from library','print the file','start printing','send to the printer'),
        'calculate_engineering':('calculate','engineering formula','voltage','current','force','pressure','density'),
        'list_inventory':('inventory','stock','parts','part number','how many'),
        'add_inventory_item':('add part','add to inventory','stock received','new part'),
        'adjust_inventory_stock':('used up','stock adjustment','received parts','remove from stock','consume parts'),
        'list_failures_and_lessons':('failure','failed before','past issue','root cause','lesson learned'),
        'record_failure_lesson':('record a failure','log a failure','save this failure','lesson learned'),
        'list_troubleshooting_guides':('troubleshoot','diagnostic guide','fault tree','symptom'),
        'create_troubleshooting_guide':('create troubleshooting','make a diagnostic','save troubleshooting'),
        'queue_research':('research report','research this','investigate','find sources'),
        'queue_engineering_review':('test plan','design review','verification plan','review this design'),
        'create_project_handoff':('handoff','hand-off','project summary','resume project'),
        'create_knowledge_note':('obsidian note','knowledge note','save as a note','create a note'),
        'create_plugin':('create a plugin','make a plugin','build a plugin','new plugin','plugin that'),
        'list_engineering_files':('cad','design file','bom','bill of materials','imported file','engineering asset','parts list'),
        'create_bom_csv':('create bom','build bom','generate bill of materials','make a parts list'),
        'create_engineering_code_project':('create code','make a program','write a tool','openscad','code project'),
        'add_research_watch':('watch this topic','research watchlist','keep an eye on'),
        'get_project_brief':('project brief','project status','summarize this project','what is the next step','where did we leave off'),
        'search_related_knowledge':('similar','related notes','previously','have we seen','did this happen','what have we learned','prior failure','find related'),
        'check_task_dependencies':('dependencies','blocked by','ready tasks','task blockers','what is blocking'),
        'review_project_readiness':('project readiness','readiness review','what is blocked','project blockers','are we ready'),
        'inspect_engineering_asset':('inspect this file','read design file','show imported file','what is in this cad','inspect asset','file contents','read this cad','show file'),
        'audit_bom_file':('audit bom','check bom','validate parts list','duplicate part numbers','bom errors','check the bill of materials','review bom'),
        'analyze_measurement_run':('measurement run','check these measurements','within tolerance','compare with target','analyze readings'),
        'compare_measurement_runs':('measurement trend','compare measurement runs','previous measurements','measurement changed'),
        'get_project_timeline':('project timeline','project history','what happened on this project','sequence of work'),
        'analyze_task_graph':('dependency cycle','circular task','task graph','dependency chain','what can start'),
        'compare_bom_revisions':('compare bom revisions','bom changes','parts list revisions','what changed in the bom','compare two boms'),
        'explain_memory_provenance':('where did you learn','where did this memory come from','memory provenance','source of that memory','why do you remember'),
        'save_design_version':('save a design version','snapshot this design','version this file'),
        'run_engineering_tests':('run tests','run the test suite','execute tests'),
        'run_linux_command':('run linux command','run a shell command','execute command','use the terminal','run this command','shell command','run in terminal','terminal command'),
        'install_system_packages':('install ','apt install','apt-get install','package manager','system package'),
        'linux_desktop_action':('click the screen','click at','type into the desktop','press the key','scroll the page','use the desktop'),
        'inspect_linux_screen':('look at my screen','inspect the screen','what is on my screen','check my kali screen','see the desktop','look at the desktop'),
        'open_browser_url':('open this website','open this url','go to this website','open a web page','visit this page','open website','open url','navigate to','browse to','visit '),
        'linux_file_action':('file on my computer','files on my computer','desktop file','documents folder','home folder','move a file','copy a file','delete a file','create a file on my pc','read a file on my pc','list files in','open file from','move file from'),
    }
    triggers.update(extra_tools.TRIGGERS)
    always={'web_search','search_memory','search_workshop_notebook','list_projects','list_inventory'}
    explicit=set(explicit_tool_names(query))
    selected=[]
    # Resolve common goal phrases before the model sees tool definitions. This reduces
    # ambiguity between reading/searching, summarizing, drafting, and changing data.
    email_domain = bool(re.search(r'\b(?:email|emails|mail|inbox|message|messages)\b', text))
    if email_domain:
        local_email_content = re.search(r'\b(?:this|that|attached|below)\s+(?:email|message)\b', text)
        if re.search(r'\b(?:inbox brief|unread|brief me|check my inbox|important emails|summari[sz]e my inbox|summari[sz]e my emails)\b', text):
            explicit.add('inbox_brief')
        if re.search(r'\b(?:latest|most recent|last|newest)\s+(?:email|message)s?\b', text):
            explicit.add('latest_emails')
        if re.search(r'\b(?:draft|write|prepare|reply|respond)\b', text) and re.search(r'\b(?:reply|respond|email|mail|message)\b', text):
            explicit.add('draft_email_reply')
        if re.search(r'\b(?:search|find|look for|from|subject|about|which|what did|show me)\b', text):
            explicit.add('search_emails')
        if local_email_content and 'draft_email_reply' not in explicit and not re.search(r'\b(?:search|find|look for)\b', text):
            explicit.difference_update({'inbox_brief','latest_emails','search_emails'})
        if 'search_emails' in explicit and re.search(r'\b(?:from|subject|about|containing|with the phrase)\b', text) and not re.search(r'\b(?:unread|inbox brief|newest unread)\b', text):
            explicit.difference_update({'inbox_brief','latest_emails'})
        if not local_email_content and not any(name in explicit for name in ('inbox_brief','latest_emails','draft_email_reply','search_emails')):
            explicit.update(('search_emails','inbox_brief'))
    if re.search(r'\b(?:find|search|look for)\b.{0,45}\b(?:file|files|document|documents|pdf|spreadsheet|note)\b|\b(?:file|document)\s+search\b', text):
        explicit.add('search_user_files')
    if re.search(r'\b(?:find|search|when is|when are|which day|where is)\b.{0,50}\b(?:calendar|event|appointment|meeting|trip|holiday)\b', text):
        explicit.add('search_calendar_events')
    for item in catalog:
        name=item.get('function',{}).get('name','')
        if (name in always and not strict) or name in explicit or _mentions(_name_terms(name),text) or any(term in text for term in triggers.get(name,())):selected.append(item)
    if re.search(r'\b(?:use|control|operate|look at|check|open|click|type|manage)\b.{0,35}\b(?:my )?(?:pc|computer|desktop|kali|screen|browser|files)\b|\b(?:on my pc|on my computer|use my pc|use the computer|control my computer|click at|type into the desktop|use the desktop)\b',text):
        pc_tools={'inspect_linux_screen','linux_desktop_action','run_linux_command','install_system_packages','open_browser_url','linux_file_action','launch_app','workspace_files','workspace_write'}
        have={item['function']['name'] for item in selected}
        selected.extend(item for item in catalog if item['function']['name'] in pc_tools and item['function']['name'] not in have)
    if strict:
        return selected
    return selected if strict else (selected or catalog[:8])


def base_permission(name):
    """The tool's own permission level, ignoring who is asking."""
    for builtin in BUILTINS:
        if builtin[0]==name: return builtin[3] if len(builtin)>3 else 'safe'
    meta=next((p for p in plugins.discover() if p.get('name')==name),None)
    return meta.get('permission','blocked') if meta else 'blocked'


_INSTALL_COMMAND = re.compile(r'(?i)(?:^|[;&|\s])(?:sudo\s+)?(?:apt(?:-get)?\s+(?:install|reinstall|build-dep|download)|dpkg\s+-i|pip(?:3)?\s+install|python(?:3)?\s+-m\s+pip\s+install|npm\s+(?:install|i)|yarn\s+add|pnpm\s+add|gem\s+install|cargo\s+install|go\s+install|flatpak\s+install|snap\s+install|ollama\s+pull|conda\s+install|mamba\s+install|brew\s+install|curl\b|wget\b)\b')
def permission(name, arguments=None):
    """Permission for this request. Routine system actions are direct; installs and Sage-source edits need approval."""
    level=base_permission(name)
    if name == 'run_linux_command':
        command = str((arguments or {}).get('command', ''))
        cwd = Path(str((arguments or {}).get('cwd', '') or Path.home())).expanduser()
        if system_control.requires_install_approval(command) or _INSTALL_COMMAND.search(command) or system_control.requires_source_approval(command,cwd):
            return 'confirm'
    if name == 'linux_file_action':
        args = arguments or {}
        action = str(args.get('action', 'list'))
        candidate = (Path.home() / str(args.get('path', ''))).expanduser().resolve()
        destination = (Path.home() / str(args.get('destination', ''))).expanduser().resolve()
        try: touches_sage = any(system_control.is_sage_source_path(p) for p in (candidate, destination))
        except (OSError, ValueError): touches_sage = False
        if action in {'write','mkdir','copy','move','delete'} and touches_sage:
            return 'confirm'
    if level=='safe' and name not in {'run_linux_command','linux_desktop_action'} and trust.voice_blocks(name): return 'confirm'
    return level


def _audit(name,args,result):
    db.log_action(name,args,'completed',result)
    return result


def _create_plugin(args):
    requested=str(args.get('name','')).strip().lower()
    slug=re.sub(r'[^a-z0-9]+','_',requested).strip('_')[:48]
    purpose=str(args.get('purpose','')).strip()
    if not slug:raise ValueError('Plugin name must contain letters or numbers')
    if not purpose:raise ValueError('Plugin purpose is required')
    if slug in {item[0] for item in BUILTINS}:raise ValueError('Plugin name conflicts with a built-in tool')
    plugin_dir=config.PLUGIN_DIR.resolve()
    source_path=plugin_dir/(slug+'.py')
    manifest_path=plugin_dir/(slug+'.json')
    if source_path.exists() or manifest_path.exists():raise ValueError(f'Plugin {slug!r} already exists')
    prompt=(
        'Create one complete Sage plugin implementation. Return only one JSON object with keys '
        '"description" (string), "input_schema" (JSON Schema object schema), and "code" (Python source string). '
        'The Python source must define run(arguments, context), validate inputs, avoid import-time side effects, '
        'use only the Python standard library, and return JSON-serializable data. The plugin runs with the '
        'user account permissions when explicitly approved. Never include secrets, network access, subprocess '
        'execution, dynamic code execution (eval/exec), or operations outside the provided workspace. '
        'The context contains only a workspace path. Name: '+slug+'\nPurpose: '+purpose[:4000]
    )
    generated=llm.chat([
        {'role':'system','content':'You generate small, safe, complete Python plugins for Sage. Follow the schema exactly; output valid JSON and no prose.'},
        {'role':'user','content':prompt},
    ],max_tokens=1800,model=config.CODING_MODEL)
    try:spec=json.loads(generated)
    except (TypeError,ValueError) as exc:raise ValueError(f'Plugin generator returned invalid JSON: {exc}') from exc
    if not isinstance(spec,dict):raise ValueError('Plugin generator response must be a JSON object')
    description=str(spec.get('description','')).strip()
    schema=spec.get('input_schema')
    code=spec.get('code')
    if not description or not isinstance(schema,dict) or schema.get('type')!='object' or not isinstance(schema.get('properties'),dict):
        raise ValueError('Plugin generator must return a description and an object input schema')
    required=schema.get('required',[])
    if not isinstance(required,list) or any(not isinstance(key,str) or key not in schema['properties'] for key in required):
        raise ValueError('Plugin input schema has invalid required fields')
    if not isinstance(code,str) or not code.strip():raise ValueError('Plugin generator returned no Python implementation')
    try:tree=ast.parse(code)
    except SyntaxError as exc:raise ValueError(f'Generated plugin has invalid Python syntax: {exc}') from exc
    run_function=next((node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='run'),None)
    if run_function is None or len(run_function.args.args)<2 or [arg.arg for arg in run_function.args.args[:2]]!=['arguments','context']:
        raise ValueError('Generated plugin must define run(arguments, context)')
    plugin_dir.mkdir(parents=True,exist_ok=True)
    if source_path.exists() or manifest_path.exists():raise ValueError(f'Plugin {slug!r} already exists')
    metadata={
        'name':slug,
        'description':description[:2000],
        'version':'0.1.0',
        'permission':'confirm',
        'input_schema':schema,
        'entrypoint':slug+'.py:run',
    }
    source_temp=plugin_dir/('.'+slug+'.'+uuid.uuid4().hex+'.py.tmp')
    manifest_temp=plugin_dir/('.'+slug+'.'+uuid.uuid4().hex+'.json.tmp')
    try:
        source_temp.write_text(code.rstrip()+'\n',encoding='utf-8')
        manifest_temp.write_text(json.dumps(metadata,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        source_temp.replace(source_path)
        manifest_temp.replace(manifest_path)
    except Exception:
        source_temp.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)
        if not manifest_path.exists():source_path.unlink(missing_ok=True)
        raise
    return _audit('create_plugin',args,{
        'name':slug,
        'description':description,
        'status':'integrated',
        'permission':'confirm',
        'source':str(source_path),
        'note':'The plugin is now available to Sage. Its execution requires explicit user approval.',
    })


def execute(name,args,approved=False,cancel=None):
    if not isinstance(args,dict): raise ValueError('Tool arguments must be a JSON object')
    if permission(name,args)=='confirm' and not approved:
        return {'status':'pending_approval','tool':name,'arguments':args}
    if name in extra_tools.NAMES:
        return _audit(name,args,extra_tools.run(name,args))
    if name not in {item[0] for item in BUILTINS}:
        return plugins.call(name,args,approved=approved)['result']
    if name=='create_plugin':return _create_plugin(args)
    if name=='run_linux_command':
        result=system_control.run_command(args, approved=approved, cancel=cancel)
        db.log_action(name,{'command':'[redacted]','cwd':result.get('cwd',''),'timeout_seconds':args.get('timeout_seconds',60)},'completed',f"exit={result['returncode']}; timed_out={result['timed_out']}; output truncated={result['truncated']}")
        return result
    if name=='install_system_packages':
        result=system_control.install_system_packages(args)
        db.log_action(name,{'packages':result['packages']},'completed',f"exit={result['returncode']}; timed_out={result['timed_out']}")
        return result
    if name=='linux_desktop_action':
        result=system_control.desktop_action(args)
        safe_args={k:('[redacted after approval]' if k=='text' else v) for k,v in args.items()}
        db.log_action(name,safe_args,'completed','One desktop action completed')
        return result
    if name=='inspect_linux_screen':
        result=system_control.inspect_screen(args.get('question','Describe the current desktop, including visible app, dialog and buttons.'))
        db.log_action(name,{'question':'[screen prompt omitted]'},'completed','One transient screenshot analyzed locally; image discarded')
        return result
    if name=='open_browser_url':
        result=system_control.open_url(args)
        db.log_action(name,{'host':result['host']},'completed','Opened requested web page in default browser')
        return result
    if name=='linux_file_action':
        result=system_control.home_file_action(args,approved=approved)
        audit_args={k:('[file contents omitted]' if k=='content' else v) for k,v in args.items()}
        db.log_action(name,audit_args,'completed',f"File action {args.get('action')} completed")
        return result
    if name=='get_outlook_calendar':
        events=outlook.upcoming(args.get('days',7))
        db.log_action(name,{'days':args.get('days',7)},'completed',f'Returned {len(events)} Outlook events')
        return events
    if name=='search_calendar_events':
        return outlook.search_events(args['query'],args.get('days',90))
    if name=='search_emails':
        result=outlook.search_emails(args['query'],args.get('limit',10))
        db.log_action(name,{'query':'[private query omitted]','limit':args.get('limit',10)},'completed',f"Returned {len(result['messages'])} matching message previews; email content omitted")
        return result
    if name=='inbox_brief':
        result=outlook.inbox_brief(args.get('limit',12))
        db.log_action(name,{'limit':args.get('limit',12)},'completed',f"Returned {result['count']} unread message previews; email content omitted")
        return result
    if name=='latest_emails':
        result=outlook.latest_emails(args.get('limit',5))
        db.log_action(name,{'limit':args.get('limit',5)},'completed',f"Returned {result['count']} recent message previews; email content omitted")
        return result
    if name=='draft_email_reply':
        result=outlook.draft_email_reply(args['message_id'],args['reply_text'])
        db.log_action(name,{'message_id':'[private id omitted]','reply_text':'[private draft omitted]'},'completed','Reply draft saved in Outlook; not sent')
        return result
    if name=='search_user_files':
        result=file_search.search_files(args['query'],args.get('limit',20))
        db.log_action(name,{'query':'[private query omitted]','limit':args.get('limit',20)},'completed',f"Returned {len(result['results'])} matching text-file snippets; file content omitted")
        return result
    if name=='get_printer_status':
        return _audit(name,args,octoprint.status())
    if name=='list_printer_library':
        return _audit(name,args,octoprint.list_files(args.get('storage'),args.get('limit',100)))
    if name=='print_library_file':
        return _audit(name,args,octoprint.start_print(args['path'],args.get('storage')))
    if name=='create_outlook_event':
        result=outlook.create_event(args['title'],args['start'],args['end'],args.get('location',''),args.get('notes',''))
        return _audit(name,args,result)
    if name=='list_projects':
        projects=db.rows('SELECT * FROM projects ORDER BY id DESC')
        for p in projects:p['tasks']=db.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY id',(p['id'],))
        return _audit(name,args,projects)
    if name=='create_project':
        title=str(args.get('name','')).strip()
        if not title:raise ValueError('Project name is required')
        pid=db.execute('INSERT INTO projects(name,description,status,priority,due_date,next_action,created_at) VALUES(?,?,\'active\',\'medium\',NULL,?,?)',(title,str(args.get('description','')),str(args.get('next_action','')),db.now()))
        tasks=[]
        for task in args.get('tasks',[])[:30]:
            if str(task).strip(): tasks.append(db.execute('INSERT INTO tasks(project_id,title,description,status,priority,due_date,depends_on,created_at) VALUES(?,?,\'\',\'todo\',\'medium\',NULL,\'[]\',?)',(pid,str(task).strip()[:300],db.now())))
        (config.VAULT/f'Project {pid}.md').write_text(f"# {title}\n\n{args.get('description','')}\n\nNext action: {args.get('next_action','Not set')}\n",encoding='utf-8')
        return _audit(name,args,{'id':pid,'name':title,'task_ids':tasks})
    if name=='add_project_task':
        if not db.rows('SELECT id FROM projects WHERE id=?',(args['project_id'],)):raise ValueError('Project not found')
        tid=db.execute('INSERT INTO tasks(project_id,title,description,status,priority,due_date,depends_on,created_at) VALUES(?,?,?,\'todo\',?,NULL,\'[]\',?)',(args['project_id'],str(args['title'])[:300],str(args.get('description',''))[:1000],args.get('priority','medium'),db.now()))
        return _audit(name,args,{'id':tid,'status':'todo'})
    if name=='complete_project_task':
        if args.get('status') not in ('todo','doing','blocked','done'):raise ValueError('Choose todo, doing, blocked, or done')
        task=db.rows('SELECT id FROM tasks WHERE id=?',(args['task_id'],))
        if not task:raise ValueError('Task not found')
        db.execute('UPDATE tasks SET status=? WHERE id=?',(args['status'],args['task_id']))
        return _audit(name,args,{'id':args['task_id'],'status':args['status']})
    if name=='search_memory':
        return _audit(name,args,memory.semantic_search(str(args.get('query',''))[:1000],limit=10))
    if name=='save_memory':
        content=str(args.get('content','')).strip()
        if not content:raise ValueError('Memory content is required')
        mid=memory.save(content,args.get('kind','knowledge'),'conversation tool',0.85)
        safe=''.join(c for c in content[:48] if c.isalnum() or c in ' -_').strip() or f'Memory {mid}'
        (config.VAULT/f'{safe}.md').write_text(f'# {safe}\n\n{content}\n\nSaved by Sage · {db.now()}\n',encoding='utf-8')
        return _audit(name,args,{'id':mid,'saved':True})
    if name=='search_workshop_notebook':
        q='%'+str(args.get('query',''))[:200]+'%';found=[]
        specs=[('workshop_entry','SELECT id,note AS title,measurements AS body,created_at FROM workshop_entries WHERE note LIKE ? OR measurements LIKE ? ORDER BY id DESC LIMIT 20',2),('failure','SELECT id,title,lesson AS body,created_at FROM failure_lessons WHERE title LIKE ? OR symptom LIKE ? OR cause LIKE ? OR fix LIKE ? OR lesson LIKE ? ORDER BY id DESC LIMIT 20',5),('document','SELECT id,title,body,created_at FROM engineering_docs WHERE title LIKE ? OR body LIKE ? ORDER BY id DESC LIMIT 20',2),('project','SELECT id,name AS title,description AS body,created_at FROM projects WHERE name LIKE ? OR description LIKE ? ORDER BY id DESC LIMIT 20',2),('memory','SELECT id,kind AS title,content AS body,updated_at AS created_at FROM memories WHERE content LIKE ? ORDER BY id DESC LIMIT 20',1)]
        for kind,sql,n in specs:
            for row in db.rows(sql,tuple(q for _ in range(n))):row['type']=kind;found.append(row)
        found.sort(key=lambda r:r.get('created_at',''),reverse=True)
        return _audit(name,args,found[:50])
    if name=='add_workshop_entry':
        note=str(args.get('note','')).strip()
        if not note:raise ValueError('Workshop note is required')
        pid=args.get('project_id') or None
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        eid=db.execute('INSERT INTO workshop_entries(project_id,note,measurements,image_name,created_at) VALUES(?,?,?,\'\',?)',(pid,note,str(args.get('measurements',''))[:4000],db.now()))
        (config.VAULT/f'Workshop Entry {eid}.md').write_text(f'# Workshop entry {eid}\n\n{note}\n\n{args.get("measurements","")}\n',encoding='utf-8')
        return _audit(name,args,{'id':eid,'saved':True})
    if name=='calculate_engineering':
        result=engineering.calculate(args['operation'],args['values']);cid=db.execute('INSERT INTO engineering_calculations(project_id,operation,inputs_json,result_json,file_path,created_at) VALUES(NULL,?,?,?,?,?)',(args['operation'],json.dumps(args['values']),json.dumps(result),'',db.now()))
        text=f'# Calculation {cid}\n\n{args["operation"]}\n\nInputs: `{json.dumps(args["values"],ensure_ascii=False)}`\n\nResult: `{json.dumps(result,ensure_ascii=False)}`\n'
        folder=(config.WORKSPACE/'engineering'/'calculations');folder.mkdir(parents=True,exist_ok=True);dest=folder/f'calculation_{cid}.md';dest.write_text(text,encoding='utf-8');db.execute('UPDATE engineering_calculations SET file_path=? WHERE id=?',(str(dest),cid));(config.VAULT/f'Calculation {cid}.md').write_text(text,encoding='utf-8')
        return _audit(name,args,{'id':cid,**result})
    if name=='list_inventory':
        items=db.rows('SELECT * FROM inventory_items ORDER BY name COLLATE NOCASE');q=str(args.get('query','')).lower()
        return _audit(name,args,[x for x in items if not q or q in (x['name']+' '+x['part_number']+' '+x['description']+' '+x['location']).lower()])
    if name=='add_inventory_item':
        name_value=str(args.get('name','')).strip()
        if not name_value:raise ValueError('Part name is required')
        quantity=float(args.get('quantity',0)); threshold=float(args.get('reorder_level',0))
        if quantity<0 or threshold<0:raise ValueError('Stock values cannot be negative')
        pid=args.get('project_id') or None
        iid=db.execute('INSERT INTO inventory_items(project_id,part_number,name,description,quantity,unit,location,supplier,reorder_level,updated_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,str(args.get('part_number',''))[:120],name_value,str(args.get('description',''))[:1000],quantity,str(args.get('unit','each'))[:40],str(args.get('location',''))[:200],str(args.get('supplier',''))[:200],threshold,db.now(),db.now()))
        if quantity:db.execute('INSERT INTO inventory_movements(item_id,delta,reason,created_at) VALUES(?,?,?,?)',(iid,quantity,'Initial stock recorded by Sage',db.now()))
        return _audit(name,args,{'id':iid,'quantity':quantity,'unit':args.get('unit','each')})
    if name=='adjust_inventory_stock':
        row=db.rows('SELECT quantity FROM inventory_items WHERE id=?',(args['item_id'],))
        if not row:raise ValueError('Inventory item not found')
        delta=float(args['delta']);quantity=row[0]['quantity']+delta
        if quantity<0:raise ValueError('Stock adjustment would make quantity negative')
        db.execute('UPDATE inventory_items SET quantity=?,updated_at=? WHERE id=?',(quantity,db.now(),args['item_id']));db.execute('INSERT INTO inventory_movements(item_id,delta,reason,created_at) VALUES(?,?,?,?)',(args['item_id'],delta,str(args['reason'])[:500],db.now()))
        return _audit(name,args,{'id':args['item_id'],'quantity':quantity})
    if name=='list_failures_and_lessons':
        q='%'+str(args.get('query',''))[:200]+'%';return _audit(name,args,db.rows('SELECT * FROM failure_lessons WHERE title LIKE ? OR symptom LIKE ? OR cause LIKE ? OR fix LIKE ? OR lesson LIKE ? ORDER BY id DESC LIMIT 30',(q,q,q,q,q)))
    if name=='record_failure_lesson':
        title=str(args.get('title','')).strip();symptom=str(args.get('symptom','')).strip()
        if not title or not symptom:raise ValueError('Failure title and symptom are required')
        pid=args.get('project_id') or None;stamp=db.now();fid=db.execute('INSERT INTO failure_lessons(project_id,title,symptom,cause,fix,lesson,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(pid,title,symptom,str(args.get('cause','')),str(args.get('fix','')),str(args.get('lesson','')),stamp,stamp))
        (config.VAULT/f'Failure Lesson {fid}.md').write_text(f'# {title}\n\nSymptom: {symptom}\n\nCause: {args.get("cause","Not determined")}\n\nFix: {args.get("fix","Not recorded")}\n\nLesson: {args.get("lesson","Not recorded")}\n',encoding='utf-8')
        return _audit(name,args,{'id':fid,'saved':True})
    if name=='list_troubleshooting_guides':
        guides=db.rows('SELECT * FROM troubleshooting_trees ORDER BY id DESC');q=str(args.get('query','')).lower()
        selected=[]
        for g in guides:
            g['steps']=json.loads(g.pop('steps_json'))
            if not q or q in (g['title']+' '+g['symptom']+' '+' '.join(g['steps'])).lower():selected.append(g)
        return _audit(name,args,selected)
    if name=='create_troubleshooting_guide':
        title=str(args['title']).strip();symptom=str(args['symptom']).strip();steps=[str(x).strip() for x in args['steps'] if str(x).strip()][:40]
        if not title or not symptom or not steps:raise ValueError('Title, symptom, and at least one step are required')
        tid=db.execute('INSERT INTO troubleshooting_trees(project_id,title,symptom,steps_json,created_at) VALUES(?,?,?,?,?)',(args.get('project_id') or None,title,symptom,json.dumps(steps,ensure_ascii=False),db.now()))
        return _audit(name,args,{'id':tid,'steps':len(steps)})
    if name=='queue_research':
        jid=jobs.create(str(args['title'])[:200],str(args['question'])[:5000],'research');return _audit(name,args,{'job_id':jid,'status':'queued','note':'Runs when Sage is idle.'})
    if name=='queue_engineering_review':
        kind=args['kind']
        if kind not in ('test_plan','design_review'):raise ValueError('Unsupported review kind')
        pid=args.get('project_id') or None
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        jid=jobs.create(str(args['title'])[:200],str(args['prompt'])[:5000],kind,json.dumps({'project_id':pid},ensure_ascii=False));return _audit(name,args,{'job_id':jid,'status':'queued'})
    if name=='create_project_handoff':
        pid=args['project_id'];p=db.rows('SELECT * FROM projects WHERE id=?',(pid,))
        if not p:raise ValueError('Project not found')
        p=p[0];tasks=db.rows('SELECT title,status,priority,due_date FROM tasks WHERE project_id=? ORDER BY status,due_date',(pid,));notes=db.rows('SELECT note,measurements,created_at FROM workshop_entries WHERE project_id=? ORDER BY id DESC LIMIT 10',(pid,));failures=db.rows('SELECT title,symptom,cause,fix,lesson FROM failure_lessons WHERE project_id=? ORDER BY id DESC LIMIT 10',(pid,));versions=db.rows('SELECT name,version,sha256,notes FROM design_versions WHERE project_id=? ORDER BY created_at DESC LIMIT 15',(pid,));inventory=db.rows('SELECT name,quantity,unit,location FROM inventory_items WHERE project_id=?',(pid,))
        lines=[f'# Project handoff: {p["name"]}',f'\nGenerated {db.now()}','\n## Goal\n'+(p['description'] or 'Not recorded'),'\n## Status\n'+p['status'],'\n## Next action\n'+(p['next_action'] or 'Not set'),'\n## Tasks']+[f'- [{"x" if t["status"]=="done" else " "}] {t["title"]} — {t["status"]}' for t in tasks]+['\n## Recent workshop notes']+[f'- {n["created_at"]}: {n["note"]} {n["measurements"]}' for n in notes]+['\n## Inventory']+[f'- {i["name"]}: {i["quantity"]} {i["unit"]} · {i["location"]}' for i in inventory]+['\n## Design versions']+[f'- {v["name"]} v{v["version"]} · `{v["sha256"]}` · {v["notes"]}' for v in versions]+['\n## Failures and lessons']+[f'- {f["title"]}: {f["symptom"]}; cause {f["cause"] or "unknown"}; fix {f["fix"]}; lesson {f["lesson"]}' for f in failures]
        content='\n'.join(lines)+'\n';folder=config.WORKSPACE/'engineering'/'handoffs';folder.mkdir(parents=True,exist_ok=True);dest=folder/f'project_{pid}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.md';dest.write_text(content,encoding='utf-8');docid=db.execute('INSERT INTO engineering_docs(project_id,kind,title,prompt,body,metadata_json,status,file_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(pid,'handoff',f'{p["name"]} handoff','Generated by assistant tool',content,'{}','ready',str(dest),db.now()));(config.VAULT/f'Project {pid} Handoff.md').write_text(content,encoding='utf-8')
        return _audit(name,args,{'id':docid,'title':f'{p["name"]} handoff','path':str(dest)})
    if name=='create_knowledge_note':
        title=''.join(ch for ch in str(args['title']).strip() if ch.isalnum() or ch in ' -_')[:120].strip()
        if not title:raise ValueError('Enter a valid note title')
        body=str(args['content']).strip()[:30000]
        links=[str(x).strip() for x in args.get('links',[]) if str(x).strip()][:30]
        if links:body+='\n\nRelated: '+' '.join(f'[[{x}]]' for x in links)
        (config.VAULT/f'{title}.md').write_text(body+'\n',encoding='utf-8')
        return _audit(name,args,{'title':title,'saved':True})
    if name=='list_engineering_files':
        rows=db.rows('SELECT id,project_id,name,extension,size_bytes,sha256,metadata_json,created_at FROM engineering_assets ORDER BY id DESC')
        pid=args.get('project_id')
        result=[]
        for item in rows:
            if pid and item['project_id']!=pid:continue
            item['metadata']=json.loads(item.pop('metadata_json'));result.append(item)
        return _audit(name,args,result)
    if name=='create_bom_csv':
        title=''.join(ch for ch in str(args['title']).strip() if ch.isalnum() or ch in ' -_')[:100].strip() or 'Workshop BOM';items=args['parts']
        if not isinstance(items,list) or not items or len(items)>200:raise ValueError('BOM needs 1 to 200 parts')
        pid=args.get('project_id') or None
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        keys=('reference','part_number','description','quantity','unit','supplier','notes');out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(('Reference','Part number','Description','Quantity','Unit','Supplier','Notes'));count=0
        for item in items:
            if not isinstance(item,dict):continue
            try:qty=float(item.get('quantity',1))
            except (ValueError,TypeError):continue
            if qty<=0:continue
            cells=[]
            for key in keys:
                value=qty if key=='quantity' else str(item.get(key,''))[:500].strip()
                if key!='quantity' and str(value).startswith(('=','+','-','@','\t','\r')):value="'"+str(value)
                cells.append(value)
            writer.writerow(cells);count+=1
        if not count:raise ValueError('No valid BOM parts with positive quantities')
        root=(config.DATA/'engineering'/'assets').resolve();root.mkdir(parents=True,exist_ok=True);stored=f'sage-bom-{uuid.uuid4().hex}.csv';dest=(root/stored).resolve();dest.relative_to(root);dest.write_text(out.getvalue(),encoding='utf-8',newline='');meta=engineering.asset_metadata(dest,title+'.csv','.csv');aid=db.execute('INSERT INTO engineering_assets(project_id,name,stored_name,extension,size_bytes,sha256,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?)',(pid,title+'.csv',stored,'.csv',dest.stat().st_size,meta['sha256'],json.dumps(meta),db.now()))
        (config.VAULT/f'{title} BOM.md').write_text(f'# {title} BOM\n\nParts: {count}\n\nCSV file stored in Sage engineering assets.\n',encoding='utf-8')
        return _audit(name,args,{'asset_id':aid,'name':title+'.csv','parts':count})
    if name=='create_engineering_code_project':
        pid=args.get('project_id') or None
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        context=json.dumps({'project_id':pid},ensure_ascii=False);jid=jobs.create('Code project: '+str(args['title'])[:150],str(args['description'])[:5000],'code_project',context)
        return _audit(name,args,{'job_id':jid,'status':'queued','note':'Sage will create reviewable files during idle time; no code is run automatically.'})
    if name=='add_research_watch':
        topic=str(args['topic']).strip()[:300]
        if not topic:raise ValueError('Topic is required')
        interval=args.get('interval_days',7)
        if interval not in (1,3,7,30):raise ValueError('Choose an interval of 1, 3, 7, or 30 days')
        try:wid=db.execute('INSERT INTO watchlist(topic,interval_days,last_checked,enabled,created_at) VALUES(?,?,\'\',1,?)',(topic,interval,db.now()))
        except Exception:raise ValueError('That topic is already on the research watchlist')
        return _audit(name,args,{'id':wid,'topic':topic,'interval_days':interval})
    if name=='check_project_parts':
        pid=int(args['project_id']); project=db.rows('SELECT id FROM projects WHERE id=?',(pid,))
        if not project:raise ValueError('Project not found')
        required=[]
        for asset in db.rows("SELECT * FROM engineering_assets WHERE project_id=? AND extension IN ('.csv','.bom')",(pid,)):
            root=(config.DATA/'engineering'/'assets').resolve();file=(root/asset['stored_name']).resolve();file.relative_to(root)
            if not file.is_file():continue
            with file.open(encoding='utf-8-sig',newline='') as stream:
                for row in csv.DictReader(stream):
                    lower={str(k or '').strip().lower():str(v or '').strip() for k,v in row.items()};part=lower.get('part number') or lower.get('part_number') or '';item_name=lower.get('description') or lower.get('name') or part
                    try:qty=float(lower.get('quantity','1') or 1)
                    except ValueError:continue
                    stock=db.rows("SELECT quantity,unit FROM inventory_items WHERE (project_id=? OR project_id IS NULL) AND ((?<>'' AND lower(part_number)=lower(?)) OR lower(name)=lower(?)) ORDER BY project_id DESC LIMIT 1",(pid,part,part,item_name));on_hand=float(stock[0]['quantity']) if stock else 0
                    required.append({'part_number':part,'name':item_name,'required':qty,'on_hand':on_hand,'shortage':max(0,qty-on_hand),'unit':stock[0]['unit'] if stock else lower.get('unit','each')})
        return _audit(name,args,{'project_id':pid,'parts':required,'shortages':[item for item in required if item['shortage']>0]})
    if name=='create_measurement_template':
        title=str(args['title']).strip()[:160];fields=args['fields'];pid=args.get('project_id') or None
        if not title or not isinstance(fields,list) or not fields or len(fields)>40:raise ValueError('Provide a title and 1–40 measurement fields')
        clean=[{k:str(f.get(k,'')).strip()[:120] for k in ('name','unit','target','tolerance')} for f in fields if isinstance(f,dict) and str(f.get('name','')).strip()]
        if not clean:raise ValueError('Each measurement field needs a name')
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        tid=db.execute('INSERT INTO measurement_templates(project_id,title,fields_json,created_at) VALUES(?,?,?,?)',(pid,title,json.dumps(clean,ensure_ascii=False),db.now()))
        return _audit(name,args,{'id':tid,'title':title,'fields':clean})
    if name=='record_engineering_decision':
        title=str(args['title']).strip()[:200];decision=str(args['decision']).strip()[:4000];pid=args.get('project_id') or None
        if not title or not decision:raise ValueError('Decision title and chosen option are required')
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        did=db.execute('INSERT INTO decision_log(project_id,title,decision,alternatives,rationale,source,created_at) VALUES(?,?,?,?,?,?,?)',(pid,title,decision,str(args.get('alternatives',''))[:3000],str(args.get('rationale',''))[:3000],'Assistant conversation',db.now()))
        (config.VAULT/f'Decision {did} - {title}.md').write_text(f'# Decision: {title}\n\n## Chosen option\n{decision}\n\n## Alternatives\n{args.get("alternatives", "Not recorded")}\n\n## Rationale\n{args.get("rationale", "Not recorded")}\n',encoding='utf-8')
        return _audit(name,args,{'id':did,'saved':True})
    if name=='draft_session_summary':
        pid=args.get('project_id') or None
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        context={'summary_id':None,'project_id':pid};title=str(args.get('title','Work session summary'))[:180]
        sid=db.execute('INSERT INTO session_summaries(project_id,title,content,status,created_at,updated_at) VALUES(?,?,?,\'drafting\',?,?)',(pid,title,'',db.now(),db.now()))
        context['summary_id']=sid
        prompt='Draft a factual work-session summary from available recent messages, tasks, workshop notes, and test results. Separate changes, verification, issues, and next steps. Do not claim unrecorded work.'
        jid=jobs.create(title,prompt,'session_summary',json.dumps(context))
        db.execute('UPDATE session_summaries SET job_id=? WHERE id=?',(jid,sid))
        return _audit(name,args,{'job_id':jid,'summary_id':sid,'status':'queued_for_review'})
    if name=='get_project_brief':
        pid=int(args['project_id']);p=db.rows('SELECT * FROM projects WHERE id=?',(pid,))
        if not p:raise ValueError('Project not found')
        project=p[0];tasks=db.rows('SELECT * FROM tasks WHERE project_id=? ORDER BY CASE priority WHEN \'high\' THEN 0 WHEN \'medium\' THEN 1 ELSE 2 END,due_date,id',(pid,));today=datetime.now().date().isoformat()
        overdue=[t for t in tasks if t['status']!='done' and t.get('due_date') and t['due_date']<today]
        blocked=[]
        for task in tasks:
            if task['status']=='done':continue
            try:deps=json.loads(task.get('depends_on') or '[]')
            except ValueError:deps=[]
            waiting=[int(dep) for dep in deps if str(dep).isdigit() and any(x['id']==int(dep) and x['status']!='done' for x in tasks)]
            if task['status']=='blocked' or waiting:blocked.append({'task':task['title'],'status':task['status'],'waiting_on':waiting})
        tests=db.rows('SELECT d.title,r.outcome,r.notes,r.created_at FROM engineering_doc_results r JOIN engineering_docs d ON d.id=r.doc_id WHERE d.project_id=? AND r.id=(SELECT MAX(r2.id) FROM engineering_doc_results r2 WHERE r2.doc_id=r.doc_id) ORDER BY r.id DESC LIMIT 5',(pid,))
        decisions=db.rows('SELECT title,decision,rationale,created_at FROM decision_log WHERE project_id=? ORDER BY id DESC LIMIT 5',(pid,))
        parts=execute('check_project_parts',{'project_id':pid})
        result={'project':project,'open_tasks':[t for t in tasks if t['status']!='done'][:12],'overdue_tasks':overdue,'blocked_tasks':blocked,'latest_test_results':tests,'recent_decisions':decisions,'part_shortages':parts['shortages'],'recent_workshop_entries':db.rows('SELECT note,measurements,created_at FROM workshop_entries WHERE project_id=? ORDER BY id DESC LIMIT 4',(pid,))}
        return _audit(name,args,result)
    if name=='search_related_knowledge':
        query=str(args['query']).strip()[:500]
        if len(query)<2:raise ValueError('Enter at least two characters to search')
        tokens=set(re.findall(r'[a-z0-9]{2,}',query.lower()))-{'the','and','for','with','from','this','that','have','been','what','when','where','how','why','about'}
        if not tokens:raise ValueError('Add a more specific search phrase')
        records=[]
        for row in db.rows('SELECT id,content AS body,kind AS title,updated_at AS created_at FROM memories ORDER BY id DESC LIMIT 400'):row['type']='memory';records.append(row)
        for table,kind,sql in [
            ('workshop_entries','workshop','SELECT id,note AS title,measurements AS body,project_id,created_at FROM workshop_entries ORDER BY id DESC LIMIT 400'),
            ('failure_lessons','failure','SELECT id,title,symptom||char(10)||cause||char(10)||fix||char(10)||lesson AS body,project_id,created_at FROM failure_lessons ORDER BY id DESC LIMIT 300'),
            ('decision_log','decision','SELECT id,title,decision||char(10)||alternatives||char(10)||rationale AS body,project_id,created_at FROM decision_log ORDER BY id DESC LIMIT 300'),
            ('engineering_docs','engineering','SELECT id,title,body,project_id,created_at FROM engineering_docs ORDER BY id DESC LIMIT 200')]:
            for row in db.rows(sql):row['type']=kind;records.append(row)
        config.VAULT.mkdir(parents=True,exist_ok=True)
        vault_root=config.VAULT.resolve()
        for note in list(config.VAULT.glob('*.md'))[:500]:
            try:
                resolved=note.resolve();resolved.relative_to(vault_root)
                if note.is_symlink():continue
                body=note.read_text(encoding='utf-8',errors='replace')[:12000]
                records.append({'id':note.name,'title':note.stem,'body':body,'type':'obsidian_note','created_at':datetime.fromtimestamp(note.stat().st_mtime).isoformat(timespec='seconds'),'project_id':None})
            except (OSError,ValueError):continue
        pid=args.get('project_id')
        if pid and not db.rows('SELECT id FROM projects WHERE id=?',(pid,)):raise ValueError('Project not found')
        normalized={id(row):((str(row.get('title',''))+' '+str(row.get('body',''))).lower()) for row in records}
        document_frequency={word:sum(word in text for text in normalized.values()) for word in tokens}
        scored=[]
        for row in records:
            if pid and row.get('project_id') not in (None,pid):continue
            text=normalized[id(row)];title_text=str(row.get('title','')).lower();present={word for word in tokens if word in text}
            if not present:continue
            score=sum(1+log(1+len(records)/(1+document_frequency[word])) for word in present)
            score+=sum(1.5 for word in present if word in title_text)
            if query.lower() in text:score+=2
            if pid and row.get('project_id')==pid:score+=1.5
            row['relevance']=round(score,2);row['body']=str(row.get('body',''))[:2500];scored.append(row)
        scored.sort(key=lambda x:(x['relevance'],str(x.get('created_at',''))),reverse=True)
        return _audit(name,args,scored[:15])
    if name=='check_task_dependencies':
        pid=int(args['project_id']);p=db.rows('SELECT id FROM projects WHERE id=?',(pid,))
        if not p:raise ValueError('Project not found')
        tasks=db.rows('SELECT id,title,status,depends_on,priority,due_date FROM tasks WHERE project_id=? ORDER BY id',(pid,));by_id={t['id']:t for t in tasks};ready=[];waiting=[];invalid=[]
        for task in tasks:
            if task['status']=='done':continue
            try:deps=json.loads(task['depends_on'] or '[]')
            except ValueError:deps=[]
            if not isinstance(deps,list):deps=[]
            missing=[dep for dep in deps if not str(dep).isdigit() or int(dep) not in by_id]
            pending=[by_id[int(dep)]['title'] for dep in deps if str(dep).isdigit() and int(dep) in by_id and by_id[int(dep)]['status']!='done']
            if missing:invalid.append({'task':task['title'],'missing_dependency_ids':missing})
            elif pending:waiting.append({'task':task['title'],'waiting_on':pending})
            else:ready.append({'id':task['id'],'task':task['title'],'priority':task['priority'],'due_date':task['due_date']})
        return _audit(name,args,{'ready':ready,'waiting':waiting,'invalid_references':invalid})
    if name=='review_project_readiness':
        pid=int(args['project_id']);p=db.rows('SELECT * FROM projects WHERE id=?',(pid,))
        if not p:raise ValueError('Project not found')
        project=p[0];deps=execute('check_task_dependencies',{'project_id':pid});parts=execute('check_project_parts',{'project_id':pid});open_tasks=db.rows("SELECT title,status,due_date FROM tasks WHERE project_id=? AND status!='done' ORDER BY id",(pid,));tests=db.rows('SELECT d.title,r.outcome,r.notes,r.created_at FROM engineering_doc_results r JOIN engineering_docs d ON d.id=r.doc_id WHERE d.project_id=? AND r.id=(SELECT MAX(r2.id) FROM engineering_doc_results r2 WHERE r2.doc_id=r.doc_id) ORDER BY r.id DESC LIMIT 12',(pid,));issues=[];gaps=[];today=datetime.now().date().isoformat()
        if open_tasks and not project.get('next_action'):gaps.append({'kind':'planning_gap','evidence':'No project next action is recorded'})
        for item in deps['waiting']:issues.append({'kind':'dependency','evidence':f"{item['task']} waits on {', '.join(item['waiting_on'])}"})
        for task in open_tasks:
            if task.get('status')=='blocked':issues.append({'kind':'blocked_task','evidence':task['title']})
            if task.get('due_date') and task['due_date']<today:issues.append({'kind':'overdue_task','evidence':task['title']+' · '+task['due_date']})
        for test in tests:
            if test['outcome'] in ('failed','blocked'):issues.append({'kind':'test_'+test['outcome'],'evidence':test['title']+' · '+test['notes'][:300]})
        for item in parts['shortages']:issues.append({'kind':'parts_shortage','evidence':f"{item['name']}: short {item['shortage']} {item['unit']}"})
        if not tests:gaps.append({'kind':'verification_gap','evidence':'No test results are recorded'})
        if not db.rows("SELECT id FROM engineering_assets WHERE project_id=? AND extension IN ('.csv','.bom')",(pid,)):gaps.append({'kind':'parts_data_gap','evidence':'No project BOM is linked; parts availability could not be checked'})
        status='needs_attention' if issues else 'no_recorded_blockers'
        return _audit(name,args,{'project':project['name'],'status':status,'issues':issues,'information_gaps':gaps,'ready_tasks':deps['ready'],'scope_note':'This reviews recorded project data only. It is not a safety, quality, or compliance certification.'})
    if name=='analyze_task_graph':
        pid=int(args['project_id']);project=db.rows('SELECT name FROM projects WHERE id=?',(pid,))
        if not project:raise ValueError('Project not found')
        tasks=db.rows('SELECT id,title,status,depends_on,priority,due_date FROM tasks WHERE project_id=? ORDER BY id',(pid,));by_id={task['id']:task for task in tasks};edges={};invalid=[]
        for task in tasks:
            try:deps=json.loads(task['depends_on'] or '[]')
            except ValueError:deps=[]
            if not isinstance(deps,list):deps=[]
            edges[task['id']]=[]
            for dep in deps:
                if not str(dep).isdigit() or int(dep) not in by_id:invalid.append({'task':task['title'],'dependency':dep})
                else:edges[task['id']].append(int(dep))
        visiting=set();visited=set();cycles=[];stack=[]
        def visit(node):
            if node in visiting:
                start=stack.index(node);cycle=stack[start:]+[node]
                if cycle not in cycles:cycles.append(cycle)
                return
            if node in visited:return
            visiting.add(node);stack.append(node)
            for dep in edges.get(node,[]):visit(dep)
            stack.pop();visiting.remove(node);visited.add(node)
        for task in tasks:visit(task['id'])
        cycle_ids={item for cycle in cycles for item in cycle}
        ready=[];waiting=[]
        for task in tasks:
            if task['status']=='done':continue
            pending=[by_id[dep]['title'] for dep in edges[task['id']] if by_id[dep]['status']!='done']
            if task['id'] in cycle_ids:waiting.append({'task':task['title'],'reason':'dependency cycle','waiting_on':pending})
            elif task['status']=='blocked' or pending:waiting.append({'task':task['title'],'reason':task['status'] if task['status']=='blocked' else 'unfinished dependencies','waiting_on':pending})
            else:ready.append({'id':task['id'],'task':task['title'],'priority':task['priority'],'due_date':task['due_date']})
        return _audit(name,args,{'project':project[0]['name'],'ready':ready,'waiting':waiting,'invalid_references':invalid,'cycles':[[by_id[item]['title'] for item in cycle] for cycle in cycles]})
    if name=='get_project_timeline':
        pid=int(args['project_id']);project=db.rows('SELECT name FROM projects WHERE id=?',(pid,))
        if not project:raise ValueError('Project not found')
        days=max(1,min(3650,int(args.get('days',365))));cutoff=(datetime.now().astimezone()-timedelta(days=days)).isoformat()
        events=[]
        def add(kind,sql,params=()):
            for row in db.rows(sql,params):row['type']=kind;events.append(row)
        add('task','SELECT id,title,status AS detail,created_at FROM tasks WHERE project_id=? AND created_at>=?',(pid,cutoff))
        add('workshop','SELECT id,note AS title,measurements AS detail,created_at FROM workshop_entries WHERE project_id=? AND created_at>=?',(pid,cutoff))
        add('test','SELECT r.id,d.title,r.outcome||\' · \'||r.notes AS detail,r.created_at FROM engineering_doc_results r JOIN engineering_docs d ON d.id=r.doc_id WHERE d.project_id=? AND r.created_at>=?',(pid,cutoff))
        add('design_version','SELECT id,name AS title,\'v\'||version||\' · \'||notes AS detail,created_at FROM design_versions WHERE project_id=? AND created_at>=?',(pid,cutoff))
        add('decision','SELECT id,title,decision AS detail,created_at FROM decision_log WHERE project_id=? AND created_at>=?',(pid,cutoff))
        add('failure_lesson','SELECT id,title,lesson AS detail,created_at FROM failure_lessons WHERE project_id=? AND created_at>=?',(pid,cutoff))
        events.sort(key=lambda item:str(item.get('created_at','')),reverse=True)
        return _audit(name,args,{'project':project[0]['name'],'days':days,'count':len(events),'events':events[:250]})
    if name=='analyze_measurement_run':
        if args.get('run_id'):runs=db.rows('SELECT * FROM measurement_runs WHERE id=?',(int(args['run_id']),))
        elif args.get('project_id'):runs=db.rows('SELECT r.* FROM measurement_runs r JOIN measurement_templates t ON t.id=r.template_id WHERE r.project_id=? OR t.project_id=? ORDER BY r.id DESC LIMIT 1',(int(args['project_id']),int(args['project_id'])))
        else:runs=[]
        if not runs and not args.get('run_id'):return _audit(name,args,{'error':'Provide a run id or project id with a saved measurement run.'})
        if not runs:raise ValueError('Measurement run not found')
        run=runs[0];template=db.rows('SELECT title FROM measurement_templates WHERE id=?',(run['template_id'],));values=db.rows('SELECT * FROM measurement_values WHERE run_id=? ORDER BY id',(run['id'],));results=[]
        for row in values:
            number_pattern=r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?';actual_match=re.search(number_pattern,row['value']);target_text=row.get('target','').strip();target_numbers=re.findall(number_pattern,target_text);target_match=re.search(r'('+number_pattern+r')',target_text) if len(target_numbers)==1 else None;tol_match=re.search(number_pattern,row.get('tolerance',''))
            result={'field':row['field_name'],'actual':row['value'],'unit':row['unit'],'target':row['target'],'tolerance':row['tolerance'],'status':'not_comparable'}
            if actual_match and target_match:
                actual=float(actual_match.group());target=float(target_match.group());operator=re.match(r'^\s*(<=|>=|<|>)',target_text)
                if operator:
                    symbol=operator.group(1);passed={'<':actual<target,'<=':actual<=target,'>':actual>target,'>=':actual>=target}[symbol];result.update({'status':'meets_target' if passed else 'outside_target','delta':round(actual-target,8)})
                elif tol_match:
                    tolerance=abs(float(tol_match.group()));delta=actual-target;result.update({'status':'within_tolerance' if abs(delta)<=tolerance else ('above_tolerance' if delta>0 else 'below_tolerance'),'delta':round(delta,8),'tolerance_value':tolerance})
                else:result.update({'status':'at_target' if actual==target else ('above_target' if actual>target else 'below_target'),'delta':round(actual-target,8)})
            results.append(result)
        return _audit(name,args,{'run_id':run['id'],'template':template[0]['title'] if template else 'Unknown template','notes':run['notes'],'created_at':run['created_at'],'measurements':results,'scope_note':'Compares recorded numeric values only. Confirm units and the appropriate acceptance criteria.'})
    if name=='compare_measurement_runs':
        if args.get('template_id'):templates=db.rows('SELECT id,title FROM measurement_templates WHERE id=?',(int(args['template_id']),))
        elif args.get('project_id'):templates=db.rows('SELECT DISTINCT t.id,t.title FROM measurement_templates t JOIN measurement_runs r ON r.template_id=t.id WHERE t.project_id=? OR r.project_id=? ORDER BY t.id DESC',(int(args['project_id']),int(args['project_id'])))
        else:templates=[]
        if len(templates)>1:return _audit(name,args,{'error':'Choose one measurement template.','choices':[{'template_id':t['id'],'title':t['title']} for t in templates]})
        if not templates and not args.get('template_id'):return _audit(name,args,{'error':'Provide a template id or project id with measurement history.'})
        if not templates:raise ValueError('Measurement template not found')
        runs=db.rows('SELECT * FROM measurement_runs WHERE template_id=? ORDER BY id DESC LIMIT 2',(int(args['template_id']),))
        if len(runs)<2:return _audit(name,args,{'template':templates[0]['title'],'runs':runs,'message':'Record at least two runs to compare.'})
        values=[]
        for run in runs:
            values.append({row['field_name']:row for row in db.rows('SELECT field_name,value,unit,target,tolerance FROM measurement_values WHERE run_id=?',(run['id'],))})
        comparison=[]
        for field in sorted(set(values[0])|set(values[1])):
            newer=values[0].get(field);older=values[1].get(field);item={'field':field,'previous':older['value'] if older else None,'latest':newer['value'] if newer else None,'unit':newer['unit'] if newer else older['unit']}
            try:
                a=float(re.search(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?',older['value']).group());b=float(re.search(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?',newer['value']).group());item['delta']=round(b-a,8)
            except (AttributeError,TypeError):item['changed']=bool(newer and older and newer['value']!=older['value'])
            comparison.append(item)
        return _audit(name,args,{'template':templates[0]['title'],'previous_run':runs[1]['created_at'],'latest_run':runs[0]['created_at'],'changes':comparison,'scope_note':'Reports recorded changes only; it does not infer their cause.'})
    if name=='compare_bom_revisions':
        def read_bom(asset_id):
            rows=db.rows('SELECT * FROM engineering_assets WHERE id=?',(int(asset_id),))
            if not rows or rows[0]['extension'] not in ('.csv','.bom'):raise ValueError(f'BOM asset {asset_id} not found or not CSV/BOM')
            asset=rows[0];root=(config.DATA/'engineering'/'assets').resolve();file=(root/asset['stored_name']).resolve();file.relative_to(root)
            if not file.is_file() or file.stat().st_size>20_000_000:raise ValueError('BOM missing or larger than 20 MB')
            out={}
            with file.open(encoding='utf-8-sig',errors='replace',newline='') as stream:
                reader=csv.DictReader(stream);columns={str(c or '').strip().lower().replace('_',' '):c for c in reader.fieldnames or []};part_col=next((columns[x] for x in ('part number','part no','sku','mpn') if x in columns),None);desc_col=next((columns[x] for x in ('description','name','part','item') if x in columns),None);qty_col=next((columns[x] for x in ('quantity','qty','count') if x in columns),None);unit_col=columns.get('unit')
                for row in reader:
                    part=str(row.get(part_col,'') or '').strip() if part_col else '';desc=str(row.get(desc_col,'') or '').strip() if desc_col else '';key=(part or desc).casefold()
                    if not key:continue
                    try:qty=float(row.get(qty_col,'1') or 1) if qty_col else 1.0
                    except (ValueError,TypeError):continue
                    prior=out.get(key,{'part_number':part,'description':desc,'quantity':0.0,'unit':str(row.get(unit_col,'') or '') if unit_col else ''});prior['quantity']+=qty;out[key]=prior
            return asset,out
        def resolve_bom(asset_id,label):
            if asset_id:return int(asset_id),[]
            candidates=db.rows("SELECT id,name FROM engineering_assets WHERE extension IN ('.csv','.bom') AND (? IS NULL OR project_id=?) ORDER BY id DESC LIMIT 100",(args.get('project_id'),args.get('project_id')))
            wanted=str(args.get(label,'')).casefold()
            if wanted:candidates=[item for item in candidates if wanted in item['name'].casefold()]
            if len(candidates)!=1:return None,[{'asset_id':item['id'],'name':item['name']} for item in candidates[:20]]
            return candidates[0]['id'],[]
        older_id,older_choices=resolve_bom(args.get('older_asset_id'),'older_name');newer_id,newer_choices=resolve_bom(args.get('newer_asset_id'),'newer_name')
        if not older_id or not newer_id:return _audit(name,args,{'error':'Choose unambiguous older and newer BOM files.','older_choices':older_choices,'newer_choices':newer_choices})
        older,old_items=read_bom(older_id);newer,new_items=read_bom(newer_id);added=[];removed=[];changed=[]
        for key in sorted(set(old_items)|set(new_items)):
            if key not in old_items:added.append(new_items[key])
            elif key not in new_items:removed.append(old_items[key])
            elif old_items[key]!=new_items[key]:changed.append({'part':new_items[key]['part_number'] or new_items[key]['description'],'before':old_items[key],'after':new_items[key]})
        return _audit(name,args,{'older':older['name'],'newer':newer['name'],'added':added,'removed':removed,'changed':changed,'scope_note':'Line-item comparison only. Verify part revisions, substitutions, units, and design suitability independently.'})
    if name=='explain_memory_provenance':
        if args.get('memory_id'):rows=db.rows('SELECT id,content,kind,source,confidence,created_at,updated_at FROM memories WHERE id=?',(int(args['memory_id']),))
        elif args.get('query'):
            query='%'+str(args['query']).strip()[:180]+'%';rows=db.rows('SELECT id,content,kind,source,confidence,created_at,updated_at FROM memories WHERE content LIKE ? ORDER BY updated_at DESC LIMIT 8',(query,))
        else:rows=[]
        if not rows:raise ValueError('Provide a saved memory id or matching phrase')
        return _audit(name,args,rows[0] if args.get('memory_id') or len(rows)==1 else {'matches':rows})
    if name=='inspect_engineering_asset':
        if args.get('asset_id'):asset_rows=db.rows('SELECT * FROM engineering_assets WHERE id=?',(int(args['asset_id']),))
        else:
            candidates=db.rows('SELECT * FROM engineering_assets WHERE (? IS NULL OR project_id=?) ORDER BY id DESC LIMIT 100',(args.get('project_id'),args.get('project_id')))
            if args.get('name'):candidates=[row for row in candidates if str(args['name']).casefold() in row['name'].casefold()]
            if len(candidates)!=1:return _audit(name,args,{'error':'Choose one imported file by name or asset id.','choices':[{'asset_id':row['id'],'name':row['name'],'extension':row['extension'],'project_id':row['project_id']} for row in candidates[:20]]})
            asset_rows=candidates
        if not asset_rows:raise ValueError('Engineering asset not found')
        asset=asset_rows[0];metadata=json.loads(asset['metadata_json'] or '{}');category='photos' if asset['extension'] in ('.jpg','.jpeg','.png','.webp') else 'assets';root=(config.DATA/'engineering'/category).resolve();source=(root/asset['stored_name']).resolve();source.relative_to(root)
        result={'id':asset['id'],'name':asset['name'],'extension':asset['extension'],'project_id':asset['project_id'],'size_bytes':asset['size_bytes'],'sha256':asset['sha256'],'metadata':metadata,'preview':''}
        text_types={'.csv','.bom','.json','.step','.stp','.dxf','.kicad_pcb','.kicad_sch','.txt','.md'}
        if asset['extension'] in text_types and source.is_file() and source.stat().st_size<=2_000_000:
            result['preview']=source.read_text(encoding='utf-8',errors='replace')[:10000]
            result['preview_truncated']=source.stat().st_size>10000
        return _audit(name,args,result)
    if name=='audit_bom_file':
        if args.get('asset_id'):asset_rows=db.rows('SELECT * FROM engineering_assets WHERE id=?',(int(args['asset_id']),))
        else:
            asset_rows=db.rows("SELECT * FROM engineering_assets WHERE extension IN ('.csv','.bom') AND (? IS NULL OR project_id=?) ORDER BY id DESC LIMIT 100",(args.get('project_id'),args.get('project_id')))
            if len(asset_rows)!=1:return _audit(name,args,{'error':'Choose one BOM file.','choices':[{'asset_id':row['id'],'name':row['name'],'project_id':row['project_id']} for row in asset_rows[:20]]})
        if not asset_rows:raise ValueError('BOM asset not found')
        asset=asset_rows[0]
        if asset['extension'] not in ('.csv','.bom'):raise ValueError('Choose an imported CSV or BOM file')
        root=(config.DATA/'engineering'/'assets').resolve();source=(root/asset['stored_name']).resolve();source.relative_to(root)
        if not source.is_file() or source.stat().st_size>20_000_000:raise ValueError('BOM file is missing or exceeds the 20 MB inspection limit')
        duplicates={};issues=[];count=0;quantity_total=0
        with source.open(encoding='utf-8-sig',errors='replace',newline='') as stream:
            reader=csv.DictReader(stream)
            if not reader.fieldnames:raise ValueError('The BOM has no header row')
            columns={str(c or '').strip().lower().replace('_',' '):c for c in reader.fieldnames}
            part_col=next((columns[x] for x in ('part number','part no','sku','mpn') if x in columns),None)
            name_col=next((columns[x] for x in ('description','name','part','item') if x in columns),None)
            qty_col=next((columns[x] for x in ('quantity','qty','count') if x in columns),None)
            if not qty_col:issues.append({'kind':'missing_quantity_column','severity':'warning','row':1})
            for rownum,row in enumerate(reader,start=2):
                if rownum>10002:issues.append({'kind':'row_limit','severity':'warning','row':rownum});break
                if not any(str(v or '').strip() for v in row.values()):issues.append({'kind':'blank_row','severity':'info','row':rownum});continue
                count+=1;part=str(row.get(part_col,'') or '').strip() if part_col else '';label=str(row.get(name_col,'') or '').strip() if name_col else ''
                if not part and not label:issues.append({'kind':'missing_part_identifier','severity':'warning','row':rownum})
                if part:duplicates.setdefault(part.casefold(),[]).append(rownum)
                if qty_col:
                    try:quantity=float(str(row.get(qty_col,'')).strip())
                    except (TypeError,ValueError):issues.append({'kind':'invalid_quantity','severity':'warning','row':rownum});continue
                    if quantity<=0:issues.append({'kind':'nonpositive_quantity','severity':'warning','row':rownum})
                    else:quantity_total+=quantity
        for number,positions in duplicates.items():
            if len(positions)>1:issues.append({'kind':'duplicate_part_number','severity':'warning','part_number':number,'rows':positions})
        if count==0:issues.append({'kind':'no_line_items','severity':'warning','row':1})
        return _audit(name,args,{'asset':asset['name'],'line_items':count,'quantity_total':quantity_total,'issues':issues,'status':'review_needed' if any(i['severity']=='warning' for i in issues) else 'no_structural_issues_found','scope_note':'This checks file structure only; it does not verify part suitability, quantities, or design correctness.'})
    if name=='save_design_version':
        asset_rows=db.rows('SELECT * FROM engineering_assets WHERE id=?',(args['asset_id'],))
        if not asset_rows:raise ValueError('Design asset not found')
        asset=asset_rows[0]
        if asset['extension'] in ('.jpg','.jpeg','.png','.webp'):raise ValueError('Choose a design or parts file, not a photo')
        srcroot=(config.DATA/'engineering'/'assets').resolve();src=(srcroot/asset['stored_name']).resolve();src.relative_to(srcroot)
        if not src.is_file():raise ValueError('Source design file is missing')
        previous=db.rows('SELECT MAX(version) AS v FROM design_versions WHERE project_id IS ? AND name=?',(asset['project_id'],asset['name']));version=(previous[0]['v'] or 0)+1;root=(config.DATA/'engineering'/'versions').resolve();root.mkdir(parents=True,exist_ok=True);stored=uuid.uuid4().hex+asset['extension'];dest=(root/stored).resolve();dest.relative_to(root);shutil.copyfile(src,dest);sha=hashlib.sha256(dest.read_bytes()).hexdigest();vid=db.execute('INSERT INTO design_versions(project_id,asset_id,version,name,stored_name,sha256,notes,created_at) VALUES(?,?,?,?,?,?,?,?)',(asset['project_id'],asset['id'],version,asset['name'],stored,sha,str(args.get('notes','')),db.now()));(config.VAULT/f'Design {asset["name"]} v{version}.md').write_text(f'# Design version {version}: {asset["name"]}\n\nSHA-256: `{sha}`\n\n{args.get("notes","")}\n',encoding='utf-8')
        return _audit(name,args,{'id':vid,'version':version,'sha256':sha})
    if name=='run_engineering_tests':
        result=jobs.run_code_tests(args['project_id'],approved=True)
        return _audit(name,args,result)
    raise ValueError('Unknown tool')
