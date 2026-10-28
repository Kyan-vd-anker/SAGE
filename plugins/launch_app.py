import json
import os
import subprocess

def run(arguments, context):
    try: allowed=json.loads(os.getenv('SAGE_ALLOWED_APPS','{}'))
    except json.JSONDecodeError as exc: raise ValueError('SAGE_ALLOWED_APPS must be a JSON object') from exc
    name=arguments['app']
    if name not in allowed: raise PermissionError('Application is not in the SAGE_ALLOWED_APPS allowlist')
    command=allowed[name]
    if isinstance(command,str): command=[command]
    if not isinstance(command,list) or not command or not all(isinstance(x,str) for x in command): raise ValueError('Allowlist value must be an executable or argument list')
    subprocess.Popen(command, shell=False, close_fds=True)
    return {"launched":name}
