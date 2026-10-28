import importlib
from sage import research

def run(arguments, context):
    # Plugin modules load on demand; refresh the replaceable provider adapter too.
    importlib.reload(research)
    return research.search_with_config(str(arguments.get('query','')))
