"""Algorithm-scoped identity; changes to unrelated supplement assembly stay separate."""
import ast
import hashlib
import importlib.metadata
import json
import platform
import sys
from pathlib import Path


def local_closure(source, entry):
    tree=ast.parse(source)
    definitions={n.name:n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))}
    if entry not in definitions: raise ValueError('missing algorithm entry')
    pending=[entry]; selected={}
    while pending:
        name=pending.pop()
        if name in selected: continue
        node=definitions[name]
        selected[name]=ast.dump(node,include_attributes=False)
        pending.extend(n.id for n in ast.walk(node) if isinstance(n,ast.Name)
                       and n.id in definitions and n.id not in selected)
    # Include module configuration and imports conservatively. Function bodies
    # outside the transitive local call/reference closure do not affect identity.
    selected['module_configuration']=[ast.dump(n,include_attributes=False) for n in tree.body
        if not isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
    return selected


def identity(directory=None):
    directory=directory or Path(__file__).resolve().parent
    source=(directory/'supplement_docx.py').read_text(encoding='utf-8')
    closure=local_closure(source,'digitize_xy_markers')
    tree=ast.parse(source)
    functions=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))
               and n.name in closure]
    used={n.id for function in functions for n in ast.walk(function) if isinstance(n,ast.Name)}
    imported_local={}
    imports=[n for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom))]
    for node in imports:
        for alias in node.names:
            symbol=alias.asname or (alias.name if isinstance(node,ast.ImportFrom) else alias.name.split('.')[0])
            module=node.module if isinstance(node,ast.ImportFrom) else alias.name
            if symbol in used and module and (directory/(module.replace('.','/')+'.py')).exists():
                imported_local[module]=hashlib.sha256((directory/(module.replace('.','/')+'.py')).read_bytes()).hexdigest()
    if set(imported_local)!={'marker_geometry'}:
        raise ValueError('marker local dependencies changed; review transitive dependency manifest before reuse')
    payload={'protocol':'marker-implementation-closure-v1',
             'runtime':{'python':sys.version,'implementation':platform.python_implementation(),
                        'system':platform.system(),'machine':platform.machine()},
             'closure':closure,
             'imported_local':imported_local,
             'marker_geometry':hashlib.sha256((directory/'marker_geometry.py').read_bytes()).hexdigest(),
             'identity_implementation':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'libraries':{n:importlib.metadata.version(n) for n in ('numpy','Pillow')}}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
