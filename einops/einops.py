import functools
import itertools
from collections import OrderedDict
from typing import Tuple, List, Set, Dict

import math

from ._backends import get_backend

_reductions = ('min', 'max', 'sum', 'mean', 'prod')
_ellipsis = '…'  # NB, this is a single unicode symbol. String is used as it is not a list, but can be iterated


def _product(sequence):
    # minimalistic product that works both with numbers and symbols. Supports empty lists
    result = 1
    for element in sequence:
        result *= element
    return result


class EinopsError(RuntimeError):
    """ Runtime error thrown by einops """
    pass


def _reduce_axes(tensor, reduction_type: str, reduced_axes: Tuple[int], backend):
    reduced_axes = tuple(reduced_axes)
    if len(reduced_axes) == 0:
        return tensor
    assert reduction_type in _reductions
    if reduction_type == 'mean':
        if not backend.is_float_type(tensor):
            raise NotImplementedError('reduce_mean is not available for non-floating tensors')
    return backend.reduce(tensor, reduction_type, reduced_axes)


def _optimize_transformation(init_shapes, reduced_axes, axes_reordering, final_shapes):
    # TODO this method is very slow
    assert len(axes_reordering) + len(reduced_axes) == len(init_shapes)
    # joining consecutive axes that will be reduced
    # possibly we can skip this if all backends can optimize this (not sure)
    reduced_axes = tuple(sorted(reduced_axes))
    for i in range(len(reduced_axes) - 1)[::-1]:
        if reduced_axes[i] + 1 == reduced_axes[i + 1]:
            removed_axis = reduced_axes[i + 1]
            removed_length = init_shapes[removed_axis]
            init_shapes = init_shapes[:removed_axis] + init_shapes[removed_axis + 1:]
            init_shapes[removed_axis - 1] *= removed_length
            reduced_axes = reduced_axes[:i + 1] + tuple(axis - 1 for axis in reduced_axes[i + 2:])

    # removing axes that are moved together during reshape
    def build_mapping():
        init_to_final = {}
        for axis in range(len(init_shapes)):
            if axis in reduced_axes:
                init_to_final[axis] = None
            else:
                after_reduction = sum(x is not None for x in init_to_final.values())
                init_to_final[axis] = list(axes_reordering).index(after_reduction)
        return init_to_final

    init_axis_to_final_axis = build_mapping()

    for init_axis in range(len(init_shapes) - 1)[::-1]:
        if init_axis_to_final_axis[init_axis] is None:
            continue
        if init_axis_to_final_axis[init_axis + 1] is None:
            continue
        if init_axis_to_final_axis[init_axis] + 1 == init_axis_to_final_axis[init_axis + 1]:
            removed_axis = init_axis + 1
            removed_length = init_shapes[removed_axis]
            removed_axis_after_reduction = sum(x not in reduced_axes for x in range(removed_axis))

            reduced_axes = tuple(axis if axis < removed_axis else axis - 1 for axis in reduced_axes)
            init_shapes = init_shapes[:removed_axis] + init_shapes[removed_axis + 1:]
            init_shapes[removed_axis - 1] *= removed_length
            old_reordering = axes_reordering
            axes_reordering = []
            for axis in old_reordering:
                if axis == removed_axis_after_reduction:
                    pass
                elif axis < removed_axis_after_reduction:
                    axes_reordering.append(axis)
                else:
                    axes_reordering.append(axis - 1)
            init_axis_to_final_axis = build_mapping()

    return init_shapes, reduced_axes, axes_reordering, final_shapes


