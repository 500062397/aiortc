import binascii
import io
from unittest import TestCase

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization

from aioquic import tls
from aioquic.connection import QuicConnection
from aioquic.packet import QuicErrorCode, QuicFrameType, QuicProtocolVersion

from .utils import load, run

SERVER_CERTIFICATE = x509.load_pem_x509_certificate(
    load("ssl_cert.pem"), backend=default_backend()
)
SERVER_PRIVATE_KEY = serialization.load_pem_private_key(
    load("ssl_key.pem"), password=None, backend=default_backend()
)


class FakeTransport:
    sent = 0
    target = None

    def sendto(self, data):
        self.sent += 1
        if self.target is not None:
            self.target.datagram_received(data)


def create_transport(client, server):
    client_transport = FakeTransport()
    client_transport.target = server

    server_transport = FakeTransport()
    server_transport.target = client

    server.connection_made(server_transport)
    client.connection_made(client_transport)

    return client_transport, server_transport


class QuicConnectionTest(TestCase):
    def _test_connect_with_version(self, client_versions, server_versions):
        client = QuicConnection(is_client=True)
        client.supported_versions = client_versions
        client.version = max(client_versions)

        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
        )
        server.supported_versions = server_versions
        server.version = max(server_versions)

        # perform handshake
        client_transport, server_transport = create_transport(client, server)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 4)
        run(client.connect())

        # send data over stream
        client_reader, client_writer = client.create_stream()
        client_writer.write(b"ping")
        self.assertEqual(client_transport.sent, 5)
        self.assertEqual(server_transport.sent, 5)

        # FIXME: needs an API
        server_reader, server_writer = (
            server.streams[0].reader,
            server.streams[0].writer,
        )
        self.assertEqual(run(server_reader.read(1024)), b"ping")
        server_writer.write(b"pong")
        self.assertEqual(client_transport.sent, 6)
        self.assertEqual(server_transport.sent, 6)

        # client receives pong
        self.assertEqual(run(client_reader.read(1024)), b"pong")

        # client writes EOF
        client_writer.write_eof()
        self.assertEqual(client_transport.sent, 7)
        self.assertEqual(server_transport.sent, 7)

        # server receives EOF
        self.assertEqual(run(server_reader.read()), b"")

        return client, server

    def test_connect_draft_17(self):
        self._test_connect_with_version(
            client_versions=[QuicProtocolVersion.DRAFT_17],
            server_versions=[QuicProtocolVersion.DRAFT_17],
        )

    def test_connect_draft_18(self):
        self._test_connect_with_version(
            client_versions=[QuicProtocolVersion.DRAFT_18],
            server_versions=[QuicProtocolVersion.DRAFT_18],
        )

    def test_connect_draft_19(self):
        self._test_connect_with_version(
            client_versions=[QuicProtocolVersion.DRAFT_19],
            server_versions=[QuicProtocolVersion.DRAFT_19],
        )

    def test_connect_draft_20(self):
        self._test_connect_with_version(
            client_versions=[QuicProtocolVersion.DRAFT_20],
            server_versions=[QuicProtocolVersion.DRAFT_20],
        )

    def test_connect_with_log(self):
        client_log_file = io.StringIO()
        client = QuicConnection(is_client=True, secrets_log_file=client_log_file)
        server_log_file = io.StringIO()
        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
            secrets_log_file=server_log_file,
        )

        # perform handshake
        client_transport, server_transport = create_transport(client, server)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 4)

        # check secrets were logged
        client_log = client_log_file.getvalue()
        server_log = server_log_file.getvalue()
        self.assertEqual(client_log, server_log)
        labels = []
        for line in client_log.splitlines():
            labels.append(line.split()[0])
        self.assertEqual(
            labels,
            [
                "QUIC_SERVER_HANDSHAKE_TRAFFIC_SECRET",
                "QUIC_CLIENT_HANDSHAKE_TRAFFIC_SECRET",
                "QUIC_SERVER_TRAFFIC_SECRET_0",
                "QUIC_CLIENT_TRAFFIC_SECRET_0",
            ],
        )

    def test_create_stream(self):
        client = QuicConnection(is_client=True)
        client._initialize(b"")

        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
        )
        server._initialize(b"")

        # client
        reader, writer = client.create_stream()
        self.assertEqual(writer.get_extra_info("stream_id"), 0)

        reader, writer = client.create_stream()
        self.assertEqual(writer.get_extra_info("stream_id"), 4)

        reader, writer = client.create_stream(is_unidirectional=True)
        self.assertEqual(writer.get_extra_info("stream_id"), 2)

        reader, writer = client.create_stream(is_unidirectional=True)
        self.assertEqual(writer.get_extra_info("stream_id"), 6)

        # server
        reader, writer = server.create_stream()
        self.assertEqual(writer.get_extra_info("stream_id"), 1)

        reader, writer = server.create_stream()
        self.assertEqual(writer.get_extra_info("stream_id"), 5)

        reader, writer = server.create_stream(is_unidirectional=True)
        self.assertEqual(writer.get_extra_info("stream_id"), 3)

        reader, writer = server.create_stream(is_unidirectional=True)
        self.assertEqual(writer.get_extra_info("stream_id"), 7)

    def test_decryption_error(self):
        client = QuicConnection(is_client=True)

        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
        )

        # perform handshake
        client_transport, server_transport = create_transport(client, server)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 4)

        # mess with encryption key
        server.spaces[tls.Epoch.ONE_RTT].crypto.send.setup(
            tls.CipherSuite.AES_128_GCM_SHA256, bytes(48)
        )

        # close
        server.close(error_code=QuicErrorCode.NO_ERROR)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 5)

    def test_retry(self):
        client = QuicConnection(is_client=True)
        client.host_cid = binascii.unhexlify("c98343fe8f5f0ff4")
        client.peer_cid = binascii.unhexlify("85abb547bf28be97")

        client_transport = FakeTransport()
        client.connection_made(client_transport)
        self.assertEqual(client_transport.sent, 1)

        client.datagram_received(load("retry.bin"))
        self.assertEqual(client_transport.sent, 2)

    def test_application_close(self):
        client = QuicConnection(is_client=True)

        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
        )

        # perform handshake
        client_transport, server_transport = create_transport(client, server)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 4)

        # close
        server.close(error_code=QuicErrorCode.NO_ERROR)
        self.assertEqual(client_transport.sent, 5)
        self.assertEqual(server_transport.sent, 5)

    def test_transport_close(self):
        client = QuicConnection(is_client=True)

        server = QuicConnection(
            is_client=False,
            certificate=SERVER_CERTIFICATE,
            private_key=SERVER_PRIVATE_KEY,
        )

        # perform handshake
        client_transport, server_transport = create_transport(client, server)
        self.assertEqual(client_transport.sent, 4)
        self.assertEqual(server_transport.sent, 4)

        # close
        server.close(
            error_code=QuicErrorCode.NO_ERROR, frame_type=QuicFrameType.PADDING
        )
        self.assertEqual(client_transport.sent, 5)
        self.assertEqual(server_transport.sent, 5)

    def test_version_negotiation_fail(self):
        client = QuicConnection(is_client=True)
        client.supported_versions = [QuicProtocolVersion.DRAFT_19]

        client_transport = FakeTransport()
        client.connection_made(client_transport)
        self.assertEqual(client_transport.sent, 1)

        # no common version, no retry
        client.datagram_received(load("version_negotiation.bin"))
        self.assertEqual(client_transport.sent, 1)

    def test_version_negotiation_ok(self):
        client = QuicConnection(is_client=True)

        client_transport = FakeTransport()
        client.connection_made(client_transport)
        self.assertEqual(client_transport.sent, 1)

        # found a common version, retry
        client.datagram_received(load("version_negotiation.bin"))
        self.assertEqual(client_transport.sent, 2)