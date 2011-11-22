from twisted.internet import reactor, base, interfaces, defer, protocol
from twisted.protocols import basic
from twisted.python import failure
from twisted.web.client import getPage
from zope.interface import implements
import urllib, json

def connectToWebIRC(relay, host, nickname):
    f = WebIRCFactory(relay)
    c = WebIRCConnector(
        host, nickname, f, None, reactor
    )
    c.connect()
    return f.d

class WebIRCFactory(protocol.ClientFactory):
    def __init__(self, relay):
        self.relay = relay
        self.d = defer.Deferred()
    
    def buildProtocol(self, addr):
        p = WebIRCProtocol()
        p.factory = self
        return p
    
    def clientConnectionLost(self, c, reason):
        self.relay.ircConnectionLost(reason)
    
    def clientConnectionFailed(self, c, reason):
        self.d.errback(reason)

class WebIRCException(Exception): # a fatal error occurred, kill conns.
    pass

class CancelledException(Exception): # halt execution, one side lost a transport.
    pass

class WebIRCConnector(base.BaseConnector):
    def __init__(self, host, nickname, factory, timeout, reactor):
        self.host = host
        self.nickname = nickname
        base.BaseConnector.__init__(self, factory, timeout, reactor)
        
    def _makeTransport(self):
        return WebIRCTransport(self, self.host, self.nickname)
        
