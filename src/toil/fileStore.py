# Copyright (C) 2015-2018 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function
from future import standard_library
standard_library.install_aliases()
from builtins import map
from builtins import str
from builtins import range
from builtins import object
from abc import abstractmethod, ABCMeta
from collections import namedtuple, defaultdict
from contextlib import contextmanager
from fcntl import flock, LOCK_EX, LOCK_UN
from functools import partial
from hashlib import sha1
from threading import Thread, Semaphore, Event
from future.utils import with_metaclass
from six.moves.queue import Empty, Queue
import base64
import dill
import errno
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid

from toil.lib.objects import abstractclassmethod
from toil.lib.humanize import bytes2human
from toil.common import cacheDirName, getDirSizeRecursively, getFileSystemSize
from toil.lib.bioio import makePublicDir
from toil.resource import ModuleDescriptor

logger = logging.getLogger(__name__)


class DeferredFunction(namedtuple('DeferredFunction', 'function args kwargs name module')):
    """
    >>> df = DeferredFunction.create(defaultdict, None, {'x':1}, y=2)
    >>> df
    DeferredFunction(defaultdict, ...)
    >>> df.invoke() == defaultdict(None, x=1, y=2)
    True
    """
    @classmethod
    def create(cls, function, *args, **kwargs):
        """
        Capture the given callable and arguments as an instance of this class.

        :param callable function: The deferred action to take in the form of a function
        :param tuple args: Non-keyword arguments to the function
        :param dict kwargs: Keyword arguments to the function
        """
        # The general principle is to deserialize as late as possible, i.e. when the function is
        # to be invoked, as that will avoid redundantly deserializing deferred functions for
        # concurrently running jobs when the cache state is loaded from disk. By implication we
        # should serialize as early as possible. We need to serialize the function as well as its
        # arguments.
        return cls(*list(map(dill.dumps, (function, args, kwargs))),
                   name=function.__name__,
                   module=ModuleDescriptor.forModule(function.__module__).globalize())

    def invoke(self):
        """
        Invoke the captured function with the captured arguments.
        """
        logger.debug('Running deferred function %s.', self)
        self.module.makeLoadable()
        function, args, kwargs = list(map(dill.loads, (self.function, self.args, self.kwargs)))
        return function(*args, **kwargs)

    def __str__(self):
        return '%s(%s, ...)' % (self.__class__.__name__, self.name)

    __repr__ = __str__


class FileStore(with_metaclass(ABCMeta, object)):
    """
    An abstract base class to represent the interface between a worker and the job store.  Concrete
    subclasses will be used to manage temporary files, read and write files from the job store and
    log messages, passed as argument to the :meth:`toil.job.Job.run` method.
    """
    # Variables used for syncing reads/writes
    _pendingFileWritesLock = Semaphore()
    _pendingFileWrites = set()
    _terminateEvent = Event()  # Used to signify crashes in threads

    def __init__(self, jobStore, jobGraph, localTempDir, inputBlockFn):
        self.jobStore = jobStore
        self.jobGraph = jobGraph
        self.localTempDir = os.path.abspath(localTempDir)
        self.workFlowDir = os.path.dirname(self.localTempDir)
        self.jobName = self.jobGraph.command.split()[1]
        self.inputBlockFn = inputBlockFn
        self.loggingMessages = []
        self.filesToDelete = set()
        self.jobsToDelete = set()

    @staticmethod
    def createFileStore(jobStore, jobGraph, localTempDir, inputBlockFn, caching):
        fileStoreCls = CachingFileStore if caching else NonCachingFileStore
        return fileStoreCls(jobStore, jobGraph, localTempDir, inputBlockFn)

    @abstractmethod
    @contextmanager
    def open(self, job):
        """
        The context manager used to conduct tasks prior-to, and after a job has been run.

        :param toil.job.Job job: The job instance of the toil job to run.
        """
        raise NotImplementedError()

    # Functions related to temp files and directories
    def getLocalTempDir(self):
        """
        Get a new local temporary directory in which to write files that persist for the duration of
        the job.

        :return: The absolute path to a new local temporary directory. This directory will exist
                 for the duration of the job only, and is guaranteed to be deleted once the job
                 terminates, removing all files it contains recursively.
        :rtype: str
        """
        return os.path.abspath(tempfile.mkdtemp(prefix="t", dir=self.localTempDir))

    def getLocalTempFile(self):
        """
        Get a new local temporary file that will persist for the duration of the job.

        :return: The absolute path to a local temporary file. This file will exist for the
                 duration of the job only, and is guaranteed to be deleted once the job terminates.
        :rtype: str
        """
        handle, tmpFile = tempfile.mkstemp(prefix="tmp", suffix=".tmp", dir=self.localTempDir)
        os.close(handle)
        return os.path.abspath(tmpFile)

    def getLocalTempFileName(self):
        """
        Get a valid name for a new local file. Don't actually create a file at the path.

        :return: Path to valid file
        :rtype: str
        """
        # Create, and then delete a temp file. Creating will guarantee you a unique, unused
        # file name. There is a very, very, very low chance that another job will create the
        # same file name in the span of this one being deleted and then being used by the user.
        tempFile = self.getLocalTempFile()
        os.remove(tempFile)
        return tempFile

    # Functions related to reading, writing and removing files to/from the job store
    @abstractmethod
    def writeGlobalFile(self, localFileName, cleanup=False):
        """
        Takes a file (as a path) and uploads it to the job store.

        :param string localFileName: The path to the local file to upload.
        :param bool cleanup: if True then the copy of the global file will be deleted once the
               job and all its successors have completed running.  If not the global file must be
               deleted manually.
        :return: an ID that can be used to retrieve the file.
        :rtype: toil.fileStore.FileID
        """
        raise NotImplementedError()

    @contextmanager
    def writeGlobalFileStream(self, cleanup=False):
        """
        Similar to writeGlobalFile, but allows the writing of a stream to the job store.
        The yielded file handle does not need to and should not be closed explicitly.

        :param bool cleanup: is as in :func:`toil.fileStore.FileStore.writeGlobalFile`.
        :return: A context manager yielding a tuple of
                  1) a file handle which can be written to and
                  2) the toil.fileStore.FileID of the resulting file in the job store.
        """
        
        # TODO: Make this work with FileID
        with self.jobStore.writeFileStream(None if not cleanup else self.jobGraph.jobStoreID) as (backingStream, fileStoreID):
            # We have a string version of the file ID, and the backing stream.
            # We need to yield a stream the caller can write to, and a FileID
            # that accurately reflects the size of the data written to the
            # stream. We assume the stream is not seekable.
            
            # Make and keep a reference to the file ID, which is currently empty
            fileID = FileID(fileStoreID, 0)
            
            # Wrap the stream to increment the file ID's size for each byte written
            wrappedStream = WriteWatchingStream(backingStream)
            
            # When the stream is written to, count the bytes
            def handle(numBytes):
                fileID.size += numBytes 
            wrappedStream.onWrite(handle)
            
            yield wrappedStream, fileID

    @abstractmethod
    def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=False, symlink=False):
        """
        Makes the file associated with fileStoreID available locally. If mutable is True,
        then a copy of the file will be created locally so that the original is not modified
        and does not change the file for other jobs. If mutable is False, then a link can
        be created to the file, saving disk resources.

        If a user path is specified, it is used as the destination. If a user path isn't
        specified, the file is stored in the local temp directory with an encoded name.

        :param toil.fileStore.FileID fileStoreID: job store id for the file
        :param string userPath: a path to the name of file to which the global file will be copied
               or hard-linked (see below).
        :param bool cache: Described in :func:`toil.fileStore.CachingFileStore.readGlobalFile`
        :param bool mutable: Described in :func:`toil.fileStore.CachingFileStore.readGlobalFile`
        :return: An absolute path to a local, temporary copy of the file keyed by fileStoreID.
        :rtype: str
        """
        raise NotImplementedError()

    @abstractmethod
    def readGlobalFileStream(self, fileStoreID):
        """
        Similar to readGlobalFile, but allows a stream to be read from the job store. The yielded
        file handle does not need to and should not be closed explicitly.

        :return: a context manager yielding a file handle which can be read from.
        """
        raise NotImplementedError()

    @abstractmethod
    def deleteLocalFile(self, fileStoreID):
        """
        Deletes Local copies of files associated with the provided job store ID.

        :param str fileStoreID: File Store ID of the file to be deleted.
        """
        raise NotImplementedError()

    @abstractmethod
    def deleteGlobalFile(self, fileStoreID):
        """
        Deletes local files with the provided job store ID and then permanently deletes them from
        the job store. To ensure that the job can be restarted if necessary, the delete will not
        happen until after the job's run method has completed.

        :param fileStoreID: the job store ID of the file to be deleted.
        """
        raise NotImplementedError()

    # Functions used to read and write files directly between a source url and the job store.
    def importFile(self, srcUrl, sharedFileName=None):
        return self.jobStore.importFile(srcUrl, sharedFileName=sharedFileName)

    def exportFile(self, jobStoreFileID, dstUrl):
        raise NotImplementedError()

    # A utility method for accessing filenames
    def _resolveAbsoluteLocalPath(self, filePath):
        """
        Return the absolute path to filePath.  This is a wrapper for os.path.abspath because mac OS
        symlinks /tmp and /var (the most common places for a default tempdir) to /private/tmp and
        /private/var respectively.

        :param str filePath: The absolute or relative path to the file. If relative, it must be
               relative to the local temp working dir
        :return: Absolute path to key
        :rtype: str
        """
        if os.path.isabs(filePath):
            return os.path.abspath(filePath)
        else:
            return os.path.join(self.localTempDir, filePath)

    class _StateFile(object):
        """
        Utility class to read and write dill-ed state dictionaries from/to a file into a namespace.
        """
        def __init__(self, stateDict):
            assert isinstance(stateDict, dict)
            self.__dict__.update(stateDict)

        @abstractclassmethod
        @contextmanager
        def open(cls, outer=None):
            """
            This is a context manager that state file and reads it into an object that is returned
            to the user in the yield.

            :param outer: Instance of the calling class (to use outer methods).
            """
            raise NotImplementedError()

        @classmethod
        def _load(cls, fileName):
            """
            Load the state of the cache from the state file

            :param str fileName: Path to the cache state file.
            :return: An instance of the state as a namespace.
            :rtype: _StateFile
            """
            # Read the value from the cache state file then initialize and instance of
            # _CacheState with it.
            with open(fileName, 'rb') as fH:
                infoDict = dill.load(fH)
            return cls(infoDict)

        def write(self, fileName):
            """
            Write the current state into a temporary file then atomically rename it to the main
            state file.

            :param str fileName: Path to the state file.
            """
            with open(fileName + '.tmp', 'wb') as fH:
                # Based on answer by user "Mark" at:
                # http://stackoverflow.com/questions/2709800/how-to-pickle-yourself
                # We can't pickle nested classes. So we have to pickle the variables of the class
                # If we ever change this, we need to ensure it doesn't break FileID
                dill.dump(self.__dict__, fH)
            os.rename(fileName + '.tmp', fileName)

    # Methods related to the deferred function logic
    @abstractclassmethod
    def findAndHandleDeadJobs(cls, nodeInfo, batchSystemShutdown=False):
        """
        This function looks at the state of all jobs registered on the node and will handle them
        (clean up their presence ont he node, and run any registered defer functions)

        :param nodeInfo: Information regarding the node required for identifying dead jobs.
        :param bool batchSystemShutdown: Is the batch system in the process of shutting down?
        """
        raise NotImplementedError()

    @abstractmethod
    def _registerDeferredFunction(self, deferredFunction):
        """
        Register the given deferred function with this job.

        :param DeferredFunction deferredFunction: the function to register
        """
        raise NotImplementedError()

    @staticmethod
    def _runDeferredFunctions(deferredFunctions):
        """
        Invoke the specified deferred functions and return a list of names of functions that
        raised an exception while being invoked.

        :param list[DeferredFunction] deferredFunctions: the DeferredFunctions to run
        :rtype: list[str]
        """
        failures = []
        for deferredFunction in deferredFunctions:
            try:
                deferredFunction.invoke()
            except:
                failures.append(deferredFunction.name)
                logger.exception('%s failed.', deferredFunction)
        return failures

    # Functions related to logging
    def logToMaster(self, text, level=logging.INFO):
        """
        Send a logging message to the leader. The message will also be \
        logged by the worker at the same level.

        :param text: The string to log.
        :param int level: The logging level.
        """
        logger.log(level=level, msg=("LOG-TO-MASTER: " + text))
        self.loggingMessages.append(dict(text=text, level=level))

    # Functions run after the completion of the job.
    @abstractmethod
    def _updateJobWhenDone(self):
        """
        Update the status of the job on the disk.
        """
        raise NotImplementedError()

    @abstractmethod
    def _blockFn(self):
        """
        Blocks while _updateJobWhenDone is running. This function is called by this job's
        successor to ensure that it does not begin modifying the job store until after this job has
        finished doing so.
        """
        raise NotImplementedError()

    # Utility function used to identify if a pid is still running on the node.
    @staticmethod
    def _pidExists(pid):
        """
        This will return True if the process associated with pid is still running on the machine.
        This is based on stackoverflow question 568271.

        :param int pid: ID of the process to check for
        :return: True/False
        :rtype: bool
        """
        assert pid > 0
        try:
            os.kill(pid, 0)
        except OSError as err:
            if err.errno == errno.ESRCH:
                # ESRCH == No such process
                return False
            else:
                raise
        else:
            return True

    @abstractclassmethod
    def shutdown(cls, dir_):
        """
        Shutdown the filestore on this node.

        This is intended to be called on batch system shutdown.

        :param dir_: The jeystone directory containing the required information for fixing the state
               of failed workers on the node before cleaning up.
        """
        raise NotImplementedError()


