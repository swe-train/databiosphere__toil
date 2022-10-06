from typing import Any, Callable, Iterator, List, Sequence, Tuple, Union, ValuesView

from _typeshed import Incomplete

from .annotations import annotationType
from .listener import CallArgsInfo, Listener, ListenerValidator, UserListener
from .topicargspec import ArgsDocs, ArgsInfo, ArgSpecGiven
from .topicargspec import MessageDataSpecError as MessageDataSpecError
from .topicargspec import MsgData
from .topicargspec import SenderMissingReqdMsgDataError as SenderMissingReqdMsgDataError
from .topicargspec import SenderUnknownMsgDataError as SenderUnknownMsgDataError
from .topicargspec import topicArgsFromCallable
from .topicexc import ExcHandlerError as ExcHandlerError
from .topicexc import TopicDefnError, TopicNameError
from .topicutils import ALL_TOPICS, smartDedent, stringize, tupleize, validateName

class TreeConfig: ...

ListenerFilter = Callable[[Listener], bool]

class Topic:
    def __init__(
        self,
        treeConfig: TreeConfig,
        nameTuple: Tuple[str, ...],
        description: str,
        msgArgsInfo: ArgsInfo,
        parent: Topic = ...,
    ) -> None: ...
    def setDescription(self, desc: str) -> None: ...
    def getDescription(self) -> str: ...
    def setMsgArgSpec(
        self, argsDocs: ArgsDocs, required: Sequence[str] = ...
    ) -> None: ...
    def getArgs(self) -> Tuple[Sequence[str], Sequence[str]]: ...
    def getArgDescriptions(self) -> ArgsDocs: ...
    def setArgDescriptions(self, **docs: ArgsDocs) -> None: ...
    def hasMDS(self) -> bool: ...
    def filterMsgArgs(self, msgData: MsgData, check: bool = ...) -> MsgData: ...
    def isAll(self) -> bool: ...
    def isRoot(self) -> bool: ...
    def getName(self) -> str: ...
    def getNameTuple(self) -> Tuple[str, ...]: ...
    def getNodeName(self) -> str: ...
    def getParent(self) -> Topic: ...
    def hasSubtopic(self, name: str = ...) -> bool: ...
    def getSubtopic(self, relName: Union[str, Tuple[str, ...]]) -> Topic: ...
    def getSubtopics(self) -> ValuesView[Topic]: ...
    def getNumListeners(self) -> int: ...
    def hasListener(self, listener: UserListener) -> bool: ...
    def hasListeners(self) -> bool: ...
    def getListeners(self) -> List[Listener]: ...
    def getListenersIter(self) -> Iterator[Listener]: ...
    def validate(
        self, listener: UserListener, curriedArgNames: Sequence[str] = ...
    ) -> CallArgsInfo: ...
    def isValid(
        self, listener: UserListener, curriedArgNames: Sequence[str] = ...
    ) -> bool: ...
    def subscribe(
        self, listener: UserListener, **curriedArgs: Any
    ) -> Tuple[Listener, bool]: ...
    def unsubscribe(self, listener: UserListener) -> Listener: ...
    def unsubscribeAllListeners(
        self, filter: ListenerFilter = ...
    ) -> List[Listener]: ...
    def publish(self, **msgData: Any) -> None: ...
    name: Incomplete
    parent: Incomplete
    subtopics: Incomplete
    description: Incomplete
    listeners: Incomplete
    numListeners: Incomplete
    args: Incomplete
    argDescriptions: Incomplete
