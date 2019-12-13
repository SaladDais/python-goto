import dis
import struct
import array
import types
import functools
import weakref

try:
    import __pypy__
except:
    __pypy__ = None

try:
    _array_to_bytes = array.array.tobytes
except AttributeError:
    _array_to_bytes = array.array.tostring


class _Bytecode:
    def __init__(self):
        code = (lambda: x if x else y).__code__.co_code
        opcode, oparg = struct.unpack_from('BB', code, 2)

        # Starting with Python 3.6, the bytecode format has been changed to use
        # 16-bit words (8-bit opcode + 8-bit argument) for each instruction,
        # as opposed to previously 24-bit (8-bit opcode + 16-bit argument) for
        # instructions that expect an argument or just 8-bit for those that don't.
        # https://bugs.python.org/issue26647
        if dis.opname[opcode] == 'POP_JUMP_IF_FALSE':
            self.argument = struct.Struct('B')
            self.have_argument = 0
            # As of Python 3.6, jump targets are still addressed by their byte
            # unit. This, however, is matter to change, so that jump targets,
            # in the future, will refer to the code unit (address in bytes / 2).
            # https://bugs.python.org/issue26647
            self.jump_unit = 8 // oparg
        else:
            self.argument = struct.Struct('<H')
            self.have_argument = dis.HAVE_ARGUMENT
            self.jump_unit = 1
            
        self.has_loop_blocks = 'SETUP_LOOP' in dis.opmap

    @property
    def argument_bits(self):
        return self.argument.size * 8


_BYTECODE = _Bytecode()

_patched_code_cache = weakref.WeakKeyDictionary() # use a weak dictionary in case code objects can be garbage-collected
try:
    _patched_code_cache[_Bytecode.__init__.__code__] = None
except TypeError:
    _patched_code_cache = {} # ...unless not supported

def _make_code(code, codestring):
    try:
        return code.replace(co_code=codestring) # new in 3.8+
    except:
        args = [
            code.co_argcount,  code.co_nlocals,     code.co_stacksize,
            code.co_flags,     codestring,          code.co_consts,
            code.co_names,     code.co_varnames,    code.co_filename,
            code.co_name,      code.co_firstlineno, code.co_lnotab,
            code.co_freevars,  code.co_cellvars
        ]
    
        try:
            args.insert(1, code.co_kwonlyargcount)  # PY3
        except AttributeError:
            pass
    
        return types.CodeType(*args)


def _parse_instructions(code):
    extended_arg = 0
    extended_arg_offset = None
    pos = 0

    while pos < len(code):
        offset = pos
        if extended_arg_offset is not None:
            offset = extended_arg_offset

        opcode = struct.unpack_from('B', code, pos)[0]
        pos += 1

        oparg = None
        if opcode >= _BYTECODE.have_argument:
            oparg = extended_arg | _BYTECODE.argument.unpack_from(code, pos)[0]
            pos += _BYTECODE.argument.size

            if opcode == dis.EXTENDED_ARG:
                extended_arg = oparg << _BYTECODE.argument_bits
                extended_arg_offset = offset
                continue

        extended_arg = 0
        extended_arg_offset = None
        yield (dis.opname[opcode], oparg, offset)

def _get_instruction_size(opname, oparg=0):
    size = 1
    
    extended_arg = oparg >> _BYTECODE.argument_bits
    if extended_arg != 0:
        size += _get_instruction_size('EXTENDED_ARG', extended_arg)
        oparg &= (1 << _BYTECODE.argument_bits) - 1
    
    opcode = dis.opmap[opname]
    if opcode >= _BYTECODE.have_argument:    
        size += _BYTECODE.argument.size
        
    return size   

def _get_instructions_size(ops):
    size = 0
    for op in ops:
        if isinstance(op, str):
            size += _get_instruction_size(op)
        else:
            size += _get_instruction_size(*op) 
    return size

def _write_instruction(buf, pos, opname, oparg=0):
    extended_arg = oparg >> _BYTECODE.argument_bits
    if extended_arg != 0:
        pos = _write_instruction(buf, pos, 'EXTENDED_ARG', extended_arg)
        oparg &= (1 << _BYTECODE.argument_bits) - 1

    opcode = dis.opmap[opname]
    buf[pos] = opcode
    pos += 1

    if opcode >= _BYTECODE.have_argument:
        _BYTECODE.argument.pack_into(buf, pos, oparg)
        pos += _BYTECODE.argument.size

    return pos

def _write_instructions(buf, pos, ops):
    for op in ops:
        if isinstance(op, str):
            pos = _write_instruction(buf, pos, op)
        else:
            pos = _write_instruction(buf, pos, *op)
    return pos

