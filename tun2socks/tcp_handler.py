import asyncio
import time
from asyncio import futures

import pylwip
import socks
import logging

from config import config
from tun2socks.lwip import Lwip

_logger = logging.getLogger(__name__)


async def delay():
    return
    _logger.debug("sleeping")
    await asyncio.sleep(5)


class ConnectionHandler:

    def __init__(self, lwip: Lwip, loop: asyncio.AbstractEventLoop, pcb_connection_class=None):
        self.loop = loop
        self.lwip = lwip
        self.pcb_conn_dict = {}
        self.pcb_connection_class = pcb_connection_class or PCBConnection

    def lwip_accept(self, pcb):
        """
        called when lwip accept a new tcp connection
        :param pcb:
        :return:
        """
        pcb_conn = self.pcb_connection_class(self.loop, pcb, self.lwip,
                                             done_listener=self.lwip_tcp_close)
        self.pcb_conn_dict[pcb] = pcb_conn
        pcb_conn.lwip_tcp_accept()

    def lwip_tcp_recv(self, pcb, data):
        conn: PCBConnection = self.pcb_conn_dict.get(pcb)
        if conn:
            conn.lwip_tcp_recv(data)
        else:
            _logger.error("pcb not in pcb pool when tcp_recv")

    def lwip_tcp_close(self, pcb):
        """
        called when lwip side closed
        :param pcb:
        :return:
        """
        conn: PCBConnection = self.pcb_conn_dict.pop(pcb, None)
        if not conn:
            _logger.error("pcb not in pcb pool when tcp_close")

    def lwip_tcp_sent(self, arg, pcb, length):
        """
        called by lwip tcp_sent
        :param pcb:
        :return:
        """
        conn: PCBConnection = self.pcb_conn_dict.get(pcb)
        if conn:
            conn.lwip_tcp_sent(length)
        else:
            _logger.error("pcb not in pcb pool when tcp_sent")


class PCBConnection():

    def __init__(self, loop: asyncio.AbstractEventLoop, pcb, lwip, done_listener):
        self.pcb = pcb
        self.loop = loop
        self.lwip = lwip
        self.pcb_buf = b""
        self.lwip_future = None
        self.sock = None
        self.connected = False
        self.send_handler: futures.Future = None
        self.recv_handler: futures.Future = None
        self._create_time = time.time()
        self.done_listener = done_listener

    def start(self):
        pass

    def lwip_tcp_recv(self, data):
        if not data:
            self.handler_close_connection()
        else:
            self.pcb_buf += data
            self.start_send()

    def start_send(self):
        if self.connected:
            if not self.send_handler or self.send_handler.done():
                self.send_handler = self.loop.create_task(
                    self.handle_send(),
                )

    def lwip_tcp_accept(self):
        self.recv_handler = self.loop.create_task(
            self.handle_new_connection(),
        )

    async def handle_send(self):
        while self.pcb_buf and self.connected:
            data = self.pcb_buf
            self.pcb_buf = b""
            await delay()
            await self.loop.sock_sendall(self.sock, data)

    async def create_connection(self):
        s = socks.socksocket()
        s.setblocking(False)
        s.setproxy(getattr(socks, config.schema.upper()),
                   config.proxy_ip, config.proxy_port)
        await self.loop.sock_connect(s, self.lwip.get_addr_from_pcb(self.pcb)[1])
        return s

    async def handle_new_connection(self):
        try:
            s = await self.create_connection()
            self.sock = s
            self.connected = True
            self.start_send()
            # FIXME when to close socket, close lwip
            while True:
                data = await self.loop.sock_recv(s, 10240)
                if not data:  # socket side is closed
                    self.connected = False
                    return
                await delay()
                await self.lwip_async_write(self.pcb, data)
        except asyncio.CancelledError as e:
            _logger.debug("task canceled")
            return
        except Exception as e:
            _logger.exception(e)
        finally:
            # no data need to be written to lwip, so we close it
            self.handler_close_connection()

    async def lwip_async_write(self, pcb, data):
        while data:
            f = self.loop.create_future()
            self.lwip_future = f
            sndbuf = pylwip.tcp_sndbuf(pcb)
            if sndbuf:
                try:
                    r = self.lwip.write(pcb, data[:sndbuf])
                    if r == pylwip.ERR_OK:
                        data = data[sndbuf:]
                    else:
                        _logger.error("sndbuf is ok, but write fail")
                except Exception as e:
                    # TODO lwip write error, clean connection?
                    _logger.exception(e)
                    raise e
            # wait for sndbuf space
            await f

    def lwip_tcp_sent(self, length):
        """
        called by lwip tcp_sent
        :param pcb:
        :return:
        """
        if self.lwip_future:
            if not self.lwip_future.done():
                self.lwip_future.set_result(length)
                self.lwip_future = None
            else:
                _logger.error("unexcpected future done")
        else:
            _logger.error("none future exists")

    def handler_close_connection(self):
        """
        called by when lwip side is closed
        :return:
        """
        def fn(*args):
            self.loop.remove_writer(self.sock)
            self.sock.close()
            self.lwip.tcp_close(self.pcb)
            self.done_listener(self.pcb)

        # just cancel socket side
        if self.recv_handler:
            self.recv_handler.cancel()
            self.loop.remove_reader(self.sock)

        # waiting for socket side's all data sent, then close socket
        if self.send_handler:
            if not self.send_handler.done():
                self.send_handler.add_done_callback(fn)
            else:
                fn()
        else:
            _logger.error("no send handler found")

    def __del__(self):
        t = time.time() - self._create_time
        if t > 60:
            _logger.debug("conn of pcb {} dealloc after {}s".format(self.pcb, t))


class OKResponsePCBConnection(PCBConnection):

    async def handle_new_connection(self):
        self.connected = True
        self.start_send()
        # FIXME when to close socket, close lwip
        data = b"""HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 0\r\n\r\n"""
        try:
            await delay()
            await self.lwip_async_write(self.pcb, data)
        except asyncio.CancelledError as e:
            _logger.debug("task canceled")
            return
        except Exception as e:
            _logger.exception(e)
        finally:
            # no data need to be written to lwip, so we close it
            self.handler_close_connection()

    async def handle_send(self):
        self.pcb_buf = b""

    def handler_close_connection(self):
        self.lwip.tcp_close(self.pcb)
        self.done_listener(self.pcb)
