import functools
import logging
import sys
import socket
import fcntl
import errno
import traceback

import tornado.ioloop

# based on twisted code, but slightly less twisted

def passthru(arg):
    return arg

def make_callback(func, *args, **kwargs):
    @functools.wraps(func)
    def newfunc(result):
        return func(result, *args, **kwargs)
    return newfunc

class AlreadyCalledError(Exception):
    pass

class Failure(object):
    count = 0

    def __init__(self, exc_value=None, exc_type=None, exc_tb=None):
        Failure.count += 1
        self.count = Failure.count
        self.type = self.value = tb = None

        if exc_value is None:
            self.type, self.value, tb = sys.exc_info()
        elif exc_type is None:
            if isinstance(exc_value, Exception):
                self.type = exc_value.__class__
            else:
                self.type = type(exc_value)
            self.value = exc_value
        else:
            self.type = exc_type
            self.value = exc_value

        self.tb = tb or exc_tb

    def raiseException(self):
        raise self.type, self.value, self.tb

    def throwExceptionIntoGenerator(self, g):
        return g.throw(self.type, self.value, self.tb)


class Deferred(object):
    called = 0
    paused = 0
    _runningCallbacks = False
    debug = False

    @staticmethod
    def defer(value):
        d = Deferred()
        d.callback(value)
        return d

    def __init__(self):
        self.callbacks = []

    def addCallbacks(self, callback, errback=passthru):
        assert callable(callback)
        assert callable(errback)
        self.callbacks.append((callback, errback))
        if self.called:
            self._runCallbacks()
        return self

    def addCallback(self, callback, *args, **kw):
        if args or kw:
            callback = make_callback(callback, *args, **kw)
        return self.addCallbacks(callback)

    def addErrback(self, errback, *args, **kw):
        if args or kw:
            errback = make_callback(errback, *args, **kw)
        return self.addCallbacks(passthru, errback)

    def addBoth(self, callback, *args, **kw):
        if args or kw:
            callback = make_callback(callback, *args, **kw)
        return self.addCallbacks(callback, callback)

    def callback(self, result):
        if self.debug: logging.debug("callback(%r)", result)
        assert not isinstance(result, Deferred)
        self._startRunCallbacks(result)

    def errback(self, fail=None):
        if not isinstance(fail, Failure):
            fail = Failure(fail)
        self._startRunCallbacks(fail)

    def pause(self):
        self.paused = self.paused + 1

    def unpause(self):
        self.paused = self.paused - 1
        if self.called and not self.paused:
            self._runCallbacks()

    def _continue(self, result):
        self.result = result
        self.unpause()

    def _startRunCallbacks(self, result):
        if self.called:
            raise AlreadyCalledError()
        self.called = True
        self.result = result
        self._runCallbacks()

    def _runCallbacks(self):
        if self._runningCallbacks:
            return
        if not self.paused:
            while self.callbacks:
                callback = self.callbacks.pop(0)[isinstance(self.result, Failure)]
                try:
                    self._runningCallbacks = True
                    try:
                        self.result = callback(self.result)
                    finally:
                        self._runningCallbacks = False
                    if isinstance(self.result, Deferred):
                        self.pause()
                        self.result.addBoth(self._continue)
                        break
                except:
                    self.result = Failure()

    def __repr__(self):
        return "Deferred(%s, %r)" % (repr(self.result) if self.called else "?",
                                     self.callbacks)

class _DefGen_Return(BaseException):
    def __init__(self, value):
        self.value = value

def returnValue(val):
    raise _DefGen_Return(val)

def _inlineCallbacks(result, g, deferred):
    while 1:
        try:
            if isinstance(result, Failure):
                result = g.throw(result.type, result.value, result.tb)
            else:
                result = g.send(result)
        except StopIteration:
            deferred.callback(None)
            return deferred
        except _DefGen_Return, e:
            deferred.callback(e.value)
            return deferred
        except:
            deferred.errback()
            return deferred

        if isinstance(result, Deferred):
            # a deferred was yielded, get the result.
            waiting = [True, None]

            def gotResult(r):
                if waiting[0]:
                    waiting[0] = False
                    waiting[1] = r
                else:
                    _inlineCallbacks(r, g, deferred)

            result.addBoth(gotResult)
            if waiting[0]:
                waiting[0] = False
                return deferred
            else:
                result = waiting[1]

def inlineCallbacks(func):
    @functools.wraps(func)
    def unwindGenerator(*args, **kwargs):
        return _inlineCallbacks(None, func(*args, **kwargs), Deferred())
    return unwindGenerator


