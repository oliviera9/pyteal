from dataclasses import dataclass, field, fields, astuple
from typing import cast, Optional
from enum import IntFlag

from algosdk import abi as sdk_abi
from algosdk import encoding

from pyteal.config import METHOD_ARG_NUM_LIMIT
from pyteal.errors import TealInputError, TealInternalError
from pyteal.types import TealType
from pyteal.compiler.compiler import compileTeal, DEFAULT_TEAL_VERSION, OptimizeOptions
from pyteal.ir.ops import Mode

from pyteal.ast import abi
from pyteal.ast.subroutine import (
    OutputKwArgInfo,
    SubroutineFnWrapper,
    ABIReturnSubroutine,
)
from pyteal.ast.assert_ import Assert
from pyteal.ast.cond import Cond
from pyteal.ast.expr import Expr
from pyteal.ast.app import OnComplete, EnumInt
from pyteal.ast.int import Int
from pyteal.ast.seq import Seq
from pyteal.ast.methodsig import MethodSignature
from pyteal.ast.naryexpr import And, Or
from pyteal.ast.txn import Txn
from pyteal.ast.return_ import Approve


class CallConfig(IntFlag):
    """
    CallConfigs: a "bitset"-like class for more fine-grained control over
    `call or create` for a method about an OnComplete case.

    This enumeration class allows for specifying one of the four following cases:
    - CALL
    - CREATE
    - ALL
    - NEVER
    for a method call on one on_complete case.
    """

    NEVER = 0
    CALL = 1
    CREATE = 2
    ALL = 3


CallConfig.__module__ = "pyteal"


@dataclass(frozen=True)
class MethodConfig:
    """
    CallConfigs keep track of one method registration's CallConfigs for all OnComplete cases.

    By ARC-0004 spec:
        If an Application is called with greater than zero Application call arguments  (NOT a bare Application call),
        the Application MUST always treat the first argument as a method selector and invoke the specified method,
        regardless of the OnCompletion action of the Application call.
        This applies to Application creation transactions as well, where the supplied Application ID is 0.

    The `CallConfigs` implementation generalized contract method call such that method call is allowed
    for certain OnCompletions.

    The `arc4_compliant` method constructs a `CallConfigs` that allows a method call to be executed
    under any OnCompletion, which is "arc4-compliant".
    """

    no_op: CallConfig = field(kw_only=True, default=CallConfig.CALL)
    opt_in: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    close_out: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    clear_state: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    update_application: CallConfig = field(kw_only=True, default=CallConfig.NEVER)
    delete_application: CallConfig = field(kw_only=True, default=CallConfig.NEVER)

    def is_never(self) -> bool:
        return all(map(lambda cc: cc == CallConfig.NEVER, astuple(self)))

    @classmethod
    def arc4_compliant(cls):
        return cls(
            no_op=CallConfig.ALL,
            opt_in=CallConfig.ALL,
            close_out=CallConfig.ALL,
            clear_state=CallConfig.ALL,
            update_application=CallConfig.ALL,
            delete_application=CallConfig.ALL,
        )

    def is_arc4_compliant(self) -> bool:
        return self == self.arc4_compliant()

    @staticmethod
    def __condition_under_config(cc: CallConfig) -> Expr | int:
        match cc:
            case CallConfig.NEVER:
                return 0
            case CallConfig.CALL:
                return Txn.application_id() != Int(0)
            case CallConfig.CREATE:
                return Txn.application_id() == Int(0)
            case CallConfig.ALL:
                return 1
            case _:
                raise TealInternalError("CallConfig scope exceeding!")

    def approval_cond(self) -> Expr | int:
        config_oc_pairs: dict[CallConfig, EnumInt] = {
            self.no_op: OnComplete.NoOp,
            self.opt_in: OnComplete.OptIn,
            self.close_out: OnComplete.CloseOut,
            self.update_application: OnComplete.UpdateApplication,
            self.delete_application: OnComplete.DeleteApplication,
        }
        if all(config == CallConfig.NEVER for config in config_oc_pairs):
            return 0
        elif all(config == CallConfig.ALL for config in config_oc_pairs):
            return 1
        else:
            cond_list = []
            for config in config_oc_pairs:
                config_cond = self.__condition_under_config(config)
                match config_cond:
                    case Expr():
                        cond_list.append(
                            And(
                                Txn.on_completion() == config_oc_pairs[config],
                                config_cond,
                            )
                        )
                    case 1:
                        cond_list.append(Txn.on_completion() == config_oc_pairs[config])
                    case 0:
                        continue
                    case _:
                        raise TealInternalError(
                            "condition_under_config scope exceeding!"
                        )
            return Or(*cond_list)

    def clear_state_cond(self) -> Expr | int:
        return self.__condition_under_config(self.clear_state)