def _find_labels_and_gotos(code):
    labels = {}
    gotos = []

    block_stack = []
    block_counter = 0
    block_exits = []

    opname1 = oparg1 = offset1 = None
    opname2 = oparg2 = offset2 = None
    opname3 = oparg3 = offset3 = None

    for opname4, oparg4, offset4 in _parse_instructions(code.co_code):
        if opname1 in ('LOAD_GLOBAL', 'LOAD_NAME'):
            if opname2 == 'LOAD_ATTR' and opname3 == 'POP_TOP':
                name = code.co_names[oparg1]
                if name == 'label':
                    labels[oparg2] = (offset1,
                                      offset4,
                                      tuple(block_stack))
                elif name == 'goto':
                    gotos.append((offset1,
                                  offset4,
                                  oparg2,
                                  tuple(block_stack)))
        elif opname1 in ('SETUP_LOOP',
                         'SETUP_EXCEPT', 'SETUP_FINALLY',
                         'SETUP_WITH', 'SETUP_ASYNC_WITH'):
            block_counter += 1
            block_stack.append((opname1, block_counter))
        elif not _BYTECODE.has_loop_blocks and opname1 == 'FOR_ITER':
            block_counter += 1
            block_stack.append((opname1, block_counter))
            block_exits.append(offset1 + oparg1)
        elif opname1 == 'POP_BLOCK' and block_stack:
            block_stack.pop()
        elif block_exits and offset1 == block_exits[-1] and block_stack:
            block_stack.pop()
            block_exits.pop()

        opname1, oparg1, offset1 = opname2, oparg2, offset2
        opname2, oparg2, offset2 = opname3, oparg3, offset3
        opname3, oparg3, offset3 = opname4, oparg4, offset4

    return labels, gotos


def _inject_nop_sled(buf, pos, end):
    while pos < end:
        pos = _write_instruction(buf, pos, 'NOP')


def _patch_code(code):
    new_code = _patched_code_cache.get(code)
    if new_code is not None:
        return new_code
    
    labels, gotos = _find_labels_and_gotos(code)
    buf = array.array('B', code.co_code)

    for pos, end, _ in labels.values():
        _inject_nop_sled(buf, pos, end)

    for pos, end, label, origin_stack in gotos:
        try:
            _, target, target_stack = labels[label]
        except KeyError:
            raise SyntaxError('Unknown label {0!r}'.format(code.co_names[label]))

        target_depth = len(target_stack)
        if origin_stack[:target_depth] != target_stack:
            raise SyntaxError('Jump into different block')
        
        ops = []
        for block, _ in reversed(origin_stack[target_depth:]):
            if block == 'FOR_ITER':
                ops.append('POP_TOP')
            else:
                ops.append('POP_BLOCK')
                if __pypy__ and block == 'SETUP_FINALLY': # pypy 3.6 keep a block around until END_FINALLY; python 3.8 reuses SETUP_FINALLY for SETUP_EXCEPT. What will pypy 3.8 do?
                    ops.append(('LOAD_CONST', code.co_consts.index(None)))
                    ops.append('END_FINALLY')
        ops.append(('JUMP_ABSOLUTE', target // _BYTECODE.jump_unit))
        
        if pos + _get_instructions_size(ops) > end:
            # not enough space, add code at buffer end and jump there & back
            buf_end = len(buf)
            
            go_to_end_ops = [('JUMP_ABSOLUTE', buf_end // _BYTECODE.jump_unit)]
            
            if pos + _get_instructions_size(go_to_end_ops) > end:
                raise SyntaxError('Goto in an incredibly huge function') # not sure if reachable
            
            pos = _write_instructions(buf, pos, go_to_end_ops)
            _inject_nop_sled(buf, pos, end)
            
            ops.append(('JUMP_ABSOLUTE', end // _BYTECODE.jump_unit))
            
            buf.extend([0] * _get_instructions_size(ops))
            _write_instructions(buf, buf_end, ops)
            
        else:
            pos = _write_instructions(buf, pos, ops)
            _inject_nop_sled(buf, pos, end)

    new_code = _make_code(code, _array_to_bytes(buf))
    
    _patched_code_cache[code] = new_code
    return new_code


def with_goto(func_or_code):
    if isinstance(func_or_code, types.CodeType):
        return _patch_code(func_or_code)

    return functools.update_wrapper(
        types.FunctionType(
            _patch_code(func_or_code.__code__),
            func_or_code.__globals__,
            func_or_code.__name__,
            func_or_code.__defaults__,
            func_or_code.__closure__,
        ),
        func_or_code
    )
