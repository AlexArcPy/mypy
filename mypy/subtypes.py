from typing import List, Optional, Callable, Tuple, Iterator, Set, Union, cast
from contextlib import contextmanager

from mypy.types import (
    Type, AnyType, UnboundType, TypeVisitor, FormalArgument, NoneTyp, function_type,
    Instance, TypeVarType, CallableType, TupleType, TypedDictType, UnionType, Overloaded,
    ErasedType, PartialType, DeletedType, UninhabitedType, TypeType, is_named_instance,
    FunctionLike, TypeOfAny
)
import mypy.applytype
import mypy.constraints
from mypy.erasetype import erase_type
# Circular import; done in the function instead.
# import mypy.solve
from mypy import messages, sametypes
from mypy.nodes import (
    FuncBase, Var, Decorator, OverloadedFuncDef, TypeInfo, CONTRAVARIANT, COVARIANT,
    ARG_POS, ARG_OPT, ARG_NAMED, ARG_NAMED_OPT, ARG_STAR, ARG_STAR2
)
from mypy.maptype import map_instance_to_supertype
from mypy.expandtype import expand_type_by_instance
from mypy.sametypes import is_same_type
from mypy.typestate import TypeState

from mypy import experiments


# Flags for detected protocol members
IS_SETTABLE = 1
IS_CLASSVAR = 2
IS_CLASS_OR_STATIC = 3


TypeParameterChecker = Callable[[Type, Type, int], bool]


def check_type_parameter(lefta: Type, righta: Type, variance: int) -> bool:
    if variance == COVARIANT:
        return is_subtype(lefta, righta, check_type_parameter)
    elif variance == CONTRAVARIANT:
        return is_subtype(righta, lefta, check_type_parameter)
    else:
        return is_equivalent(lefta, righta, check_type_parameter)


def is_subtype(left: Type, right: Type,
               type_parameter_checker: TypeParameterChecker = check_type_parameter,
               *, ignore_pos_arg_names: bool = False,
               ignore_declared_variance: bool = False) -> bool:
    """Is 'left' subtype of 'right'?

    Also consider Any to be a subtype of any type, and vice versa. This
    recursively applies to components of composite types (List[int] is subtype
    of List[Any], for example).

    type_parameter_checker is used to check the type parameters (for example,
    A with B in is_subtype(C[A], C[B]). The default checks for subtype relation
    between the type arguments (e.g., A and B), taking the variance of the
    type var into account.
    """
    if (isinstance(right, AnyType) or isinstance(right, UnboundType)
            or isinstance(right, ErasedType)):
        return True
    elif isinstance(right, UnionType) and not isinstance(left, UnionType):
        # Normally, when 'left' is not itself a union, the only way
        # 'left' can be a subtype of the union 'right' is if it is a
        # subtype of one of the items making up the union.
        is_subtype_of_item = any(is_subtype(left, item, type_parameter_checker,
                                            ignore_pos_arg_names=ignore_pos_arg_names)
                                 for item in right.items)
        # However, if 'left' is a type variable T, T might also have
        # an upper bound which is itself a union. This case will be
        # handled below by the SubtypeVisitor. We have to check both
        # possibilities, to handle both cases like T <: Union[T, U]
        # and cases like T <: B where B is the upper bound of T and is
        # a union. (See #2314.)
        if not isinstance(left, TypeVarType):
            return is_subtype_of_item
        elif is_subtype_of_item:
            return True
        # otherwise, fall through
    return left.accept(SubtypeVisitor(right, type_parameter_checker,
                                      ignore_pos_arg_names=ignore_pos_arg_names,
                                      ignore_declared_variance=ignore_declared_variance))


def is_subtype_ignoring_tvars(left: Type, right: Type) -> bool:
    def ignore_tvars(s: Type, t: Type, v: int) -> bool:
        return True
    return is_subtype(left, right, ignore_tvars)


def is_equivalent(a: Type,
                  b: Type,
                  type_parameter_checker: TypeParameterChecker = check_type_parameter,
                  *,
                  ignore_pos_arg_names: bool = False
                  ) -> bool:
    return (
        is_subtype(a, b, type_parameter_checker, ignore_pos_arg_names=ignore_pos_arg_names)
        and is_subtype(b, a, type_parameter_checker, ignore_pos_arg_names=ignore_pos_arg_names))