class TransformRecipe:
    """
    Recipe describes actual computation pathway.
    Recipe can be applied to a tensor or variable.
    """
    # structure is non-mutable. In future, this can be non-mutable dataclass (python 3.7+)

    def __init__(self,
                 # list of expressions (or just sizes) for elementary axes as they appear in left expression.
                 # this is what (after computing unknown parts) will be a shape after first transposition.
                 # If ellipsis is present, it forms one dimension here (in the right position).
                 elementary_axes_lengths: List,
                 # each dimension in input can help to reconstruct length of one elementary axis
                 # or verify one of dimensions. Each element points to element of elementary_axes_lengths
                 input_composite_axes: List[Tuple[List[int], List[int]]],
                 # indices of axes to be squashed
                 reduced_elementary_axes: Tuple[int],
                 # in which order should axes be reshuffled after reduction
                 axes_permutation: Tuple[int],
                 # at which positions which of elementary axes should appear
                 added_axes: Dict[int, int],
                 # ids of axes as they appear in result, again pointers to elementary_axes_lengths,
                 # only used to infer result dimensions
                 output_composite_axes: List[List[int]],
                 reduction_type: str = 'rearrange',
                 # positions of ellipsis in lhs and rhs of expression
                 ellipsis_positions: Tuple[int, int] = (math.inf, math.inf),
                 ):
        self.elementary_axes_lengths = elementary_axes_lengths
        self.input_composite_axes = input_composite_axes
        self.output_composite_axes = output_composite_axes
        # self.final_axes_grouping_flat = list(itertools.chain(*output_composite_axes))
        self.axes_permutation = axes_permutation
        self.added_axes = added_axes
        self.reduction_type = reduction_type
        # This is redundant information, but more convenient during to use in reconstruction
        self.reduced_elementary_axes = reduced_elementary_axes
        self.ellipsis_positions = ellipsis_positions

    @functools.lru_cache(maxsize=1024)
    def reconstruct_from_shape(self, shape, optimize=False):
        """
        Reconstruct all actual parameters using shape.
        Shape is a tuple that may contain integers, shape symbols (tf, keras, theano) and UnknownSize (keras, mxnet)
        known axes can be integers or symbols, but not Nones.
        """
        axes_lengths = list(self.elementary_axes_lengths)
        if self.ellipsis_positions != (math.inf, math.inf):
            if len(shape) < len(self.input_composite_axes) - 1:
                raise EinopsError('Expected at least {} dimensions, got {}'.format(
                    len(self.input_composite_axes) - 1, len(shape)))
        else:
            if len(shape) != len(self.input_composite_axes):
                raise EinopsError('Expected {} dimensions, got {}'.format(len(self.input_composite_axes), len(shape)))
        for input_axis, (known_axes, unknown_axes) in enumerate(self.input_composite_axes):
            before_ellipsis = input_axis
            after_ellipsis = input_axis + len(shape) - len(self.input_composite_axes)
            if input_axis == self.ellipsis_positions[0]:
                assert len(known_axes) == 0 and len(unknown_axes) == 1
                unknown_axis, = unknown_axes
                ellipsis_shape = shape[before_ellipsis:after_ellipsis + 1]
                if any(d is None for d in ellipsis_shape):
                    raise EinopsError("Couldn't infer shape for one or more axes represented by ellipsis")
                axes_lengths[unknown_axis] = _product(ellipsis_shape)
            else:
                if input_axis < self.ellipsis_positions[0]:
                    length = shape[before_ellipsis]
                else:
                    length = shape[after_ellipsis]
                known_product = 1
                for axis in known_axes:
                    known_product *= axes_lengths[axis]

                if len(unknown_axes) == 0:
                    if isinstance(length, int) and isinstance(known_product, int) and length != known_product:
                        raise EinopsError('Shape mismatch, {} != {}'.format(length, known_product))
                else:
                    if isinstance(length, int) and isinstance(known_product, int) and length % known_product != 0:
                        raise EinopsError("Shape mismatch, can't divide axis of length {} in chunks of {}".format(
                            length, known_product))

                    unknown_axis, = unknown_axes
                    axes_lengths[unknown_axis] = length // known_product

        # at this point all axes_lengths are computed (either have values or variables, but not Nones)

        # TODO more readable expression. and confirm we don't want to deal with ellipsis
        init_shapes = axes_lengths[:len(axes_lengths) - len(self.added_axes)]
        # reduced_axes_lengths = [dim for i, dim in enumerate(axes_lengths) if i not in self.reduced_elementary_axes]
        final_shapes = []
        for output_axis, grouping in enumerate(self.output_composite_axes):
            if output_axis == self.ellipsis_positions[1]:
                final_shapes.extend(ellipsis_shape)
            else:
                lengths = [axes_lengths[elementary_axis] for elementary_axis in grouping]
                final_shapes.append(_product(lengths))
        reduced_axes = self.reduced_elementary_axes
        axes_reordering = self.axes_permutation
        added_axes = {pos: axes_lengths[pos_in_elementary] for pos, pos_in_elementary in self.added_axes.items()}
        if optimize:
            assert len(self.added_axes) == 0
            return _optimize_transformation(init_shapes, reduced_axes, axes_reordering, final_shapes)
        else:
            return init_shapes, reduced_axes, axes_reordering, added_axes, final_shapes

    def apply(self, tensor):
        backend = get_backend(tensor)
        init_shapes, reduced_axes, axes_reordering, added_axes, final_shapes = self.reconstruct_from_shape(
            backend.shape(tensor))
        tensor = backend.reshape(tensor, init_shapes)
        tensor = _reduce_axes(tensor, reduction_type=self.reduction_type, reduced_axes=reduced_axes, backend=backend)
        tensor = backend.transpose(tensor, axes_reordering)
        if len(added_axes) > 0:
            tensor = backend.add_axes(tensor, n_axes=len(axes_reordering) + len(added_axes), pos2len=added_axes)
        return backend.reshape(tensor, final_shapes)