class Socket(object):

    def __init__(self, sock, io_loop=None):
        self._sock = sock
        self._sock.setblocking(False)
        self._io_loop = io_loop or tornado.ioloop.IOLoop.instance()
        self._readable = []
        self._writeable = []
        self._io_loop.add_handler(
            self._sock.fileno(), self._handle_events, self._get_events())

    def _update_events(self):
        self._io_loop.update_handler(self._sock.fileno(), self._get_events())

    def _get_events(self):
        events = 0
        if 1 or self._readable: events |= self._io_loop.READ
        if self._writeable: events |= self._io_loop.WRITE
        return events

    def _handle_events(self, fd, events):
        logging.debug("handle_events(%s, %x)", fd, events)
        if events & self._io_loop.READ and self._readable:
            self._readable.pop().callback(self)
        if events & self._io_loop.WRITE and self._writeable:
            self._writeable.pop().callback(self)

    def connect(self, hpp):
        try:
            self._sock.connect(hpp)
        except socket.error, e:
            if e.errno in (errno.EINPROGRESS, errno.EAGAIN):
                return self.wait_for_write()
            raise
        else:
            return Deferred.defer(self)

    def wait_for_read(self):
        d = Deferred()
        self._readable.append(d)
        self._update_events()
        return d

    def wait_for_write(self):
        d = Deferred()
        self._writeable.append(d)
        self._update_events()
        return d

    def recv(self, buffer_size):
        #logging.debug('recv %s', buffer_size)
        try:
            data = self._sock.recv(buffer_size)
        except socket.error, e:
            if e.errno in (errno.EINPROGRESS, errno.EAGAIN):
                return self.wait_for_read().addCallback(
                    lambda x: self.recv(buffer_size))
            raise
        else:
            return Deferred.defer(data)

    def send(self, data):
        try:
            n = self._sock.send(data)
        except socket.error, e:
            if e.errno in (errno.EINPROGRESS, errno.EAGAIN):
                return self.wait_for_write().addCallback(
                    lambda x: self.send(data))
            raise
        else:
            return Deferred.defer(n)


class Stream(object):
    read_buffer_size = 4096

    def __init__(self, sock):
        assert isinstance(sock, Socket)
        self.socket = sock
        self._read_buffer = ""
        self._write_buffer = ""

    @inlineCallbacks
    def _read_more(self, size=0):
        size = size or self.read_buffer_size
        data = yield self.socket.recv(size)
        self._read_buffer += data
        returnValue(len(data))

    def _consume(self, size):
        data = self._read_buffer[:size]
        self._read_buffer = self._read_buffer[size:]
        return data

    @inlineCallbacks
    def read_bytes(self, size):
        while len(self._read_buffer) < size:
            if not (yield self._read_more(size - len(self._read_buffer))):
                returnValue(self._consume(len(self._read_buffer)))
        returnValue(self._consume(size))
    read = read_bytes

    @inlineCallbacks
    def read_until(self, delimiter):
        while True:
            loc = self._read_buffer.find(delimiter)
            if loc != -1:
                returnValue(self._consume(loc + len(delimiter)))
            if not (yield self._read_more()):
                break

    def readline(self):
        return self.read_until('\n')

    @inlineCallbacks
    def write(self, data):
        while data:
            n = yield self.socket.send(data)
            data = data[n:]


def run(d):
    import tornado.ioloop
    ioloop = tornado.ioloop.IOLoop.instance()

    holder = [None]

    def done(result):
        holder[0] = result
        ioloop.stop()

    d.addBoth(done)
    ioloop.start()

    result = holder[0]
    if isinstance(result, Failure):
        result.raiseException()
    else:
        return result


@inlineCallbacks
def go():
    io_loop = tornado.ioloop.IOLoop.instance()
    sock = Socket(socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0))
    #sock._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8)
    try:
        print "connecting"
        yield sock.connect(("www.google.com", 80))
        stream = Stream(sock)
        stream.read_buffer_size = 8
        print "connected"
        yield stream.write("GET / HTTP/1.0\r\n\r\n")
        print "sent"
        while True:
            line = yield stream.read_until('\n')
            line = line.rstrip()
            if not line: break
            print line
        while True:
            data = yield stream.read(4096)
            print "received %d bytes" % len(data)
            if not data: break
    except Exception:
        traceback.print_exc()
    finally:
        sock._sock.close()
        io_loop.stop()


def main():
    logging.root.setLevel(logging.DEBUG)
    io_loop = tornado.ioloop.IOLoop.instance()
    io_loop.add_callback(go)
    io_loop.start()

if __name__ == "__main__":
    main()

