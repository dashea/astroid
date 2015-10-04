# copyright 2003-2013 LOGILAB S.A. (Paris, FRANCE), all rights reserved.
# contact http://www.logilab.fr/ -- mailto:contact@logilab.fr
#
# This file is part of astroid.
#
# astroid is free software: you can redistribute it and/or modify it
# under the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 2.1 of the License, or (at your
# option) any later version.
#
# astroid is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License
# for more details.
#
# You should have received a copy of the GNU Lesser General Public License along
# with astroid. If not, see <http://www.gnu.org/licenses/>.
"""Module for some node classes. More nodes in scoped_nodes.py
"""

import abc
import warnings

import lazy_object_proxy
import six

from astroid import bases
from astroid import context as contextmod
from astroid import decorators
from astroid import exceptions
from astroid import mixins
from astroid import util
from astroid import zipper


BUILTINS = six.moves.builtins.__name__


def unpack_infer(stmt, context=None):
    """recursively generate nodes inferred by the given statement.
    If the inferred value is a list or a tuple, recurse on the elements
    """
    if isinstance(stmt, (List, Tuple)):
        for elt in stmt.elts:
            for inferred_elt in unpack_infer(elt, context):
                yield inferred_elt
        return
    # if inferred is a final node, return it and stop
    inferred = next(stmt.infer(context))
    if inferred is stmt:
        yield inferred
        return
    # else, infer recursivly, except YES object that should be returned as is
    for inferred in stmt.infer(context):
        if inferred is util.YES:
            yield inferred
        else:
            for inf_inf in unpack_infer(inferred, context):
                yield inf_inf


def are_exclusive(stmt1, stmt2, exceptions=None):
    """return true if the two given statements are mutually exclusive

    `exceptions` may be a list of exception names. If specified, discard If
    branches and check one of the statement is in an exception handler catching
    one of the given exceptions.

    algorithm :
     1) index stmt1's parents
     2) climb among stmt2's parents until we find a common parent
     3) if the common parent is a If or TryExcept statement, look if nodes are
        in exclusive branches
    """
    # index stmt1's parents
    stmt1_parents = {}
    children = {}
    node = stmt1.parent
    previous = stmt1
    while node:
        stmt1_parents[node] = 1
        children[node] = previous
        previous = node
        node = node.parent
    # climb among stmt2's parents until we find a common parent
    node = stmt2.parent
    previous = stmt2
    while node:
        if node in stmt1_parents:
            # if the common parent is a If or TryExcept statement, look if
            # nodes are in exclusive branches
            if isinstance(node, If) and exceptions is None:
                if (node.locate_child(previous)[1]
                        is not node.locate_child(children[node])[1]):
                    return True
            elif isinstance(node, TryExcept):
                c2attr, c2node = node.locate_child(previous)
                c1attr, c1node = node.locate_child(children[node])
                if c1node is not c2node:
                    if ((c2attr == 'body'
                         and c1attr == 'handlers'
                         and children[node].catch(exceptions)) or
                            (c2attr == 'handlers' and c1attr == 'body' and previous.catch(exceptions)) or
                            (c2attr == 'handlers' and c1attr == 'orelse') or
                            (c2attr == 'orelse' and c1attr == 'handlers')):
                        return True
                elif c2attr == 'handlers' and c1attr == 'handlers':
                    return previous is not children[node]
            return False
        previous = node
        node = node.parent
    return False


def _container_getitem(instance, elts, index):
    """Get a slice or an item, using the given *index*, for the given sequence."""
    if isinstance(index, slice):
        new_cls = instance.__class__()
        new_cls.elts = elts[index]
        new_cls.parent = instance.parent
        return new_cls
    else:
        return elts[index]


@six.add_metaclass(abc.ABCMeta)
class _BaseContainer(mixins.ParentAssignTypeMixin,
                     bases.NodeNG,
                     bases.Instance):
    """Base class for Set, FrozenSet, Tuple and List."""

    _astroid_fields = ('elts',)
    elts = zipper.NodeSequence()

    # def __init__(self, lineno=None, col_offset=None, parent=None):
    #     self.elts = []
    #     super(_BaseContainer, self).__init__(lineno, col_offset, parent)

    # def postinit(self, elts):
    #     self.elts = elts

    @classmethod
    def from_constants(cls, elts=None):
        # pylint: disable=abstract-class-instantiated; False positive on Pylint #627.
        node = cls()
        if elts is None:
            node.elts = zipper.NodeSequence()
        else:
            node.elts = zipper.NodeSequence(const_factory(e) for e in elts)
        return node

    def itered(self):
        return self.elts

    def bool_value(self):
        return bool(self.elts)

    @abc.abstractmethod
    def pytype(self):
        pass


