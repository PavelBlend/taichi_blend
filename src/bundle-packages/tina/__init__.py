from .utils import *


@ti.data_oriented
class INode:
    pass


@eval('lambda x: x()')
class A:
    def __init__(self):
        self.nodes = {}

    def __getattr__(self, name):
        if name not in self.nodes:
            raise AttributeError(f'Cannot find any node matches name `{name}`')
        return self.nodes[name].original

    def unregister(self, name):
        if name in self.nodes:
            del self.nodes[name]

    def register(self, cls):
        docs = cls.__doc__.strip().splitlines()

        node_name = None
        inputs = []
        outputs = []
        category = 'uncategorized'
        converter = getattr(cls, 'ns_convert', lambda *x: x)

        for line in docs:
            line = [l.strip() for l in line.split(':', 1)]
            if line[0] == 'Name':
                node_name = line[1].replace(' ', '_')
            if line[0] == 'Inputs':
                inputs = line[1].split()
            if line[0] == 'Output':
                outputs = line[1].split()
            if line[0] == 'Category':
                category = line[1]

        if node_name in self.nodes:
            raise KeyError(f'Node with name `{node_name}` already registered')

        cls.__name__ = node_name

        type2socket = {
                'm': 'meta',
                'f': 'field',
                'cf': 'cached_field',
                'vf': 'vector_field',
                'n': 'callable',
                'x': 'matrix',
                't': 'task',
                'a': 'any',
        }
        type2option = {
                'i': 'int',
                'c': 'float',
                'b': 'bool',
                's': 'str',
                'dt': 'enum',
                'fmt': 'enum',
                'so': 'search_object',
                'i2': 'vec_int_2',
                'i3': 'vec_int_3',
                'c2': 'vec_float_2',
                'c3': 'vec_float_3',
        }
        type2items = {
                'dt': 'float int i8 i16 i32 i64 u8 u16 u32 u64 f32 f64'.split(),
                'fmt': 'npy npy.gz npy.xz png jpg bmp none'.split(),
        }

        class Def:
            pass

        if len(inputs):
            name, type = inputs[-1].split(':', 1)
            if name.startswith('*') and name.endswith('s'):
                name = name[1:-1]
                inputs.pop()
                for i in range(2):
                    inputs.append(f'{name}{i}:{type}')

        lut = []
        omap = []
        iopt, isoc = 0, 0
        for i, arg in enumerate(inputs):
            name, type = arg.split(':', 1)
            if type in type2option:
                option = type2option[type]
                lut.append((True, iopt))
                iopt += 1
                setattr(Def, f'option_{iopt}', (name, option))
                if option == 'enum':
                    items = tuple(type2items[type])
                    setattr(Def, f'items_{iopt}', items)
            else:
                socket = type2socket[type]
                lut.append((False, isoc))
                isoc += 1
                setattr(Def, f'input_{isoc}', (name, socket))

        for i, arg in enumerate(outputs):
            name, type = arg.split(':', 1)
            if type.endswith('%'):
                type = type[:-1]
                omap.append(name)
            else:
                omap.append(None)
            socket = type2socket[type]
            setattr(Def, f'output_{i + 1}', (name, socket))

        def wrapped(self, inputs, options):
            # print('+++', inputs, options)
            args = []
            for isopt, index in lut:
                if isopt:
                    args.append(options[index])
                else:
                    args.append(inputs[index])
            # print('===', cls, args)
            args = converter(*args)
            try:
                ret = cls(*args)
            except Exception as e:
                print(f'Exception while constructing node `{node_name}`!')
                raise e
            rets = []
            for name in omap:
                if name is None:
                    rets.append(ret)
                else:
                    rets.append(getattr(ret, name, NotImplemented))
            # print('---', cls, rets)
            return ret, tuple(rets)

        setattr(Def, 'category', category)
        setattr(Def, 'wrapped', wrapped)
        setattr(Def, 'original', cls)

        self.nodes[node_name] = Def
        self.register_callback(node_name, Def)

        return cls

    def register_callback(self, name, cls):
        pass


