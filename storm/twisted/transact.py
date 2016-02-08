import time
import random
import transaction

from threading import current_thread
from functools import wraps

from zope.component import getUtility

from storm.zope.interfaces import IZStorm
from storm.exceptions import IntegrityError, DisconnectionError

from twisted.internet.threads import deferToThreadPool


RETRIABLE_ERRORS = (DisconnectionError, )
try:
    from psycopg2.extensions import TransactionRollbackError
    RETRIABLE_ERRORS = RETRIABLE_ERRORS + (TransactionRollbackError,)
except ImportError:
    pass


class Transactor(object):
    """Run in a thread code that needs to interact with the database.

    This class makes sure that code interacting with the database is run
    in a separate thread and that the associated transaction is aborted or
    committed in the same thread.

    @param threadpool: The C{ThreadPool} to get threads from.
    @param _transaction: The C{TransactionManager} to use, for test cases only.

    @ivar retries: Maximum number of retries upon retriable exceptions. The
        default is to retry a function up to 2 times upon possibly transient
        or spurious errors like L{IntegrityError} and L{DisconnectionError}.
    @ivar on_retry: If not C{None}, a callable that will be called before
        retrying to run a function, and passed a L{RetryContext} instance with
        the details about the retry.

    @see: C{twisted.python.threadpool.ThreadPool}
    """
    retries = 2
    on_retry = None

    sleep = time.sleep
    uniform = random.uniform
    debug = False

    def __init__(self, threadpool, _transaction=None):
        self._threadpool = threadpool
        if _transaction is None:
            _transaction = transaction
        self._transaction = _transaction
        self.threads_in_transact = {}

    def begin_transaction(self, force=False):
        if not self.in_transact or force:
            self.rollback_transaction()
            self._starting_transact()
            return True
        else:
            return False

    def commit_transaction(self):
        self._transaction.commit()
        self._leaving_transact()

    def rollback_transaction(self):
        self._transaction.abort()
        self._leaving_transact()

    @property
    def in_transact(self):
        return current_thread() in self.threads_in_transact

    def _starting_transact(self):
        self.threads_in_transact[current_thread()] = True

    def _leaving_transact(self):
        cur_thread = current_thread()
        if self.cur_thread in self.threads_in_transact:
            del self.threads_in_transact[cur_thread]

    def run(self, function, *args, **kwargs):
        """Run C{function} in a thread.

        The function is run in a thread by a function wrapper, which
        commits the transaction if the function runs successfully. If it
        raises an exception the transaction is aborted.

        If the named variable 'async' is set to False we don't run the
        function in the ThreadPool but in the main thread.

        @param function: The function to run.
        @param args: Positional arguments to pass to C{function}.
        @param kwargs: Keyword arguments to pass to C{function}.
        @param nested_transact: run nested transacts in the same thread
        @return: A C{Deferred} that will fire after the function has been run.
        """

        run_async = kwargs.pop('async', True)
        nested_transact = kwargs.pop("nested", True)

        if nested:
            new_transct = self.begin_transaction(force=False)
        else:
            new_transact = True
            self._starting_transact()

        if not new_transact:
            kwargs.update({"auto_commit": False})
            if run_async:
                return defer.maybeDeferred(
                    self._wrap, function, *args, **kwargs)
            else:
                return self._wrap(function, *args, **kwargs)

        else:
            print "NewTransact" * 8

        if run_async:
            # Inline the reactor import here for sake of safeness, in case a
            # custom reactor needs to be installed
            from twisted.internet import reactor
            return deferToThreadPool(
                reactor, self._threadpool, self._wrap,
                function, *args, **kwargs)

        return self._wrap(function, *args, **kwargs)

    def _wrap(self, function, *args, **kwargs):
        retries = 0
        while True:
            try:
                auto_commit = kwargs.pop('auto_commit', True)
                result = function(*args, **kwargs)
                if auto_commit:
                    self._transaction.commit()
                    self.leaving_transact()
            except RETRIABLE_ERRORS, error:
                if isinstance(error, DisconnectionError):
                    # If we got a disconnection, calling rollback may not be
                    # enough because psycopg2 doesn't necessarily use the
                    # connection, so we call a dummy query to be sure that all
                    # the stores are correct.
                    zstorm = getUtility(IZStorm)
                    for name, store in zstorm.iterstores():
                        try:
                            store.execute("SELECT 1")
                        except DisconnectionError:
                            pass
                self._transaction.abort()
                if retries < self.retries:
                    retries += 1
                    if self.on_retry is not None:
                        context = RetryContext(function, args, kwargs, retries,
                                               error)
                        self.on_retry(context)
                    self.sleep(self.uniform(1, 2 ** retries))
                    continue
                else:
                    raise
            except:
                self._transaction.abort()
                raise
            else:
                return result


class RetryContext(object):
    """Hold details about a function that is going to be retried.

    @ivar function: The function that is going to be retried.
    @ivar args: The positional arguments passed to the function.
    @ivar kwargs: The keyword arguments passed to the function.
    @ivar retry: The sequential number of the retry that is going to be
        performed.
    @ivar error: The Exception instance that caused a retry to be scheduled.
    """

    def __init__(self, function, args, kwargs, retry, error):
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.retry = retry
        self.error = error


def transact(method):
    """Decorate L{method} so that it is invoked via L{Transactor.run}.

    Example::

        from twisted.python.threadpool import ThreadPool
        from storm.twisted.transact import Transactor, transact

        class Foo(object):

            def __init__(self, transactor):
                self.transactor = transactor

            @transact
            def bar(self):
                # code that uses Storm

        threadpool = ThreadPool(0, 10)
        threadpool.start()
        transactor = Transactor(threadpool)
        foo = Foo(transactor)
        deferred = foo.bar()
        deferred.addCallback(...)

    @param method: The method to decorate.
    @return: A decorated method.

    @note: The return value of the decorated method should *not* contain
        any reference to Storm objects, because they were retrieved in
        a different thread and cannot be used outside it.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        return self.transactor.run(method, self, *args, **kwargs)
    return wrapper
