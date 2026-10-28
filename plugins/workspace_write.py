from pathlib import Path

def run(arguments, context):
    root=Path(context['workspace']).resolve()
    target=(root/str(arguments['path'])).resolve()
    target.relative_to(root)
    if target.exists() and target.is_dir(): raise ValueError('Cannot replace a directory')
    if len(arguments['content'].encode('utf-8'))>500_000: raise ValueError('File is larger than 500 KB')
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(arguments['content'],encoding='utf-8')
    return {"path":str(target.relative_to(root)),"bytes":target.stat().st_size}
