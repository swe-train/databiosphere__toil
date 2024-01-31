from typing import List, Mapping, TextIO

from _typeshed import Incomplete

from ..core import INotificationHandler, Listener, Publisher, Topic, TopicManager

class IgnoreNotificationsMixin(INotificationHandler):
    def notifySubscribe(self, pubListener: Listener, topicObj: Topic, newSub: bool): ...
    def notifyUnsubscribe(self, pubListener: Listener, topicObj: Topic): ...
    def notifyDeadListener(self, pubListener: Listener, topicObj: Topic): ...
    def notifySend(self, stage: str, topicObj: Topic, pubListener: Listener = ...): ...
    def notifyNewTopic(
        self,
        topicObj: Topic,
        description: str,
        required: List[str],
        argsDocs: Mapping[str, str],
    ): ...
    def notifyDelTopic(self, topicName: str): ...

class NotifyByWriteFile(INotificationHandler):
    defaultPrefix: str
    def __init__(self, fileObj: TextIO = ..., prefix: str = ...) -> None: ...
    def changeFile(self, fileObj) -> None: ...
    def notifySubscribe(self, pubListener: Listener, topicObj: Topic, newSub: bool): ...
    def notifyUnsubscribe(self, pubListener: Listener, topicObj: Topic): ...
    def notifyDeadListener(self, pubListener: Listener, topicObj: Topic): ...
    def notifySend(self, stage: str, topicObj: Topic, pubListener: Listener = ...): ...
    def notifyNewTopic(
        self,
        topicObj: Topic,
        description: str,
        required: List[str],
        argsDocs: Mapping[str, str],
    ): ...
    def notifyDelTopic(self, topicName: str): ...

class NotifyByPubsubMessage(INotificationHandler):
    topicRoot: str
    topics: Incomplete
    def __init__(self, topicMgr: TopicManager = ...) -> None: ...
    def createNotificationTopics(self, topicMgr: TopicManager): ...
    def notifySubscribe(self, pubListener: Listener, topicObj: Topic, newSub: bool): ...
    def notifyUnsubscribe(self, pubListener: Listener, topicObj: Topic): ...
    def notifyDeadListener(self, pubListener: Listener, topicObj: Topic): ...
    def notifySend(self, stage: str, topicObj: Topic, pubListener: Listener = ...): ...
    def notifyNewTopic(
        self,
        topicObj: Topic,
        description: str,
        required: List[str],
        argsDocs: Mapping[str, str],
    ): ...
    def notifyDelTopic(self, topicName: str): ...

def useNotifyByPubsubMessage(publisher: Publisher = ..., all: bool = ..., **kwargs): ...
def useNotifyByWriteFile(
    fileObj: TextIO = ...,
    prefix: str = ...,
    publisher: Publisher = ...,
    all: bool = ...,
    **kwargs
): ...