CompositeAxis = List[str]


def parse_expression(expression: str) -> Tuple[Set[str], List[CompositeAxis]]:
    """
    Parses an indexing expression (for a single tensor).
    Checks uniqueness of names, checks usage of '...' (allowed only once)
    Returns set of all used identifiers and a list of axis groups.
    """
    identifiers = set()
    composite_axes = []
    if '.' in expression:
        if '...' not in expression:
            raise EinopsError('Expression may contain dots only inside ellipsis (...)')
        if str.count(expression, '...') != 1 or str.count(expression, '.') != 3:
            raise EinopsError('Expression may contain dots only inside ellipsis (...); only one ellipsis for tensor ')
        expression = expression.replace('...', _ellipsis)

    bracket_group = None

    def add_axis_name(x):
        if x is not None:
            if x in identifiers:
                raise ValueError('Indexing expression contains duplicate dimension "{}"'.format(x))
            identifiers.add(x)
            if bracket_group is None:
                composite_axes.append([x])
            else:
                bracket_group.append(x)

    current_identifier = None
    for char in expression:
        if char in '() ' + _ellipsis:
            add_axis_name(current_identifier)
            current_identifier = None
            if char == _ellipsis:
                if bracket_group is not None:
                    raise EinopsError("Ellipsis can't be used inside the composite axis (inside brackets)")
                composite_axes.append(_ellipsis)
                identifiers.add(_ellipsis)
            elif char == '(':
                if bracket_group is not None:
                    raise EinopsError("Axis composition is one-level (brackets inside brackets not allowed)")
                bracket_group = []
            elif char == ')':
                if bracket_group is None:
                    raise EinopsError('Brackets are not balanced')
                composite_axes.append(bracket_group)
                bracket_group = None
        elif '0' <= char <= '9':
            if current_identifier is None:
                raise EinopsError("Axis name can't start with a digit")
            current_identifier += char
        elif 'a' <= char <= 'z':
            if current_identifier is None:
                current_identifier = char
            else:
                current_identifier += char
        else:
            if 'A' <= char <= 'Z':
                raise EinopsError("Only lower-case latin letters allowed in names, not '{}'".format(char))
            raise EinopsError("Unknown character '{}'".format(char))

    if bracket_group is not None:
        raise EinopsError('Imbalanced parentheses in expression: "{}"'.format(expression))
    add_axis_name(current_identifier)
    return identifiers, composite_axes


