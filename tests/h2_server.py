"""
A real HTTP/2 server on loopback, with TLS and a certificate of its own.

Every other test here runs against an HTTP/1.1 server, which cannot answer the
question the fast path exists for: httpx falls back to HTTP/1.1 whenever ALPN
does not offer h2, quietly, so "one connection, every request in flight on it"
was a claim no test could check. This serves h2 properly and counts both the
requests and the connections they arrived on, which is how multiplexing is
told apart from a pool.

Needs `cryptography` and `h2`, both of which come with `.[dev]`. The tests that
use it skip when they are missing.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import socket
import ssl
import tempfile
import threading
from pathlib import Path

import h2.config
import h2.connection
import h2.events
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _certificate(directory: Path) -> tuple[Path, Path, Path]:
    """
    A little certificate authority and one leaf for 127.0.0.1, good for an hour.

    A proper two-level chain rather than one self-signed certificate used as its
    own trust anchor, which OpenSSL will not build a chain for without a flag
    that cannot be set on this machine: truststore's patch makes the stdlib
    `verify_flags` setter recurse until the stack runs out.
    """
    def keypair():
        return ec.generate_private_key(ec.SECP256R1())

    def named(common):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = now - datetime.timedelta(minutes=5), now + datetime.timedelta(hours=1)

    authority_key = keypair()
    authority = (
        x509.CertificateBuilder()
        .subject_name(named("jev test CA"))
        .issuer_name(named("jev test CA"))
        .public_key(authority_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(authority_key, hashes.SHA256())
    )

    leaf_key = keypair()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(named("127.0.0.1"))
        .issuer_name(authority.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.ObjectIdentifier("1.3.6.1.5.5.7.3.1")]),
                       critical=False)
        .sign(authority_key, hashes.SHA256())
    )

    ca_path = directory / "ca.pem"
    certificate_path = directory / "server.pem"
    key_path = directory / "server.key"
    ca_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(leaf_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()))
    return ca_path, certificate_path, key_path


class H2Server:
    """Answers every POST, over HTTP/2, and remembers how it was reached."""

    def __init__(self, answer=0.8):
        self.answer = answer
        self.seen = 0                       # requests
        self.connections = 0                # sockets they arrived on
        self.protocols: set[str] = set()    # what ALPN settled on
        self.streams = 0                    # the most concurrent on one connection
        self._lock = threading.Lock()
        self._directory = Path(tempfile.mkdtemp(prefix="jev-h2-"))
        self.ca_path, certificate_path, key_path = _certificate(self._directory)

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate_path, key_path)
        context.set_alpn_protocols(["h2"])   # h2 only: no quiet fallback to hide behind
        self._context = context

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(16)
        self.url = f"https://127.0.0.1:{self._socket.getsockname()[1]}/v1/systemone"
        self._running = True
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    # -- the socket side ---------------------------------------------------
    def _accept(self) -> None:
        while self._running:
            try:
                raw, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(raw,), daemon=True).start()

    def _serve(self, raw: socket.socket) -> None:
        try:
            tls = self._context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        with self._lock:
            self.connections += 1
            self.protocols.add(tls.selected_alpn_protocol() or "none")

        connection = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
        connection.initiate_connection()
        tls.sendall(connection.data_to_send())

        bodies: dict[int, bytearray] = {}
        try:
            while self._running:
                data = tls.recv(65535)
                if not data:
                    return
                for event in connection.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        bodies[event.stream_id] = bytearray()
                        with self._lock:
                            self.streams = max(self.streams, len(bodies))
                    elif isinstance(event, h2.events.DataReceived):
                        bodies.setdefault(event.stream_id, bytearray()).extend(event.data)
                        connection.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id)
                    elif isinstance(event, h2.events.StreamEnded):
                        self._reply(connection, event.stream_id, bytes(bodies.pop(
                            event.stream_id, b"")))
                out = connection.data_to_send()
                if out:
                    tls.sendall(out)
        except (OSError, ssl.SSLError, h2.exceptions.ProtocolError):
            return
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def _reply(self, connection, stream_id: int, body: bytes) -> None:
        try:
            asked = json.loads(body or b"{}")
        except ValueError:
            asked = {}
        names = [name for name in (asked.get("state") or {}) if name.startswith("item_")]
        with self._lock:
            self.seen += 1
        payload = json.dumps({
            "model": "jev-1.13.0",
            "answers": {name: {"type": "noul", "noul": self.answer} for name in names},
            "usage": {"input_tokens": 10 * max(1, len(names)), "output_tokens": len(names)},
        }).encode()
        connection.send_headers(stream_id, [
            (":status", "200"),
            ("content-type", "application/json"),
            ("content-length", str(len(payload))),
        ])
        connection.send_data(stream_id, payload, end_stream=True)

    def close(self) -> None:
        self._running = False
        try:
            self._socket.close()
        except OSError:
            pass


def client_context(ca_path) -> ssl.SSLContext:
    """
    A context that really trusts this server's certificate.

    `ssl.SSLContext` can be replaced at run time, and on a machine with pip's
    vendored truststore injected it has been: verification becomes the operating
    system's decision and `cafile` is quietly ignored. So the stdlib class is
    fetched out of the MRO rather than taken on trust.
    """
    stdlib = next(cls for cls in ssl.SSLContext.__mro__ if cls.__module__ == "ssl")
    context = stdlib(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(ca_path))
    context.check_hostname = True
    return context


def intercepted(server: "H2Server") -> str:
    """
    Empty when the server's own certificate is what a client sees, otherwise who
    is standing in the way.

    Loopback is not safe from this. On the machine this was written on, Norton
    re-signs even 127.0.0.1 with a root it then declines to trust, so no local
    TLS server can be reached at all and every test using this one skips. CI has
    no interceptor, which is where these actually run.
    """
    import ssl as _ssl

    from cryptography import x509 as _x509

    port = int(server.url.rsplit(":", 1)[1].split("/")[0])
    try:
        pem = _ssl.get_server_certificate(("127.0.0.1", port))
    except Exception as error:                       # noqa: BLE001 - any failure is a skip
        return f"the handshake did not complete ({type(error).__name__})"
    presented = _x509.load_pem_x509_certificate(pem.encode())
    mine = _x509.load_pem_x509_certificate(server.ca_path.read_bytes())
    if presented.issuer == mine.subject:
        return ""
    common = presented.issuer.rfc4514_string()
    return f"TLS to loopback is being re-signed by {common}"