class LookupMixIn(object):
    """Mixin looking up a name in the right scope
    """

    def lookup(self, name):
        """lookup a variable name

        return the scope node and the list of assignments associated to the
        given name according to the scope where it has been found (locals,
        globals or builtin)

        The lookup is starting from self's scope. If self is not a frame itself
        and the name is found in the inner frame locals, statements will be
        filtered to remove ignorable statements according to self's location
        """
        return self.scope().scope_lookup(self, name)

    def ilookup(self, name):
        """inferred lookup

        return an iterator on inferred values of the statements returned by
        the lookup method
        """
        frame, stmts = self.lookup(name)
        context = contextmod.InferenceContext()
        return bases._infer_stmts(stmts, context, frame)

    def _filter_stmts(self, stmts, frame, offset):
        """filter statements to remove ignorable statements.

        If self is not a frame itself and the name is found in the inner
        frame locals, statements will be filtered to remove ignorable
        statements according to self's location
        """
        # if offset == -1, my actual frame is not the inner frame but its parent
        #
        # class A(B): pass
        #
        # we need this to resolve B correctly
        if offset == -1:
            myframe = self.frame().parent.frame()
        else:
            myframe = self.frame()
            # If the frame of this node is the same as the statement
            # of this node, then the node is part of a class or
            # a function definition and the frame of this node should be the
            # the upper frame, not the frame of the definition.
            # For more information why this is important,
            # see Pylint issue #295.
            # For example, for 'b', the statement is the same
            # as the frame / scope:
            #
            # def test(b=1):
            #     ...

            if self.statement() is myframe and myframe.parent:
                myframe = myframe.parent.frame()
        if not myframe is frame or self is frame:
            return stmts
        mystmt = self.statement()
        # line filtering if we are in the same frame
        #
        # take care node may be missing lineno information (this is the case for
        # nodes inserted for living objects)
        if myframe is frame and mystmt.fromlineno is not None:
            assert mystmt.fromlineno is not None, mystmt
            mylineno = mystmt.fromlineno + offset
        else:
            # disabling lineno filtering
            mylineno = 0
        _stmts = []
        _stmt_parents = []
        for node in stmts:
            stmt = node.statement()
            # line filtering is on and we have reached our location, break
            if mylineno > 0 and stmt.fromlineno > mylineno:
                break
            assert hasattr(node, 'assign_type'), (node, node.scope(),
                                                  node.scope().locals)
            assign_type = node.assign_type()

            if node.has_base(self):
                break

            _stmts, done = assign_type._get_filtered_stmts(self, node, _stmts, mystmt)
            if done:
                break

            optional_assign = assign_type.optional_assign
            if optional_assign and assign_type.parent_of(self):
                # we are inside a loop, loop var assigment is hidding previous
                # assigment
                _stmts = [node]
                _stmt_parents = [stmt.parent]
                continue

            # XXX comment various branches below!!!
            try:
                pindex = _stmt_parents.index(stmt.parent)
            except ValueError:
                pass
            else:
                # we got a parent index, this means the currently visited node
                # is at the same block level as a previously visited node
                if _stmts[pindex].assign_type().parent_of(assign_type):
                    # both statements are not at the same block level
                    continue
                # if currently visited node is following previously considered
                # assignement and both are not exclusive, we can drop the
                # previous one. For instance in the following code ::
                #
                #   if a:
                #     x = 1
                #   else:
                #     x = 2
                #   print x
                #
                # we can't remove neither x = 1 nor x = 2 when looking for 'x'
                # of 'print x'; while in the following ::
                #
                #   x = 1
                #   x = 2
                #   print x
                #
                # we can remove x = 1 when we see x = 2
                #
                # moreover, on loop assignment types, assignment won't
                # necessarily be done if the loop has no iteration, so we don't
                # want to clear previous assigments if any (hence the test on
                # optional_assign)
                if not (optional_assign or are_exclusive(_stmts[pindex], node)):
                    del _stmt_parents[pindex]
                    del _stmts[pindex]
            if isinstance(node, AssignName):
                if not optional_assign and stmt.parent is mystmt.parent:
                    _stmts = []
                    _stmt_parents = []
            elif isinstance(node, DelName):
                _stmts = []
                _stmt_parents = []
                continue
            if not are_exclusive(self, node):
                _stmts.append(node)
                _stmt_parents.append(stmt.parent)
        return _stmts


# Name classes

class AssignName(LookupMixIn, mixins.ParentAssignTypeMixin, bases.NodeNG):
    """class representing an AssignName node"""
    _other_fields = ('name',)

    # def __init__(self, name=None, lineno=None, col_offset=None, parent=None):
    #     self.name = name
    #     super(AssignName, self).__init__(lineno, col_offset, parent)


class DelName(LookupMixIn, mixins.ParentAssignTypeMixin, bases.NodeNG):
    """class representing a DelName node"""
    _other_fields = ('name',)

    # def __init__(self, name=None, lineno=None, col_offset=None, parent=None):
    #     self.name = name
    #     super(DelName, self).__init__(lineno, col_offset, parent)