def _parse_composite_axis(composite_axis_name: str):
    axes_names = [axis for axis in composite_axis_name.split(' ') if len(axis) > 0]
    for axis in axes_names:
        if axis == '_':
            continue
        assert 'a' <= axis[0] <= 'z'
        for letter in axis:
            assert str.isdigit(letter) or 'a' <= letter <= 'z'
    return axes_names


def _check_elementary_axis_name(name: str) -> bool:
    """
    Valid elementary axes contain only lower latin letters and digits and start with a letter.
    """
    if len(name) == 0:
        return False
    if not 'a' <= name[0] <= 'z':
        return False
    for letter in name:
        if (not letter.isdigit()) and not ('a' <= letter <= 'z'):
            return False
    return True


# TODO parenthesis within brackets
# TODO add logaddexp, std, var, ptp, l1, l2
@functools.lru_cache(256)
def _prepare_transformation_recipe(pattern: str, operation: str, axes_lengths: Tuple[Tuple]) -> TransformRecipe:
    """ Perform initial parsing of pattern and provided supplementary info
    axes_lengths is a tuple of tuples (axis_name, axis_length)
    """
    left, right = pattern.split('->')
    identifiers_left, composite_axes_left = parse_expression(left)
    identifiers_rght, composite_axes_rght = parse_expression(right)

    # checking that both have similar letters
    if operation == 'rearrange':
        difference = set.symmetric_difference(identifiers_left, identifiers_rght)
        if len(difference) > 0:
            raise EinopsError('Identifiers only on one side of expression (should be on both): {}'.format(difference))
    elif operation == 'repeat':
        difference = set.difference(identifiers_left, identifiers_rght)
        if len(difference) > 0:
            raise EinopsError('Unexpected identifiers on the left side of repeat: {}'.format(difference))
        axes_without_size = set.difference(identifiers_rght, {*identifiers_left, *(ax for ax, _ in axes_lengths)})
        if len(axes_without_size) > 0:
            raise EinopsError('Specify sizes for new axes in repeat: {}'.format(axes_without_size))
    elif operation in _reductions:
        difference = set.difference(identifiers_rght, identifiers_left)
        if len(difference) > 0:
            raise EinopsError('Unexpected identifiers on the right side of reduce {}: {}'.format(operation, difference))
    else:
        raise EinopsError('Unknown reduction {}. Expect one of {}.'.format(operation, _reductions))

    # parsing all dimensions to find out lengths
    axis_name2known_length = OrderedDict()
    for composite_axis in composite_axes_left:
        for axis_name in composite_axis:
            axis_name2known_length[axis_name] = None

    # axis_ids_after_first_reshape = range(len(axis_name2known_length))
    # position_lookup_after_reduction = {}

    repeat_axes_names = []
    for axis_name in identifiers_rght:
        if axis_name not in axis_name2known_length:
            axis_name2known_length[axis_name] = None
            repeat_axes_names.append(axis_name)

    axis_name2position = {name: position for position, name in enumerate(axis_name2known_length)}
    reduced_axes = [position for axis, position in axis_name2position.items() if axis not in identifiers_rght]

    for elementary_axis, axis_length in axes_lengths:
        if not _check_elementary_axis_name(elementary_axis):
            raise EinopsError('Invalid name for an axis', elementary_axis)
        if elementary_axis not in axis_name2known_length:
            raise EinopsError('Axis {} is not used in transform'.format(elementary_axis))
        # check that element was not set, this can be deleted
        assert axis_name2known_length[elementary_axis] is None
        axis_name2known_length[elementary_axis] = axis_length

    input_axes_known_unknown = []
    # some of shapes will be inferred later - all information is prepared to know
    for composite_axis in composite_axes_left:
        known = {axis for axis in composite_axis if axis_name2known_length[axis] is not None}
        unknown = {axis for axis in composite_axis if axis_name2known_length[axis] is None}
        if len(unknown) > 1:
            raise EinopsError('Could not infer sizes for {}'.format(unknown))
        assert len(unknown) + len(known) == len(composite_axis)
        input_axes_known_unknown.append(
            ([axis_name2position[axis] for axis in known], [axis_name2position[axis] for axis in unknown]))

    axis_position_after_reduction = {}
    for axis_name in itertools.chain(*composite_axes_left):
        if axis_name in identifiers_rght:
            axis_position_after_reduction[axis_name] = len(axis_position_after_reduction)

    result_axes_grouping = [[axis_name2position[axis] for axis in composite_axis]
                            for composite_axis in composite_axes_rght]
    ordered_axis_right = list(itertools.chain(*composite_axes_rght))
    axes_permutation = tuple(
        axis_position_after_reduction[axis] for axis in ordered_axis_right if axis in identifiers_left)
    added_axes = {i: axis_name2position[axis_name] for i, axis_name in enumerate(ordered_axis_right)
                  if axis_name not in identifiers_left}

    ellipsis_left = math.inf if _ellipsis not in composite_axes_left else composite_axes_left.index(_ellipsis)
    ellipsis_rght = math.inf if _ellipsis not in composite_axes_rght else composite_axes_rght.index(_ellipsis)

    return TransformRecipe(
        elementary_axes_lengths=list(axis_name2known_length.values()),
        input_composite_axes=input_axes_known_unknown,
        reduced_elementary_axes=tuple(reduced_axes),
        axes_permutation=axes_permutation,
        added_axes=added_axes,
        output_composite_axes=result_axes_grouping,
        reduction_type=operation,
        # TODO get rid of ellipses position on right side, put marks directly
        ellipsis_positions=(ellipsis_left, ellipsis_rght)
    )


