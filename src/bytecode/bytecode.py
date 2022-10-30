# alias to keep the 'bytecode' variable free
import sys
import types
from abc import abstractmethod
from typing import (
    Any,
    Generic,
    Iterator,
    List,
    Optional,
    Sequence,
    SupportsIndex,
    Tuple,
    TypeVar,
    Union,
    overload,
)

import bytecode as _bytecode
from bytecode.flags import CompilerFlags, infer_flags
from bytecode.instr import UNSET, BaseInstr, Instr, Label, SetLineno, TryBegin, TryEnd


class BaseBytecode:
    def __init__(self):
        self.argcount = 0
        self.posonlyargcount = 0
        self.kwonlyargcount = 0
        self.first_lineno = 1
        self.name = "<module>"
        self.filename = "<string>"
        self.docstring = UNSET
        self.cellvars = []
        # we cannot recreate freevars from instructions because of super()
        # special-case
        self.freevars: List[str] = []
        self._flags: CompilerFlags = CompilerFlags(0)

    def _copy_attr_from(self, bytecode: "BaseBytecode") -> None:
        self.argcount = bytecode.argcount
        self.posonlyargcount = bytecode.posonlyargcount
        self.kwonlyargcount = bytecode.kwonlyargcount
        self.flags = bytecode.flags
        self.first_lineno = bytecode.first_lineno
        self.name = bytecode.name
        self.filename = bytecode.filename
        self.docstring = bytecode.docstring
        self.cellvars = list(bytecode.cellvars)
        self.freevars = list(bytecode.freevars)

    def __eq__(self, other: Any) -> bool:
        if type(self) != type(other):
            return False

        if self.argcount != other.argcount:
            return False
        if self.posonlyargcount != other.posonlyargcount:
            return False
        if self.kwonlyargcount != other.kwonlyargcount:
            return False
        if self.flags != other.flags:
            return False
        if self.first_lineno != other.first_lineno:
            return False
        if self.filename != other.filename:
            return False
        if self.name != other.name:
            return False
        if self.docstring != other.docstring:
            return False
        if self.cellvars != other.cellvars:
            return False
        if self.freevars != other.freevars:
            return False
        if self.compute_stacksize() != other.compute_stacksize():
            return False

        return True

    @property
    def flags(self) -> CompilerFlags:
        return self._flags

    @flags.setter
    def flags(self, value: CompilerFlags) -> None:
        if not isinstance(value, CompilerFlags):
            value = CompilerFlags(value)
        self._flags = value

    def update_flags(self, *, is_async: Optional[bool] = None) -> None:
        # infer_flags reasonably only accept concrete subclasses
        self.flags = infer_flags(self, is_async)  # type: ignore

    @abstractmethod
    def compute_stacksize(self, *, check_pre_and_post: bool = True) -> int:
        raise NotImplementedError


T = TypeVar("T", bound="_BaseBytecodeList")
U = TypeVar("U")


class _BaseBytecodeList(BaseBytecode, list, Generic[U]):
    """List subclass providing type stable slicing and copying."""

    @overload
    def __getitem__(self, index: SupportsIndex) -> U:
        ...

    @overload
    def __getitem__(self: T, index: slice) -> T:
        ...

    def __getitem__(self, index):
        value = super().__getitem__(index)
        if isinstance(index, slice):
            value = type(self)(value)
            value._copy_attr_from(self)

        return value

    def copy(self: T) -> T:
        # This is a list subclass and works
        new = type(self)(super().copy())  # type: ignore
        new._copy_attr_from(self)
        return new

    def legalize(self) -> None:
        """Check that all the element of the list are valid and remove SetLineno."""
        lineno_pos = []
        set_lineno = None
        current_lineno = self.first_lineno

        for pos, instr in enumerate(self):
            if isinstance(instr, SetLineno):
                set_lineno = instr.lineno
                lineno_pos.append(pos)
                continue
            # Filter out Labels
            if not isinstance(instr, BaseInstr):
                continue
            if set_lineno is not None:
                instr.lineno = set_lineno
            elif instr.lineno is UNSET:
                instr.lineno = current_lineno
            else:
                current_lineno = instr.lineno

        for i in reversed(lineno_pos):
            del self[i]

    def __iter__(self) -> Iterator[U]:
        instructions = super().__iter__()
        for instr in instructions:
            self._check_instr(instr)
            yield instr

    def _check_instr(self, instr):
        raise NotImplementedError()


V = TypeVar("V")


