"""Save fenced code from Sage replies inside its contained workspace."""
import re
from datetime import datetime
from . import config

FENCE = re.compile(r"```([^\n`]*)\n([\s\S]*?)```", re.MULTILINE)
EXTENSIONS = {
    'python': 'py', 'py': 'py', 'javascript': 'js', 'js': 'js', 'typescript': 'ts',
    'ts': 'ts', 'html': 'html', 'css': 'css', 'json': 'json', 'bash': 'sh',
    'sh': 'sh', 'powershell': 'ps1', 'ps1': 'ps1', 'sql': 'sql', 'java': 'java',
    'c': 'c', 'cpp': 'cpp', 'rust': 'rs', 'rs': 'rs', 'go': 'go', 'yaml': 'yaml',
    'yml': 'yml', 'toml': 'toml', 'markdown': 'md', 'md': 'md', 'text': 'txt',
    'plaintext': 'txt', 'scad': 'scad', 'csv': 'csv',
}


def extract_blocks(text):
    """Return (language, source) pairs from Markdown fenced code blocks."""
    blocks = []
    for match in FENCE.finditer(text):
        info = match.group(1).strip().split()
        language = info[0].lower() if info else 'text'
        source = match.group(2).rstrip() + '\n'
        if source.strip():
            blocks.append((language, source))
    return blocks


def save_answer(text):
    """Save each fenced code block without overwriting an existing workspace file."""
    blocks = extract_blocks(text)
    if not blocks:
        return text, []
    workspace = config.WORKSPACE.resolve()
    target_dir = (workspace / 'generated').resolve()
    target_dir.relative_to(workspace)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir = target_dir.resolve()
    target_dir.relative_to(workspace)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    saved = []
    for index, (language, source) in enumerate(blocks, 1):
        extension = EXTENSIONS.get(language, 'txt')
        filename = f'sage_{stamp}_{index}.{extension}'
        destination = (target_dir / filename).resolve()
        destination.relative_to(workspace)
        with destination.open('x', encoding='utf-8', newline='\n') as output:
            output.write(source)
        saved.append({'filename': filename, 'path': f'workspace/generated/{filename}', 'language': language})
    notice = '\n\nSaved code files:\n' + '\n'.join(f"- {item['path']}" for item in saved)
    return text.rstrip() + notice, saved
