from twisted.internet import reactor, base, interfaces, defer, protocol, error
from twisted.protocols import basic
from twisted.python import failure, log
from twisted.web.client import getPage
from zope.interface import implements
import urllib, json
from time import time

def connectToWebIRC(host, nickname, factory):
    c = WebIRCConnector(
        host, nickname, factory, None, reactor
    )
    c.connect()
    return c

class WebIRCException(Exception):
    """
        Something bad happened, and we can no longer use the transport to communicate with the IRC server.
    """
    pass

class CancelledException(Exception):
    """
        I get raised if a response is received after the connection is cancelled, or we try
        to send a request when the connection is cancelled.
    """
    pass


class WebIRCConnector(base.BaseConnector):
    """
        Connector that is used to connect to a qWebIRC server
    """
    def __init__(self, host, nickname, factory, timeout, reactor):
        self.host = host
        self.nickname = nickname
        base.BaseConnector.__init__(self, factory, timeout, reactor)
        
    def _makeTransport(self):
        return WebIRCTransport(self, self.host, self.nickname)
        
class WebIRCTransport(object):
    """
        Transport that transparently handles all IO between us and the qWebIRC server
    """
    implements(interfaces.IPushProducer, interfaces.ITransport)
    
    def __init__(self, connector, host, nickname):
        self.connector = connector
        self.host = host
        self.nickname = nickname
        self.sessionId = None
        self.paused = False
        self.inPoll = False
        self.i = 0
        self.connected = 0
        self.delayed = None
        self.failedRequests = 0
        self.cancelled = False
        self.dispatcher = WebIRCDispatcher(self)
        self._connect()
        
        self.failedPoll = 0
        self.failedSend = 0
        
    def _getPage(self, url, postData = None, *a, **kw):
        """
            A wrapper around getPage to properly fetch data from the server,
            returns an defer.Deferred instance that will fire when the request
            is done with the proper result, or error.
            
            Will errbaxck with a CancelledException if called on a cancelled connection.
        """
        if self.cancelled:
            return defer.fail(
                failure.Failure(
                    CancelledException("Connection cancelled.")
                )
            )
        
        if postData:
            method = "POST"
            postData = urllib.urlencode(postData)
            kw.setdefault('headers', {})
            kw['headers'].update({
                'Content-Type': 'application/x-www-form-urlencoded'
            })
        
        else:
            method = "GET"
            
        self.i += 1
        return getPage(
            self.host + url + ('?t=%i' % self.i),
            agent = 'txWebIRCTransport',
            postdata = postData,
            method = method,
            *a, **kw
        )
        
    def failIfNotConnected(self, r):
        if not self.connected:
            self._connectionLost()
        
    @defer.inlineCallbacks
    def _connect(self):
        """
            Attempts to request a sessionid from the qwebirc server, and use that
            to poll.
        """
        try:
            response = yield self._getPage('e/n', dict(nick = self.nickname))
            if self.cancelled: # we cancelled the connection, so we don't care anymore.
                raise CancelledException()
                
            if not response:
                raise WebIRCException("Server sent back empty response.")
                
            success, sessionId = json.loads(response)
            if not success: # something wrong happened
                raise WebIRCException(sessionId)
            
        except CancelledException:
            pass # we don't care if this exception is raised.
        
        except (WebIRCException, Exception): # any other exception and something wrong happened.
            if not self.cancelled:
                f = failure.Failure()
                self.connector.connectionFailed(f)

        else:
            self.connected = 1
            self.connectTime = time()
            self.sessionId = sessionId
            self.protocol = self.connector.buildProtocol(self.host)
            self.protocol.makeConnection(self)
            self.delayed = reactor.callLater(0, self._poll)
            
    @defer.inlineCallbacks
    def _poll(self):
        """
            Runs a long poll iteration to the server, and dispatches based on responses
        """
        
        self.delayed = None
        if self.inPoll or self.paused:
            defer.returnValue(None)
        
        try:
            response = yield self._getPage('e/s', dict(s=self.sessionId))
            
            if self.cancelled: # connection was cancelled, we no longer care.
                raise CancelledException()
                
            if not response:
                raise ValueError("Server sent empty response.")
                
            data = json.loads(response)
            self._dispatchEvents(data)
            self.failedRequests = 0 # reset the failed requests counter as this was a successful iteration
            
        except WebIRCException: 
            log.msg("Got WebIRCException, closing connection!")
            log.err()
            self._connectionLost(failure.Failure())
        
        except error.ConnectionDone, e:
            self._connectionLost()
        
        except CancelledException:
            pass
            
        except: # anything else, something bad happened, but we can recover if we wait a bit and try and poll
            self.failedPoll += 1
            self.failedRequests += 1
            print "Could not read a proper response from the server"
            f = failure.Failure()
            if options.debug:
                f.printTraceback()
            self.delayed = reactor.callLater(1, self._poll) # wait 1 second before polling again.
            
        if self.failedRequests >= 10 and not self.cancelled: # too many consecutive failed requests, server is dead.
            self._connectionLost(failure.Failure(WebIRCException("Too many failed connections")))
        elif not self.cancelled and not self.delayed and not self.paused: 
            self.delayed = reactor.callLater(0.25, self._poll) # a slight delay between polls to allow messages to accum
            
    def _connectionLost(self, reason = protocol.connectionDone):
        """
            The connection was lost, and it was not requested.
        """
        if not self.cancelled:
            self.cancelled = True
            self.connected = 0
            if self.sessionId:
                if self.delayed:
                    self.delayed.cancel()
                    self.delayed = None
                self.sessionId = None
                self.protocol.connectionLost(reason)
                self.connector.connectionLost(reason)
            
    def loseConnection(self, reason = None):
        """
            Close the connection!
        """
        if self.sessionId:
            self.write("QUIT :Exiting")
        self.connected = 0
        self.sessionId = None
        self.cancelled = True
        if self.delayed: # if we have a poll queued, cancel it.
            self.delayed.cancel()
            self.delayed = None
    
    def _write(self, d, data, maxRetry = 5):
        self._getPage(
            'e/p',
            dict(s=self.sessionId, c = data)
        ) \
        .addCallback(lambda r: d.callback(r)) \
        .addErrback(self._writeFailed, d, data, maxRetry)
    
    def _writeFailed(self, reason, d, data, maxRetry):
        if self.cancelled:
            d.errback(failure.Failure(CancelledException("IRC connection lost while trying to send.")))
            return
        
        self.failedSend += 1
        if maxRetry == 0:
            log.msg("failed to send message %s" % data)
            log.err(reason)
            d.errback(reason)
        else:
            reactor.callLater(0.25, self._write, d, data, maxRetry - 1)
    
    def write(self, data):
        """
            Writes data to the IRC server, will attempt to retry a write 5 times
            before giving up, and raising an error.
        """
        d = defer.Deferred()
        self._write(d, data)
        return d
    
    @defer.inlineCallbacks # make sure the requests are sent in a proper order as best we can.
    def writeSequence(self, iovec):
        """
            Tries our best to write data to the IRC server in strict succession, will only
            call the next write when we are sure data was written successfully.
        """
        for i in iovec:
            yield self.write(i)

    def pauseProducing(self):
        """
            Stop polling the server for a bit.
        """
        self.paused = True
        
    def resumeProducing(self):
        """
            Start polling again.
        """
        self.paused = False
        if self.delayed:
            self.delayed.cancel()
        self.delayed = reactor.callLater(0, self._poll)
        
    def _dispatchEvents(self, data):
        """
            Attempt to parse the events and turn them into a line
        """
        if isinstance(data, list) and len(data) == 0:
            return # this means no new events from server.
            
        if data[0] == False: # this means some kind of error happened!
            raise WebIRCException(data[1]) # data[1] is usually the error reported by the server.
            
        for e in data:
            try:
                line = self.dispatcher.eventReceived(e) # dispatch the event to our dispatcher and have it translate.
                if line:
                    self.protocol.lineReceived(line)
            except WebIRCException:
                raise # propegate this error back to _poll, so it will know to close the connection.
            except:
                log.msg("dispatcher encountered an error.")
                log.err()