def reduce(tensor, pattern: str, reduction: str, **axes_lengths: int):
    """
    einops.reduce provides combination of reordering and reduction using reader-friendly notation.
    
    Examples for reduce operation:
    
    >>> x = np.random.randn(100, 32, 64)
    >>> # perform max-reduction on the first axis
    >>> y = reduce(x, 't b c -> b c', 'max')
    >>> # same as previous, but with clearer axes meaning
    >>> y = reduce(x, 'time batch channel -> batch channel', 'max')

    >>> x = np.random.randn(10, 20, 30, 40)
    >>> # 2d max-pooling with kernel size = 2 * 2 for image processing
    >>> y1 = reduce(x, 'b c (h1 h2) (w1 w2) -> b c h1 w1', 'max', h2=2, w2=2)
    >>> # if one wants to go back to the original height and width, depth-to-space trick can be applied
    >>> y2 = rearrange(y1, 'b (c h2 w2) h1 w1 -> b c (h1 h2) (w1 w2)', h2=2, w2=2)
    >>> assert parse_shape(x, 'b _ h w') == parse_shape(y2, 'b _ h w')
    >>> # Adaptive 2d max-pooling to 3 * 4 grid
    >>> reduce(x, 'b c (h1 h2) (w1 w2) -> b c h1 w1', 'max', h1=3, w1=4).shape
    (10, 20, 3, 4)
    >>> # Global average pooling
    >>> reduce(x, 'b c h w -> b c', 'mean').shape
    (10, 20)
    >>> # Subtracting mean over batch for each channel
    >>> y = x - reduce(x, 'b c h w -> () c () ()', 'mean')
    >>> # Subtracting per-image mean for each channel
    >>> y = x - reduce(x, 'b c h w -> b c () ()', 'mean') 
    
    :param tensor: tensor: tensor of any supported library (e.g. numpy.ndarray, tensorflow, pytorch, mxnet.ndarray).
            list of tensors is also accepted, those should be of the same type and shape
    :param pattern: string, reduction pattern
    :param reduction: one of available reductions ('min', 'max', 'sum', 'mean', 'prod'), case-sensitive
    :param axes_lengths: any additional specifications for dimensions
    :return: tensor of the same type as input
    """
    try:
        hashable_axes_lengths = tuple(sorted(axes_lengths.items()))
        recipe = _prepare_transformation_recipe(pattern, reduction, axes_lengths=hashable_axes_lengths)
        return recipe.apply(tensor)
    except EinopsError as e:
        message = ' Error while processing {}-reduction pattern "{}".'.format(reduction, pattern)
        if not isinstance(tensor, list):
            message += '\n Input tensor shape: {}. '.format(get_backend(tensor).shape(tensor))
        else:
            message += '\n Input is list. '
        message += 'Additional info: {}.'.format(axes_lengths)
        raise EinopsError(message + '\n {}'.format(e))