from . import make_meta
from .make_meta import Meta, C


class IRun(INode):
    def __init__(self, chain):
        self.chain = chain

    def run(self):
        self.chain.run()
        self._run()

    def _run(self):
        raise NotImplementedError


class ICall:
    def call(self, *args):
        raise NotImplementedError


class IField(INode):
    is_taichi_class = True

    meta = Meta()

    @ti.func
    def _subscript(self, I):
        raise NotImplementedError

    def subscript(self, *indices):
        I = tovector(indices)
        return self._subscript(I)

    @ti.func
    def __iter__(self):
        if ti.static(self.meta.store is not None):
            for I in ti.static(self.meta.store):
                yield I
        else:
            for I in ti.grouped(ti.ndrange(*self.meta.shape)):
                yield I

    @ti.kernel
    def _to_numpy(self, arr: ti.ext_arr()):
        for I in ti.static(self):
            val = self[I]
            if ti.static(not isinstance(val, ti.Matrix)):
                arr[I] = val
            elif ti.static(val.m == 1):
                for j in ti.static(range(val.n)):
                    arr[I, j] = val[j]
            else:
                raise NotImplementedError

    def to_numpy(self):
        shape = tuple(self.meta.shape) + tuple(self.meta.vdims)
        dtype = self.meta.dtype
        if dtype is int:
            dtype = ti.get_runtime().default_ip
        elif dtype is float:
            dtype = ti.get_runtime().default_fp
        dtype = to_numpy_type(dtype)
        arr = np.empty(shape, dtype=dtype)
        self._to_numpy(arr)
        return arr

    @ti.kernel
    def _from_numpy(self, arr: ti.ext_arr()):
        for I in ti.static(self):
            val = ti.static(ti.subscript(self, I))
            if ti.static(not isinstance(val, ti.Matrix)):
                val = arr[I]
            elif ti.static(val.m == 1):
                for j in ti.static(range(val.n)):
                    val[j] = arr[I, j]
            else:
                raise NotImplementedError

    def from_numpy(self, arr):
        assert isinstance(arr, np.ndarray), type(arr)
        shape = tuple(self.meta.shape) + tuple(self.meta.vdims)
        dtype = to_numpy_type(self.meta.dtype)
        assert arr.shape == shape, (arr.shape, shape)
        assert arr.dtype == dtype, (arr.dtype, dtype)
        self._from_numpy(arr)

    def __str__(self):
        return str(self.to_numpy())


from . import get_meta
from .get_meta import FMeta
from . import edit_meta
from .edit_meta import MEdit
from . import specify_meta
from . import field_storage
from .field_storage import Field
from . import dynamic_field
from .dynamic_field import DynamicField
from . import cache_field
from . import double_buffer
from . import bind_source
from . import const_field
from . import uniform_field
from . import flatten_field
from . import disk_frame_cache
from . import clamp_sample
from . import repeat_sample 
from . import boundary_sample
from . import mix_value
from . import lerp_value
from . import map_range
from . import multiply_value
from . import custom_function
from . import vector_component
from . import vector_length
from . import pack_vector
from . import field_index
from . import field_shuffle
from . import field_bilerp
from . import chessboard_texture
from . import random_generator
from . import gaussian_dist
from . import field_laplacian
from . import field_gradient
from . import copy_field
from . import merge_tasks
from . import repeat_task
from . import null_task
from . import canvas_visualize
from . import static_print
from . import physics
from . import render
from .render import IMatrix, mapply


__all__ = ['ti', 'A', 'C', 'V', 'np', 'IRun', 'IField', 'Meta', 'MEdit',
        'Field', 'DynamicField', 'IMatrix', 'ICall', 'FMeta', 'INode',
        'mapply', 'clamp', 'bilerp', 'totuple', 'tovector', 'to_numpy_type']
