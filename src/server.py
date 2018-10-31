import datetime
from distutils.command.config import config
import logging
import pickle
import Queue
import signal
import socket
import sys
import time
import thread
import traceback
import uuid
import modules.storage as storage
import modules.logger as logger
import modules.protocol as protocol
import modules.configuration as configuration


buffSize = 524288
delimiter = '\n\n12345ZEEK6789\n'

# (string:url) - Crawling algo
urlVisited = dict() # url already visited
urlPool = Queue.Queue(0) # url scrapped by working nodes
urlToVisit = Queue.Queue(0) # url scrapped by working nodes

# (string:url) - For stats
scrappedURLlist = []
visitedURLlist = []
skippedURLlist = []

# (packet+payload) - To be sent to _any_ node
outputQueue = Queue.Queue(200)

# (session:session) - for storage
sessionStorageQueue = Queue.Queue(0)

# temporary for server.run()
serverRunning = False
skippedSessions = []

class Server:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.s = None
        self.clientDict = {}
        self.isActive = True
        self.requestLimit = 0
        self.requestCount = 0

    def setup(self, configuration):
        """Basic setup operation (socket binding, listen, etc)"""
        logger.log(logging.DEBUG, "Socket initialization")
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind((self.host, self.port))
        self.s.listen(5)
        logger.log(logging.INFO, "Listening on [" + str(self.host) + ":" + str(self.port) + "]")

        self.configurationPayload = configuration
        self.requestLimit = configuration.config.requestLimit

    def run(self):
        """Launches the urlDispatcher and mainRoutine threads"""
        logger.log(logging.DEBUG, "Starting beginCrawlingProcedure")
        thread.start_new_thread(self.urlDispatcher, ())
        thread.start_new_thread(self.mainRoutine, ())
        thread.start_new_thread(self.storageRoutine, ())

    def listen(self):
        """Waits for new clients to connect and launches a new client thread accordingly"""
        print("- - - - - - - - - - - - - - -")
        logger.log(logging.INFO, "Waiting for working nodes to connect...")
        while self.isActive:
            try:
                client, address = self.s.accept()
                thread.start_new_thread(self.connectionHandler, (client, address))
            except:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                message = ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                logger.log(logging.CRITICAL, message)
                self.isActive = False

    def connectionHandler(self, socket, address):
        """Creates a server-side client object and makes it listen for inputs"""
        clientID = uuid.uuid4()
        client = SSClient(clientID, socket, address)
        self.clientDict[clientID] = client

        #temp testing, could take a parameter from config
        global serverRunning
        if len(self.clientDict) > 0  and serverRunning == False:
            self.run()
            serverRunning = True

        #for clients in self.clientDict:
        #    logger.log(logging.DEBUG, "Working node connected : " + str(self.clientDict[clients].id))

        try:
            client.sendConfig(self.configurationPayload)
            client.run()
            while client.isActive:
                time.sleep(0.3)
        except EOFError:
            pass
        except:
            client.isActive = False
            exc_type, exc_value, exc_traceback = sys.exc_info()
            message = "\n" + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
            logger.log(logging.ERROR, message)
        finally:
            client.disconnect()
            del self.clientDict[clientID]

    def urlDispatcher(self):
        """Reads from the urlPool, makes sure the url has not been visited and adds it to the urlToVisit Queue"""
        logger.log(logging.INFO, "Starting server urlDispatcher")

        while self.isActive:
            try:
                url = urlPool.get(True)
                if url not in urlVisited:
                    urlVisited[url] = True
                    #logic if static crawling will come here
                    urlToVisit.put(url)
                    scrappedURLlist.append(url)
            except:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                message = "\n" + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                logger.log(logging.ERROR, message)

    def mainRoutine(self):
        """To Come in da future. For now, no use"""
        logger.log(logging.INFO, "Starting server mainRoutine")

        for url in self.configurationPayload.config.rootUrls:
            payload = protocol.URLPayload([str(url)], protocol.URLPayload.TOVISIT)
            packet = protocol.Packet(protocol.URL, payload)
            urlVisited[url] = True
            outputQueue.put(packet)

            if self.configurationPayload.crawlingType == protocol.ConfigurationPayload.STATIC_CRAWLING and (self.configurationPayload.config.crawlDelay != 0):
                if self.configurationPayload.config.crawlDelay != 0:
                        time.sleep(self.configurationPayload.config.crawlDelay)

        while self.isActive:
            try:
                if self.configurationPayload.crawlingType == protocol.ConfigurationPayload.DYNAMIC_CRAWLING:
                    url = urlToVisit.get(True)
                    payload = protocol.URLPayload([str(url)], protocol.URLPayload.TOVISIT)
                    packet = protocol.Packet(protocol.URL, payload)
                    outputQueue.put(packet)
                    self.requestCount = self.requestCount + 1

                    if self.configurationPayload.config.crawlDelay != 0:
                        time.sleep(self.configurationPayload.config.crawlDelay)

                    if self.requestLimit != 0 and len(visitedURLlist)+1 > self.requestLimit:
                        break

                elif self.configurationPayload.crawlingType == protocol.ConfigurationPayload.STATIC_CRAWLING:
                    if (len(skippedURLlist+visitedURLlist) == len(self.configurationPayload.config.rootUrls)):
                        break
                    else:
                        time.sleep(0.3)
            except:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                message = "\n" + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                logger.log(logging.ERROR, message)

        logger.log(logging.INFO, "Scrapping complete. Terminating...")
        self.disconnectAllClient()
        self.isActive = False

    def storageRoutine(self):
        """Stores session and data"""
        logger.log(logging.INFO, "Starting server storageRoutine")

        while self.isActive:
            try:
                sessions = protocol.deQueue([sessionStorageQueue])

                if not sessions:
                        continue

                for session in sessions:
                    storage.writeToFile(session, session.dataContainer)
            except:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                message = "\n" + ''.join(traceback.format_exception(exc_type, exc_value, exc_traceback))
                logger.log(logging.ERROR, message)

    def disconnectAllClient(self):
        """Disconnects all clients"""

        for connectedClient in self.clientDict:
            if self.clientDict[connectedClient].isActive:
                self.clientDict[connectedClient].disconnect()