@dataclass(frozen=True)
class OnCompleteAction:
    """
    OnComplete Action, registers bare calls to one single OnCompletion case.
    """

    on_create: Optional[Expr | SubroutineFnWrapper | ABIReturnSubroutine] = field(
        kw_only=True, default=None
    )
    on_call: Optional[Expr | SubroutineFnWrapper | ABIReturnSubroutine] = field(
        kw_only=True, default=None
    )

    @staticmethod
    def never() -> "OnCompleteAction":
        return OnCompleteAction()

    @staticmethod
    def create_only(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(on_create=f)

    @staticmethod
    def call_only(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(on_call=f)

    @staticmethod
    def always(
        f: Expr | SubroutineFnWrapper | ABIReturnSubroutine,
    ) -> "OnCompleteAction":
        return OnCompleteAction(on_create=f, on_call=f)

    def is_empty(self) -> bool:
        return not (self.on_call or self.on_create)


OnCompleteAction.__module__ = "pyteal"


@dataclass(frozen=True)
class BareCallActions:
    """
    OnCompletion Actions keep track of bare-call registrations to all OnCompletion cases.
    """

    close_out: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    clear_state: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )
    delete_application: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )
    no_op: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    opt_in: OnCompleteAction = field(kw_only=True, default=OnCompleteAction.never())
    update_application: OnCompleteAction = field(
        kw_only=True, default=OnCompleteAction.never()
    )

    def is_empty(self) -> bool:
        for action_field in fields(self):
            action: OnCompleteAction = getattr(self, action_field.name)
            if not action.is_empty():
                return False
        return True

    def approval_construction(self) -> Optional[Expr]:
        oc_action_pair: dict[EnumInt, OnCompleteAction] = {
            OnComplete.NoOp: self.no_op,
            OnComplete.OptIn: self.opt_in,
            OnComplete.CloseOut: self.close_out,
            OnComplete.UpdateApplication: self.update_application,
            OnComplete.DeleteApplication: self.delete_application,
        }
        if all(oca.is_empty() for oca in oc_action_pair.values()):
            return None
        conditions_n_branches: list[CondNode] = list()
        for oc, oca in oc_action_pair.items():
            if oca.on_call:
                conditions_n_branches.append(
                    CondNode(
                        And(Txn.on_completion() == oc, Txn.application_id() != Int(0)),
                        ASTBuilder.wrap_handler(False, oca.on_call),
                    )
                )
            if oca.on_create:
                conditions_n_branches.append(
                    CondNode(
                        And(Txn.on_completion() == oc, Txn.application_id() == Int(0)),
                        ASTBuilder.wrap_handler(False, oca.on_create),
                    )
                )
        return Cond(*[[n.condition, n.branch] for n in conditions_n_branches])

    def clear_state_construction(self) -> Optional[Expr]:
        if self.clear_state.is_empty():
            return None
        conditions_n_branches: list[CondNode] = list()
        if self.clear_state.on_call:
            conditions_n_branches.append(
                CondNode(
                    Txn.application_id() != Int(0),
                    ASTBuilder.wrap_handler(False, self.clear_state.on_call),
                )
            )
        if self.clear_state.on_create:
            conditions_n_branches.append(
                CondNode(
                    Txn.application_id() == Int(0),
                    ASTBuilder.wrap_handler(False, self.clear_state.on_create),
                )
            )
        return Cond(*[[n.condition, n.branch] for n in conditions_n_branches])


BareCallActions.__module__ = "pyteal"


@dataclass(frozen=True)
class CondNode:
    condition: Expr
    branch: Expr


CondNode.__module__ = "pyteal"