class SubtypeVisitor(TypeVisitor[bool]):

    def __init__(self, right: Type,
                 type_parameter_checker: TypeParameterChecker,
                 *, ignore_pos_arg_names: bool = False,
                 ignore_declared_variance: bool = False) -> None:
        self.right = right
        self.check_type_parameter = type_parameter_checker
        self.ignore_pos_arg_names = ignore_pos_arg_names
        self.ignore_declared_variance = ignore_declared_variance

    # visit_x(left) means: is left (which is an instance of X) a subtype of
    # right?

    def visit_unbound_type(self, left: UnboundType) -> bool:
        return True

    def visit_any(self, left: AnyType) -> bool:
        return True

    def visit_none_type(self, left: NoneTyp) -> bool:
        if experiments.STRICT_OPTIONAL:
            return (isinstance(self.right, NoneTyp) or
                    is_named_instance(self.right, 'builtins.object') or
                    isinstance(self.right, Instance) and self.right.type.is_protocol and
                    not self.right.type.protocol_members)
        else:
            return True

    def visit_uninhabited_type(self, left: UninhabitedType) -> bool:
        return True

    def visit_erased_type(self, left: ErasedType) -> bool:
        return True

    def visit_deleted_type(self, left: DeletedType) -> bool:
        return True

    def visit_instance(self, left: Instance) -> bool:
        if left.type.fallback_to_any:
            return True
        right = self.right
        if isinstance(right, TupleType) and right.fallback.type.is_enum:
            return is_subtype(left, right.fallback)
        if isinstance(right, Instance):
            if TypeState.is_cached_subtype_check(left, right):
                return True
            # NOTE: left.type.mro may be None in quick mode if there
            # was an error somewhere.
            if left.type.mro is not None:
                for base in left.type.mro:
                    # TODO: Also pass recursively ignore_declared_variance
                    if base._promote and is_subtype(
                            base._promote, self.right, self.check_type_parameter,
                            ignore_pos_arg_names=self.ignore_pos_arg_names):
                        TypeState.record_subtype_cache_entry(left, right)
                        return True
            rname = right.type.fullname()
            # Always try a nominal check if possible,
            # there might be errors that a user wants to silence *once*.
            if ((left.type.has_base(rname) or rname == 'builtins.object') and
                    not self.ignore_declared_variance):
                # Map left type to corresponding right instances.
                t = map_instance_to_supertype(left, right.type)
                nominal = all(self.check_type_parameter(lefta, righta, tvar.variance)
                              for lefta, righta, tvar in
                              zip(t.args, right.args, right.type.defn.type_vars))
                if nominal:
                    TypeState.record_subtype_cache_entry(left, right)
                return nominal
            if right.type.is_protocol and is_protocol_implementation(left, right):
                return True
            return False
        if isinstance(right, TypeType):
            item = right.item
            if isinstance(item, TupleType):
                item = item.fallback
            if is_named_instance(left, 'builtins.type'):
                return is_subtype(TypeType(AnyType(TypeOfAny.special_form)), right)
            if left.type.is_metaclass():
                if isinstance(item, AnyType):
                    return True
                if isinstance(item, Instance):
                    return is_named_instance(item, 'builtins.object')
        if isinstance(right, CallableType):
            # Special case: Instance can be a subtype of Callable.
            call = find_member('__call__', left, left)
            if call:
                return is_subtype(call, right)
            return False
        else:
            return False

    def visit_type_var(self, left: TypeVarType) -> bool:
        right = self.right
        if isinstance(right, TypeVarType) and left.id == right.id:
            return True
        if left.values and is_subtype(UnionType.make_simplified_union(left.values), right):
            return True
        return is_subtype(left.upper_bound, self.right)

    def visit_callable_type(self, left: CallableType) -> bool:
        right = self.right
        if isinstance(right, CallableType):
            return is_callable_compatible(
                left, right,
                is_compat=is_subtype,
                ignore_pos_arg_names=self.ignore_pos_arg_names)
        elif isinstance(right, Overloaded):
            return all(is_subtype(left, item, self.check_type_parameter,
                                  ignore_pos_arg_names=self.ignore_pos_arg_names)
                       for item in right.items())
        elif isinstance(right, Instance):
            return is_subtype(left.fallback, right,
                              ignore_pos_arg_names=self.ignore_pos_arg_names)
        elif isinstance(right, TypeType):
            # This is unsound, we don't check the __init__ signature.
            return left.is_type_obj() and is_subtype(left.ret_type, right.item)
        else:
            return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if is_named_instance(right, 'typing.Sized'):
                return True
            elif (is_named_instance(right, 'builtins.tuple') or
                  is_named_instance(right, 'typing.Iterable') or
                  is_named_instance(right, 'typing.Container') or
                  is_named_instance(right, 'typing.Sequence') or
                  is_named_instance(right, 'typing.Reversible')):
                if right.args:
                    iter_type = right.args[0]
                else:
                    iter_type = AnyType(TypeOfAny.special_form)
                return all(is_subtype(li, iter_type) for li in left.items)
            elif is_subtype(left.fallback, right, self.check_type_parameter):
                return True
            return False
        elif isinstance(right, TupleType):
            if len(left.items) != len(right.items):
                return False
            for l, r in zip(left.items, right.items):
                if not is_subtype(l, r, self.check_type_parameter):
                    return False
            if not is_subtype(left.fallback, right.fallback, self.check_type_parameter):
                return False
            return True
        else:
            return False

    def visit_typeddict_type(self, left: TypedDictType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            return is_subtype(left.fallback, right, self.check_type_parameter)
        elif isinstance(right, TypedDictType):
            if not left.names_are_wider_than(right):
                return False
            for name, l, r in left.zip(right):
                if not is_equivalent(l, r, self.check_type_parameter):
                    return False
                # Non-required key is not compatible with a required key since
                # indexing may fail unexpectedly if a required key is missing.
                # Required key is not compatible with a non-required key since
                # the prior doesn't support 'del' but the latter should support
                # it.
                #
                # NOTE: 'del' support is currently not implemented (#3550). We
                #       don't want to have to change subtyping after 'del' support
                #       lands so here we are anticipating that change.
                if (name in left.required_keys) != (name in right.required_keys):
                    return False
            # (NOTE: Fallbacks don't matter.)
            return True
        else:
            return False

    def visit_overloaded(self, left: Overloaded) -> bool:
        right = self.right
        if isinstance(right, Instance):
            return is_subtype(left.fallback, right)
        elif isinstance(right, CallableType):
            for item in left.items():
                if is_subtype(item, right, self.check_type_parameter,
                              ignore_pos_arg_names=self.ignore_pos_arg_names):
                    return True
            return False
        elif isinstance(right, Overloaded):
            # Ensure each overload in the right side (the supertype) is accounted for.
            previous_match_left_index = -1
            matched_overloads = set()
            possible_invalid_overloads = set()

            for right_index, right_item in enumerate(right.items()):
                found_match = False

                for left_index, left_item in enumerate(left.items()):
                    subtype_match = is_subtype(left_item, right_item, self.check_type_parameter,
                                               ignore_pos_arg_names=self.ignore_pos_arg_names)

                    # Order matters: we need to make sure that the index of
                    # this item is at least the index of the previous one.
                    if subtype_match and previous_match_left_index <= left_index:
                        if not found_match:
                            # Update the index of the previous match.
                            previous_match_left_index = left_index
                            found_match = True
                            matched_overloads.add(left_item)
                            possible_invalid_overloads.discard(left_item)
                    else:
                        # If this one overlaps with the supertype in any way, but it wasn't
                        # an exact match, then it's a potential error.
                        if (is_callable_compatible(left_item, right_item,
                                    is_compat=is_subtype, ignore_return=True,
                                    ignore_pos_arg_names=self.ignore_pos_arg_names) or
                                is_callable_compatible(right_item, left_item,
                                        is_compat=is_subtype, ignore_return=True,
                                        ignore_pos_arg_names=self.ignore_pos_arg_names)):
                            # If this is an overload that's already been matched, there's no
                            # problem.
                            if left_item not in matched_overloads:
                                possible_invalid_overloads.add(left_item)

                if not found_match:
                    return False

            if possible_invalid_overloads:
                # There were potentially invalid overloads that were never matched to the
                # supertype.
                return False
            return True
        elif isinstance(right, UnboundType):
            return True
        elif isinstance(right, TypeType):
            # All the items must have the same type object status, so
            # it's sufficient to query only (any) one of them.
            # This is unsound, we don't check all the __init__ signatures.
            return left.is_type_obj() and is_subtype(left.items()[0], right)
        else:
            return False

    def visit_union_type(self, left: UnionType) -> bool:
        return all(is_subtype(item, self.right, self.check_type_parameter)
                   for item in left.items)

    def visit_partial_type(self, left: PartialType) -> bool:
        # This is indeterminate as we don't really know the complete type yet.
        raise RuntimeError

    def visit_type_type(self, left: TypeType) -> bool:
        right = self.right
        if isinstance(right, TypeType):
            return is_subtype(left.item, right.item)
        if isinstance(right, CallableType):
            # This is unsound, we don't check the __init__ signature.
            return is_subtype(left.item, right.ret_type)
        if isinstance(right, Instance):
            if right.type.fullname() in ['builtins.object', 'builtins.type']:
                return True
            item = left.item
            if isinstance(item, TypeVarType):
                item = item.upper_bound
            if isinstance(item, Instance):
                metaclass = item.type.metaclass_type
                return metaclass is not None and is_subtype(metaclass, right)
        return False


@contextmanager
def pop_on_exit(stack: List[Tuple[Instance, Instance]],
                left: Instance, right: Instance) -> Iterator[None]:
    stack.append((left, right))
    yield
    stack.pop()


def is_protocol_implementation(left: Instance, right: Instance,
                               proper_subtype: bool = False) -> bool:
    """Check whether 'left' implements the protocol 'right'.

    If 'proper_subtype' is True, then check for a proper subtype.
    Treat recursive protocols by using the 'assuming' structural subtype matrix
    (in sparse representation, i.e. as a list of pairs (subtype, supertype)),
    see also comment in nodes.TypeInfo. When we enter a check for classes
    (A, P), defined as following::

      class P(Protocol):
          def f(self) -> P: ...
      class A:
          def f(self) -> A: ...

    this results in A being a subtype of P without infinite recursion.
    On every false result, we pop the assumption, thus avoiding an infinite recursion
    as well.
    """
    assert right.type.is_protocol
    # We need to record this check to generate protocol fine-grained dependencies.
    TypeState.record_protocol_subtype_check(left.type, right.type)
    assuming = right.type.assuming_proper if proper_subtype else right.type.assuming
    for (l, r) in reversed(assuming):
        if sametypes.is_same_type(l, left) and sametypes.is_same_type(r, right):
            return True
    with pop_on_exit(assuming, left, right):
        for member in right.type.protocol_members:
            # nominal subtyping currently ignores '__init__' and '__new__' signatures
            if member in ('__init__', '__new__'):
                continue
            # The third argument below indicates to what self type is bound.
            # We always bind self to the subtype. (Similarly to nominal types).
            supertype = find_member(member, right, left)
            assert supertype is not None
            subtype = find_member(member, left, left)
            # Useful for debugging:
            # print(member, 'of', left, 'has type', subtype)
            # print(member, 'of', right, 'has type', supertype)
            if not subtype:
                return False
            if not proper_subtype:
                # Nominal check currently ignores arg names
                is_compat = is_subtype(subtype, supertype, ignore_pos_arg_names=True)
            else:
                is_compat = is_proper_subtype(subtype, supertype)
            if not is_compat:
                return False
            if isinstance(subtype, NoneTyp) and isinstance(supertype, CallableType):
                # We want __hash__ = None idiom to work even without --strict-optional
                return False
            subflags = get_member_flags(member, left.type)
            superflags = get_member_flags(member, right.type)
            if IS_SETTABLE in superflags:
                # Check opposite direction for settable attributes.
                if not is_subtype(supertype, subtype):
                    return False
            if (IS_CLASSVAR in subflags) != (IS_CLASSVAR in superflags):
                return False
            if IS_SETTABLE in superflags and IS_SETTABLE not in subflags:
                return False
            # This rule is copied from nominal check in checker.py
            if IS_CLASS_OR_STATIC in superflags and IS_CLASS_OR_STATIC not in subflags:
                return False
    if proper_subtype:
        TypeState.record_proper_subtype_cache_entry(left, right)
    else:
        TypeState.record_subtype_cache_entry(left, right)
    return True


def find_member(name: str, itype: Instance, subtype: Type) -> Optional[Type]:
    """Find the type of member by 'name' in 'itype's TypeInfo.

    Fin the member type after applying type arguments from 'itype', and binding
    'self' to 'subtype'. Return None if member was not found.
    """
    # TODO: this code shares some logic with checkmember.analyze_member_access,
    # consider refactoring.
    info = itype.type
    method = info.get_method(name)
    if method:
        if method.is_property:
            assert isinstance(method, OverloadedFuncDef)
            dec = method.items[0]
            assert isinstance(dec, Decorator)
            return find_node_type(dec.var, itype, subtype)
        return find_node_type(method, itype, subtype)
    else:
        # don't have such method, maybe variable or decorator?
        node = info.get(name)
        if not node:
            v = None
        else:
            v = node.node
        if isinstance(v, Decorator):
            v = v.var
        if isinstance(v, Var):
            return find_node_type(v, itype, subtype)
        if not v and name not in ['__getattr__', '__setattr__', '__getattribute__']:
            for method_name in ('__getattribute__', '__getattr__'):
                # Normally, mypy assumes that instances that define __getattr__ have all
                # attributes with the corresponding return type. If this will produce
                # many false negatives, then this could be prohibited for
                # structural subtyping.
                method = info.get_method(method_name)
                if method and method.info.fullname() != 'builtins.object':
                    getattr_type = find_node_type(method, itype, subtype)
                    if isinstance(getattr_type, CallableType):
                        return getattr_type.ret_type
        if itype.type.fallback_to_any:
            return AnyType(TypeOfAny.special_form)
    return None


def get_member_flags(name: str, info: TypeInfo) -> Set[int]:
    """Detect whether a member 'name' is settable, whether it is an
    instance or class variable, and whether it is class or static method.

    The flags are defined as following:
    * IS_SETTABLE: whether this attribute can be set, not set for methods and
      non-settable properties;
    * IS_CLASSVAR: set if the variable is annotated as 'x: ClassVar[t]';
    * IS_CLASS_OR_STATIC: set for methods decorated with @classmethod or
      with @staticmethod.
    """
    method = info.get_method(name)
    setattr_meth = info.get_method('__setattr__')
    if method:
        # this could be settable property
        if method.is_property:
            assert isinstance(method, OverloadedFuncDef)
            dec = method.items[0]
            assert isinstance(dec, Decorator)
            if dec.var.is_settable_property or setattr_meth:
                return {IS_SETTABLE}
        return set()
    node = info.get(name)
    if not node:
        if setattr_meth:
            return {IS_SETTABLE}
        return set()
    v = node.node
    if isinstance(v, Decorator):
        if v.var.is_staticmethod or v.var.is_classmethod:
            return {IS_CLASS_OR_STATIC}
    # just a variable
    if isinstance(v, Var):
        flags = {IS_SETTABLE}
        if v.is_classvar:
            flags.add(IS_CLASSVAR)
        return flags
    return set()


def find_node_type(node: Union[Var, FuncBase], itype: Instance, subtype: Type) -> Type:
    """Find type of a variable or method 'node' (maybe also a decorated method).
    Apply type arguments from 'itype', and bind 'self' to 'subtype'.
    """
    from mypy.checkmember import bind_self
    if isinstance(node, FuncBase):
        typ = function_type(node,
                            fallback=Instance(itype.type.mro[-1], []))  # type: Optional[Type]
    else:
        typ = node.type
    if typ is None:
        return AnyType(TypeOfAny.from_error)
    # We don't need to bind 'self' for static methods, since there is no 'self'.
    if isinstance(node, FuncBase) or isinstance(typ, FunctionLike) and not node.is_staticmethod:
        assert isinstance(typ, FunctionLike)
        signature = bind_self(typ, subtype)
        if node.is_property:
            assert isinstance(signature, CallableType)
            typ = signature.ret_type
        else:
            typ = signature
    itype = map_instance_to_supertype(itype, node.info)
    typ = expand_type_by_instance(typ, itype)
    return typ


def non_method_protocol_members(tp: TypeInfo) -> List[str]:
    """Find all non-callable members of a protocol."""

    assert tp.is_protocol
    result = []  # type: List[str]
    anytype = AnyType(TypeOfAny.special_form)
    instance = Instance(tp, [anytype] * len(tp.defn.type_vars))

    for member in tp.protocol_members:
        typ = find_member(member, instance, instance)
        if not isinstance(typ, CallableType):
            result.append(member)
    return result


def is_callable_compatible(left: CallableType, right: CallableType,
                           *,
                           is_compat: Callable[[Type, Type], bool],
                           is_compat_return: Optional[Callable[[Type, Type], bool]] = None,
                           ignore_return: bool = False,
                           ignore_pos_arg_names: bool = False,
                           check_args_covariantly: bool = False,
                           allow_partial_overlap: bool = False) -> bool:
    """Is the left compatible with the right, using the provided compatibility check?

    is_compat:
        The check we want to run against the parameters.

    is_compat_return:
        The check we want to run against the return type.
        If None, use the 'is_compat' check.

    check_args_covariantly:
        If true, check if the left's args is compatible with the right's
        instead of the other way around (contravariantly).

        This function is mostly used to check if the left is a subtype of the right which
        is why the default is to check the args contravariantly. However, it's occasionally
        useful to check the args using some other check, so we leave the variance
        configurable.

        For example, when checking the validity of overloads, it's useful to see if
        the first overload alternative has more precise arguments then the second.
        We would want to check the arguments covariantly in that case.

        Note! The following two function calls are NOT equivalent:

            is_callable_compatible(f, g, is_compat=is_subtype, check_args_covariantly=False)
            is_callable_compatible(g, f, is_compat=is_subtype, check_args_covariantly=True)

        The two calls are similar in that they both check the function arguments in
        the same direction: they both run `is_subtype(argument_from_g, argument_from_f)`.

        However, the two calls differ in which direction they check things like
        keyword arguments. For example, suppose f and g are defined like so:

            def f(x: int, *y: int) -> int: ...
            def g(x: int) -> int: ...

        In this case, the first call will succeed and the second will fail: f is a
        valid stand-in for g but not vice-versa.

    allow_partial_overlap:
        By default this function returns True if and only if *all* calls to left are
        also calls to right (with respect to the provided 'is_compat' function).

        If this parameter is set to 'True', we return True if *there exists at least one*
        call to left that's also a call to right.

        In other words, we perform an existential check instead of a universal one;
        we require left to only overlap with right instead of being a subset.

        For example, suppose we set 'is_compat' to some subtype check and compare following:

            f(x: float, y: str = "...", *args: bool) -> str
            g(*args: int) -> str

        This function would normally return 'False': f is not a subtype of g.
        However, we would return True if this parameter is set to 'True': the two
        calls are compatible if the user runs "f_or_g(3)". In the context of that
        specific call, the two functions effectively have signatures of:

            f2(float) -> str
            g2(int) -> str

        Here, f2 is a valid subtype of g2 so we return True.

        Specifically, if this parameter is set this function will:

        -   Ignore optional arguments on either the left or right that have no
            corresponding match.
        -   No longer mandate optional arguments on either side are also optional
            on the other.
        -   No longer mandate that if right has a *arg or **kwarg that left must also
            have the same.

        Note: when this argument is set to True, this function becomes "symmetric" --
        the following calls are equivalent:

            is_callable_compatible(f, g,
                                   is_compat=some_check,
                                   check_args_covariantly=False,
                                   allow_partial_overlap=True)
            is_callable_compatible(g, f,
                                   is_compat=some_check,
                                   check_args_covariantly=True,
                                   allow_partial_overlap=True)

        If the 'some_check' function is also symmetric, the two calls would be equivalent
        whether or not we check the args covariantly.
    """
    if is_compat_return is None:
        is_compat_return = is_compat

    # If either function is implicitly typed, ignore positional arg names too
    if left.implicit or right.implicit:
        ignore_pos_arg_names = True

    # Non-type cannot be a subtype of type.
    if right.is_type_obj() and not left.is_type_obj():
        return False

    # A callable L is a subtype of a generic callable R if L is a
    # subtype of every type obtained from R by substituting types for
    # the variables of R. We can check this by simply leaving the
    # generic variables of R as type variables, effectively varying
    # over all possible values.

    # It's okay even if these variables share ids with generic
    # type variables of L, because generating and solving
    # constraints for the variables of L to make L a subtype of R
    # (below) treats type variables on the two sides as independent.
    if left.variables:
        # Apply generic type variables away in left via type inference.
        unified = unify_generic_callable(left, right, ignore_return=ignore_return)
        if unified is None:
            return False
        else:
            left = unified

    # If we allow partial overlaps, we don't need to leave R generic:
    # if we can find even just a single typevar assignment which
    # would make these callables compatible, we should return True.

    # So, we repeat the above checks in the opposite direction. This also
    # lets us preserve the 'symmetry' property of allow_partial_overlap.
    if allow_partial_overlap and right.variables:
        unified = unify_generic_callable(right, left, ignore_return=ignore_return)
        if unified is not None:
            right = unified

    # Check return types.
    if not ignore_return and not is_compat_return(left.ret_type, right.ret_type):
        return False

    if check_args_covariantly:
        is_compat = flip_compat_check(is_compat)

    if right.is_ellipsis_args:
        return True

    left_star = left.var_arg
    left_star2 = left.kw_arg
    right_star = right.var_arg
    right_star2 = right.kw_arg

    # Match up corresponding arguments and check them for compatibility. In
    # every pair (argL, argR) of corresponding arguments from L and R, argL must
    # be "more general" than argR if L is to be a subtype of R.

    # Arguments are corresponding if they either share a name, share a position,
    # or both. If L's corresponding argument is ambiguous, L is not a subtype of R.

    # If left has one corresponding argument by name and another by position,
    # consider them to be one "merged" argument (and not ambiguous) if they're
    # both optional, they're name-only and position-only respectively, and they
    # have the same type.  This rule allows functions with (*args, **kwargs) to
    # properly stand in for the full domain of formal arguments that they're
    # used for in practice.

    # Every argument in R must have a corresponding argument in L, and every
    # required argument in L must have a corresponding argument in R.

    # Phase 1: Confirm every argument in R has a corresponding argument in L.

    # Phase 1a: If left and right can both accept an infinite number of args,
    #           their types must be compatible.
    #
    #           Furthermore, if we're checking for compatibility in all cases,
    #           we confirm that if R accepts an infinite number of arguments,
    #           L must accept the same.
    def _incompatible(left_arg: Optional[FormalArgument],
                      right_arg: Optional[FormalArgument]) -> bool:
        if right_arg is None:
            return False
        if left_arg is None:
            return not allow_partial_overlap
        return not is_compat(right_arg.typ, left_arg.typ)

    if _incompatible(left_star, right_star) or _incompatible(left_star2, right_star2):
        return False

    # Phase 1b: Check non-star args: for every arg right can accept, left must
    #           also accept. The only exception is if we are allowing partial
    #           partial overlaps: in that case, we ignore optional args on the right.
    for right_arg in right.formal_arguments():
        left_arg = left.corresponding_argument(right_arg)
        if left_arg is None:
            if allow_partial_overlap and not right_arg.required:
                continue
            return False
        if not are_args_compatible(left_arg, right_arg, ignore_pos_arg_names,
                                   allow_partial_overlap, is_compat):
            return False

    # Phase 1c: Check var args. Right has an infinite series of optional positional
    #           arguments. Get all further positional args of left, and make sure
    #           they're more general then the corresponding member in right.
    if right_star is not None:
        # Synthesize an anonymous formal argument for the right
        right_by_position = right.try_synthesizing_arg_from_vararg(None)
        assert right_by_position is not None

        i = right_star.pos
        assert i is not None
        while i < len(left.arg_kinds) and left.arg_kinds[i] in (ARG_POS, ARG_OPT):
            if allow_partial_overlap and left.arg_kinds[i] == ARG_OPT:
                break

            left_by_position = left.argument_by_position(i)
            assert left_by_position is not None

            if not are_args_compatible(left_by_position, right_by_position,
                                       ignore_pos_arg_names, allow_partial_overlap,
                                       is_compat):
                return False
            i += 1

    # Phase 1d: Check kw args. Right has an infinite series of optional named
    #           arguments. Get all further named args of left, and make sure
    #           they're more general then the corresponding member in right.
    if right_star2 is not None:
        right_names = {name for name in right.arg_names if name is not None}
        left_only_names = set()
        for name, kind in zip(left.arg_names, left.arg_kinds):
            if name is None or kind in (ARG_STAR, ARG_STAR2) or name in right_names:
                continue
            left_only_names.add(name)

        # Synthesize an anonymous formal argument for the right
        right_by_name = right.try_synthesizing_arg_from_kwarg(None)
        assert right_by_name is not None

        for name in left_only_names:
            left_by_name = left.argument_by_name(name)
            assert left_by_name is not None

            if allow_partial_overlap and not left_by_name.required:
                continue

            if not are_args_compatible(left_by_name, right_by_name, ignore_pos_arg_names,
                                       allow_partial_overlap, is_compat):
                return False

    # Phase 2: Left must not impose additional restrictions.
    #          (Every required argument in L must have a corresponding argument in R)
    #          Note: we already checked the *arg and **kwarg arguments in phase 1a.
    for left_arg in left.formal_arguments():
        right_by_name = (right.argument_by_name(left_arg.name)
                         if left_arg.name is not None
                         else None)

        right_by_pos = (right.argument_by_position(left_arg.pos)
                        if left_arg.pos is not None
                        else None)

        # If the left hand argument corresponds to two right-hand arguments,
        # neither of them can be required.
        if (right_by_name is not None
                and right_by_pos is not None
                and right_by_name != right_by_pos
                and (right_by_pos.required or right_by_name.required)):
            return False

        # All *required* left-hand arguments must have a corresponding
        # right-hand argument.  Optional args do not matter.
        if left_arg.required and right_by_pos is None and right_by_name is None:
            return False

    return True


def are_args_compatible(
        left: FormalArgument,
        right: FormalArgument,
        ignore_pos_arg_names: bool,
        allow_partial_overlap: bool,
        is_compat: Callable[[Type, Type], bool]) -> bool:
    def is_different(left_item: Optional[object], right_item: Optional[object]) -> bool:
        """Checks if the left and right items are different.

        If the right item is unspecified (e.g. if the right callable doesn't care
        about what name or position its arg has), we default to returning False.

        If we're allowing partial overlap, we also default to returning False
        if the left callable also doesn't care."""
        if right_item is None:
            return False
        if allow_partial_overlap and left_item is None:
            return False
        return left_item != right_item

    # If right has a specific name it wants this argument to be, left must
    # have the same.
    if is_different(left.name, right.name):
        # But pay attention to whether we're ignoring positional arg names
        if not ignore_pos_arg_names or right.pos is None:
            return False

    # If right is at a specific position, left must have the same:
    if is_different(left.pos, right.pos):
        return False

    # If right's argument is optional, left's must also be
    # (unless we're relaxing the checks to allow potential
    # rather then definite compatibility).
    if not allow_partial_overlap and not right.required and left.required:
        return False

    # If we're allowing partial overlaps and neither arg is required,
    # the types don't actually need to be the same
    if allow_partial_overlap and not left.required and not right.required:
        return True

    # Left must have a more general type
    return is_compat(right.typ, left.typ)


def flip_compat_check(is_compat: Callable[[Type, Type], bool]) -> Callable[[Type, Type], bool]:
    def new_is_compat(left: Type, right: Type) -> bool:
        return is_compat(right, left)
    return new_is_compat


def unify_generic_callable(type: CallableType, target: CallableType,
                           ignore_return: bool,
                           return_constraint_direction: int = mypy.constraints.SUBTYPE_OF,
                           ) -> Optional[CallableType]:
    """Try to unify a generic callable type with another callable type.

    Return unified CallableType if successful; otherwise, return None.
    """
    import mypy.solve
    constraints = []  # type: List[mypy.constraints.Constraint]
    for arg_type, target_arg_type in zip(type.arg_types, target.arg_types):
        c = mypy.constraints.infer_constraints(
            arg_type, target_arg_type, mypy.constraints.SUPERTYPE_OF)
        constraints.extend(c)
    if not ignore_return:
        c = mypy.constraints.infer_constraints(
            type.ret_type, target.ret_type, return_constraint_direction)
        constraints.extend(c)
    type_var_ids = [tvar.id for tvar in type.variables]
    inferred_vars = mypy.solve.solve_constraints(type_var_ids, constraints)
    if None in inferred_vars:
        return None
    non_none_inferred_vars = cast(List[Type], inferred_vars)
    msg = messages.temp_message_builder()
    applied = mypy.applytype.apply_generic_arguments(type, non_none_inferred_vars, msg,
                                                     context=target)
    if msg.is_errors():
        return None
    return applied


def restrict_subtype_away(t: Type, s: Type) -> Type:
    """Return t minus s.

    If we can't determine a precise result, return a supertype of the
    ideal result (just t is a valid result).

    This is used for type inference of runtime type checks such as
    isinstance.

    Currently this just removes elements of a union type.
    """
    if isinstance(t, UnionType):
        # Since runtime type checks will ignore type arguments, erase the types.
        erased_s = erase_type(s)
        # TODO: Implement more robust support for runtime isinstance() checks,
        # see issue #3827
        new_items = [item for item in t.relevant_items()
                     if (not (is_proper_subtype(erase_type(item), erased_s) or
                              is_proper_subtype(item, erased_s))
                         or isinstance(item, AnyType))]
        return UnionType.make_union(new_items)
    else:
        return t


def is_proper_subtype(left: Type, right: Type) -> bool:
    """Is left a proper subtype of right?

    For proper subtypes, there's no need to rely on compatibility due to
    Any types. Every usable type is a proper subtype of itself.
    """
    if isinstance(right, UnionType) and not isinstance(left, UnionType):
        return any([is_proper_subtype(left, item)
                    for item in right.items])
    return left.accept(ProperSubtypeVisitor(right))


class ProperSubtypeVisitor(TypeVisitor[bool]):
    def __init__(self, right: Type) -> None:
        self.right = right

    def visit_unbound_type(self, left: UnboundType) -> bool:
        # This can be called if there is a bad type annotation. The result probably
        # doesn't matter much but by returning True we simplify these bad types away
        # from unions, which could filter out some bogus messages.
        return True

    def visit_any(self, left: AnyType) -> bool:
        return isinstance(self.right, AnyType)

    def visit_none_type(self, left: NoneTyp) -> bool:
        if experiments.STRICT_OPTIONAL:
            return (isinstance(self.right, NoneTyp) or
                    is_named_instance(self.right, 'builtins.object'))
        return True

    def visit_uninhabited_type(self, left: UninhabitedType) -> bool:
        return True

    def visit_erased_type(self, left: ErasedType) -> bool:
        # This may be encountered during type inference. The result probably doesn't
        # matter much.
        return True

    def visit_deleted_type(self, left: DeletedType) -> bool:
        return True

    def visit_instance(self, left: Instance) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if TypeState.is_cached_proper_subtype_check(left, right):
                return True
            for base in left.type.mro:
                if base._promote and is_proper_subtype(base._promote, right):
                    TypeState.record_proper_subtype_cache_entry(left, right)
                    return True

            if left.type.has_base(right.type.fullname()):
                def check_argument(leftarg: Type, rightarg: Type, variance: int) -> bool:
                    if variance == COVARIANT:
                        return is_proper_subtype(leftarg, rightarg)
                    elif variance == CONTRAVARIANT:
                        return is_proper_subtype(rightarg, leftarg)
                    else:
                        return sametypes.is_same_type(leftarg, rightarg)
                # Map left type to corresponding right instances.
                left = map_instance_to_supertype(left, right.type)

                nominal = all(check_argument(ta, ra, tvar.variance) for ta, ra, tvar in
                              zip(left.args, right.args, right.type.defn.type_vars))
                if nominal:
                    TypeState.record_proper_subtype_cache_entry(left, right)
                return nominal
            if (right.type.is_protocol and
                    is_protocol_implementation(left, right, proper_subtype=True)):
                return True
            return False
        if isinstance(right, CallableType):
            call = find_member('__call__', left, left)
            if call:
                return is_proper_subtype(call, right)
            return False
        return False

    def visit_type_var(self, left: TypeVarType) -> bool:
        if isinstance(self.right, TypeVarType) and left.id == self.right.id:
            return True
        if left.values and is_subtype(UnionType.make_simplified_union(left.values), self.right):
            return True
        return is_proper_subtype(left.upper_bound, self.right)

    def visit_callable_type(self, left: CallableType) -> bool:
        right = self.right
        if isinstance(right, CallableType):
            return is_callable_compatible(left, right, is_compat=is_proper_subtype)
        elif isinstance(right, Overloaded):
            return all(is_proper_subtype(left, item)
                       for item in right.items())
        elif isinstance(right, Instance):
            return is_proper_subtype(left.fallback, right)
        elif isinstance(right, TypeType):
            # This is unsound, we don't check the __init__ signature.
            return left.is_type_obj() and is_proper_subtype(left.ret_type, right.item)
        return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if (is_named_instance(right, 'builtins.tuple') or
                    is_named_instance(right, 'typing.Iterable') or
                    is_named_instance(right, 'typing.Container') or
                    is_named_instance(right, 'typing.Sequence') or
                    is_named_instance(right, 'typing.Reversible')):
                if not right.args:
                    return False
                iter_type = right.args[0]
                if is_named_instance(right, 'builtins.tuple') and isinstance(iter_type, AnyType):
                    # TODO: We shouldn't need this special case. This is currently needed
                    #       for isinstance(x, tuple), though it's unclear why.
                    return True
                return all(is_proper_subtype(li, iter_type) for li in left.items)
            return is_proper_subtype(left.fallback, right)
        elif isinstance(right, TupleType):
            if len(left.items) != len(right.items):
                return False
            for l, r in zip(left.items, right.items):
                if not is_proper_subtype(l, r):
                    return False
            return is_proper_subtype(left.fallback, right.fallback)
        return False

    def visit_typeddict_type(self, left: TypedDictType) -> bool:
        right = self.right
        if isinstance(right, TypedDictType):
            for name, typ in left.items.items():
                if name in right.items and not is_same_type(typ, right.items[name]):
                    return False
            for name, typ in right.items.items():
                if name not in left.items:
                    return False
            return True
        return is_proper_subtype(left.fallback, right)

    def visit_overloaded(self, left: Overloaded) -> bool:
        # TODO: What's the right thing to do here?
        return False

    def visit_union_type(self, left: UnionType) -> bool:
        return all([is_proper_subtype(item, self.right) for item in left.items])

    def visit_partial_type(self, left: PartialType) -> bool:
        # TODO: What's the right thing to do here?
        return False

    def visit_type_type(self, left: TypeType) -> bool:
        # TODO: Handle metaclasses?
        right = self.right
        if isinstance(right, TypeType):
            # This is unsound, we don't check the __init__ signature.
            return is_proper_subtype(left.item, right.item)
        if isinstance(right, CallableType):
            # This is also unsound because of __init__.
            return right.is_type_obj() and is_proper_subtype(left.item, right.ret_type)
        if isinstance(right, Instance):
            if right.type.fullname() == 'builtins.type':
                # TODO: Strictly speaking, the type builtins.type is considered equivalent to
                #       Type[Any]. However, this would break the is_proper_subtype check in
                #       conditional_type_map for cases like isinstance(x, type) when the type
                #       of x is Type[int]. It's unclear what's the right way to address this.
                return True
            if right.type.fullname() == 'builtins.object':
                return True
        return False


def is_more_precise(left: Type, right: Type) -> bool:
    """Check if left is a more precise type than right.

    A left is a proper subtype of right, left is also more precise than
    right. Also, if right is Any, left is more precise than right, for
    any left.
    """
    # TODO Should List[int] be more precise than List[Any]?
    if isinstance(right, AnyType):
        return True
    return is_proper_subtype(left, right)