class Name(LookupMixIn, bases.NodeNG):
    """class representing a Name node"""
    _other_fields = ('name',)

    # def __init__(self, name=None, lineno=None, col_offset=None, parent=None):
    #     self.name = name
    #     super(Name, self).__init__(lineno, col_offset, parent)


class Arguments(mixins.AssignTypeMixin, bases.NodeNG):
    """class representing an Arguments node"""
    if six.PY3:
        # Python 3.4+ uses a different approach regarding annotations,
        # each argument is a new class, _ast.arg, which exposes an
        # 'annotation' attribute. In astroid though, arguments are exposed
        # as is in the Arguments node and the only way to expose annotations
        # is by using something similar with Python 3.3:
        #  - we expose 'varargannotation' and 'kwargannotation' of annotations
        #    of varargs and kwargs.
        #  - we expose 'annotation', a list with annotations for
        #    for each normal argument. If an argument doesn't have an
        #    annotation, its value will be None.

        _astroid_fields = ('args', 'defaults', 'kwonlyargs',
                           'kw_defaults', 'annotations', 'varargannotation',
                           'kwargannotation')
        varargannotation = None
        kwargannotation = None
        kwonlyargs = zipper.NodeSequence()
        kw_defaults = zipper.NodeSequence()
        annotations = zipper.NodeSequence()
    else:
        _astroid_fields = ('args', 'defaults', 'kwonlyargs', 'kw_defaults')
        _other_fields = ('vararg', 'kwarg')
    args = zipper.NodeSequence()
    defaults = zipper.NodeSequence()
    vararg = None
    kwarg = None

    # def __init__(self, vararg=None, kwarg=None, parent=None):
    #     self.vararg = vararg
    #     self.kwarg = kwarg
    #     self.parent = parent
    #     self.args = []
    #     self.defaults = []
    #     self.kwonlyargs = []
    #     self.kw_defaults = []
    #     self.annotations = []

    # def postinit(self, args, defaults, kwonlyargs, kw_defaults,
    #              annotations, varargannotation=None, kwargannotation=None):
    #     self.args = args
    #     self.defaults = defaults
    #     self.kwonlyargs = kwonlyargs
    #     self.kw_defaults = kw_defaults
    #     self.annotations = annotations
    #     self.varargannotation = varargannotation
    #     self.kwargannotation = kwargannotation

    def _infer_name(self, frame, name):
        if self.parent is frame:
            return name
        return None

    @decorators.cachedproperty
    def fromlineno(self):
        lineno = super(Arguments, self).fromlineno
        return max(lineno, self.parent.fromlineno or 0)

    def format_args(self):
        """return arguments formatted as string"""
        result = []
        if self.args:
            result.append(
                _format_args(self.args, self.defaults,
                             getattr(self, 'annotations', None))
            )
        if self.vararg:
            result.append('*%s' % self.vararg)
        if self.kwonlyargs:
            if not self.vararg:
                result.append('*')
            result.append(_format_args(self.kwonlyargs, self.kw_defaults))
        if self.kwarg:
            result.append('**%s' % self.kwarg)
        return ', '.join(result)

    def default_value(self, argname):
        """return the default value for an argument

        :raise `NoDefault`: if there is no default value defined
        """
        i = _find_arg(argname, self.args)[0]
        if i is not None:
            idx = i - (len(self.args) - len(self.defaults))
            if idx >= 0:
                return self.defaults[idx]
        i = _find_arg(argname, self.kwonlyargs)[0]
        if i is not None and self.kw_defaults[i] is not None:
            return self.kw_defaults[i]
        raise exceptions.NoDefault()

    def is_argument(self, name):
        """return True if the name is defined in arguments"""
        if name == self.vararg:
            return True
        if name == self.kwarg:
            return True
        return self.find_argname(name, True)[1] is not None

    def find_argname(self, argname, rec=False):
        """return index and Name node with given name"""
        if self.args: # self.args may be None in some cases (builtin function)
            return _find_arg(argname, self.args, rec)
        return None, None

    def get_children(self):
        """override get_children to skip over None elements in kw_defaults"""
        for child in super(Arguments, self).get_children():
            if child is not None:
                yield child


def _find_arg(argname, args, rec=False):
    for i, arg in enumerate(args):
        if isinstance(arg, Tuple):
            if rec:
                found = _find_arg(argname, arg.elts)
                if found[0] is not None:
                    return found
        elif arg.name == argname:
            return i, arg
    return None, None