@dataclass
class ASTBuilder:
    conditions_n_branches: list[CondNode] = field(default_factory=list)

    @staticmethod
    def wrap_handler(
        is_method_call: bool, handler: ABIReturnSubroutine | SubroutineFnWrapper | Expr
    ) -> Expr:
        """This is a helper function that handles transaction arguments passing in bare-app-call/abi-method handlers.

        If `is_method_call` is True, then it can only be `ABIReturnSubroutine`,
        otherwise:
            - both `ABIReturnSubroutine` and `Subroutine` takes 0 argument on the stack.
            - all three cases have none (or void) type.

        On ABI method case, if the ABI method has more than 15 args, this function manages to de-tuple
        the last (16-th) Txn app-arg into a list of ABI method arguments, and pass in to the ABI method.

        Args:
            is_method_call: a boolean value that specify if the handler is an ABI method.
            handler: an `ABIReturnSubroutine`, or `SubroutineFnWrapper` (for `Subroutine` case), or an `Expr`.
        Returns:
            Expr:
                - for bare-appcall it returns an expression that the handler takes no txn arg and Approve
                - for abi-method it returns the txn args correctly decomposed into ABI variables,
                  passed in ABIReturnSubroutine and logged, then approve.
        """
        if not is_method_call:
            match handler:
                case Expr():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"bare appcall handler should be TealType.none not {handler.type_of()}."
                        )
                    return handler if handler.has_return() else Seq(handler, Approve())
                case SubroutineFnWrapper():
                    if handler.type_of() != TealType.none:
                        raise TealInputError(
                            f"subroutine call should be returning TealType.none not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"subroutine call should take 0 arg for bare-app call. "
                            f"this subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(handler(), Approve())
                case ABIReturnSubroutine():
                    if handler.type_of() != "void":
                        raise TealInputError(
                            f"abi-returning subroutine call should be returning void not {handler.type_of()}."
                        )
                    if handler.subroutine.argument_count() != 0:
                        raise TealInputError(
                            f"abi-returning subroutine call should take 0 arg for bare-app call. "
                            f"this abi-returning subroutine takes {handler.subroutine.argument_count()}."
                        )
                    return Seq(cast(Expr, handler()), Approve())
                case _:
                    raise TealInputError(
                        "bare appcall can only accept: none type Expr, or Subroutine/ABIReturnSubroutine with none return and no arg"
                    )
        else:
            if not isinstance(handler, ABIReturnSubroutine):
                raise TealInputError(
                    f"method call should be only registering ABIReturnSubroutine, got {type(handler)}."
                )
            if not handler.is_abi_routable():
                raise TealInputError(
                    f"method call ABIReturnSubroutine is not routable "
                    f"got {handler.subroutine.argument_count()} args with {len(handler.subroutine.abi_args)} ABI args."
                )

            arg_type_specs = cast(
                list[abi.TypeSpec], handler.subroutine.expected_arg_types
            )
            if handler.subroutine.argument_count() > METHOD_ARG_NUM_LIMIT:
                last_arg_specs_grouped = arg_type_specs[METHOD_ARG_NUM_LIMIT - 1 :]
                arg_type_specs = arg_type_specs[: METHOD_ARG_NUM_LIMIT - 1]
                last_arg_spec = abi.TupleTypeSpec(*last_arg_specs_grouped)
                arg_type_specs.append(last_arg_spec)

            arg_abi_vars: list[abi.BaseType] = [
                type_spec.new_instance() for type_spec in arg_type_specs
            ]
            decode_instructions: list[Expr] = [
                arg_abi_vars[i].decode(Txn.application_args[i + 1])
                for i in range(len(arg_type_specs))
            ]

            if handler.subroutine.argument_count() > METHOD_ARG_NUM_LIMIT:
                tuple_arg_type_specs: list[abi.TypeSpec] = cast(
                    list[abi.TypeSpec],
                    handler.subroutine.expected_arg_types[METHOD_ARG_NUM_LIMIT - 1 :],
                )
                tuple_abi_args: list[abi.BaseType] = [
                    t_arg_ts.new_instance() for t_arg_ts in tuple_arg_type_specs
                ]
                last_tuple_arg: abi.Tuple = cast(abi.Tuple, arg_abi_vars[-1])
                de_tuple_instructions: list[Expr] = [
                    last_tuple_arg[i].store_into(tuple_abi_args[i])
                    for i in range(len(tuple_arg_type_specs))
                ]
                decode_instructions += de_tuple_instructions
                arg_abi_vars = arg_abi_vars[:-1] + tuple_abi_args

            # NOTE: does not have to have return, can be void method
            if handler.type_of() == "void":
                return Seq(
                    *decode_instructions,
                    cast(Expr, handler(*arg_abi_vars)),
                    Approve(),
                )
            else:
                output_temp: abi.BaseType = cast(
                    OutputKwArgInfo, handler.output_kwarg_info
                ).abi_type.new_instance()
                subroutine_call: abi.ReturnedValue = cast(
                    abi.ReturnedValue, handler(*arg_abi_vars)
                )
                return Seq(
                    *decode_instructions,
                    subroutine_call.store_into(output_temp),
                    abi.MethodReturn(output_temp),
                    Approve(),
                )

    def add_method_to_ast(
        self, method_signature: str, cond: Expr | int, handler: ABIReturnSubroutine
    ) -> None:
        walk_in_cond = Txn.application_args[0] == MethodSignature(method_signature)
        match cond:
            case Expr():
                self.conditions_n_branches.append(
                    CondNode(
                        walk_in_cond,
                        Seq(Assert(cond), self.wrap_handler(True, handler)),
                    )
                )
            case 1:
                self.conditions_n_branches.append(
                    CondNode(walk_in_cond, self.wrap_handler(True, handler))
                )
            case 0:
                return
            case _:
                raise TealInputError("Invalid condition input for add_method_to_ast")

    def program_construction(self) -> Expr:
        if not self.conditions_n_branches:
            raise TealInputError("ABIRouter: Cannot build program with an empty AST")
        return Cond(*[[n.condition, n.branch] for n in self.conditions_n_branches])


class Router:
    """
    Class that help constructs:
    - a *Generalized* ARC-4 app's approval/clear-state programs
    - and a contract JSON object allowing for easily read and call methods in the contract

    *DISCLAIMER*: ABI-Router is still taking shape and is subject to backwards incompatible changes.

    * Based on feedback, the API and usage patterns are likely to change.
    * Expect migration issues.
    """

    def __init__(
        self,
        name: str,
        bare_calls: BareCallActions = None,
    ) -> None:
        """
        Args:
            name: the name of the smart contract, used in the JSON object.
            bare_calls: the bare app call registered for each on_completion.
        """

        self.name: str = name
        self.approval_ast = ASTBuilder()
        self.clear_state_ast = ASTBuilder()

        self.method_sig_to_selector: dict[str, bytes] = dict()
        self.method_selector_to_sig: dict[bytes, str] = dict()

        if bare_calls and not bare_calls.is_empty():
            bare_call_approval = bare_calls.approval_construction()
            if bare_call_approval:
                self.approval_ast.conditions_n_branches.append(
                    CondNode(
                        Txn.application_args.length() == Int(0),
                        cast(Expr, bare_call_approval),
                    )
                )
            bare_call_clear = bare_calls.clear_state_construction()
            if bare_call_clear:
                self.clear_state_ast.conditions_n_branches.append(
                    CondNode(
                        Txn.application_args.length() == Int(0),
                        cast(Expr, bare_call_clear),
                    )
                )

    def add_method_handler(
        self,
        method_call: ABIReturnSubroutine,
        overriding_name: str = None,
        call_configs: MethodConfig = MethodConfig(),
    ) -> None:
        if not isinstance(method_call, ABIReturnSubroutine):
            raise TealInputError(
                "for adding method handler, must be ABIReturnSubroutine"
            )
        method_signature = method_call.method_signature(overriding_name)
        if call_configs.is_never():
            raise TealInputError(
                f"registered method {method_signature} is never executed"
            )
        method_selector = encoding.checksum(bytes(method_signature, "utf-8"))[:4]

        if method_signature in self.method_sig_to_selector:
            raise TealInputError(f"re-registering method {method_signature} detected")
        if method_selector in self.method_selector_to_sig:
            raise TealInputError(
                f"re-registering method {method_signature} has hash collision "
                f"with {self.method_selector_to_sig[method_selector]}"
            )
        self.method_sig_to_selector[method_signature] = method_selector
        self.method_selector_to_sig[method_selector] = method_signature

        method_approval_cond = call_configs.approval_cond()
        method_clear_state_cond = call_configs.clear_state_cond()
        self.approval_ast.add_method_to_ast(
            method_signature, method_approval_cond, method_call
        )
        self.clear_state_ast.add_method_to_ast(
            method_signature, method_clear_state_cond, method_call
        )

    def contract_construct(self) -> sdk_abi.Contract:
        """A helper function in constructing contract JSON object.

        It takes out the method signatures from approval program `ProgramNode`'s,
        and constructs an `Contract` object.

        Returns:
            contract: a dictified `Contract` object constructed from
                approval program's method signatures and `self.name`.
        """
        method_collections = [
            sdk_abi.Method.from_signature(sig)
            for sig in self.method_sig_to_selector
            if isinstance(sig, str)
        ]
        return sdk_abi.Contract(self.name, method_collections)

    def build_program(self) -> tuple[Expr, Expr, sdk_abi.Contract]:
        """
        Constructs ASTs for approval and clear-state programs from the registered methods in the router,
        also generates a JSON object of contract to allow client read and call the methods easily.

        Returns:
            approval_program: AST for approval program
            clear_state_program: AST for clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        return (
            self.approval_ast.program_construction(),
            self.clear_state_ast.program_construction(),
            self.contract_construct(),
        )

    def compile_program(
        self,
        *,
        version: int = DEFAULT_TEAL_VERSION,
        assembleConstants: bool = False,
        optimize: OptimizeOptions = None,
    ) -> tuple[str, str, sdk_abi.Contract]:
        """
        Combining `build_program` and `compileTeal`, compiles built Approval and ClearState programs
        and returns Contract JSON object for off-chain calling.

        Returns:
            approval_program: compiled approval program
            clear_state_program: compiled clear-state program
            contract: JSON object of contract to allow client start off-chain call
        """
        ap, csp, contract = self.build_program()
        ap_compiled = compileTeal(
            ap,
            Mode.Application,
            version=version,
            assembleConstants=assembleConstants,
            optimize=optimize,
        )
        csp_compiled = compileTeal(
            csp,
            Mode.Application,
            version=version,
            assembleConstants=assembleConstants,
            optimize=optimize,
        )
        return ap_compiled, csp_compiled, contract


Router.__module__ = "pyteal"