class SSClient:
    def __init__(self, cId, socket, address):
        self.id = cId
        self.socket = socket
        self.address = address
        self.isActive = True
        self.formattedAddr = logger.formatBrackets(str(str(address[0]) + ":" + str(address[1]))) + " "
        self.sentCount = 0
        self.data = ""
        self.configuration = None

        logger.log(logging.INFO, logger.GREEN + self.formattedAddr + "Working node connected" + logger.NOCOLOR)

    def sendConfig(self, configuration):
        """Sends the configuration to the client"""
        logger.log(logging.DEBUG, self.formattedAddr + "Sending configuration")
        self.configuration = configuration

        packet = protocol.Packet(protocol.CONFIG, self.configuration)
        self.writeSocket(packet)

        logger.log(logging.DEBUG, self.formattedAddr + "Configuration sent waiting for ACK")
        packet = self.readSocket(5)

        if packet.type == protocol.INFO:
            if packet.payload.info == protocol.InfoPayload.CLIENT_ACK:
                logger.log(logging.DEBUG, self.formattedAddr + "Working node ACK received (configuration)")
                return
            else:
                self.isActive = False
                raise Exception("Unable to transmit configuration")

    def run(self):
        """Launched the input and output thread with the client itself"""
        thread.start_new_thread(self.inputThread, ())
        thread.start_new_thread(self.outputThread, ())

    def inputThread(self):
        """Listens for inputs from the client"""
        logger.log(logging.DEBUG, self.formattedAddr +  "Listening for packets")

        while self.isActive:
            try:
                deserializedPacket = self.readSocket()
                self.dispatcher(deserializedPacket)

            except EOFError:
                #Fixes the pickle error if clients disconnects
                self.isActive = False

    def outputThread(self):
        """Checks if there are messages to send to the client and sends them"""
        while self.isActive:
            if self.sentCount > 5:
                time.sleep(0.03)
                continue
            packetToBroadCast = protocol.deQueue([outputQueue])

            if not packetToBroadCast:
                    continue

            for packet in packetToBroadCast:
                self.writeSocket(packet)
                self.sentCount = self.sentCount+1
                logger.log(logging.DEBUG, self.formattedAddr + "Sending URL " + str(packet.payload.urlList[0]))

    def dispatcher(self, packet):
        """Dispatches packets to the right packet Queue or takes action if needed (ie: infoPacket)"""
        if packet is None:
            return
        logger.log(logging.DEBUG, "Dispatching packet of type: " + str(packet.type))

        if packet.type == protocol.INFO:
            logger.log(logging.DEBUG, self.formattedAddr + "Received INFO packet")
        elif packet.type == protocol.URL:

            if packet.payload.type == protocol.URLPayload.SCRAPPED_URL:
                logger.log(logging.INFO, self.formattedAddr + "Receiving scrapped URLs : " + str(len(packet.payload.urlList)).center(5) + " / " + str(len(scrappedURLlist)).center(7) + " - " + str(len(skippedURLlist)).center(5))
                for url in packet.payload.urlList:
                    urlPool.put(url)

            if packet.payload.type == protocol.URLPayload.VISITED:
                self.sentCount = self.sentCount-1
                for url in packet.payload.urlList:
                    logger.log(logging.INFO, self.formattedAddr + "Receiving scrapped data")
                    logger.log(logging.DEBUG, self.formattedAddr + "Receiving scrapped data" + url)
                    f=open("data.txt","a+")
                    f.write(url);
                    f.write("\n")
                    visitedURLlist.append(url)
                if hasattr(packet.payload, 'session'):
                    if packet.payload.session is not None:
                        sessionStorageQueue.put(packet.payload.session)

            if packet.payload.type == protocol.URLPayload.SKIPPED:
                self.sentCount = self.sentCount-1
                for url in packet.payload.urlList:
                    skippedURLlist.append(url)
                if hasattr(packet.payload, 'session'):
                    if packet.payload.session is not None:
                        sessionStorageQueue.put(packet.payload.session)
                        if packet.payload.session.returnCode == -1:
                            logger.log(logging.INFO, logger.PINK + self.formattedAddr + "Skipped (timeout) : " + url + logger.NOCOLOR)
                        elif packet.payload.session.returnCode == -2:
                            logger.log(logging.INFO, logger.PINK + self.formattedAddr + "Skipped (request not allowed - robot parser) : " + url + logger.NOCOLOR)
                        elif packet.payload.session.returnCode == -100:
                            logger.log(logging.INFO, logger.YELLOW + self.formattedAddr + "Skipped (unknown error) : " + url + logger.NOCOLOR)
                        else:
                            logger.log(logging.INFO, logger.BLUE + self.formattedAddr + "Skipped (html error " + str(packet.payload.session.returnCode) + ") : " + url + logger.NOCOLOR)
                else:
                    logger.log(logging.INFO, logger.RED + self.formattedAddr + "No session returned" + url + logger.NOCOLOR)
        else:
            logger.log(logging.CRITICAL, "Unrecognized packet type : " + str(packet.type) + ". This packet was dropped")
            return

    def writeSocket(self, obj):
        try:
            serializedObj = pickle.dumps(obj)
            logger.log(logging.DEBUG, self.formattedAddr + "Sending " + str(len(serializedObj + delimiter)) + " bytes")
            self.socket.sendall(serializedObj + delimiter)
        except:
            raise Exception("Unable to write to socket (client disconnected)")

    def readSocket(self, timeOut=None):
        self.socket.settimeout(timeOut)
        data = self.data

        if "\n\n12345ZEEK6789\n" in data:
            data = data.split("\n\n12345ZEEK6789\n")
            self.data = "\n\n12345ZEEK6789\n".join(data[1:])
            return pickle.loads(data[0])

        while self.isActive:
            buffer = self.socket.recv(buffSize)
            data = data + buffer

            if not buffer:
                logger.log(logging.INFO, logger.RED + self.formattedAddr + "Lost connection" + logger.NOCOLOR)
                self.isActive = False

            if "\n\n12345ZEEK6789\n" in data:
                data = data.split("\n\n12345ZEEK6789\n")
                self.data = "\n\n12345ZEEK6789\n".join(data[1:])
                break

        if self.isActive == False:
            return

        logger.log(logging.DEBUG, self.formattedAddr + "Receiving " + str(len(data[0])) + " bytes")
        return pickle.loads(data[0])

    def disconnect(self):
        """Disconnects the client"""

        if self.socket != None:
            logger.log(logging.INFO, logger.RED + self.formattedAddr + "Disconnecting" + logger.NOCOLOR)
            self.isActive = False
            self.socket.close()
            self.socket = None