def _format_args(args, defaults=None, annotations=None):
    values = []
    if args is None:
        return ''
    if annotations is None:
        annotations = []
    if defaults is not None:
        default_offset = len(args) - len(defaults)
    packed = six.moves.zip_longest(args, annotations)
    for i, (arg, annotation) in enumerate(packed):
        if isinstance(arg, Tuple):
            values.append('(%s)' % _format_args(arg.elts))
        else:
            argname = arg.name
            if annotation is not None:
                argname += ':' + annotation.as_string()
            values.append(argname)

            if defaults is not None and i >= default_offset:
                if defaults[i-default_offset] is not None:
                    values[-1] += '=' + defaults[i-default_offset].as_string()
    return ', '.join(values)


class AssignAttr(mixins.ParentAssignTypeMixin, bases.NodeNG):
    """class representing an AssignAttr node"""
    _astroid_fields = ('expr',)
    _other_fields = ('attrname',)
    attrname = None
    expr = None

    # def __init__(self, attrname=None, lineno=None, col_offset=None, parent=None):
    #     self.attrname = attrname
    #     super(AssignAttr, self).__init__(lineno, col_offset, parent)

    # def postinit(self, expr=None):
    #     self.expr = expr


class Assert(bases.Statement):
    """class representing an Assert node"""
    _astroid_fields = ('test', 'fail',)
    test = None
    fail = None

    # def postinit(self, test=None, fail=None):
    #     self.fail = fail
    #     self.test = test


class Assign(mixins.AssignTypeMixin, bases.Statement):
    """class representing an Assign node"""
    _astroid_fields = ('targets', 'value',)
    targets = None
    value = None

    # def postinit(self, targets=None, value=None):
    #     self.targets = targets
    #     self.value = value


class AugAssign(mixins.AssignTypeMixin, bases.Statement):
    """class representing an AugAssign node"""
    _astroid_fields = ('target', 'value')
    _other_fields = ('op',)
    target = None
    value = None

    # def __init__(self, op=None, lineno=None, col_offset=None, parent=None):
    #     self.op = op
    #     super(AugAssign, self).__init__(lineno, col_offset, parent)

    # def postinit(self, target=None, value=None):
    #     self.target = target
    #     self.value = value

    # This is set by inference.py
    def _infer_augassign(self, context=None):
        raise NotImplementedError

    def type_errors(self, context=None):
        """Return a list of TypeErrors which can occur during inference.

        Each TypeError is represented by a :class:`BinaryOperationError`,
        which holds the original exception.
        """
        try:
            results = self._infer_augassign(context=context)
            return [result for result in results
                    if isinstance(result, exceptions.BinaryOperationError)]
        except exceptions.InferenceError:
            return []


class Repr(bases.NodeNG):
    """class representing a Repr node"""
    _astroid_fields = ('value',)
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class BinOp(bases.NodeNG):
    """class representing a BinOp node"""
    _astroid_fields = ('left', 'right')
    _other_fields = ('op',)
    left = None
    right = None
    op = None

    # def __init__(self, op=None, lineno=None, col_offset=None, parent=None):
    #     self.op = op
    #     super(BinOp, self).__init__(lineno, col_offset, parent)

    # def postinit(self, left=None, right=None):
    #     self.left = left
    #     self.right = right

    # This is set by inference.py
    def _infer_binop(self, context=None):
        raise NotImplementedError

    def type_errors(self, context=None):
        """Return a list of TypeErrors which can occur during inference.

        Each TypeError is represented by a :class:`BinaryOperationError`,
        which holds the original exception.
        """
        try:
            results = self._infer_binop(context=context)
            return [result for result in results
                    if isinstance(result, exceptions.BinaryOperationError)]
        except exceptions.InferenceError:
            return []


class BoolOp(bases.NodeNG):
    """class representing a BoolOp node"""
    _astroid_fields = ('values',)
    _other_fields = ('op',)
    values = None
    op = None

    # def __init__(self, op=None, lineno=None, col_offset=None, parent=None):
    #     self.op = op
    #     super(BoolOp, self).__init__(lineno, col_offset, parent)

    # def postinit(self, values=None):
    #     self.values = values


class Break(bases.Statement):
    """class representing a Break node"""


class Call(bases.NodeNG):
    """class representing a Call node"""
    _astroid_fields = ('func', 'args', 'keywords')
    func = None
    args = None
    keywords = None

    # def postinit(self, func=None, args=None, keywords=None):
    #     self.func = func
    #     self.args = args
    #     self.keywords = keywords

    @property
    def starargs(self):
        args = self.args or []
        return [arg for arg in args if isinstance(arg, Starred)]

    @property
    def kwargs(self):
        keywords = self.keywords or []
        return [keyword for keyword in keywords if keyword.arg is None]


class Compare(bases.NodeNG):
    """class representing a Compare node"""
    _astroid_fields = ('left', 'ops',)
    left = None
    ops = None

    # def postinit(self, left=None, ops=None):
    #     self.left = left
    #     self.ops = ops

    def get_children(self):
        """override get_children for tuple fields"""
        yield self.left
        for _, comparator in self.ops:
            yield comparator # we don't want the 'op'

    def last_child(self):
        """override last_child"""
        # XXX maybe if self.ops:
        return self.ops[-1][1]
        #return self.left