class WebIRCDispatcher():
    """
        This attempts to reconstruct events sent by qWebIRC into a proper irc line.
    """
    
    def __init__(self, t):
        self.transport = t
        
    def eventReceived(self, e):
        opcode = e[0]
        if opcode == 'c':
            command, user, message = e[1:]
            messageLen = len(message) # how many elements in msg
            handler = getattr(self, 'e_%s' % command, None) or getattr(self, 'elen_%i' % messageLen, None) or self.eUnknown
            line = handler(command, user, message)
            if line:
                if not user:
                    line = line[2:]
                return line
        elif opcode == 'disconnect':
            raise error.ConnectionDone()
        
    def elen_1(self, command, user, message):
        return u':%s %s %s' % (user, command, message[0])
    
    def elen_2(self, command, user, message):
        return u':%s %s %s :%s' % (user, command, message[0], message[1])
    
    def elen_4(self, command, user, message):
        return u':%s %s %s %s %s :%s' % (user, command, message[0], message[1], message[2], message[3])
    
    def eUnknown(self, command, user, message):
        return u':%s %s %s' % (user, command, ' '.join(message))
        
    def e_332(self, command, user, message):
        """ fix so that topic messages get printed properly. """
        return ':%s 332 %s %s :%s' % (user, message[0], message[1], message[2])
        
    def e_NICK(self, command, user, message):
        nick = user.split('!', 1)[0]
        if nick.lower() == self.transport.nickname.lower():
            self.transport.nickname = message[0]
        
        return ':%s NICK :%s' % (user, message[0])
    
    e_333 = eUnknown

class WebIRCLineReceiver(protocol.BaseProtocol):
    def lineReceived(self, line):
        """
            Override this for when each line is received from qwebirc
        """
        raise NotImplementedError
    
    def sendLine(self, line):
        """
            Sends a line to QWebIRC
            @param line: the line to send, not including the delimeter
        """
        self.transport.write(line)
    
    def connectionLost(self, reason = None):
        """
            Called when connection is shut down.
        """


