from twisted.internet import reactor, defer, protocol
from twisted.protocols import basic
import sys, os
sys.path.append(os.path.join(os.getcwd(),'..'))
from src.webirc import WebIRCConnector

def connectToWebIRC(relay, host, nickname):
    f = RelayIRCFactory(relay)
    c = WebIRCConnector(
        host, nickname, f, None, reactor
    )
    c.connect()
    return f.d

class RelayIRCFactory(protocol.ClientFactory):
    def __init__(self, relay):
        self.relay = relay
        self.d = defer.Deferred()
    
    def buildProtocol(self, addr):
        p = RelayIRCProtocol()
        p.factory = self
        return p
    
    def clientConnectionLost(self, c, reason):
        self.relay.ircConnectionLost(reason)
    
    def clientConnectionFailed(self, c, reason):
        self.d.errback(reason)
        self.relay.ircConnectionFailed(reason)
    
def utf8(value):
    if isinstance(value, unicode):
        return value.encode("utf-8")
    assert isinstance(value, str)
    return value

class RelayIRCProtocol(protocol.BaseProtocol):
    def connectionMade(self):
        self.factory.d.callback(self)
        
    def writeToRelay(self, line):
        self.factory.relay.sendLine(utf8(line))
    lineReceived = writeToRelay
        
    def writeToServer(self, line):
        return self.transport.write(line)

def getMessage(line):
    if ' :' in line:
        return line.split(' :', 1)[1]
    return ''

class IRCRelay(basic.LineReceiver):
    def __init__(self):
        self._queue = []
        self._irc = None
        self._gotNick = False
        self.tnick = '???'
        
    def lineReceived(self, line):
        if not self._gotNick:
            cmd, nick = line.split(' ', 1)
            if cmd == 'NICK':
                print "Attempting to establish a connection as %s" % nick
                d = connectToWebIRC(self, self.factory.host, nick)
                d.addCallback(self.ircConnectionMade)
                self.tnick = nick
                
            self._gotNick = True
            
        elif not self._irc:
            self._queue.append(line)
        
        else:
            try:
                print line
                params = line.split()
                print params
                if params[0] == "PRIVMSG" and params[1].lower() == '*relay*':
                    return self.handleCommand(line)
                    
            except:
                pass
            
            self._irc.writeToServer(line).addErrback(self.sendFailed)
    
    def sendToUser(self, message):
        if self._irc:
            nickname = self._irc.transport.nickname
        else:
            nickname = self.tnick
        self.sendLine(utf8(
            ':*relay*!localhost PRIVMSG %s :%s' % (
                nickname, message
            )
        ))
    
    def handleCommand(self, line):
        message = getMessage(line)
        if not message:
            return
        
        if message.startswith('!'):
            message = message[1:].strip()
            params = message.split()
            cmd, params = params[0], params[1:]
            handler = getattr(self, 'COMMAND_%s' % cmd.lower(), None)
            if handler:
                return handler(params)

        commands = ['!' + s[8:] for s in dir(self) if s.startswith('COMMAND_')]
        self.sendToUser("Usable commands are %s" % ', '.join(commands))
    
    def COMMAND_status(self, params):
        if not self._irc:
            self.sendToUser("not yet connected")
        else:
            self.sendToUser(
                "Currently tunneling qWebIRC to %s nick: %s" % (self._irc.transport.host, self._irc.transport.nickname)
            )
            self.sendToUser(
                "Requests sent: %i " % self._irc.transport.i
            )
            self.sendToUser(
                "Polls failed: %i " % self._irc.transport.failedPoll
            )
            self.sendToUser(
                "Sends failed: %i " % self._irc.transport.failedSend
            )
            self.sendToUser(
                "Connected for %s" %  dateDiff(time() - self._irc.transport.connectTime)
            )
        
    
    def sendFailed(self, e):
        self.sendToUser(
            e.value[0]
        )
            
    def connectionLost(self, reason):
        if self._irc:
            self._irc.transport.loseConnection()
        
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
            
_timeOrder = (
    ('week', 60*60*24*7),
    ('day', 60*60*24),
    ('hour', 60*60),
    ('minute', 60),
    ('second', 1)
)

def dateDiff(secs, n = True):
    if not isinstance(secs, int):
        secs = int(secs)
    
    secs = abs(secs)
    if secs == 0:
        return '0 seconds'
        
    h = []
    a = h.append
    for name, value in _timeOrder:
        x = secs/value
        if x > 0:
            a('%i %s%s' % (x, name, ('s', '')[x is 1]))
            secs -= x*value
    z=len(h)
    if n is True and z > 1: h.insert(z-1, 'and')
            
    return ' '.join(h)
    
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
      
    