class Comprehension(bases.NodeNG):
    """class representing a Comprehension node"""
    _astroid_fields = ('target', 'iter', 'ifs')
    target = None
    iter = None
    ifs = None

    # def __init__(self, parent=None):
    #     self.parent = parent

    # def postinit(self, target=None, iter=None, ifs=None):
    #     self.target = target
    #     self.iter = iter
    #     self.ifs = ifs

    optional_assign = True
    def assign_type(self):
        return self

    def ass_type(self):
        warnings.warn('%s.ass_type() is deprecated and slated for removal'
                      'in astroid 2.0, use %s.assign_type() instead.'
                      % (type(self).__name__, type(self).__name__),
                      PendingDeprecationWarning, stacklevel=2)
        return self.assign_type()

    def _get_filtered_stmts(self, lookup_node, node, stmts, mystmt):
        """method used in filter_stmts"""
        if self is mystmt:
            if isinstance(lookup_node, (Const, Name)):
                return [lookup_node], True

        elif self.statement() is mystmt:
            # original node's statement is the assignment, only keeps
            # current node (gen exp, list comp)

            return [node], True

        return stmts, False


class Const(bases.NodeNG, bases.Instance):
    """represent a constant node like num, str, bool, None, bytes"""
    _other_fields = ('value',)
    value = None

    # def __init__(self, value, lineno=None, col_offset=None, parent=None):
    #     self.value = value
    #     super(Const, self).__init__(lineno, col_offset, parent)

    def getitem(self, index, context=None):
        if isinstance(self.value, six.string_types):
            return Const(self.value[index])
        if isinstance(self.value, bytes) and six.PY3:
            # Bytes aren't instances of six.string_types
            # on Python 3. Also, indexing them should return
            # integers.
            return Const(self.value[index])
        raise TypeError('%r (value=%s)' % (self, self.value))

    def has_dynamic_getattr(self):
        return False

    def itered(self):
        if isinstance(self.value, six.string_types):
            return self.value
        raise TypeError()

    def pytype(self):
        return self._proxied.qname()

    def bool_value(self):
        return bool(self.value)


class Continue(bases.Statement):
    """class representing a Continue node"""


class Decorators(bases.NodeNG):
    """class representing a Decorators node"""
    _astroid_fields = ('nodes',)
    nodes = None

    # def postinit(self, nodes):
    #     self.nodes = nodes

    def scope(self):
        # skip the function node to go directly to the upper level scope
        return self.parent.parent.scope()


class DelAttr(mixins.ParentAssignTypeMixin, bases.NodeNG):
    """class representing a DelAttr node"""
    _astroid_fields = ('expr',)
    _other_fields = ('attrname',)
    expr = None
    attrname = None

    # def __init__(self, attrname=None, lineno=None, col_offset=None, parent=None):
    #     self.attrname = attrname
    #     super(DelAttr, self).__init__(lineno, col_offset, parent)

    # def postinit(self, expr=None):
    #     self.expr = expr


class Delete(mixins.AssignTypeMixin, bases.Statement):
    """class representing a Delete node"""
    _astroid_fields = ('targets',)
    targets = None

    # def postinit(self, targets=None):
    #     self.targets = targets


class Dict(bases.NodeNG, bases.Instance):
    """class representing a Dict node"""
    _astroid_fields = ('items',)

    # def __init__(self, lineno=None, col_offset=None, parent=None):
    #     self.items = []
    #     super(Dict, self).__init__(lineno, col_offset, parent)

    # def postinit(self, items):
    #     self.items = items

    @classmethod
    def from_constants(cls, items=None):
        node = cls()
        if items is None:
            node.items = []
        else:
            node.items = zipper.NodeSequence((const_factory(k), const_factory(v))
                                             for k, v in items.items())
        return node

    def pytype(self):
        return '%s.dict' % BUILTINS

    def get_children(self):
        """get children of a Dict node"""
        # overrides get_children
        for key, value in self.items:
            yield key
            yield value

    def last_child(self):
        """override last_child"""
        if self.items:
            return self.items[-1][1]
        return None

    def itered(self):
        return self.items[::2]

    def getitem(self, lookup_key, context=None):
        for key, value in self.items:
            for inferredkey in key.infer(context):
                if inferredkey is util.YES:
                    continue
                if isinstance(inferredkey, Const) \
                        and inferredkey.value == lookup_key:
                    return value
        # This should raise KeyError, but all call sites only catch
        # IndexError. Let's leave it like that for now.
        raise IndexError(lookup_key)

    def bool_value(self):
        return bool(self.items)


class Expr(bases.Statement):
    """class representing a Expr node"""
    _astroid_fields = ('value',)
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class Ellipsis(bases.NodeNG): # pylint: disable=redefined-builtin
    """class representing an Ellipsis node"""

    def bool_value(self):
        return True