class WebIRCTransport(object):
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
        self.dispatcher = WebIRCDispatcher()
        self._connect()
        
    def _getPage(self, url, postData = None, *a, **kw):
        if self.cancelled:
            return defer.fail(
                failure.Failure(
                    WebIRCException("Connection cancelled.")
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
            print "finc"
            self._loseConnection()
        
    @defer.inlineCallbacks
    def _connect(self):
        try:
            response = yield self._getPage('e/n', dict(nick = self.nickname))
            if self.cancelled:
                raise CancelledException
            success, sessionId = json.loads(response)
            if not success:
                raise WebIRCException(sessionId)
            
        except CancelledException:
            print "request cancelled"
        
        except (WebIRCException, Exception):
            print "exception?"
            if not self.cancelled:
                f = failure.Failure()
                self.connector.connectionFailed(f)
        except:
            f = failure.Failure()
            f.printTraceback()
            
        else:
            self.connected = 1
            self.sessionId = sessionId
            self.protocol = self.connector.buildProtocol(self.host)
            self.protocol.makeConnection(self)
            self.delayed = reactor.callLater(0, self._poll)
            
    @defer.inlineCallbacks
    def _poll(self):
        self.delayed = None
        if self.inPoll or self.paused:
            defer.returnValue(None)
        
        try:
            response = yield self._getPage('e/s', dict(s=self.sessionId))
            if not self.sessionId or self.cancelled: # connection was cancelled, we no longer care.
                raise CancelledException()
            data = json.loads(response)
            self._dispatchEvents(data)
            self.failedRequests = 0
        except ValueError:
            self.failedRequests += 1
            print "Could not decode JSON Response!"
            self.delayed = reactor.callLater(1, self._poll)
            defer.returnValue(None)
            
        except WebIRCException:
            print "Got WebIRCException, closing connection!"
            self._loseConnection(failure.Failure())
            defer.returnValue(None)
        
        except CancelledException:
            print "request cancelled"

        
        except:
            self.failedRequests += 1
            
        if self.failedRequests >= 10:
            self._loseConnection(failure.Failure(WebIRCException("Too many failed connections")))
        elif not self.cancelled and not self.delayed:
            self.delayed = reactor.callLater(0, self._poll)
            
    def _loseConnection(self, reason = None):
        self.cancelled = True
        self.connected = 0
        if self.sessionId:
            if self.delayed:
                self.delayed.cancel()
                self.delayed = None
            self.sessionId = None
            self.connector.connectionLost(reason)
        self.protocol.factory.relay.ircConnectionLost(reason)
            
    def loseConnection(self, reason = None):
        if self.sessionId:
            self.write("QUIT :Exiting")
        self.connected = 0
        self.sessionId = None
        self.cancelled = True
        if self.delayed:
            self.delayed.cancel()
            self.delayed = None
    
    def _writeFailed(self, reason, data, isRetry):
        if self.cancelled:
            return
        if isRetry:
            self.protocol.writeFailed(reason, data)
        else:
            reactor.callLater(0.5, self.write, data, 1)
    
    def write(self, data, isRetry = 0):
        d = self._getPage(
            'e/p',
            dict(s=self.sessionId, c = data)
        )
        d.addErrback(self._writeFailed, data, isRetry)
        return d
    
    @defer.inlineCallbacks # make sure the requests are sent in a proper order as best we can.
    def writeSequence(self, iovec):
        for i in iovec:
            yield self.write(i)

    def pauseProducing(self):
        self.paused = True
        
    def resumeProducing(self):
        self.paused = False
        if self.delayed:
            self.delayed.cancel()
        self.delayed = reactor.callLater(0, self._poll)
        
    def _dispatchEvents(self, data):
        for e in data:
            if not e:
                print data
                raise WebIRCException("Connection lost!")
            try:
                line = self.dispatcher.eventReceived(e)
                if line:
                    self.protocol.lineReceived(line)
            except WebIRCException:
                raise
            except:
                f = failure.Failure()
                f.printTraceback()

class WebIRCDispatcher():
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
            raise WebIRCException("server sent disconnect opcode.")

    
    def writeFailed(self, l, r):
        print "write failed", l, r
        
    def elen_1(self, command, user, message):
        return u':%s %s %s' % (user, command, message[0])
    
    def elen_2(self, command, user, message):
        return u':%s %s %s :%s' % (user, command, message[0], message[1])
    
    def elen_4(self, command, user, message):
        return u':%s %s %s %s %s :%s' % (user, command, message[0], message[1], message[2], message[3])
    
    def eUnknown(self, command, user, message):
        return u':%s %s %s' % (user, command, ' '.join(message))
        
    def e_332(self, command, user, message):
        return ':%s 332 %s %s :%s' % (user, message[0], message[1], message[2])
    
    e_333 = eUnknown

def utf8(value):
    if isinstance(value, unicode):
        return value.encode("utf-8")
    assert isinstance(value, str)
    return value

class WebIRCProtocol(protocol.BaseProtocol):
    def connectionMade(self):
        self.factory.d.callback(self)
        
    def writeToRelay(self, line):
        self.factory.relay.sendLine(utf8(line))
    lineReceived = writeToRelay
        
    def writeToServer(self, line):
        self.transport.write(line)
    

class IRCRelay(basic.LineReceiver):
    def __init__(self):
        self._queue = []
        self._irc = None
        self._gotNick = False
        
    def lineReceived(self, line):
        if not self._gotNick:
            cmd, nick = line.split(' ', 1)
            if cmd == 'NICK':
                print "Attempting to establish a connection as %s" % nick
                d = connectToWebIRC(self, self.factory.host, nick)
                d.addCallback(self.ircConnectionMade)
                d.addErrback(self.ircConnectionFailed)
                
            self._gotNick = True
            
        elif not self._irc:
            self._queue.append(line)
        
        else:
            self._irc.writeToServer(line)
        
    def ircConnectionFailed(self, reason):
        er = utf8("ERROR :Could not connect to relay server, %s: %s" % (reason.type.__name__, ', '.join(str(v) for v in reason.value)))
        self.sendLine(er)
        print er
        self.transport.loseConnection()
    
    def ircConnectionLost(self, reason):
        er = utf8("ERROR :Connection to relay server lost %s: %s" % (reason.type.__name__, ', '.join(str(v) for v in reason.value)))
        self.sendLine(er)
        print er
        self.transport.loseConnection()
        
    def ircConnectionMade(self, irc):
        self._irc = irc
        print "Connection to IRC server made successfully, %r" % irc
        if not self.transport.connected:
            irc.loseConnection()
        elif self._queue:
            q = self._queue[:]
            irc.transport.writeSequence(q)
            self._queue = []
            

if __name__ == '__main__':
    import optparse
    parser = optparse.OptionParser(usage = 'usage: %prog [options] (webirc server)')
    parser.add_option('-d', '--debug', action = "store_true", dest = "debug", default = False, help = "debug [default: %default]")
    parser.add_option('-i', '--interface', dest = "interface", default = "127.0.0.1", help = "interface to listen on [default: %default]")
    parser.add_option('-p', '--port', dest = "port", type = "int", default = 6667, help = "port to listen on [default: %default]")
    
    options, args = parser.parse_args()
    if not args:
        parser.print_usage()
    else:
        if options.debug:
            import sys
            import twisted.python.log
            twisted.python.log.startLogging(sys.stdout)
        f = protocol.ServerFactory()
        f.protocol = IRCRelay
        f.host = args[0]
        reactor.listenTCP(
            options.port, f, interface = options.interface
        )
        print "Listening for connections to relay to %s on %s:%i" % (
            f.host, options.interface, options.port
        )
        reactor.run()
      
    