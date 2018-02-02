import random
import socket
import struct
import subprocess
import time
import unittest
from http.client import HTTPConnection
from urllib import parse

from daphne.test_application import TestApplication


class DaphneTestingInstance:
    """
    Launches an instance of Daphne to test against, with an application
    object you can read messages from and feed messages to.

    Works as a context manager.
    """

    def __init__(self, xff=False):
        self.xff = xff
        self.host = "127.0.0.1"

    def port_in_use(self, port):
        """
        Tests if a port is in use on the local machine.
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except socket.error as e:
            if e.errno in [13, 98]:
                return True
            else:
                raise
        else:
            return False
        finally:
            s.close()

    def find_free_port(self):
        """
        Finds an unused port to test stuff on
        """
        for _ in range(100):
            port = random.randint(11200, 11300)
            if not self.port_in_use(port):
                return port
        raise RuntimeError("Cannot find a free port to test on")

    def __enter__(self):
        # Clear result storage
        TestApplication.delete_setup()
        TestApplication.delete_result()
        # Find a port to listen on
        self.port = self.find_free_port()
        daphne_args = ["daphne", "-p", str(self.port), "-v", "0"]
        # Optionally enable X-Forwarded-For support.
        if self.xff:
            daphne_args += ["--proxy-headers"]
        # Start up process and make sure it begins listening. Try this 3 times.
        for _ in range(3):
            self.process = subprocess.Popen(daphne_args + ["daphne.test_application:TestApplication"])
            for _ in range(30):
                time.sleep(0.1)
                if self.port_in_use(self.port):
                    return self
            # Daphne didn't start up. Sadface.
            self.process.terminate()
        raise RuntimeError("Daphne never came up.")

    def __exit__(self, exc_type, exc_value, traceback):
        # Shut down the process
        self.process.terminate()
        del self.process

    def get_received(self):
        """
        Returns the scope and messages the test application has received
        so far. Note you'll get all messages since scope start, not just any
        new ones since the last call.

        Also checks for any exceptions in the application. If there are,
        raises them.
        """
        try:
            inner_result = TestApplication.load_result()
        except FileNotFoundError:
            raise ValueError("No results available yet.")
        # Check for exception
        if "exception" in inner_result:
            raise inner_result["exception"]
        return inner_result["scope"], inner_result["messages"]

    def add_send_messages(self, messages):
        """
        Adds messages for the application to send back.
        The next time it receives an incoming message, it will reply with these.
        """
        TestApplication.save_setup(
            response_messages=messages,
        )


class DaphneTestCase(unittest.TestCase):
    """
    Base class for Daphne integration test cases.

    Boots up a copy of Daphne on a test port and sends it a request, and
    retrieves the response. Uses a custom ASGI application and temporary files
    to store/retrieve the request/response messages.
    """

    ### Plain HTTP helpers

    def run_daphne_http(self, method, path, params, body, responses, headers=None, timeout=1, xff=False):
        """
        Runs Daphne with the given request callback (given the base URL)
        and response messages.
        """
        with DaphneTestingInstance(xff=xff) as test_app:
            # Add the response messages
            test_app.add_send_messages(responses)
            # Send it the request. We have to do this the long way to allow
            # duplicate headers.
            conn = HTTPConnection(test_app.host, test_app.port, timeout=timeout)
            # Make sure path is urlquoted and add any params
            path = parse.quote(path)
            if params:
                path += "?" + parse.urlencode(params, doseq=True)
            conn.putrequest(method, path, skip_accept_encoding=True, skip_host=True)
            # Manually send over headers (encoding any non-safe values as best we can)
            if headers:
                for header_name, header_value in headers:
                    conn.putheader(header_name.encode("utf8"), header_value.encode("utf8"))
            # Send body if provided.
            if body:
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(message_body=body)
            else:
                conn.endheaders()
            try:
                response = conn.getresponse()
            except socket.timeout:
                # See if they left an exception for us to load
                test_app.get_received()
                raise RuntimeError("Daphne timed out handling request, no exception found.")
            # Return scope, messages, response
            return test_app.get_received() + (response, )

    def run_daphne_request(self, method, path, params=None, body=None, headers=None, xff=False):
        """
        Convenience method for just testing request handling.
        Returns (scope, messages)
        """
        scope, messages, _ = self.run_daphne_http(
            method=method,
            path=path,
            params=params,
            body=body,
            headers=headers,
            xff=xff,
            responses=[
                {"type": "http.response.start", "status": 200},
                {"type": "http.response.body", "body": b"OK"},
            ],
        )
        return scope, messages

    def run_daphne_response(self, response_messages):
        """
        Convenience method for just testing response handling.
        Returns (scope, messages)
        """
        _, _, response = self.run_daphne_http(
            method="GET",
            path="/",
            params={},
            body=b"",
            responses=response_messages,
        )
        return response

    ### WebSocket helpers

    def websocket_handshake(self, test_app, path="/", params=None, headers=None, subprotocols=None, timeout=1):
        """
        Runs a WebSocket handshake negotiation and returns the raw socket
        object & the selected subprotocol.

        You'll need to inject an accept or reject message before this
        to let it complete.
        """
        # Send it the request. We have to do this the long way to allow
        # duplicate headers.
        conn = HTTPConnection(test_app.host, test_app.port, timeout=timeout)
        # Make sure path is urlquoted and add any params
        path = parse.quote(path)
        if params:
            path += "?" + parse.urlencode(params, doseq=True)
        conn.putrequest("GET", path, skip_accept_encoding=True, skip_host=True)
        # Do WebSocket handshake headers + any other headers
        if headers is None:
            headers = []
        headers.extend([
            ("Host", "example.com"),
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Key", "x3JJHMbDL1EzLkh9GBhXDw=="),
            ("Sec-WebSocket-Version", "13"),
            ("Origin", "http://example.com")
        ])
        if subprotocols:
            headers.append(("Sec-WebSocket-Protocol", ", ".join(subprotocols)))
        if headers:
            for header_name, header_value in headers:
                conn.putheader(header_name.encode("utf8"), header_value.encode("utf8"))
        conn.endheaders()
        # Read out the response
        try:
            response = conn.getresponse()
        except socket.timeout:
            # See if they left an exception for us to load
            test_app.get_received()
            raise RuntimeError("Daphne timed out handling request, no exception found.")
        # Check we got a good response code
        if response.status != 101:
            raise RuntimeError("WebSocket upgrade did not result in status code 101")
        # Prepare headers for subprotocol searching
        response_headers = dict(
            (n.lower(), v)
            for n, v in response.getheaders()
        )
        response.read()
        assert not response.closed
        # Return the raw socket and any subprotocol
        return conn.sock, response_headers.get("sec-websocket-protocol", None)

    def websocket_send_frame(self, sock, value):
        """
        Sends a WebSocket text or binary frame. Cannot handle long frames.
        """
        # Header and text opcode
        if isinstance(value, str):
            frame = b"\x81"
            value = value.encode("utf8")
        else:
            frame = b"\x82"
        # Length plus masking signal bit
        frame += struct.pack("!B", len(value) | 0b10000000)
        # Mask badly
        frame += b"\0\0\0\0"
        # Payload
        frame += value
        sock.sendall(frame)

    def receive_from_socket(self, sock, length, timeout=1):
        """
        Receives the given amount of bytes from the socket, or times out.
        """
        buf = b""
        started = time.time()
        while len(buf) < length:
            buf += sock.recv(length - len(buf))
            time.sleep(0.001)
            if time.time() - started > timeout:
                raise ValueError("Timed out reading from socket")
        return buf

    def websocket_receive_frame(self, sock):
        """
        Receives a WebSocket frame. Cannot handle long frames.
        """
        # Read header byte
        # TODO: Proper receive buffer handling
        opcode = self.receive_from_socket(sock, 1)
        if opcode in [b"\x81", b"\x82"]:
            # Read length
            length = struct.unpack("!B", self.receive_from_socket(sock, 1))[0]
            # Read payload
            payload = self.receive_from_socket(sock, length)
            if opcode == b"\x81":
                payload = payload.decode("utf8")
            return payload
        else:
            raise ValueError("Unknown websocket opcode: %r" % opcode)

    ### Assertions and test management

    def tearDown(self):
        """
        Ensures any storage files are cleared.
        """
        TestApplication.delete_setup()
        TestApplication.delete_result()

    def assert_is_ip_address(self, address):
        """
        Tests whether a given address string is a valid IPv4 or IPv6 address.
        """
        try:
            socket.inet_aton(address)
        except socket.error:
            self.fail("'%s' is not a valid IP address." % address)

    def assert_key_sets(self, required_keys, optional_keys, actual_keys):
        """
        Asserts that all required_keys are in actual_keys, and that there
        are no keys in actual_keys that aren't required or optional.
        """
        present_keys = set(actual_keys)
        # Make sure all required keys are present
        self.assertTrue(required_keys <= present_keys)
        # Assert that no other keys are present
        self.assertEqual(
            set(),
            present_keys - required_keys - optional_keys,
        )

    def assert_valid_path(self, path, request_path):
        """
        Checks the path is valid and already url-decoded.
        """
        self.assertIsInstance(path, str)
        self.assertEqual(path, request_path)
        # Assert that it's already url decoded
        self.assertEqual(path, parse.unquote(path))

    def assert_valid_address_and_port(self, host):
        """
        Asserts the value is a valid (host, port) tuple.
        """
        address, port = host
        self.assertIsInstance(address, str)
        self.assert_is_ip_address(address)
        self.assertIsInstance(port, int)