def ending():
    """Temporary ending routine"""
    try:
        scrapped = len(scrappedURLlist)
        skipped = len(skippedURLlist)
        visited = len(visitedURLlist)
        skipRate = (float(skipped)/float(skipped+visited) * 100)

        print("\n\n-------------------------")
        print("Scrapped : " + str(scrapped))
        print("Skipped : " + str(skipped))
        print("Visited : " + str(visited))
        print("-------------------------")
        print(str(skipRate) + "% skipping rate\n")
    except:
        #handles cases where crawling did occur (list were empty)
        pass
    sys.exit()

def handler(signum, frame):
    ending()

def main():
    signal.signal(signal.SIGINT, handler)
    
    #parse config file
    config = configuration.configParser()

    #logging
    logger.init(config.logPath, "server-" + str(datetime.datetime.now()))
    logger.debugFlag = config.verbose

    #node configration
    if config.crawling == 'dynamic':
        nodeConfig = protocol.ConfigurationPayload(protocol.ConfigurationPayload.DYNAMIC_CRAWLING, config)
    else:
        nodeConfig = protocol.ConfigurationPayload(protocol.ConfigurationPayload.STATIC_CRAWLING, config)

    #server
    server = Server(config.host, config.port)
    server.setup(nodeConfig)
    thread.start_new_thread(server.listen, ()) #testing

    while server.isActive:
        time.sleep(0.5)

    server.disconnectAllClient()
    ending()


if __name__ == "__main__":
    main()