def rearrange(tensor, pattern, **axes_lengths):
    """
    einops.rearrange is a reader-friendly smart element reordering for multidimensional tensors.
    This operation includes functionality of transpose (axes permutation), reshape (view), squeeze, unsqueeze,
    stack, concatenate and other operations.

    Examples for rearrange operation:

    >>> # suppose we have a set of 32 images in "h w c" format (height-width-channel)
    >>> images = [np.random.randn(30, 40, 3) for _ in range(32)]
    >>> # stack along first (batch) axis, output is a single array
    >>> rearrange(images, 'b h w c -> b h w c').shape
    (32, 30, 40, 3)
    >>> # concatenate images along height (vertical axis), 960 = 32 * 30
    >>> rearrange(images, 'b h w c -> (b h) w c').shape
    (960, 40, 3)
    >>> # concatenated images along horizontal axis, 1280 = 32 * 40
    >>> rearrange(images, 'b h w c -> h (b w) c').shape
    (30, 1280, 3)
    >>> # reordered axes to "b c h w" format for deep learning
    >>> rearrange(images, 'b h w c -> b c h w').shape
    (32, 3, 30, 40)
    >>> # flattened each image into a vector, 3600 = 30 * 40 * 3
    >>> rearrange(images, 'b h w c -> b (c h w)').shape
    (32, 3600)
    >>> # split each image into 4 smaller (top-left, top-right, bottom-left, bottom-right), 128 = 32 * 2 * 2
    >>> rearrange(images, 'b (h1 h) (w1 w) c -> (b h1 w1) h w c', h1=2, w1=2).shape
    (128, 15, 20, 3)
    >>> # space-to-depth operation
    >>> rearrange(images, 'b (h h1) (w w1) c -> b h w (c h1 w1)', h1=2, w1=2).shape
    (32, 15, 20, 12)

    :param tensor: tensor of any supported library (e.g. numpy.ndarray, tensorflow, pytorch, mxnet.ndarray).
            list of tensors is also accepted, those should be of the same type and shape
    :param pattern: string, rearrangement pattern
    :param axes_lengths: any additional specifications for dimensions
    :return: tensor of the same type as input. If possible, a view to the original tensor is returned.

    When composing axes, C-order enumeration used (consecutive elements have different last axis)
    Find more examples in einops tutorial.
    """
    if isinstance(tensor, list):
        if len(tensor) == 0:
            raise TypeError("Rearrange can't be applied to an empty list")
        tensor = get_backend(tensor[0]).stack_on_zeroth_dimension(tensor)
    return reduce(tensor, pattern, reduction='rearrange', **axes_lengths)