class EmptyNode(bases.NodeNG):
    """class representing an EmptyNode node"""


class ExceptHandler(mixins.AssignTypeMixin, bases.Statement):
    """class representing an ExceptHandler node"""
    _astroid_fields = ('type', 'name', 'body',)
    type = None
    name = None
    body = None

    # def postinit(self, type=None, name=None, body=None):
    #     self.type = type
    #     self.name = name
    #     self.body = body

    @decorators.cachedproperty
    def blockstart_tolineno(self):
        if self.name:
            return self.name.tolineno
        elif self.type:
            return self.type.tolineno
        else:
            return self.lineno

    def catch(self, exceptions):
        if self.type is None or exceptions is None:
            return True
        for node in self.type.nodes_of_class(Name):
            if node.name in exceptions:
                return True


class Exec(bases.Statement):
    """class representing an Exec node"""
    _astroid_fields = ('expr', 'globals', 'locals',)
    expr = None
    globals = None
    locals = None

    # def postinit(self, expr=None, globals=None, locals=None):
    #     self.expr = expr
    #     self.globals = globals
    #     self.locals = locals


class ExtSlice(bases.NodeNG):
    """class representing an ExtSlice node"""
    _astroid_fields = ('dims',)
    dims = None

    # def postinit(self, dims=None):
    #     self.dims = dims


class For(mixins.BlockRangeMixIn, mixins.AssignTypeMixin, bases.Statement):
    """class representing a For node"""
    _astroid_fields = ('target', 'iter', 'body', 'orelse',)
    target = None
    iter = None
    body = None
    orelse = None

    # def postinit(self, target=None, iter=None, body=None, orelse=None):
    #     self.target = target
    #     self.iter = iter
    #     self.body = body
    #     self.orelse = orelse

    optional_assign = True
    @decorators.cachedproperty
    def blockstart_tolineno(self):
        return self.iter.tolineno


class AsyncFor(For):
    """Asynchronous For built with `async` keyword."""


class Await(bases.NodeNG):
    """Await node for the `await` keyword."""

    _astroid_fields = ('value', )
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class ImportFrom(mixins.ImportFromMixin, bases.Statement):
    """class representing a ImportFrom node"""
    _other_fields = ('modname', 'names', 'level')
    fromname = None
    names = None
    level = 0

    # def __init__(self, fromname, names, level=0, lineno=None,
    #              col_offset=None, parent=None):
    #     self.modname = fromname
    #     self.names = names
    #     self.level = level
    #     super(ImportFrom, self).__init__(lineno, col_offset, parent)


class Attribute(bases.NodeNG):
    """class representing a Attribute node"""
    _astroid_fields = ('expr',)
    _other_fields = ('attrname',)
    expr = None
    attrname = None

    # def __init__(self, attrname=None, lineno=None, col_offset=None, parent=None):
    #     self.attrname = attrname
    #     super(Attribute, self).__init__(lineno, col_offset, parent)

    # def postinit(self, expr=None):
    #     self.expr = expr


class Global(bases.Statement):
    """class representing a Global node"""
    _other_fields = ('names',)
    names = None

    # def __init__(self, names, lineno=None, col_offset=None, parent=None):
    #     self.names = names
    #     super(Global, self).__init__(lineno, col_offset, parent)

    def _infer_name(self, frame, name):
        return name


class If(mixins.BlockRangeMixIn, bases.Statement):
    """class representing an If node"""
    _astroid_fields = ('test', 'body', 'orelse')
    test = None
    body = None
    orelse = None

    # def postinit(self, test=None, body=None, orelse=None):
    #     self.test = test
    #     self.body = body
    #     self.orelse = orelse

    @decorators.cachedproperty
    def blockstart_tolineno(self):
        return self.test.tolineno

    def block_range(self, lineno):
        """handle block line numbers range for if statements"""
        if lineno == self.body[0].fromlineno:
            return lineno, lineno
        if lineno <= self.body[-1].tolineno:
            return lineno, self.body[-1].tolineno
        return self._elsed_block_range(lineno, self.orelse,
                                       self.body[0].fromlineno - 1)


class IfExp(bases.NodeNG):
    """class representing an IfExp node"""
    _astroid_fields = ('test', 'body', 'orelse')
    test = None
    body = None
    orelse = None

    # def postinit(self, test=None, body=None, orelse=None):
    #     self.test = test
    #     self.body = body
    #     self.orelse = orelse


class Import(mixins.ImportFromMixin, bases.Statement):
    """class representing an Import node"""
    _other_fields = ('names',)

    def __init__(self, names=None, lineno=None, col_offset=None, parent=None):
        self.names = names
        super(Import, self).__init__(lineno, col_offset, parent)


