import ast
import operator

OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
       ast.USub: operator.neg, ast.UAdd: operator.pos}

def safe_eval(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float): return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left, right = safe_eval(node.left), safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 8: raise ValueError('Exponent is too large')
        return OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS: return OPS[type(node.op)](safe_eval(node.operand))
    raise ValueError('Only numeric arithmetic is allowed')

def run(arguments, context):
    expr = str(arguments.get('expression', ''))
    if len(expr) > 160: raise ValueError('Expression is too long')
    return {"expression": expr, "value": safe_eval(ast.parse(expr, mode='eval').body)}