def repeat(tensor, pattern, **axes_lengths):
    """
    einops.repeat allows reordering elements and repeating them in arbitrary combinations.
    This operation includes functionality of repeat, tile, broadcast functions.

    Examples for repeat operation:
    >>> # a grayscale image (of shape height x width)
    >>> image = np.random.randn(30, 40)
    >>> # change it to RGB format by repeating in each channel
    >>> repeat(image, 'h w -> h w c', c=3).shape
    (30, 40, 3)
    >>> # repeat image 2 times along height (vertical axis)
    >>> repeat(image, 'h w -> (repeat h) w', repeat=2).shape
    (60, 40)
    >>> # repeat image 2 time along height and 3 times along width
    >>> repeat(image, 'h w -> h (repeat w)', repeat=3).shape
    (30, 120)
    >>> # convert each pixel to a small square 2x2. Upsample image by 2x
    >>> repeat(image, 'h w -> (h h2) (w w2)', h2=2, w2=2).shape
    (60, 80)
    >>> # pixelate image first by downsampling by 2x, then upsampling
    >>> downsampled = reduce(image, '(h h2) (w w2) -> h w', 'mean', h2=2, w2=2)
    >>> repeat(downsampled, 'h w -> (h h2) (w w2)', h2=2, w2=2).shape
    (30, 40)

    :param tensor: tensor of any supported library (e.g. numpy.ndarray, tensorflow, pytorch, mxnet.ndarray).
            list of tensors is also accepted, those should be of the same type and shape
    :param pattern: string, rearrangement pattern
    :param axes_lengths: any additional specifications for dimensions
    :return: tensor of the same type as input. If possible, a view to the original tensor is returned.

    When composing axes, C-order enumeration used (consecutive elements have different last axis)
    Find more examples in einops tutorial.
    """
    return reduce(tensor, pattern, reduction='repeat', **axes_lengths)


def parse_shape(x, pattern: str):
    """
    Parse a tensor shape to dictionary mapping axes names to their lengths.
    Use underscore to skip the dimension in parsing.
    >>> x = np.zeros([2, 3, 5, 7])
    >>> parse_shape(x, 'batch _ h w')
    {'batch': 2, 'h': 5, 'w': 7}

    parse_shape output can be used to specify axes_lengths for other operations
    >>> y = np.zeros([700])
    >>> rearrange(y, '(b c h w) -> b c h w', **parse_shape(x, 'b _ h w')).shape
    (2, 10, 5, 7)

    For symbolic frameworks may return symbols, not integers.
    :param x: tensor of any of supported frameworks
    :param pattern: str, space separated names for axes, underscore means skip axis
    :return: dict, maps axes names to their lengths
    """
    names = [elementary_axis for elementary_axis in pattern.split(' ') if len(elementary_axis) > 0]
    shape = get_backend(x).shape(x)
    if len(shape) != len(names):
        raise RuntimeError("Can't parse shape with different number of dimensions: {pattern} {shape}".format(
            pattern=pattern, shape=shape))
    result = {}
    for axis_name, axis_length in zip(names, shape):
        if axis_name != '_':
            result[axis_name] = axis_length
    return result


# this one is probably not needed in the public API
def _enumerate_directions(x):
    """
    For an n-dimensional tensor, returns tensors to enumerate each axis.
    >>> x = np.zeros([2, 3, 4]) # or any other tensor
    >>> i, j, k = _enumerate_directions(x)
    >>> result = i + 2 * j + 3 * k

    result[i, j, k] = i + 2 * j + 3 * k, and also has the same shape as result
    Works very similarly to numpy.ogrid (open indexing grid)
    """
    backend = get_backend(x)
    shape = backend.shape(x)
    result = []
    for axis_id, axis_length in enumerate(shape):
        shape = [1] * len(shape)
        shape[axis_id] = axis_length
        result.append(backend.reshape(backend.arange(0, axis_length), shape))
    return result


def asnumpy(tensor):
    """
    Convert a tensor of an imperative framework (i.e. numpy/cupy/torch/gluon/etc.) to numpy.ndarray

    :param tensor: tensor of any of known imperative framework
    :return: numpy.ndarray, converted to numpy
    """
    return get_backend(tensor).to_numpy(tensor)