class _InstrList(List[V]):
    # Providing a stricter typing for this helper whose use is limited to the __eq__
    # implementation is more effort than it is worth.
    def _flat(self) -> List:
        instructions: List = []
        labels = {}
        jumps = []

        offset = 0
        instr: Any
        for index, instr in enumerate(self):
            if isinstance(instr, Label):
                instructions.append("label_instr%s" % index)
                labels[instr] = offset
            else:
                if isinstance(instr, Instr) and isinstance(instr.arg, Label):
                    target_label = instr.arg
                    instr = _bytecode.ConcreteInstr(
                        instr.name, 0, location=instr.location
                    )
                    jumps.append((target_label, instr))
                instructions.append(instr)
                offset += 1

        for target_label, instr in jumps:
            instr.arg = labels[target_label]

        return instructions

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, _InstrList):
            other = _InstrList(other)

        return self._flat() == other._flat()


class Bytecode(
    _InstrList[Union[Instr, Label, TryBegin, TryEnd, SetLineno]],
    _BaseBytecodeList[Union[Instr, Label, TryBegin, TryEnd, SetLineno]],
):
    def __init__(
        self,
        instructions: Sequence[Union[Instr, Label, TryBegin, TryEnd, SetLineno]] = (),
    ) -> None:
        BaseBytecode.__init__(self)
        self.argnames: List[str] = []
        for instr in instructions:
            self._check_instr(instr)
        self.extend(instructions)

    def __iter__(self) -> Iterator[Union[Instr, Label, TryBegin, TryEnd, SetLineno]]:
        instructions = super().__iter__()
        seen_try_begin = False
        for instr in instructions:
            self._check_instr(instr)
            if isinstance(instr, TryBegin):
                if seen_try_begin:
                    raise RuntimeError("TryBegin pseudo instructions cannot be nested.")
                seen_try_begin = True
            elif isinstance(instr, TryEnd):
                seen_try_begin = False
            yield instr

    def _check_instr(self, instr: Any) -> None:
        if not isinstance(instr, (Label, SetLineno, Instr, TryBegin, TryEnd)):
            raise ValueError(
                "Bytecode must only contain Label, "
                "SetLineno, and Instr objects, "
                "but %s was found" % type(instr).__name__
            )

    def _copy_attr_from(self, bytecode: BaseBytecode) -> None:
        super()._copy_attr_from(bytecode)
        if isinstance(bytecode, Bytecode):
            self.argnames = bytecode.argnames

    @staticmethod
    def from_code(
        code: types.CodeType,
        prune_caches: bool = True,
        conserve_exception_block_stackdepth: bool = False,
    ) -> "Bytecode":
        concrete = _bytecode.ConcreteBytecode.from_code(code)
        return concrete.to_bytecode(
            prune_caches=prune_caches,
            conserve_exception_block_stackdepth=conserve_exception_block_stackdepth,
        )

    def compute_stacksize(self, *, check_pre_and_post: bool = True) -> int:
        cfg = _bytecode.ControlFlowGraph.from_bytecode(self)
        return cfg.compute_stacksize(check_pre_and_post=check_pre_and_post)

    def to_code(
        self,
        compute_jumps_passes: Optional[int] = None,
        stacksize: Optional[int] = None,
        *,
        check_pre_and_post: bool = True,
        compute_exception_stack_depths: bool = True,
    ) -> types.CodeType:
        # Prevent reconverting the concrete bytecode to bytecode and cfg to do the
        # calculation if we need to do it.
        if stacksize is None or (
            sys.version_info >= (3, 11) and compute_exception_stack_depths
        ):
            cfg = _bytecode.ControlFlowGraph.from_bytecode(self)
            stacksize = cfg.compute_stacksize(
                check_pre_and_post=check_pre_and_post,
                compute_exception_stack_depths=compute_exception_stack_depths,
            )
            self = cfg.to_bytecode()
            compute_exception_stack_depths = False  # avoid redoing everything
        bc = self.to_concrete_bytecode(
            compute_jumps_passes=compute_jumps_passes,
            compute_exception_stack_depths=compute_exception_stack_depths,
        )
        return bc.to_code(
            stacksize=stacksize,
            compute_exception_stack_depths=compute_exception_stack_depths,
        )

    def to_concrete_bytecode(
        self,
        compute_jumps_passes: Optional[int] = None,
        compute_exception_stack_depths: bool = True,
    ) -> "_bytecode.ConcreteBytecode":
        converter = _bytecode._ConvertBytecodeToConcrete(self)
        return converter.to_concrete_bytecode(
            compute_jumps_passes=compute_jumps_passes,
            compute_exception_stack_depths=compute_exception_stack_depths,
        )