class CachingFileStore(FileStore):
    """
    A cache-enabled file store that attempts to use hard-links and asynchronous job store writes to
    reduce I/O between, and during jobs.
    """

    def __init__(self, jobStore, jobGraph, localTempDir, inputBlockFn):
        super(CachingFileStore, self).__init__(jobStore, jobGraph, localTempDir, inputBlockFn)
        # Variables related to asynchronous writes.
        self.workerNumber = 2
        self.queue = Queue()
        self.updateSemaphore = Semaphore()
        self.workers = [Thread(target=self.asyncWrite) for i in range(self.workerNumber)]
        for worker in self.workers:
            worker.start()
        # Variables related to caching
        # cacheDir has to be 1 levels above local worker tempdir, at the same level as the
        # worker dirs. At this point, localTempDir is the worker directory, not the job
        # directory.
        self.localCacheDir = os.path.join(os.path.dirname(localTempDir),
                                          cacheDirName(self.jobStore.config.workflowID))
        self.cacheLockFile = os.path.join(self.localCacheDir, '.cacheLock')
        self.cacheStateFile = os.path.join(self.localCacheDir, '_cacheState')
        # Since each worker has it's own unique CachingFileStore instance, and only one Job can run
        # at a time on a worker, we can bookkeep the job's file store operated files in a
        # dictionary.
        self.jobSpecificFiles = {}
        self.jobName = str(self.jobGraph)
        self.jobID = sha1(self.jobName.encode('utf-8')).hexdigest()
        logger.debug('Starting job (%s) with ID (%s).', self.jobName, self.jobID)
        # A variable to describe how many hard links an unused file in the cache will have.
        # Also used as the canonical place to record whether we are using a
        # FileJobStore that we can just reach into and that will own the disk
        # usage of files.
        self.nlinkThreshold = None
        self.workflowAttemptNumber = self.jobStore.config.workflowAttemptNumber
        # This is a flag to better resolve cache equation imbalances at cleanup time.
        self.cleanupInProgress = False
        # Now that we've setup all the required variables, setup the cache directory for the
        # job if required.
        self._setupCache()

    @contextmanager
    def open(self, job):
        """
        This context manager decorated method allows cache-specific operations to be conducted
        before and after the execution of a job in worker.py
        """
        # Create a working directory for the job
        startingDir = os.getcwd()
        self.localTempDir = makePublicDir(os.path.join(self.localTempDir, str(uuid.uuid4())))
        # Check the status of all jobs on this node. If there are jobs that started and died before
        # cleaning up their presence from the cache state file, restore the cache file to a state
        # where the jobs don't exist.
        with self._CacheState.open(self) as cacheInfo:
            self.findAndHandleDeadJobs(cacheInfo)
            # While we have a lock on the cache file, run a naive check to see if jobs on this node
            # have greatly gone over their requested limits.
            if cacheInfo.sigmaJob < 0:
                logger.warning('Detecting that one or more jobs on this node have used more '
                               'resources than requested.  Turn on debug logs to see more'
                               'information on cache usage.')
        # Get the requirements for the job and clean the cache if necessary. cleanCache will
        # ensure that the requirements for this job are stored in the state file.
        jobReqs = job.disk
        # Cleanup the cache to free up enough space for this job (if needed)
        self.cleanCache(jobReqs)
        try:
            os.chdir(self.localTempDir)
            yield
        finally:
            diskUsed = getDirSizeRecursively(self.localTempDir)
            logString = ("Job {jobName} used {percent:.2f}% ({humanDisk}B [{disk}B] used, "
                         "{humanRequestedDisk}B [{requestedDisk}B] requested) at the end of "
                         "its run.".format(jobName=self.jobName,
                                           percent=(float(diskUsed) / jobReqs * 100 if
                                                    jobReqs > 0 else 0.0),
                                           humanDisk=bytes2human(diskUsed),
                                           disk=diskUsed,
                                           humanRequestedDisk=bytes2human(jobReqs),
                                           requestedDisk=jobReqs))
            self.logToMaster(logString, level=logging.DEBUG)
            if diskUsed > jobReqs:
                self.logToMaster("Job used more disk than requested. Please reconsider modifying "
                                 "the user script to avoid the chance  of failure due to "
                                 "incorrectly requested resources. " + logString,
                                 level=logging.WARNING)
            os.chdir(startingDir)
            self.cleanupInProgress = True
            # Delete all the job specific files and return sizes to jobReqs
            self.returnJobReqs(jobReqs)
            with self._CacheState.open(self) as cacheInfo:
                # Carry out any user-defined cleanup actions
                deferredFunctions = cacheInfo.jobState[self.jobID]['deferredFunctions']
                failures = self._runDeferredFunctions(deferredFunctions)
                for failure in failures:
                    self.logToMaster('Deferred function "%s" failed.' % failure, logging.WARN)
                # Finally delete the job from the cache state file
                cacheInfo.jobState.pop(self.jobID)

    # Functions related to reading, writing and removing files to/from the job store
    def writeGlobalFile(self, localFileName, cleanup=False):
        """
        Takes a file (as a path) and uploads it to the job store.  Depending on the jobstore
        used, carry out the appropriate cache functions.
        """
        absLocalFileName = self._resolveAbsoluteLocalPath(localFileName)
        # Work out what job created the file.
        # The job store takes this as a hint.
        creatorID = self.jobGraph.jobStoreID
        # If the file is from the scope of local temp dir
        if absLocalFileName.startswith(self.localTempDir):
            # If the job store is of type FileJobStore and the job store and the local temp dir
            # are on the same file system, then we want to hard link the files instead of copying
            # barring the case where the file being written was one that was previously read
            # from the file store. In that case, you want to copy to the file store so that
            # the two have distinct nlink counts.
            # Can read without a lock because we're only reading job-specific info.
            jobSpecificFiles = list(self._CacheState._load(self.cacheStateFile).jobState[
                self.jobID]['filesToFSIDs'].keys())
            # Saying nlink is 2 implicitly means we are using the job file store, and it is on
            # the same device as the work dir.
            if self.nlinkThreshold == 2 and absLocalFileName not in jobSpecificFiles:
                jobStoreFileID = self.jobStore.getEmptyFileStoreID(creatorID, cleanup)
                # getEmptyFileStoreID creates the file in the scope of the job store hence we
                # need to delete it before linking.
                # TODO: expose a link operation on the job store interface.
                filePath = self.jobStore._getFilePathFromId(jobStoreFileID)
                os.remove(filePath)
                os.link(absLocalFileName, filePath)
            # If they're not on the file system, or if the file is already linked with an
            # existing file, we need to copy to the job store.
            # Check if the user allows asynchronous file writes
            elif self.jobStore.config.useAsync:
                jobStoreFileID = self.jobStore.getEmptyFileStoreID(creatorID, cleanup)
                # Before we can start the async process, we should also create a dummy harbinger
                # file in the cache such that any subsequent jobs asking for this file will not
                # attempt to download it from the job store till the write is complete.  We do
                # this now instead of in the writing thread because there is an edge case where
                # readGlobalFile in a subsequent job is called before the writing thread has
                # received the message to write the file and has created the dummy harbinger
                # (and the file was unable to be cached/was evicted from the cache).
                harbingerFile = self.HarbingerFile(self, fileStoreID=jobStoreFileID)
                harbingerFile.write()
                fileHandle = open(absLocalFileName, 'rb')
                with self._pendingFileWritesLock:
                    self._pendingFileWrites.add(jobStoreFileID)
                # A file handle added to the queue allows the asyncWrite threads to remove their
                # jobID from _pendingFileWrites. Therefore, a file should only be added after
                # its fileID is added to _pendingFileWrites
                self.queue.put((fileHandle, jobStoreFileID))
            # Else write directly to the job store.
            else:
                jobStoreFileID = self.jobStore.writeFile(absLocalFileName, creatorID, cleanup)
            # Local files are cached by default, unless they were written from previously read
            # files.
            if absLocalFileName not in jobSpecificFiles:
                self.addToCache(absLocalFileName, jobStoreFileID, 'write')
            else:
                self._JobState.updateJobSpecificFiles(self, jobStoreFileID, absLocalFileName,
                                                      0.0, False)
        # Else write directly to the job store.
        else:
            jobStoreFileID = self.jobStore.writeFile(absLocalFileName, creatorID, cleanup)
            # Non local files are NOT cached by default, but they are tracked as local files.
            self._JobState.updateJobSpecificFiles(self, jobStoreFileID, None,
                                                  0.0, False)
        return FileID.forPath(jobStoreFileID, absLocalFileName)

    def writeGlobalFileStream(self, cleanup=False):
        # TODO: Make this work with caching
        return super(CachingFileStore, self).writeGlobalFileStream(cleanup)

    def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=False, symlink=False):
        """
        Downloads a file described by fileStoreID from the file store to the local directory.
        The function first looks for the file in the cache and if found, it hardlinks to the
        cached copy instead of downloading.

        The cache parameter will be used only if the file isn't already in the cache, and
        provided user path (if specified) is in the scope of local temp dir.

        :param bool cache: If True, a copy of the file will be saved into a cache that can be
               used by other workers. caching supports multiple concurrent workers requesting the
               same file by allowing only one to download the file while the others wait for it to
               complete.
        :param bool mutable: If True, the file path returned points to a file that is
               modifiable by the user. Using False is recommended as it saves disk by making
               multiple workers share a file via hard links. The default is False.
        """
        # Check that the file hasn't been deleted by the user
        if fileStoreID in self.filesToDelete:
            raise RuntimeError('Trying to access a file in the jobStore you\'ve deleted: ' + \
                               '%s' % fileStoreID)
        # Get the name of the file as it would be in the cache
        cachedFileName = self.encodedFileID(fileStoreID)
        # setup the harbinger variable for the file.  This is an identifier that the file is
        # currently being downloaded by another job and will be in the cache shortly. It is used
        # to prevent multiple jobs from simultaneously downloading the same file from the file
        # store.
        harbingerFile = self.HarbingerFile(self, cachedFileName=cachedFileName)
        # setup the output filename.  If a name is provided, use it - This makes it a Named
        # Local File. If a name isn't provided, use the base64 encoded name such that we can
        # easily identify the files later on.
        if userPath is not None:
            localFilePath = self._resolveAbsoluteLocalPath(userPath)
            if os.path.exists(localFilePath):
                # yes, this is illegal now.
                raise RuntimeError(' File %s ' % localFilePath + ' exists. Cannot Overwrite.')
            fileIsLocal = True if localFilePath.startswith(self.localTempDir) else False
        else:
            localFilePath = self.getLocalTempFileName()
            fileIsLocal = True
        # First check whether the file is in cache.  If it is, then hardlink the file to
        # userPath. Cache operations can only occur on local files.
        with self.cacheLock() as lockFileHandle:
            if fileIsLocal and self._fileIsCached(fileStoreID):
                logger.debug('CACHE: Cache hit on file with ID \'%s\'.' % fileStoreID)
                assert not os.path.exists(localFilePath)
                if mutable:
                    shutil.copyfile(cachedFileName, localFilePath)
                    cacheInfo = self._CacheState._load(self.cacheStateFile)
                    jobState = self._JobState(cacheInfo.jobState[self.jobID])
                    jobState.addToJobSpecFiles(fileStoreID, localFilePath, -1, None)
                    cacheInfo.jobState[self.jobID] = jobState.__dict__
                    cacheInfo.write(self.cacheStateFile)
                else:
                    os.link(cachedFileName, localFilePath)
                    self.returnFileSize(fileStoreID, localFilePath, lockFileHandle,
                                        fileAlreadyCached=True)
            # If the file is not in cache, check whether the .harbinger file for the given
            # FileStoreID exists.  If it does, the wait and periodically check for the removal
            # of the file and the addition of the completed download into cache of the file by
            # the other job. Then we link to it.
            elif fileIsLocal and harbingerFile.exists():
                harbingerFile.waitOnDownload(lockFileHandle)
                # If the code reaches here, the harbinger file has been removed. This means
                # either the file was successfully downloaded and added to cache, or something
                # failed. To prevent code duplication, we recursively call readGlobalFile.
                flock(lockFileHandle, LOCK_UN)
                return self.readGlobalFile(fileStoreID, userPath=userPath, cache=cache,
                                           mutable=mutable)
            # If the file is not in cache, then download it to the userPath and then add to
            # cache if specified.
            else:
                logger.debug('CACHE: Cache miss on file with ID \'%s\'.' % fileStoreID)
                if fileIsLocal and cache:
                    # If caching of the downloaded file is desired, First create the harbinger
                    # file so other jobs know not to redundantly download the same file.  Write
                    # the PID of this process into the file so other jobs know who is carrying
                    # out the download.
                    harbingerFile.write()
                    # Now release the file lock while the file is downloaded as download could
                    # take a while.
                    flock(lockFileHandle, LOCK_UN)
                    # Use try:finally: so that the .harbinger file is removed whether the
                    # download succeeds or not.
                    try:
                        self.jobStore.readFile(fileStoreID,
                                               '/.'.join(os.path.split(cachedFileName)),
                                               symlink=False)
                    except:
                        if os.path.exists('/.'.join(os.path.split(cachedFileName))):
                            os.remove('/.'.join(os.path.split(cachedFileName)))
                        raise
                    else:
                        # If the download succeded, officially add the file to cache (by
                        # recording it in the cache lock file) if possible.
                        if os.path.exists('/.'.join(os.path.split(cachedFileName))):
                            os.rename('/.'.join(os.path.split(cachedFileName)), cachedFileName)
                            # If this is not true we get into trouble in our internal reference counting.
                            assert(os.stat(cachedFileName).st_nlink == self.nlinkThreshold)
                            self.addToCache(localFilePath, fileStoreID, 'read', mutable)
                            # We don't need to return the file size here because addToCache
                            # already does it for us
                    finally:
                        # In any case, delete the harbinger file.
                        harbingerFile.delete()
                else:
                    # Release the cache lock since the remaining stuff is not cache related.
                    flock(lockFileHandle, LOCK_UN)
                    self.jobStore.readFile(fileStoreID, localFilePath, symlink=False)
                    # Make sure we got a file with the number of links we expect.
                    # If this is not true we get into trouble in our internal reference counting.
                    assert(os.stat(localFilePath).st_nlink == self.nlinkThreshold)
                    os.chmod(localFilePath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                    # Now that we have the file, we have 2 options. It's modifiable or not.
                    # Either way, we need to account for FileJobStore making links instead of
                    # copies.
                    if mutable:
                        if self.nlinkThreshold == 2:
                            # nlinkThreshold can only be 1 or 2 and it can only be 2 iff the
                            # job store is FilejobStore, and the job store and local temp dir
                            # are on the same device. An atomic rename removes the nlink on the
                            # file handle linked from the job store.
                            shutil.copyfile(localFilePath, localFilePath + '.tmp')
                            os.rename(localFilePath + '.tmp', localFilePath)
                        self._JobState.updateJobSpecificFiles(self, fileStoreID, localFilePath,
                                                              -1, False)
                    # If it was immutable
                    else:
                        if self.nlinkThreshold == 2:
                            self._accountForNlinkEquals2(localFilePath)
                        self._JobState.updateJobSpecificFiles(self, fileStoreID, localFilePath,
                                                              0.0, False)
        return localFilePath

    def exportFile(self, jobStoreFileID, dstUrl):
        while jobStoreFileID in self._pendingFileWrites:
            # The file is still being writting to the job store - wait for this process to finish prior to
            # exporting it
            time.sleep(1)
        self.jobStore.exportFile(jobStoreFileID, dstUrl)

    def readGlobalFileStream(self, fileStoreID):
        if fileStoreID in self.filesToDelete:
            raise RuntimeError(
                "Trying to access a file in the jobStore you've deleted: %s" % fileStoreID)

        # If fileStoreID is in the cache provide a handle from the local cache
        if self._fileIsCached(fileStoreID):
            logger.debug('CACHE: Cache hit on file with ID \'%s\'.' % fileStoreID)
            return open(self.encodedFileID(fileStoreID), 'rb')
        else:
            logger.debug('CACHE: Cache miss on file with ID \'%s\'.' % fileStoreID)
            return self.jobStore.readFileStream(fileStoreID)

    def deleteLocalFile(self, fileStoreID):
        # The local file may or may not have been cached. If it was, we need to do some
        # bookkeeping. If it wasn't, we just delete the file and continue with no might need
        # some bookkeeping if the file store and cache live on the same filesystem. We can know
        # if a file was cached or not based on the value held in the third tuple value for the
        # dict item having key = fileStoreID. If it was cached, it holds the value True else
        # False.
        with self._CacheState.open(self) as cacheInfo:
            jobState = self._JobState(cacheInfo.jobState[self.jobID])
            if fileStoreID not in list(jobState.jobSpecificFiles.keys()):
                # EOENT indicates that the file did not exist
                raise OSError(errno.ENOENT, "Attempting to delete a non-local file")
            # filesToDelete is a dictionary of file: fileSize
            filesToDelete = jobState.jobSpecificFiles[fileStoreID]
            allOwnedFiles = jobState.filesToFSIDs
            for (fileToDelete, fileSize) in list(filesToDelete.items()):
                # Handle the case where a file not in the local temp dir was written to
                # filestore
                if fileToDelete is None:
                    filesToDelete.pop(fileToDelete)
                    allOwnedFiles[fileToDelete].remove(fileStoreID)
                    cacheInfo.jobState[self.jobID] = jobState.__dict__
                    cacheInfo.write(self.cacheStateFile)
                    continue
                # If the file size is zero (copied into the local temp dir) or -1 (mutable), we
                # can safely delete without any bookkeeping
                if fileSize in (0, -1):
                    # Only remove the file if there is only one FSID associated with it.
                    if len(allOwnedFiles[fileToDelete]) == 1:
                        try:
                            os.remove(fileToDelete)
                        except OSError as err:
                            if err.errno == errno.ENOENT and fileSize == -1:
                                logger.debug('%s was read mutably and deleted by the user',
                                             fileToDelete)
                            else:
                                raise IllegalDeletionCacheError(fileToDelete)
                    allOwnedFiles[fileToDelete].remove(fileStoreID)
                    filesToDelete.pop(fileToDelete)
                    cacheInfo.jobState[self.jobID] = jobState.__dict__
                    cacheInfo.write(self.cacheStateFile)
                    continue
                # If not, we need to do bookkeeping
                # Get the size of the file to be deleted, and the number of jobs using the file
                # at the moment.
                if not os.path.exists(fileToDelete):
                    raise IllegalDeletionCacheError(fileToDelete)
                fileStats = os.stat(fileToDelete)
                if fileSize != fileStats.st_size:
                    logger.warn("the size on record differed from the real size by " +
                                "%s bytes" % str(fileSize - fileStats.st_size))
                # Remove the file and return file size to the job
                if len(allOwnedFiles[fileToDelete]) == 1:
                    os.remove(fileToDelete)
                cacheInfo.sigmaJob += fileSize
                filesToDelete.pop(fileToDelete)
                allOwnedFiles[fileToDelete].remove(fileStoreID)
                jobState.updateJobReqs(fileSize, 'remove')
                cacheInfo.jobState[self.jobID] = jobState.__dict__
            # If the job is not in the process of cleaning up, then we may need to remove the
            # cached copy of the file as well.
            if not self.cleanupInProgress:
                # If the file is cached and if other jobs are using the cached copy of the file,
                # or if retaining the file in the cache doesn't affect the cache equation, then
                # don't remove it from cache.
                if self._fileIsCached(fileStoreID):
                    cachedFile = self.encodedFileID(fileStoreID)
                    jobsUsingFile = os.stat(cachedFile).st_nlink
                    if not cacheInfo.isBalanced() and jobsUsingFile == self.nlinkThreshold:
                        os.remove(cachedFile)
                        cacheInfo.cached -= fileSize
                self.logToMaster('Successfully deleted cached copy of file with ID '
                                 '\'%s\'.' % fileStoreID, level=logging.DEBUG)
            self.logToMaster('Successfully deleted local copies of file with ID '
                             '\'%s\'.' % fileStoreID, level=logging.DEBUG)

    def deleteGlobalFile(self, fileStoreID):
        jobStateIsPopulated = False
        with self._CacheState.open(self) as cacheInfo:
            if self.jobID in cacheInfo.jobState:
                jobState = self._JobState(cacheInfo.jobState[self.jobID])
                jobStateIsPopulated = True
        if jobStateIsPopulated and fileStoreID in list(jobState.jobSpecificFiles.keys()):
            # Use deleteLocalFile in the backend to delete the local copy of the file.
            self.deleteLocalFile(fileStoreID)
            # At this point, the local file has been deleted, and possibly the cached copy. If
            # the cached copy exists, it is either because another job is using the file, or
            # because retaining the file in cache doesn't unbalance the caching equation. The
            # first case is unacceptable for deleteGlobalFile and the second requires explicit
            # deletion of the cached copy.
        # Check if the fileStoreID is in the cache. If it is, ensure only the current job is
        # using it.
        cachedFile = self.encodedFileID(fileStoreID)
        if os.path.exists(cachedFile):
            self.removeSingleCachedFile(fileStoreID)
        # Add the file to the list of files to be deleted once the run method completes.
        self.filesToDelete.add(fileStoreID)
        self.logToMaster('Added file with ID \'%s\' to the list of files to be' % fileStoreID +
                         ' globally deleted.', level=logging.DEBUG)

    # Cache related methods
    @contextmanager
    def cacheLock(self):
        """
        This is a context manager to acquire a lock on the Lock file that will be used to
        prevent synchronous cache operations between workers.
        :yields: File descriptor for cache lock file in w mode
        """
        cacheLockFile = open(self.cacheLockFile, 'w')
        try:
            flock(cacheLockFile, LOCK_EX)
            logger.debug("CACHE: Obtained lock on file %s" % self.cacheLockFile)
            yield cacheLockFile
        except IOError:
            logger.critical('CACHE: Unable to acquire lock on %s' % self.cacheLockFile)
            raise
        finally:
            cacheLockFile.close()
            logger.debug("CACHE: Released lock")

    def _setupCache(self):
        """
        Setup the cache based on the provided values for localCacheDir.
        """
        # we first check whether the cache directory exists. If it doesn't, create it.
        if not os.path.exists(self.localCacheDir):
            # Create a temporary directory as this worker's private cache. If all goes well, it
            # will be renamed into the cache for this node.
            personalCacheDir = ''.join([os.path.dirname(self.localCacheDir), '/.ctmp-',
                                        str(uuid.uuid4())])
            os.mkdir(personalCacheDir, 0o755)
            self._createCacheLockFile(personalCacheDir)
            try:
                os.rename(personalCacheDir, self.localCacheDir)
            except OSError as err:
                # The only acceptable FAIL case is that the destination is a non-empty directory
                # directory.  Assuming (it's ambiguous) atomic renaming of directories, if the
                # dst is non-empty, it only means that another worker has beaten this one to the
                # rename.
                if err.errno == errno.ENOTEMPTY:
                    # Cleanup your own mess.  It's only polite.
                    shutil.rmtree(personalCacheDir)
                else:
                    raise
        # You can't reach here unless a local cache directory has been created successfully
        with self._CacheState.open(self) as cacheInfo:
            # Ensure this cache is from the correct attempt at the workflow!  If it isn't, we
            # need to reset the cache lock file
            if cacheInfo.attemptNumber != self.workflowAttemptNumber:
                if cacheInfo.nlink == 2:
                    cacheInfo.cached = 0  # cached file sizes are accounted for by job store
                else:
                    allCachedFiles = [os.path.join(self.localCacheDir, x)
                                      for x in os.listdir(self.localCacheDir)
                                      if not self._isHidden(x)]
                    cacheInfo.cached = sum([os.stat(cachedFile).st_size
                                            for cachedFile in allCachedFiles])
                    # TODO: Delete the working directories
                cacheInfo.sigmaJob = 0
                cacheInfo.attemptNumber = self.workflowAttemptNumber
            self.nlinkThreshold = cacheInfo.nlink

    def _createCacheLockFile(self, tempCacheDir):
        """
        Create the cache lock file file to contain the state of the cache on the node.

        :param str tempCacheDir: Temporary directory to use for setting up a cache lock file the
               first time.
        """
        # The nlink threshold is setup along with the first instance of the cache class on the
        # node. It needs the cache dir to sniff link count for files form the job store.
        self.setNlinkThreshold(tempCacheDir)
        # Get the free space on the device
        freeSpace, _ = getFileSystemSize(tempCacheDir)
        # Create the cache lock file.
        open(os.path.join(tempCacheDir, os.path.basename(self.cacheLockFile)), 'w').close()
        # Setup the cache state file
        personalCacheStateFile = os.path.join(tempCacheDir,
                                              os.path.basename(self.cacheStateFile))
        # Setup the initial values for the cache state file in a dict
        cacheInfo = self._CacheState({
            'nlink': self.nlinkThreshold,
            'attemptNumber': self.workflowAttemptNumber,
            'total': freeSpace,
            'cached': 0,
            'sigmaJob': 0,
            'cacheDir': self.localCacheDir,
            'jobState': {}})
        cacheInfo.write(personalCacheStateFile)

    def encodedFileID(self, jobStoreFileID):
        """
        Uses a url safe base64 encoding to encode the jobStoreFileID into a unique identifier to
        use as filename within the cache folder.  jobstore IDs are essentially urls/paths to
        files and thus cannot be used as is. Base64 encoding is used since it is reversible.

        :param jobStoreFileID: string representing a job store file ID
        :return: outCachedFile: A path to the hashed file in localCacheDir
        :rtype: str
        """
        
        base64Text = base64.urlsafe_b64encode(jobStoreFileID.encode('utf-8')).decode('utf-8')
        
        outCachedFile = os.path.join(self.localCacheDir, base64Text)
        return outCachedFile

    def _fileIsCached(self, jobStoreFileID):
        """
        Is the file identified by jobStoreFileID in cache or not.
        """
        return os.path.exists(self.encodedFileID(jobStoreFileID))

    def decodedFileID(self, cachedFilePath):
        """
        Decode a cached fileName back to a job store file ID.

        :param str cachedFilePath: Path to the cached file
        :return: The jobstore file ID associated with the file
        :rtype: str
        """
        fileDir, fileName = os.path.split(cachedFilePath)
        assert fileDir == self.localCacheDir, 'Can\'t decode uncached file names'
        # We encode and decode here because base64 can't work with unencoded text
        # Its probably worth, later, converting all file name variables to bytes
        # and not text.
        return base64.urlsafe_b64decode(fileName.encode('utf-8')).decode('utf-8')

    def addToCache(self, localFilePath, jobStoreFileID, callingFunc, mutable=False):
        """
        Used to process the caching of a file. This depends on whether a file is being written
        to file store, or read from it.
        WRITING
        The file is in localTempDir. It needs to be linked into cache if possible.
        READING
        The file is already in the cache dir. Depending on whether it is modifiable or not, does
        it need to be linked to the required location, or copied. If it is copied, can the file
        still be retained in cache?

        :param str localFilePath: Path to the Source file
        :param jobStoreFileID: jobStoreID for the file
        :param str callingFunc: Who called this function, 'write' or 'read'
        :param bool mutable: See modifiable in readGlobalFile
        """
        assert callingFunc in ('read', 'write')
        with self.cacheLock() as lockFileHandle:
            cachedFile = self.encodedFileID(jobStoreFileID)
            # The file to be cached MUST originate in the environment of the TOIL temp directory
            if (os.stat(self.localCacheDir).st_dev !=
                    os.stat(os.path.dirname(localFilePath)).st_dev):
                raise InvalidSourceCacheError('Attempting to cache a file across file systems '
                                              'cachedir = %s, file = %s.' % (self.localCacheDir,
                                                                             localFilePath))
            if not localFilePath.startswith(self.localTempDir):
                raise InvalidSourceCacheError('Attempting a cache operation on a non-local file '
                                              '%s.' % localFilePath)
            if callingFunc == 'read' and mutable:
                shutil.copyfile(cachedFile, localFilePath)
                fileSize = os.stat(cachedFile).st_size
                cacheInfo = self._CacheState._load(self.cacheStateFile)
                cacheInfo.cached += fileSize if cacheInfo.nlink != 2 else 0
                if not cacheInfo.isBalanced():
                    os.remove(cachedFile)
                    cacheInfo.cached -= fileSize if cacheInfo.nlink != 2 else 0
                    logger.debug('Could not download both download ' +
                                 '%s as mutable and add to ' % os.path.basename(localFilePath) +
                                 'cache. Hence only mutable copy retained.')
                else:
                    logger.debug('CACHE: Added file with ID \'%s\' to the cache.' %
                                jobStoreFileID)
                jobState = self._JobState(cacheInfo.jobState[self.jobID])
                jobState.addToJobSpecFiles(jobStoreFileID, localFilePath, -1, False)
                cacheInfo.jobState[self.jobID] = jobState.__dict__
                cacheInfo.write(self.cacheStateFile)
            else:
                # There are two possibilities, read and immutable, and write. both cases do
                # almost the same thing except for the direction of the os.link hence we're
                # writing them together.
                if callingFunc == 'read':  # and mutable is inherently False
                    src = cachedFile
                    dest = localFilePath
                    # To mirror behaviour of shutil.copyfile
                    if os.path.exists(dest):
                        os.remove(dest)
                else:  # write
                    src = localFilePath
                    dest = cachedFile
                try:
                    os.link(src, dest)
                except OSError as err:
                    if err.errno != errno.EEXIST:
                        raise
                    # If we get the EEXIST error, it can only be from write since in read we are
                    # explicitly deleting the file.  This shouldn't happen with the .partial
                    # logic hence we raise a cache error.
                    raise CacheError('Attempting to recache a file %s.' % src)
                else:
                    # Chmod the cached file. Cached files can never be modified.
                    os.chmod(cachedFile, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                    # Return the filesize of cachedFile to the job and increase the cached size
                    # The values passed here don't matter since rFS looks at the file only for
                    # the stat
                    self.returnFileSize(jobStoreFileID, localFilePath, lockFileHandle,
                                        fileAlreadyCached=False)
                if callingFunc == 'read':
                    logger.debug('CACHE: Read file with ID \'%s\' from the cache.' %
                                 jobStoreFileID)
                else:
                    logger.debug('CACHE: Added file with ID \'%s\' to the cache.' %
                                 jobStoreFileID)

    def returnFileSize(self, fileStoreID, cachedFileSource, lockFileHandle,
                       fileAlreadyCached=False):
        """
        Returns the fileSize of the file described by fileStoreID to the job requirements pool
        if the file was recently added to, or read from cache (A job that reads n bytes from
        cache doesn't really use those n bytes as a part of it's job disk since cache is already
        accounting for that disk space).

        :param fileStoreID: fileStore ID of the file bein added to cache
        :param str cachedFileSource: File being added to cache
        :param file lockFileHandle: Open file handle to the cache lock file
        :param bool fileAlreadyCached: A flag to indicate whether the file was already cached or
               not. If it was, then it means that you don't need to add the filesize to cache again.
        """
        fileSize = os.stat(cachedFileSource).st_size
        cacheInfo = self._CacheState._load(self.cacheStateFile)
        # If the file isn't cached, add the size of the file to the cache pool. However, if the
        # nlink threshold is not 1 -  i.e. it is 2 (it can only be 1 or 2), then don't do this
        # since the size of the file is accounted for by the file store copy.
        if not fileAlreadyCached and self.nlinkThreshold == 1:
            cacheInfo.cached += fileSize
        cacheInfo.sigmaJob -= fileSize
        if not cacheInfo.isBalanced():
            self.logToMaster('CACHE: The cache was not balanced on returning file size',
                             logging.WARN)
        # Add the info to the job specific cache info
        jobState = self._JobState(cacheInfo.jobState[self.jobID])
        jobState.addToJobSpecFiles(fileStoreID, cachedFileSource, fileSize, True)
        cacheInfo.jobState[self.jobID] = jobState.__dict__
        cacheInfo.write(self.cacheStateFile)

    @staticmethod
    def _isHidden(filePath):
        """
        This is a function that checks whether filePath is hidden

        :param str filePath: Path to the file under consideration
        :return: A boolean indicating whether the file is hidden or not.
        :rtype: bool
        """
        assert isinstance(filePath, (str, bytes))
        # I can safely assume i will never see an empty string because this is always called on
        # the results of an os.listdir()
        return filePath[0] in ('.', '_')

    def cleanCache(self, newJobReqs):
        """
        Cleanup all files in the cache directory to ensure that at lead newJobReqs are available
        for use.

        :param float newJobReqs: the total number of bytes of files allowed in the cache.
        """
        with self._CacheState.open(self) as cacheInfo:
            # Add the new job's disk requirements to the sigmaJobDisk variable
            cacheInfo.sigmaJob += newJobReqs
            # Initialize the job state here. we use a partial in the jobSpecificFiles call so
            # that this entire thing is pickleable. Based on answer by user Nathaniel Gentile at
            # http://stackoverflow.com/questions/2600790
            assert self.jobID not in cacheInfo.jobState
            cacheInfo.jobState[self.jobID] = {
                'jobName': self.jobName,
                'jobReqs': newJobReqs,
                'jobDir': self.localTempDir,
                'jobSpecificFiles': defaultdict(partial(defaultdict,int)),
                'filesToFSIDs': defaultdict(set),
                'pid': os.getpid(),
                'deferredFunctions': []}
            # If the caching equation is balanced, do nothing.
            if cacheInfo.isBalanced():
                return None

            # List of deletable cached files.  A deletable cache file is one
            #  that is not in use by any other worker (identified by the number of symlinks to
            # the file)
            allCacheFiles = [os.path.join(self.localCacheDir, x)
                             for x in os.listdir(self.localCacheDir)
                             if not self._isHidden(x)]
            allCacheFiles = [(path, os.stat(path)) for path in allCacheFiles]
            # TODO mtime vs ctime
            deletableCacheFiles = {(path, inode.st_mtime, inode.st_size)
                                   for path, inode in allCacheFiles
                                   if inode.st_nlink == self.nlinkThreshold}

            # Sort in descending order of mtime so the first items to be popped from the list
            # are the least recently created.
            deletableCacheFiles = sorted(deletableCacheFiles, key=lambda x: (-x[1], -x[2]))
            logger.debug('CACHE: Need %s bytes for new job. Detecting an estimated %s (out of a '
                         'total %s) bytes available for running the new job. The size of the cache '
                         'is %s bytes.', newJobReqs,
                         (cacheInfo.total - (cacheInfo.cached + cacheInfo.sigmaJob - newJobReqs)),
                         cacheInfo.total, cacheInfo.cached)
            logger.debug('CACHE: Evicting files to make room for the new job.')

            # Now do the actual file removal
            totalEvicted = 0
            while not cacheInfo.isBalanced() and len(deletableCacheFiles) > 0:
                cachedFile, fileCreateTime, cachedFileSize = deletableCacheFiles.pop()
                os.remove(cachedFile)
                cacheInfo.cached -= cachedFileSize if self.nlinkThreshold != 2 else 0
                totalEvicted += cachedFileSize
                assert cacheInfo.cached >= 0
                logger.debug('CACHE: Evicted  file with ID \'%s\' (%s bytes)' %
                             (self.decodedFileID(cachedFile), cachedFileSize))
            logger.debug('CACHE: Evicted a total of %s bytes. Available space is now %s bytes.',
                         totalEvicted,
                         (cacheInfo.total - (cacheInfo.cached + cacheInfo.sigmaJob - newJobReqs)))
            if not cacheInfo.isBalanced():
                raise CacheUnbalancedError()

    def removeSingleCachedFile(self, fileStoreID):
        """
        Removes a single file described by the fileStoreID from the cache forcibly.
        """
        with self._CacheState.open(self) as cacheInfo:
            cachedFile = self.encodedFileID(fileStoreID)
            cachedFileStats = os.stat(cachedFile)
            # We know the file exists because this function was called in the if block.  So we
            # have to ensure nothing has changed since then.
            assert cachedFileStats.st_nlink <= self.nlinkThreshold, \
                   'Attempting to delete a global file that is in use by another job.'

            assert cachedFileStats.st_nlink >= self.nlinkThreshold, \
                   'A global file has too FEW links at deletion time. Our link threshold is incorrect!'
            # Remove the file size from the cached file size if the jobstore is not fileJobStore
            # and then delete the file
            os.remove(cachedFile)
            if self.nlinkThreshold != 2:
                cacheInfo.cached -= cachedFileStats.st_size
            if not cacheInfo.isBalanced():
                self.logToMaster('CACHE: The cache was not balanced on removing single file',
                                 logging.WARN)
            self.logToMaster('CACHE: Successfully removed file with ID \'%s\'.' % fileStoreID)
        return None

    def setNlinkThreshold(self, tempCacheDir):
        # FIXME Can't do this at the top because of loopy (circular) import errors

        # We can't predict in advance whether the job store will give us hard links or not.
        # Even if the devices appear the same, the only way to know for sure is to try it.

        from toil.jobStores.fileJobStore import FileJobStore
        if isinstance(self.jobStore, FileJobStore):
            # A bunch of code depends on nlinkThreshold==2 -> jobStore is FileJobStore.
            # So only do this check if the job store is the file job store.

            # Create an empty file.
            emptyID = self.jobStore.getEmptyFileStoreID()

            # Read it out.
            # We have exclusive ownership of tempCacheDir at this point, so we
            # can just write any name in there.
            cachedFile = os.path.join(tempCacheDir, 'sniffLinkCount') 
            self.jobStore.readFile(emptyID, cachedFile, symlink=False)

            # Check the link count
            self.nlinkThreshold = os.stat(cachedFile).st_nlink

            # Only 1 or 2 is allowed.
            assert(self.nlinkThreshold == 1 or self.nlinkThreshold == 2)

            # Clean up
            os.unlink(cachedFile)
            self.jobStore.deleteFile(emptyID)
        else:
            # Unless we are on the file job store, we need to have a link count threshold of 1.
            # TODO: This relies on something the job store interface doesn't actually guarantee!
            self.nlinkThreshold = 1

    def _accountForNlinkEquals2(self, localFilePath):
        """
        This is a utility function that accounts for the fact that if nlinkThreshold == 2, the
        size of the file is accounted for by the file store copy of the file and thus the file
        size shouldn't be added to the cached file sizes.

        :param str localFilePath: Path to the local file that was linked to the file store copy.
        """
        fileStats = os.stat(localFilePath)
        assert fileStats.st_nlink >= self.nlinkThreshold
        with self._CacheState.open(self) as cacheInfo:
            cacheInfo.sigmaJob -= fileStats.st_size
            jobState = self._JobState(cacheInfo.jobState[self.jobID])
            jobState.updateJobReqs(fileStats.st_size, 'remove')

    def returnJobReqs(self, jobReqs):
        """
        This function returns the effective job requirements back to the pool after the job
        completes. It also deletes the local copies of files with the cache lock held.

        :param float jobReqs: Original size requirement of the job
        """
        # Since we are only reading this job's specific values from the state file, we don't
        # need a lock
        jobState = self._JobState(self._CacheState._load(self.cacheStateFile
                                                         ).jobState[self.jobID])
        for x in list(jobState.jobSpecificFiles.keys()):
            self.deleteLocalFile(x)
        with self._CacheState.open(self) as cacheInfo:
            cacheInfo.sigmaJob -= jobReqs
            # assert cacheInfo.isBalanced() # commenting this out for now. God speed

    class _CacheState(FileStore._StateFile):
        """
        Utility class to read and write the cache lock file. Also for checking whether the
        caching equation is balanced or not.  It extends the _StateFile class to add other cache
        related functions.
        """
        @classmethod
        @contextmanager
        def open(cls, outer=None):
            """
            This is a context manager that opens the cache state file and reads it into an object
            that is returned to the user in the yield
            """
            assert outer is not None
            with outer.cacheLock():
                cacheInfo = cls._load(outer.cacheStateFile)
                yield cacheInfo
                cacheInfo.write(outer.cacheStateFile)

        def isBalanced(self):
            """
            Checks for the inequality of the caching equation, i.e.
                            cachedSpace + sigmaJobDisk <= totalFreeSpace
            Essentially, the sum of all cached file + disk requirements of all running jobs
            should always be less than the available space on the system

            :return: Boolean for equation is balanced (T) or not (F)
            :rtype: bool
            """
            return self.cached + self.sigmaJob <= self.total

        def purgeRequired(self, jobReqs):
            """
            Similar to isBalanced, however it looks at the actual state of the system and
            decides whether an eviction is required.

            :return: Is a purge required(T) or no(F)
            :rtype: bool
            """
            return not self.isBalanced()
            # totalStats = os.statvfs(self.cacheDir)
            # totalFree = totalStats.f_bavail * totalStats.f_frsize
            # return totalFree < jobReqs

    # Methods related to the deferred function logic
    @classmethod
    def findAndHandleDeadJobs(cls, nodeInfo, batchSystemShutdown=False):
        """

        :param toil.fileStore.CachingFileStore._CacheState nodeInfo: The state of the node cache as
               a _CacheState object
        """
        # A list of tuples of (hashed job id, pid or process running job)
        registeredJobs = [(jid, state['pid']) for jid, state in list(nodeInfo.jobState.items())]
        for jobID, jobPID in registeredJobs:
            if not cls._pidExists(jobPID):
                jobState = CachingFileStore._JobState(nodeInfo.jobState[jobID])
                logger.warning('Detected that job (%s) prematurely terminated.  Fixing the state '
                               'of the cache.', jobState.jobName)
                if not batchSystemShutdown:
                    logger.debug("Returning dead job's used disk to cache.")
                    # Delete the old work directory if it still exists, to remove unwanted nlinks.
                    # Do this only during the life of the program and dont' do it during the
                    # batch system cleanup.  Leave that to the batch system cleanup code.
                    if os.path.exists(jobState.jobDir):
                        shutil.rmtree(jobState.jobDir)
                    nodeInfo.sigmaJob -= jobState.jobReqs
                logger.debug('Running user-defined deferred functions.')
                cls._runDeferredFunctions(jobState.deferredFunctions)
                # Remove job from the cache state file
                nodeInfo.jobState.pop(jobID)

    def _registerDeferredFunction(self, deferredFunction):
        with self._CacheState.open(self) as cacheInfo:
            cacheInfo.jobState[self.jobID]['deferredFunctions'].append(deferredFunction)
            logger.debug('Registered "%s" with job "%s".', deferredFunction, self.jobName)

    class _JobState(object):
        """
        This is a utility class to handle the state of a job in terms of it's current disk
        requirements, working directory, and job specific files.
        """

        def __init__(self, dictObj):
            assert isinstance(dictObj, dict)
            self.__dict__.update(dictObj)

        @classmethod
        def updateJobSpecificFiles(cls, outer, jobStoreFileID, filePath, fileSize, cached):
            """
            This method will update the job specifc files in the job state object. It deals with
            opening a cache lock file, etc.

            :param toil.fileStore.CachingFileStore outer: An instance of CachingFileStore
            :param str jobStoreFileID: job store Identifier for the file
            :param str filePath: The path to the file
            :param float fileSize: The size of the file (may be deprecated soon)
            :param bool cached: T : F : None :: cached : not cached : mutably read
            """
            with outer._CacheState.open(outer) as cacheInfo:
                jobState = cls(cacheInfo.jobState[outer.jobID])
                jobState.addToJobSpecFiles(jobStoreFileID, filePath, fileSize, cached)
                cacheInfo.jobState[outer.jobID] = jobState.__dict__

        def addToJobSpecFiles(self, jobStoreFileID, filePath, fileSize, cached):
            """
            This is the real method that actually does the updations.

            :param jobStoreFileID: job store Identifier for the file
            :param filePath: The path to the file
            :param fileSize: The size of the file (may be deprecated soon)
            :param cached: T : F : None :: cached : not cached : mutably read
            """
            # If there is no entry for the jsfID, make one. self.jobSpecificFiles is a default
            # dict of default dicts and the absence of a key will return an empty dict
            # (equivalent to a None for the if)
            if not self.jobSpecificFiles[jobStoreFileID]:
                self.jobSpecificFiles[jobStoreFileID][filePath] = fileSize
            else:
                # If there's no entry for the filepath, create one
                if not self.jobSpecificFiles[jobStoreFileID][filePath]:
                    self.jobSpecificFiles[jobStoreFileID][filePath] = fileSize
                # This should never happen
                else:
                    raise RuntimeError()
            # Now add the file to the reverse mapper. This will speed up cleanup and local file
            # deletion.
            self.filesToFSIDs[filePath].add(jobStoreFileID)
            if cached:
                self.updateJobReqs(fileSize, 'add')

        def updateJobReqs(self, fileSize, actions):
            """
            This method will update the current state of the disk required by the job after the
            most recent cache operation.

            :param fileSize: Size of the last file added/removed from the cache
            :param actions: 'add' or 'remove'
            """
            assert actions in ('add', 'remove')
            multiplier = 1 if actions == 'add' else -1
            # If the file was added to the cache, the value is subtracted from the requirements,
            # and it is added if the file was removed form the cache.
            self.jobReqs -= (fileSize * multiplier)

        def isPopulated(self):
            return self.__dict__ != {}

    class HarbingerFile(object):
        """
        Represents the placeholder file that harbinges the arrival of a local copy of a file in
        the job store.
        """

        def __init__(self, fileStore, fileStoreID=None, cachedFileName=None):
            """
            Returns the harbinger file name for a cached file, or for a job store ID

            :param class fileStore: The 'self' object of the fileStore class
            :param str fileStoreID: The file store ID for an input file
            :param str cachedFileName: The cache file name corresponding to a given file
            """
            # We need either a file store ID, or a cached file name, but not both (XOR).
            assert (fileStoreID is None) != (cachedFileName is None)
            if fileStoreID is not None:
                self.fileStoreID = fileStoreID
                cachedFileName = fileStore.encodedFileID(fileStoreID)
            else:
                self.fileStoreID = fileStore.decodedFileID(cachedFileName)
            self.fileStore = fileStore
            self.harbingerFileName = '/.'.join(os.path.split(cachedFileName)) + '.harbinger'

        def write(self):
            self.fileStore.logToMaster('CACHE: Creating a harbinger file for (%s). '
                                       % self.fileStoreID, logging.DEBUG)
            with open(self.harbingerFileName + '.tmp', 'w') as harbingerFile:
                harbingerFile.write(str(os.getpid()))
            # Make this File read only to prevent overwrites
            os.chmod(self.harbingerFileName + '.tmp', 0o444)
            os.rename(self.harbingerFileName + '.tmp', self.harbingerFileName)

        def waitOnDownload(self, lockFileHandle):
            """
            This method is called when a readGlobalFile process is waiting on another process to
            write a file to the cache.

            :param lockFileHandle: The open handle to the cache lock file
            """
            while self.exists():
                logger.debug('CACHE: Waiting for another worker to download file with ID %s.'
                            % self.fileStoreID)
                # Ensure that the process downloading the file is still alive.  The PID will
                # be in the harbinger file.
                pid = self.read()
                if FileStore._pidExists(pid):
                    # Release the file lock and then wait for a bit before repeating.
                    flock(lockFileHandle, LOCK_UN)
                    time.sleep(20)
                    # Grab the file lock before repeating.
                    flock(lockFileHandle, LOCK_EX)
                else:
                    # The process that was supposed to download the file has died so we need
                    # to remove the harbinger.
                    self._delete()

        def read(self):
            return int(open(self.harbingerFileName).read())

        def exists(self):
            return os.path.exists(self.harbingerFileName)

        def delete(self):
            """
            Acquires the cache lock then attempts to delete the harbinger file.
            """
            with self.fileStore.cacheLock():
                self._delete()

        def _delete(self):
            """
            This function assumes you already have the cache lock!
            """
            assert self.exists()
            self.fileStore.logToMaster('CACHE: Deleting the harbinger file for (%s)' %
                                       self.fileStoreID, logging.DEBUG)
            os.remove(self.harbingerFileName)

    # Functions related to async updates
    def asyncWrite(self):
        """
        A function to write files asynchronously to the job store such that subsequent jobs are
        not delayed by a long write operation.
        """
        try:
            while True:
                try:
                    # Block for up to two seconds waiting for a file
                    args = self.queue.get(timeout=2)
                except Empty:
                    # Check if termination event is signaled
                    # (set in the event of an exception in the worker)
                    if self._terminateEvent.isSet():
                        raise RuntimeError("The termination flag is set, exiting")
                    continue
                # Normal termination condition is getting None from queue
                if args is None:
                    break
                inputFileHandle, jobStoreFileID = args
                cachedFileName = self.encodedFileID(jobStoreFileID)
                # Ensure that the harbinger exists in the cache directory and that the PID
                # matches that of this writing thread.
                # If asyncWrite is ported to subprocesses instead of threads in the future,
                # insert logic here to securely overwrite the harbinger file.
                harbingerFile = self.HarbingerFile(self, cachedFileName=cachedFileName)
                assert harbingerFile.exists()
                assert harbingerFile.read() == int(os.getpid())
                # We pass in a fileHandle, rather than the file-name, in case
                # the file itself is deleted. The fileHandle itself should persist
                # while we maintain the open file handle
                with self.jobStore.updateFileStream(jobStoreFileID) as outputFileHandle:
                    shutil.copyfileobj(inputFileHandle, outputFileHandle)
                inputFileHandle.close()
                # Remove the file from the lock files
                with self._pendingFileWritesLock:
                    self._pendingFileWrites.remove(jobStoreFileID)
                # Remove the harbinger file
                harbingerFile.delete()
        except:
            self._terminateEvent.set()
            raise

    def _updateJobWhenDone(self):
        """
        Asynchronously update the status of the job on the disk, first waiting \
        until the writing threads have finished and the input blockFn has stopped \
        blocking.
        """

        def asyncUpdate():
            try:
                # Wait till all file writes have completed
                for i in range(len(self.workers)):
                    self.queue.put(None)

                for thread in self.workers:
                    thread.join()

                # Wait till input block-fn returns - in the event of an exception
                # this will eventually terminate
                self.inputBlockFn()

                # Check the terminate event, if set we can not guarantee
                # that the workers ended correctly, therefore we exit without
                # completing the update
                if self._terminateEvent.isSet():
                    raise RuntimeError("The termination flag is set, exiting before update")

                # Indicate any files that should be deleted once the update of
                # the job wrapper is completed.
                self.jobGraph.filesToDelete = list(self.filesToDelete)

                # Complete the job
                self.jobStore.update(self.jobGraph)

                # Delete any remnant jobs
                list(map(self.jobStore.delete, self.jobsToDelete))

                # Delete any remnant files
                list(map(self.jobStore.deleteFile, self.filesToDelete))

                # Remove the files to delete list, having successfully removed the files
                if len(self.filesToDelete) > 0:
                    self.jobGraph.filesToDelete = []
                    # Update, removing emptying files to delete
                    self.jobStore.update(self.jobGraph)
            except:
                self._terminateEvent.set()
                raise
            finally:
                # Indicate that _blockFn can return
                # This code will always run
                self.updateSemaphore.release()

        # The update semaphore is held while the job is written to the job store
        try:
            self.updateSemaphore.acquire()
            t = Thread(target=asyncUpdate)
            t.start()
        except:
            # This is to ensure that the semaphore is released in a crash to stop a deadlock
            # scenario
            self.updateSemaphore.release()
            raise

    def _blockFn(self):
        self.updateSemaphore.acquire()
        self.updateSemaphore.release()  # Release so that the block function can be recalled
        # This works, because once acquired the semaphore will not be acquired
        # by _updateJobWhenDone again.
        return

    @classmethod
    def shutdown(cls, dir_):
        """
        :param dir_: The directory that will contain the cache state file.
        """
        cacheInfo = cls._CacheState._load(os.path.join(dir_, '_cacheState'))
        cls.findAndHandleDeadJobs(cacheInfo, batchSystemShutdown=True)
        shutil.rmtree(dir_)

    def __del__(self):
        """
        Cleanup function that is run when destroying the class instance that ensures that all the
        file writing threads exit.
        """
        self.updateSemaphore.acquire()
        for i in range(len(self.workers)):
            self.queue.put(None)
        for thread in self.workers:
            thread.join()
        self.updateSemaphore.release()


class NonCachingFileStore(FileStore):
    def __init__(self, jobStore, jobGraph, localTempDir, inputBlockFn):
        self.jobStore = jobStore
        self.jobGraph = jobGraph
        self.jobName = str(self.jobGraph)
        self.localTempDir = os.path.abspath(localTempDir)
        self.inputBlockFn = inputBlockFn
        self.jobsToDelete = set()
        self.loggingMessages = []
        self.filesToDelete = set()
        super(NonCachingFileStore, self).__init__(jobStore, jobGraph, localTempDir, inputBlockFn)
        # This will be defined in the `open` method.
        self.jobStateFile = None
        self.localFileMap = defaultdict(list)

    @contextmanager
    def open(self, job):
        jobReqs = job.disk
        startingDir = os.getcwd()
        self.localTempDir = makePublicDir(os.path.join(self.localTempDir, str(uuid.uuid4())))
        self.findAndHandleDeadJobs(self.workFlowDir)
        self.jobStateFile = self._createJobStateFile()
        freeSpace, diskSize = getFileSystemSize(self.localTempDir)
        if freeSpace <= 0.1 * diskSize:
            logger.warning('Starting job %s with less than 10%% of disk space remaining.',
                           self.jobName)
        try:
            os.chdir(self.localTempDir)
            yield
        finally:
            diskUsed = getDirSizeRecursively(self.localTempDir)
            logString = ("Job {jobName} used {percent:.2f}% ({humanDisk}B [{disk}B] used, "
                         "{humanRequestedDisk}B [{requestedDisk}B] requested) at the end of "
                         "its run.".format(jobName=self.jobName,
                                           percent=(float(diskUsed) / jobReqs * 100 if
                                                    jobReqs > 0 else 0.0),
                                           humanDisk=bytes2human(diskUsed),
                                           disk=diskUsed,
                                           humanRequestedDisk=bytes2human(jobReqs),
                                           requestedDisk=jobReqs))
            self.logToMaster(logString, level=logging.DEBUG)
            if diskUsed > jobReqs:
                self.logToMaster("Job used more disk than requested. Consider modifying the user "
                                 "script to avoid the chance of failure due to incorrectly "
                                 "requested resources. " + logString, level=logging.WARNING)
            os.chdir(startingDir)
            jobState = self._readJobState(self.jobStateFile)
            deferredFunctions = jobState['deferredFunctions']
            failures = self._runDeferredFunctions(deferredFunctions)
            for failure in failures:
                self.logToMaster('Deferred function "%s" failed.' % failure, logging.WARN)
            # Finally delete the job from the worker
            os.remove(self.jobStateFile)

    def writeGlobalFile(self, localFileName, cleanup=False):
        absLocalFileName = self._resolveAbsoluteLocalPath(localFileName)
        creatorID = self.jobGraph.jobStoreID
        fileStoreID = self.jobStore.writeFile(absLocalFileName, creatorID, cleanup)
        self.localFileMap[fileStoreID].append(absLocalFileName)
        return FileID.forPath(fileStoreID, absLocalFileName)

    def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=False, symlink=False):
        if userPath is not None:
            localFilePath = self._resolveAbsoluteLocalPath(userPath)
            if os.path.exists(localFilePath):
                raise RuntimeError(' File %s ' % localFilePath + ' exists. Cannot Overwrite.')
        else:
            localFilePath = self.getLocalTempFileName()

        self.jobStore.readFile(fileStoreID, localFilePath, symlink=symlink)
        self.localFileMap[fileStoreID].append(localFilePath)
        return localFilePath

    @contextmanager
    def readGlobalFileStream(self, fileStoreID):
        with self.jobStore.readFileStream(fileStoreID) as f:
            yield f

    def exportFile(self, jobStoreFileID, dstUrl):
        self.jobStore.exportFile(jobStoreFileID, dstUrl)

    def deleteLocalFile(self, fileStoreID):
        try:
            localFilePaths = self.localFileMap.pop(fileStoreID)
        except KeyError:
            raise OSError(errno.ENOENT, "Attempting to delete a non-local file")
        else:
            for localFilePath in localFilePaths:
                os.remove(localFilePath)

    def deleteGlobalFile(self, fileStoreID):
        try:
            self.deleteLocalFile(fileStoreID)
        except OSError as e:
            if e.errno == errno.ENOENT:
                # the file does not exist locally, so no local deletion necessary
                pass
            else:
                raise
        self.filesToDelete.add(fileStoreID)

    def _blockFn(self):
        # there is no asynchronicity in this file store so no need to block at all
        return True

    def _updateJobWhenDone(self):
        try:
            # Indicate any files that should be deleted once the update of
            # the job wrapper is completed.
            self.jobGraph.filesToDelete = list(self.filesToDelete)
            # Complete the job
            self.jobStore.update(self.jobGraph)
            # Delete any remnant jobs
            list(map(self.jobStore.delete, self.jobsToDelete))
            # Delete any remnant files
            list(map(self.jobStore.deleteFile, self.filesToDelete))
            # Remove the files to delete list, having successfully removed the files
            if len(self.filesToDelete) > 0:
                self.jobGraph.filesToDelete = []
                # Update, removing emptying files to delete
                self.jobStore.update(self.jobGraph)
        except:
            self._terminateEvent.set()
            raise

    def __del__(self):
        """
        Cleanup function that is run when destroying the class instance.  Nothing to do since there
        are no async write events.
        """
        pass

    # Functions related to the deferred function logic
    @classmethod
    def findAndHandleDeadJobs(cls, nodeInfo, batchSystemShutdown=False):
        """
        Look at the state of all jobs registered in the individual job state files, and handle them
        (clean up the disk, and run any registered defer functions)

        :param str nodeInfo: The location of the workflow directory on the node.
        :param bool batchSystemShutdown: Is the batch system in the process of shutting down?
        :return:
        """
        # A list of tuples of (job name, pid or process running job, registered defer functions)
        for jobState in cls._getAllJobStates(nodeInfo):
            if not cls._pidExists(jobState['jobPID']):
                # using same logic to prevent races as CachingFileStore._setupCache
                myPID = str(os.getpid())
                cleanupFile = os.path.join(jobState['jobDir'], '.cleanup')
                with open(os.path.join(jobState['jobDir'], '.' + myPID), 'w') as f:
                    f.write(myPID)
                while True:
                    try:
                        os.rename(f.name, cleanupFile)
                    except OSError as err:
                        if err.errno == errno.ENOTEMPTY:
                            with open(cleanupFile, 'r') as f:
                                cleanupPID = f.read()
                            if cls._pidExists(int(cleanupPID)):
                                # Cleanup your own mess.  It's only polite.
                                os.remove(f.name)
                                break
                            else:
                                os.remove(cleanupFile)
                                continue
                        else:
                            raise
                    else:
                        logger.warning('Detected that job (%s) prematurely terminated.  Fixing the '
                                       'state of the job on disk.', jobState['jobName'])
                        if not batchSystemShutdown:
                            logger.debug("Deleting the stale working directory.")
                            # Delete the old work directory if it still exists.  Do this only during
                            # the life of the program and dont' do it during the batch system
                            # cleanup.  Leave that to the batch system cleanup code.
                            shutil.rmtree(jobState['jobDir'])
                        # Run any deferred functions associated with the job
                        logger.debug('Running user-defined deferred functions.')
                        cls._runDeferredFunctions(jobState['deferredFunctions'])
                        break

    @staticmethod
    def _getAllJobStates(workflowDir):
        """
        Generator function that deserializes and yields the job state for every job on the node,
        one at a time.

        :param str workflowDir: The location of the workflow directory on the node.
        :return: dict with keys (jobName,  jobPID, jobDir, deferredFunctions)
        :rtype: dict
        """
        jobStateFiles = []
        for root, dirs, files in os.walk(workflowDir):
            for filename in files:
                if filename == '.jobState':
                    jobStateFiles.append(os.path.join(root, filename))
        for filename in jobStateFiles:
            try:
                yield NonCachingFileStore._readJobState(filename)
            except IOError as e:
                if e.errno == 2:
                    # job finished & deleted its jobState file since the jobState files were discovered
                    continue
                else:
                    raise

    @staticmethod
    def _readJobState(jobStateFileName):
        with open(jobStateFileName, 'rb') as fH:
            state = dill.load(fH)
        return state

    def _registerDeferredFunction(self, deferredFunction):
        with open(self.jobStateFile, 'rb') as fH:
            jobState = dill.load(fH)
        jobState['deferredFunctions'].append(deferredFunction)
        with open(self.jobStateFile + '.tmp', 'wb') as fH:
            dill.dump(jobState, fH)
        os.rename(self.jobStateFile + '.tmp', self.jobStateFile)
        logger.debug('Registered "%s" with job "%s".', deferredFunction, self.jobName)

    def _createJobStateFile(self):
        """
        Create the job state file for the current job and fill in the required
        values.

        :return: Path to the job state file
        :rtype: str
        """
        jobStateFile = os.path.join(self.localTempDir, '.jobState')
        jobState = {'jobPID': os.getpid(),
                    'jobName': self.jobName,
                    'jobDir': self.localTempDir,
                    'deferredFunctions': []}
        with open(jobStateFile + '.tmp', 'wb') as fH:
            dill.dump(jobState, fH)
        os.rename(jobStateFile + '.tmp', jobStateFile)
        return jobStateFile

    @classmethod
    def shutdown(cls, dir_):
        """
        :param dir_: The workflow directory that will contain all the individual worker directories.
        """
        cls.findAndHandleDeadJobs(dir_, batchSystemShutdown=True)


class FileID(str):
    """
    A small wrapper around Python's builtin string class. It is used to represent a file's ID in the file store, and
    has a size attribute that is the file's size in bytes. This object is returned by importFile and writeGlobalFile.
    """

    def __new__(cls, fileStoreID, *args):
        return super(FileID, cls).__new__(cls, fileStoreID)

    def __init__(self, fileStoreID, size):
        # Don't pass an argument to parent class's __init__.
        # In Python 3 we can have super(FileID, self) hand us object's __init__ which chokes on any arguments.
        super(FileID, self).__init__()
        self.size = size

    @classmethod
    def forPath(cls, fileStoreID, filePath):
        return cls(fileStoreID, os.stat(filePath).st_size)
        
class WriteWatchingStream(object):
    """
    A stream wrapping class that calls any functions passed to onWrite() with the number of bytes written for every write.
    
    Not seekable.
    """
    
    def __init__(self, backingStream):
        """
        Wrap the given backing stream.
        """
        
        self.backingStream = backingStream
        # We have no write listeners yet
        self.writeListeners = []
        
    def onWrite(self, listener):
        """
        Call the given listener with the number of bytes written on every write.
        """
        
        self.writeListeners.append(listener)
        
    # Implement the file API from https://docs.python.org/2.4/lib/bltin-file-objects.html
        
    def write(self, data):
        """
        Write the given data to the file.
        """
        
        # Do the write
        self.backingStream.write(data)
        
        for listener in self.writeListeners:
            # Send out notifications
            listener(len(data))
            
    def writelines(self, datas):
        """
        Write each string from the given iterable, without newlines.
        """
        
        for data in datas:
            self.write(data)
            
    def flush(self):
        """
        Flush the backing stream.
        """
        
        self.backingStream.flush()
        
    def close(self):
        """
        Close the backing stream.
        """
        
        self.backingStream.close()


def shutdownFileStore(workflowDir, workflowID):
    """
    Run the deferred functions from any prematurely terminated jobs still lingering on the system
    and carry out any necessary filestore-specific cleanup.

    This is a destructive operation and it is important to ensure that there are no other running
    processes on the system that are modifying or using the file store for this workflow.


    This is the intended to be the last call to the file store in a Toil run, called by the
    batch system cleanup function upon batch system shutdown.

    :param str workflowDir: The path to the cache directory
    :param str workflowID: The workflow ID for this invocation of the workflow
    """
    cacheDir = os.path.join(workflowDir, cacheDirName(workflowID))
    if os.path.exists(cacheDir):
        # The presence of the cacheDir suggests this was a cached run. We don't need the cache lock
        # for any of this since this is the final cleanup of a job and there should be  no other
        # conflicting processes using the cache.
        CachingFileStore.shutdown(cacheDir)
    else:
        # This absence of cacheDir suggests otherwise.
        NonCachingFileStore.shutdown(workflowDir)


class CacheError(Exception):
    """
    Error Raised if the user attempts to add a non-local file to cache
    """

    def __init__(self, message):
        super(CacheError, self).__init__(message)


class CacheUnbalancedError(CacheError):
    """
    Raised if file store can't free enough space for caching
    """
    message = 'Unable unable to free enough space for caching.  This error frequently arises due ' \
              'to jobs using more disk than they have requested.  Turn on debug logging to see ' \
              'more information leading up to this error through cache usage logs.'

    def __init__(self):
        super(CacheUnbalancedError, self).__init__(self.message)


class IllegalDeletionCacheError(CacheError):
    """
    Error Raised if the Toil detects the user deletes a cached file
    """

    def __init__(self, deletedFile):
        message = 'Cache tracked file (%s) deleted explicitly by user. Use deleteLocalFile to ' \
                  'delete such files.' % deletedFile
        super(IllegalDeletionCacheError, self).__init__(message)


class InvalidSourceCacheError(CacheError):
    """
    Error Raised if the user attempts to add a non-local file to cache
    """

    def __init__(self, message):
        super(InvalidSourceCacheError, self).__init__(message)