class Index(bases.NodeNG):
    """class representing an Index node"""
    _astroid_fields = ('value',)
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class Keyword(bases.NodeNG):
    """class representing a Keyword node"""
    _astroid_fields = ('value',)
    _other_fields = ('arg',)
    value = None
    arg = None

    # def __init__(self, arg=None, lineno=None, col_offset=None, parent=None):
    #     self.arg = arg
    #     super(Keyword, self).__init__(lineno, col_offset, parent)

    # def postinit(self, value=None):
    #     self.value = value


class List(_BaseContainer):
    """class representing a List node"""

    def pytype(self):
        return '%s.list' % BUILTINS

    def getitem(self, index, context=None):
        return _container_getitem(self, self.elts, index)


class Nonlocal(bases.Statement):
    """class representing a Nonlocal node"""
    _other_fields = ('names',)
    names = zipper.NodeSequence()

    # def __init__(self, names, lineno=None, col_offset=None, parent=None):
    #     self.names = names
    #     super(Nonlocal, self).__init__(lineno, col_offset, parent)

    def _infer_name(self, frame, name):
        return name


class Pass(bases.Statement):
    """class representing a Pass node"""


class Print(bases.Statement):
    """class representing a Print node"""
    _astroid_fields = ('dest', 'values',)
    dest = None
    values = None
    nl = None

    # def __init__(self, nl=None, lineno=None, col_offset=None, parent=None):
    #     self.nl = nl
    #     super(Print, self).__init__(lineno, col_offset, parent)

    # def postinit(self, dest=None, values=None):
    #     self.dest = dest
    #     self.values = values


class Raise(bases.Statement):
    """class representing a Raise node"""
    exc = None
    if six.PY2:
        _astroid_fields = ('exc', 'inst', 'tback')
        inst = None
        tback = None

        # def postinit(self, exc=None, inst=None, tback=None):
        #     self.exc = exc
        #     self.inst = inst
        #     self.tback = tback
    else:
        _astroid_fields = ('exc', 'cause')
        exc = None
        cause = None

        # def postinit(self, exc=None, cause=None):
        #     self.exc = exc
        #     self.cause = cause

    def raises_not_implemented(self):
        if not self.exc:
            return
        for name in self.exc.nodes_of_class(Name):
            if name.name == 'NotImplementedError':
                return True


class Return(bases.Statement):
    """class representing a Return node"""
    _astroid_fields = ('value',)
    value = None

    def postinit(self, value=None):
        self.value = value


class Set(_BaseContainer):
    """class representing a Set node"""

    def pytype(self):
        return '%s.set' % BUILTINS


class Slice(bases.NodeNG):
    """class representing a Slice node"""
    _astroid_fields = ('lower', 'upper', 'step')
    lower = None
    upper = None
    step = None

    # def postinit(self, lower=None, upper=None, step=None):
    #     self.lower = lower
    #     self.upper = upper
    #     self.step = step


class Starred(mixins.ParentAssignTypeMixin, bases.NodeNG):
    """class representing a Starred node"""
    _astroid_fields = ('value',)
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class Subscript(bases.NodeNG):
    """class representing a Subscript node"""
    _astroid_fields = ('value', 'slice')
    value = None
    slice = None

    # def postinit(self, value=None, slice=None):
    #     self.value = value
    #     self.slice = slice


class TryExcept(mixins.BlockRangeMixIn, bases.Statement):
    """class representing a TryExcept node"""
    _astroid_fields = ('body', 'handlers', 'orelse',)
    body = None
    handlers = None
    orelse = None

    # def postinit(self, body=None, handlers=None, orelse=None):
    #     self.body = body
    #     self.handlers = handlers
    #     self.orelse = orelse

    def _infer_name(self, frame, name):
        return name

    def block_range(self, lineno):
        """handle block line numbers range for try/except statements"""
        last = None
        for exhandler in self.handlers:
            if exhandler.type and lineno == exhandler.type.fromlineno:
                return lineno, lineno
            if exhandler.body[0].fromlineno <= lineno <= exhandler.body[-1].tolineno:
                return lineno, exhandler.body[-1].tolineno
            if last is None:
                last = exhandler.body[0].fromlineno - 1
        return self._elsed_block_range(lineno, self.orelse, last)


class TryFinally(mixins.BlockRangeMixIn, bases.Statement):
    """class representing a TryFinally node"""
    _astroid_fields = ('body', 'finalbody',)
    body = None
    finalbody = None

    # def postinit(self, body=None, finalbody=None):
    #     self.body = body
    #     self.finalbody = finalbody

    def block_range(self, lineno):
        """handle block line numbers range for try/finally statements"""
        child = self.body[0]
        # py2.5 try: except: finally:
        if (isinstance(child, TryExcept) and child.fromlineno == self.fromlineno
                and lineno > self.fromlineno and lineno <= child.tolineno):
            return child.block_range(lineno)
        return self._elsed_block_range(lineno, self.finalbody)


