import socket
import ssl
from logging.handlers import SysLogHandler

class TLSSysLogHandler(SysLogHandler):
    def __init__(self, address, tls_enabled=False, cert_path=None, key_path=None, ca_cert_path=None, **kwargs):
        self.tls_enabled = tls_enabled
        self.cert_path = cert_path
        self.key_path = key_path
        self.ca_cert_path = ca_cert_path
        if tls_enabled:
            kwargs['socktype'] = socket.SOCK_STREAM
        super().__init__(address, **kwargs)

    def _connect_sockets(self, address):
        super()._connect_sockets(address)
        if self.socktype == socket.SOCK_STREAM and self.tls_enabled:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if self.ca_cert_path:
                context.load_verify_locations(cafile=self.ca_cert_path)
                context.verify_mode = ssl.CERT_REQUIRED
            else:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            if self.cert_path and self.key_path:
                context.load_cert_chain(certfile=self.cert_path, keyfile=self.key_path)
            self.socket = context.wrap_socket(self.socket, server_hostname=address[0])

print("Compiled OK")
