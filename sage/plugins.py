import importlib.util
import json
from pathlib import Path
from . import config, db

def discover():
    found = []
    if not config.PLUGIN_DIR.exists(): return found
    for manifest in config.PLUGIN_DIR.glob('*.json'):
        try:
            meta = json.loads(manifest.read_text(encoding='utf-8'))
            meta['_manifest'] = str(manifest)
            found.append(meta)
        except Exception as exc:
            found.append({"name": manifest.stem, "description": f"Invalid plugin metadata: {exc}", "permission": "blocked"})
    return found

def call(name, arguments, approved=False, dry_run=False):
    meta = next((p for p in discover() if p.get('name') == name), None)
    if not meta: raise ValueError('Unknown plugin')
    permission = meta.get('permission', 'blocked')
    if not isinstance(arguments,dict): raise ValueError('Plugin inputs must be a JSON object')
    schema=meta.get('input_schema') or {}; properties=schema.get('properties',{})
    # Small local models often send numbers/booleans as strings; coerce the obvious cases before validating.
    for key,value in list(arguments.items()):
        kind=properties.get(key,{}).get('type')
        if kind in ('integer','number') and isinstance(value,str):
            try: arguments[key]=int(value) if kind=='integer' else float(value)
            except ValueError: pass
        elif kind=='boolean' and isinstance(value,str) and value.strip().lower() in ('true','false'):
            arguments[key]=value.strip().lower()=='true'
    missing=[key for key in schema.get('required',[]) if key not in arguments]
    if missing: raise ValueError('Missing required plugin input(s): '+', '.join(missing))
    if schema.get('additionalProperties') is False:
        extra=set(arguments)-set(properties)
        if extra: raise ValueError('Unexpected plugin input(s): '+', '.join(sorted(extra)))
    for key,value in arguments.items():
        spec=properties.get(key,{})
        if 'enum' in spec and value not in spec['enum']: raise ValueError(f'Invalid value for {key}')
        expected=spec.get('type')
        valid={'string':lambda v:isinstance(v,str),'integer':lambda v:isinstance(v,int) and not isinstance(v,bool),'number':lambda v:isinstance(v,(int,float)) and not isinstance(v,bool),'boolean':lambda v:isinstance(v,bool),'object':lambda v:isinstance(v,dict),'array':lambda v:isinstance(v,list)}
        if expected in valid and not valid[expected](value): raise ValueError(f'Plugin input {key} must be {expected}')
    if permission == 'blocked':
        db.log_action(name, arguments, 'blocked', 'Plugin is disabled')
        raise PermissionError('This plugin is blocked')
    if permission == 'confirm' and not approved:
        db.log_action(name, arguments, 'pending_approval', 'Awaiting explicit approval')
        return {"status": "pending_approval", "preview": {"plugin": name, "arguments": arguments}}
    if dry_run:
        db.log_action(name, arguments, 'dry_run', 'No action executed')
        return {"status": "dry_run", "preview": {"plugin": name, "arguments": arguments}}
    module_path = Path(meta['_manifest']).parent / meta['entrypoint'].split(':')[0]
    if module_path.suffix != '.py': module_path = module_path.with_suffix('.py')
    spec = importlib.util.spec_from_file_location('sage_user_plugin_' + name, module_path)
    if not spec or not spec.loader: raise ValueError('Plugin entrypoint cannot be loaded')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    func = getattr(module, meta['entrypoint'].split(':')[1])
    try:
        result = func(arguments, {"workspace": str(config.WORKSPACE)})
    except Exception as exc:
        db.log_action(name, arguments, 'failed', str(exc))
        raise
    db.log_action(name, arguments, 'completed', '[output hidden: sensitive]' if meta.get('sensitive_output') else result)
    return {"status": "completed", "result": result}