class Tuple(_BaseContainer):
    """class representing a Tuple node"""

    def pytype(self):
        return '%s.tuple' % BUILTINS

    def getitem(self, index, context=None):
        return _container_getitem(self, self.elts, index)


class UnaryOp(bases.NodeNG):
    """class representing an UnaryOp node"""
    _astroid_fields = ('operand',)
    _other_fields = ('op',)
    operand = None
    op = None

    # def __init__(self, op=None, lineno=None, col_offset=None, parent=None):
    #     self.op = op
    #     super(UnaryOp, self).__init__(lineno, col_offset, parent)

    # def postinit(self, operand=None):
    #     self.operand = operand

    # This is set by inference.py
    def _infer_unaryop(self, context=None):
        raise NotImplementedError

    def type_errors(self, context=None):
        """Return a list of TypeErrors which can occur during inference.

        Each TypeError is represented by a :class:`UnaryOperationError`,
        which holds the original exception.
        """
        try:
            results = self._infer_unaryop(context=context)
            return [result for result in results
                    if isinstance(result, exceptions.UnaryOperationError)]
        except exceptions.InferenceError:
            return []


class While(mixins.BlockRangeMixIn, bases.Statement):
    """class representing a While node"""
    _astroid_fields = ('test', 'body', 'orelse',)
    test = None
    body = None
    orelse = None

    # def postinit(self, test=None, body=None, orelse=None):
    #     self.test = test
    #     self.body = body
    #     self.orelse = orelse

    @decorators.cachedproperty
    def blockstart_tolineno(self):
        return self.test.tolineno

    def block_range(self, lineno):
        """handle block line numbers range for for and while statements"""
        return self. _elsed_block_range(lineno, self.orelse)


class With(mixins.BlockRangeMixIn, mixins.AssignTypeMixin, bases.Statement):
    """class representing a With node"""
    _astroid_fields = ('items', 'body')
    items = None
    body = None

    # def postinit(self, items=None, body=None):
    #     self.items = items
    #     self.body = body

    @decorators.cachedproperty
    def blockstart_tolineno(self):
        return self.items[-1][0].tolineno

    def get_children(self):
        for expr, var in self.items:
            yield expr
            if var:
                yield var
        for elt in self.body:
            yield elt


class AsyncWith(With):
    """Asynchronous `with` built with the `async` keyword."""


class Yield(bases.NodeNG):
    """class representing a Yield node"""
    _astroid_fields = ('value',)
    value = None

    # def postinit(self, value=None):
    #     self.value = value


class YieldFrom(Yield):
    """ Class representing a YieldFrom node. """


# constants ##############################################################

CONST_CLS = {
    list: List,
    tuple: Tuple,
    dict: Dict,
    set: Set,
    type(None): Const,
    type(NotImplemented): Const,
    }

def _update_const_classes():
    """update constant classes, so the keys of CONST_CLS can be reused"""
    klasses = (bool, int, float, complex, str)
    if six.PY2:
        klasses += (unicode, long)
    klasses += (bytes,)
    for kls in klasses:
        CONST_CLS[kls] = Const
_update_const_classes()


def const_factory(value):
    """return an astroid node for a python value"""
    # XXX we should probably be stricter here and only consider stuff in
    # CONST_CLS or do better treatment: in case where value is not in CONST_CLS,
    # we should rather recall the builder on this value than returning an empty
    # node (another option being that const_factory shouldn't be called with something
    # not in CONST_CLS)
    assert not isinstance(value, bases.NodeNG)
    try:
        return CONST_CLS[value.__class__](value)
    except (KeyError, AttributeError):
        node = EmptyNode()
        node.object = value
        return node


# Backward-compatibility aliases
def _instancecheck(cls, other):
    wrapped = cls.__wrapped__
    other_cls = other.__class__
    is_instance_of = wrapped is other_cls or issubclass(other_cls, wrapped)
    warnings.warn("%r is deprecated and slated for removal in astroid "
                  "2.0, use %r instead" % (cls.__class__.__name__,
                                           wrapped.__name__),
                  PendingDeprecationWarning, stacklevel=2)
    return is_instance_of


def proxy_alias(alias_name, node_type):
    proxy = type(alias_name, (lazy_object_proxy.Proxy,),
                 {'__class__': object.__dict__['__class__'],
                  '__instancecheck__': _instancecheck})
    return proxy(lambda: node_type)

Backquote = proxy_alias('Backquote', Repr)
Discard = proxy_alias('Discard', Expr)
AssName = proxy_alias('AssName', AssignName)
AssAttr = proxy_alias('AssAttr', AssignAttr)
Getattr = proxy_alias('Getattr', Attribute)
CallFunc = proxy_alias('CallFunc', Call)
From = proxy_alias('From', ImportFrom)
