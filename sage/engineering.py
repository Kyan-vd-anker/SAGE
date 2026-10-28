"""Small, explicit engineering helpers with assumptions kept visible."""
import csv
import hashlib
import json
import math
import re
import struct
from pathlib import Path

UNITS = {
    'length': {'m': (1, 0), 'mm': (.001, 0), 'cm': (.01, 0), 'in': (.0254, 0), 'ft': (.3048, 0)},
    'mass': {'kg': (1, 0), 'g': (.001, 0), 'lb': (.45359237, 0)},
    'pressure': {'Pa': (1, 0), 'kPa': (1000, 0), 'bar': (100000, 0), 'psi': (6894.757293, 0)},
    'force': {'N': (1, 0), 'kN': (1000, 0), 'lbf': (4.448221615, 0)},
    'temperature': {'K': (1, 0), 'C': (1, 273.15), 'F': (5/9, 273.15-32*5/9)},
}


def _number(values, key):
    try: value=float(values[key])
    except (KeyError, TypeError, ValueError): raise ValueError(f'Enter a valid number for {key}.')
    if not math.isfinite(value): raise ValueError(f'{key} must be a finite number.')
    return value


def calculate(operation, values):
    if operation=='convert':
        category=str(values.get('category',''))
        source=str(values.get('from_unit','')); target=str(values.get('to_unit',''))
        if category not in UNITS or source not in UNITS[category] or target not in UNITS[category]: raise ValueError('Choose two compatible units.')
        value=_number(values,'value'); scale,offset=UNITS[category][source]; out=(value*scale+offset-UNITS[category][target][1])/UNITS[category][target][0]
        return {'formula':'base = value × source scale + source offset; converted = (base − target offset) ÷ target scale','result':round(out,8),'unit':target,'assumptions':['Units are treated as exact conversion factors.']}
    if operation=='ohms_law':
        names=('voltage_v','current_a','resistance_ohm'); known={k:values[k] for k in names if values.get(k) not in (None,'')}
        if len(known)!=2: raise ValueError('Enter exactly two of voltage, current, and resistance.')
        known={k:_number(known,k) for k in known}; v=known.get('voltage_v'); i=known.get('current_a'); r=known.get('resistance_ohm')
        if v is None: v=i*r
        elif i is None:
            if r==0: raise ValueError('Resistance cannot be zero when calculating current.')
            i=v/r
        elif r is None:
            if i==0: raise ValueError('Current cannot be zero when calculating resistance.')
            r=v/i
        return {'formula':'V = I × R; I = V ÷ R; R = V ÷ I','results':{'voltage_v':round(v,8),'current_a':round(i,8),'resistance_ohm':round(r,8)},'units':{'voltage_v':'V','current_a':'A','resistance_ohm':'Ω'},'assumptions':['DC or resistive steady-state model; ignores temperature, AC phase, and component tolerances.']}
    if operation=='power':
        v=_number(values,'voltage_v'); i=_number(values,'current_a')
        return {'formula':'P = V × I','result':round(v*i,8),'unit':'W','assumptions':['DC or resistive load; AC power factor is not included.']}
    if operation=='moment':
        force=_number(values,'force_n'); arm=_number(values,'arm_m')
        return {'formula':'M = F × perpendicular lever arm','result':round(force*arm,8),'unit':'N·m','assumptions':['Force is perpendicular to the stated lever arm.']}
    if operation=='pressure_force':
        pressure=_number(values,'pressure_pa'); area=_number(values,'area_m2')
        return {'formula':'F = p × A','result':round(pressure*area,8),'unit':'N','assumptions':['Uniform pressure acts normal to the full stated area.']}
    if operation=='density':
        mass=_number(values,'mass_kg'); volume=_number(values,'volume_m3')
        if volume<=0: raise ValueError('Volume must be greater than zero.')
        return {'formula':'ρ = m ÷ V','result':round(mass/volume,8),'unit':'kg/m³','assumptions':['Mass and volume refer to the same sample and state.']}
    raise ValueError('Unknown calculation.')


def asset_metadata(path, filename, extension):
    path=Path(path); size=path.stat().st_size; meta={'type':extension.lstrip('.').upper(),'size_bytes':size}
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
    meta['sha256']=digest.hexdigest()
    if extension=='.csv':
        with path.open('r',encoding='utf-8-sig',errors='replace',newline='') as stream:
            reader=csv.reader(stream); rows=list(reader)
        meta.update({'columns':rows[0][:40] if rows else [],'row_count':max(0,len(rows)-1),'sample_rows':rows[1:6]})
    elif extension in ('.step','.stp'):
        text=path.read_text(encoding='latin-1',errors='replace')
        meta['product_names']=re.findall(r"PRODUCT\s*\(\s*'([^']+)'",text,re.I)[:20]
        meta['entity_counts']={name:len(re.findall(r'(?im)^\s*#\d+\s*=\s*'+re.escape(name)+r'\b',text)) for name in ('CARTESIAN_POINT','MANIFOLD_SOLID_BREP','ADVANCED_FACE')}
        schema=re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'",text,re.I); meta['schema']=schema.group(1) if schema else ''
    elif extension=='.stl':
        with path.open('rb') as stream: header=stream.read(84)
        if len(header)>=84:
            triangles=struct.unpack('<I',header[80:84])[0]
            if 84+triangles*50==size and triangles<=500000:
                meta['triangles']=triangles
                mins=[float('inf')]*3; maxs=[float('-inf')]*3
                with path.open('rb') as stream:
                    stream.seek(84)
                    for _ in range(triangles):
                        record=stream.read(50)
                        for offset in (12,24,36):
                            point=struct.unpack_from('<fff',record,offset)
                            for axis,value in enumerate(point): mins[axis]=min(mins[axis],value); maxs[axis]=max(maxs[axis],value)
                meta['bounds']={'min':mins,'max':maxs,'units':'file units; confirm from design source'}
            else:
                text=path.read_text(encoding='ascii',errors='replace')
                vertices=re.findall(r'(?i)vertex\s+([-+\d.e]+)\s+([-+\d.e]+)\s+([-+\d.e]+)',text)
                if vertices:
                    points=[[float(v) for v in point] for point in vertices]
                    meta['triangles']=len(points)//3
                    meta['bounds']={'min':[min(p[i] for p in points) for i in range(3)],'max':[max(p[i] for p in points) for i in range(3)],'units':'file units; confirm from design source'}
    elif extension=='.dxf':
        text=path.read_text(encoding='latin-1',errors='replace')
        meta['entity_counts']={name:len(re.findall(r'(?im)^\s*'+re.escape(name)+r'\s*$',text)) for name in ('LINE','CIRCLE','ARC','LWPOLYLINE','INSERT')}
    elif extension in ('.kicad_pcb','.kicad_sch'):
        text=path.read_text(encoding='utf-8',errors='replace')
        meta['counts']={'footprints':len(re.findall(r'\(footprint\s',text)),'symbols':len(re.findall(r'\(symbol\s',text)),'wires':len(re.findall(r'\(wire\s',text))}
    elif extension=='.json':
        try:
            obj=json.loads(path.read_text(encoding='utf-8'))
            meta['json_shape']='array' if isinstance(obj,list) else 'object' if isinstance(obj,dict) else type(obj).__name__
            meta['top_level_keys']=list(obj.keys())[:50] if isinstance(obj,dict) else []
        except (OSError,ValueError):
            meta['json_shape']='unreadable or invalid JSON'
    return meta
