import csv
import math
import re
from pathlib import Path

MAX_POINTS = 20000
MAX_SERIES = 10
MAX_CSV_BYTES = 5_000_000


def _number(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} contains a non-number')
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} contains a non-number: {value!r}')
    if not math.isfinite(number):
        raise ValueError(f'{name} contains a value that is not finite')
    return number


def _numbers(values, name):
    if not isinstance(values, list) or not values:
        raise ValueError(f'{name} must be a non-empty list')
    return [_number(v, name) for v in values]


def _from_csv(arguments, root):
    source = (root / str(arguments['csv_path'])).resolve()
    source.relative_to(root)
    if not source.is_file():
        raise ValueError('CSV file not found in the workspace')
    if source.stat().st_size > MAX_CSV_BYTES:
        raise ValueError('CSV file is larger than 5 MB')
    with source.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise ValueError('CSV file has no data rows')
    headers = list(rows[0].keys())
    x_column = str(arguments.get('x_column') or headers[0])
    y_columns = arguments.get('y_columns') or [h for h in headers if h != x_column]
    if x_column not in headers:
        raise ValueError(f'x_column {x_column!r} is not in the CSV header: {headers}')
    for column in y_columns:
        if column not in headers:
            raise ValueError(f'y column {column!r} is not in the CSV header: {headers}')
    x = [row[x_column] for row in rows]
    series = {column: [row[column] for row in rows] for column in y_columns}
    return x, series, x_column


def run(arguments, context):
    root = Path(context['workspace']).resolve()
    kind = arguments.get('kind', 'line')
    x_default = ''
    if arguments.get('csv_path'):
        x, series, x_default = _from_csv(arguments, root)
    elif 'x' in arguments and 'series' in arguments:
        x, series = arguments['x'], arguments['series']
    else:
        raise ValueError('Give either csv_path, or x together with series')
    if not isinstance(x, list) or not x:
        raise ValueError('x must be a non-empty list')
    if not isinstance(series, dict) or not series:
        raise ValueError('series must be an object such as {"label": [numbers]}')
    if len(series) > MAX_SERIES:
        raise ValueError(f'At most {MAX_SERIES} series per plot')
    if len(x) > MAX_POINTS:
        raise ValueError(f'At most {MAX_POINTS} points per series')

    labels = [str(v) for v in x] if kind == 'bar' else None
    xs = None if kind == 'bar' else _numbers(x, 'x')
    ys = {}
    for name, values in series.items():
        ys[str(name)[:60]] = _numbers(values, f'series {name}')
        if len(values) != len(x):
            raise ValueError(f'series {name} has {len(values)} values but x has {len(x)}')

    try:
        from matplotlib.figure import Figure
    except ImportError:
        raise RuntimeError('matplotlib is not installed. Run: pip install matplotlib')

    fig = Figure(figsize=(6.4, 4.2), dpi=200)
    ax = fig.subplots()
    if kind == 'bar':
        width = 0.8 / len(ys)
        for index, (name, values) in enumerate(ys.items()):
            positions = [i + (index - (len(ys) - 1) / 2) * width for i in range(len(values))]
            ax.bar(positions, values, width=width, label=name)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30 if len(labels) > 6 else 0, ha='right' if len(labels) > 6 else 'center')
    else:
        for name, values in ys.items():
            if kind == 'scatter':
                ax.scatter(xs, values, label=name, s=18)
            else:
                ax.plot(xs, values, label=name, marker='o' if len(xs) <= 30 else None, markersize=3.5, linewidth=1.6)
    if arguments.get('logx') and kind != 'bar':
        ax.set_xscale('log')
    if arguments.get('logy'):
        ax.set_yscale('log')
    title = str(arguments.get('title', ''))[:120]
    if title:
        ax.set_title(title)
    ax.set_xlabel(str(arguments.get('xlabel', x_default))[:80])
    ax.set_ylabel(str(arguments.get('ylabel', ''))[:80])
    ax.grid(True, alpha=0.3)
    if len(ys) > 1 or any(name for name in ys):
        ax.legend()
    fig.tight_layout()

    folder = root / 'plots'
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[^\w-]', '_', Path(str(arguments.get('filename') or title or 'plot')).stem).strip('_')[:60] or 'plot'
    target = folder / f'{stem}.png'
    number = 2
    while target.exists():
        target = folder / f'{stem}_{number}.png'
        number += 1
    target.resolve().relative_to(root)
    fig.savefig(target)
    return {'path': str(target.relative_to(root)).replace('\\', '/'), 'series': list(ys), 'points': len(x), 'kind': kind}
