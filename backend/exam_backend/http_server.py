"""Loopback HTTP listeners must not depend on external reverse DNS."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]
