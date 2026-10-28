from pathlib import Path

def resolve(workspace, relative):
    root=Path(workspace).resolve()
    target=(root/relative).resolve()
    target.relative_to(root)
    return target

def run(arguments, context):
    root=Path(context['workspace']).resolve()
    action=arguments.get('action','list')
    if action=='list':
        folder=resolve(root,arguments.get('path',''))
        if not folder.exists(): return {"path":arguments.get('path',''),"files":[]}
        if not folder.is_dir(): raise ValueError('Path is not a directory')
        return {"path":arguments.get('path',''),"files":[{"name":p.name,"directory":p.is_dir()} for p in sorted(folder.iterdir())]}
    if action=='read':
        file=resolve(root,arguments.get('path',''))
        if not file.is_file(): raise ValueError('File does not exist')
        if file.stat().st_size>200_000: raise ValueError('File is larger than 200 KB')
        return {"path":arguments.get('path'),"content":file.read_text(encoding='utf-8')}
    raise ValueError('Unsupported action')
