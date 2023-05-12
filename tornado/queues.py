# Copyright 2015 The Tornado Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from __future__ import absolute_import, division, print_function, with_statement

__all__ = ['Queue', 'QueueFull', 'QueueEmpty']

import collections

from tornado import gen, ioloop
from tornado.concurrent import Future
from tornado.locks import Event


class QueueEmpty(Exception):
    """Raised by `.Queue.get_nowait` when the queue has no items."""
    pass


class QueueFull(Exception):
    """Raised by `.Queue.put_nowait` when a queue is at its maximum size."""
    pass


def _set_timeout(future, timeout):
    if timeout:
        def on_timeout():
            future.set_exception(gen.TimeoutError())
        io_loop = ioloop.IOLoop.current()
        timeout_handle = io_loop.add_timeout(timeout, on_timeout)
        future.add_done_callback(
            lambda _: io_loop.remove_timeout(timeout_handle))


class Queue(object):
    """Coordinate producer and consumer coroutines.

    If maxsize is 0 (the default) the queue size is unbounded.
    """
    def __init__(self, maxsize=0):
        if maxsize is None:
            raise TypeError("maxsize can't be None")

        if maxsize < 0:
            raise ValueError("maxsize can't be negative")

        self._maxsize = maxsize
        self._init()
        self._getters = collections.deque([])  # Futures.
        self._putters = collections.deque([])  # Pairs of (item, Future).
        self._unfinished_tasks = 0
        self._finished = Event()
        self._finished.set()

    @property
    def maxsize(self):
        """Number of items allowed in the queue."""
        return self._maxsize

    def qsize(self):
        """Number of items in the queue."""
        return len(self._queue)

    def empty(self):
        return not self._queue

    def full(self):
        return False if self.maxsize == 0 else self.qsize() >= self.maxsize

    def put(self, item, timeout=None):
        """Put an item into the queue, perhaps waiting until there is room.

        Returns a Future, which raises `tornado.gen.TimeoutError` after a
        timeout.
        """
        try:
            self.put_nowait(item)
        except QueueFull:
            future = Future()
            self._putters.append((item, future))
            _set_timeout(future, timeout)
            return future
        else:
            return gen._null_future

    def put_nowait(self, item):
        """Put an item into the queue without blocking.

        If no free slot is immediately available, raise `QueueFull`.
        """
        self._consume_expired()
        if self._getters:
            assert self.empty(), "queue non-empty, why are getters waiting?"
            getter = self._getters.popleft()
            self._put(item)
            getter.set_result(self._get())
        elif self.full():
            raise QueueFull
        else:
            self._put(item)

    def get(self, timeout=None):
        """Remove and return an item from the queue.

        Returns a Future which resolves once an item is available, or raises
        `tornado.gen.TimeoutError` after a timeout.
        """
        future = Future()
        try:
            future.set_result(self.get_nowait())
        except QueueEmpty:
            self._getters.append(future)
            _set_timeout(future, timeout)
        return future

    def get_nowait(self):
        """Remove and return an item from the queue without blocking.

        Return an item if one is immediately available, else raise
        `QueueEmpty`.
        """
        self._consume_expired()
        if self._putters:
            assert self.full(), "queue not full, why are putters waiting?"
            item, putter = self._putters.popleft()
            self._put(item)
            putter.set_result(None)
            return self._get()
        elif self.qsize():
            return self._get()
        else:
            raise QueueEmpty

    def task_done(self):
        """Indicate that a formerly enqueued task is complete.

        Used by queue consumers. For each `.get` used to fetch a task, a
        subsequent call to `.task_done` tells the queue that the processing
        on the task is complete.

        If a `.join` is blocking, it resumes when all items have been
        processed; that is, when every `.put` is matched by a `.task_done`.

        Raises `ValueError` if called more times than `.put`.
        """
        if self._unfinished_tasks <= 0:
            raise ValueError('task_done() called too many times')
        self._unfinished_tasks -= 1
        if self._unfinished_tasks == 0:
            self._finished.set()

    def join(self, timeout=None):
        """Block until all items in the queue are processed. Returns a Future.

        Returns a Future, which raises `tornado.gen.TimeoutError` after a
        timeout.
        """
        return self._finished.wait(timeout)

    def _init(self):
        self._queue = collections.deque()

    def _get(self):
        return self._queue.popleft()

    def _put(self, item):
        self._unfinished_tasks += 1
        self._finished.clear()
        self._queue.append(item)

    def _consume_expired(self):
        # Remove timed-out waiters.
        while self._putters and self._putters[0][1].done():
            self._putters.popleft()

        while self._getters and self._getters[0].done():
            self._getters.popleft()

    def __repr__(self):
        return f'<{type(self).__name__} at {hex(id(self))} {self._format()}>'

    def __str__(self):
        return f'<{type(self).__name__} {self._format()}>'

    def _format(self):
        result = 'maxsize=%r' % (self.maxsize, )
        if getattr(self, '_queue', None):
            result += ' queue=%r' % self._queue
        if self._getters:
            result += f' getters[{len(self._getters)}]'
        if self._putters:
            result += f' putters[{len(self._putters)}]'
        if self._unfinished_tasks:
            result += f' tasks={self._unfinished_tasks}'
        return result
