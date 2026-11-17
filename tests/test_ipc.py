"""IPC: протокол NDJSON, сервер на unix-сокете 0600, клиент, подписка."""

from __future__ import annotations

import socket
import stat
from pathlib import Path

import pytest

from mt import paths
from mt.daemon.daemon import Daemon, parse_source
from mt.ipc import proto
from mt.ipc.client import Client, DaemonUnavailable, IPCError

MAGNET = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=fedora.iso"


# --- протокол ------------------------------------------------------------


def test_encode_decode_roundtrip() -> None:
    message = {"op": "add", "source": "magnet:?xt=urn:btih:" + "c" * 40}
    assert proto.decode(proto.encode(message)) == message


@pytest.mark.parametrize(
    "line",
    ["not json", "[1, 2]", '{"op": "frobnicate"}', '{"op": "add"}', '{"op": "add", "source": ""}'],
)
def test_parse_request_rejects_bad_input(line: str) -> None:
    with pytest.raises(proto.ProtocolError):
        proto.parse_request(line)


def test_ok_and_error_shapes() -> None:
    assert proto.ok(name="x") == {"ok": True, "name": "x"}
    assert proto.error("boom") == {"ok": False, "error": "boom"}


# --- разбор источника ------------------------------------------------------


def test_parse_source_magnet() -> None:
    infohash, name = parse_source(MAGNET)
    assert infohash == "b" * 40
    assert name == "fedora.iso"


def test_parse_source_magnet_without_btih() -> None:
    with pytest.raises(ValueError, match="btih"):
        parse_source("magnet:?dn=oops")


def test_parse_source_torrent_file(tmp_path: Path) -> None:
    torrent = tmp_path / "arch.torrent"
    torrent.write_bytes(b"d4:infod4:name4:archeee")
    infohash, name = parse_source(str(torrent))
    assert name == "arch"
    assert infohash == ""


def test_parse_source_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="не найден"):
        parse_source(str(tmp_path / "nope.torrent"))


# --- сервер и клиент против живого демона ---------------------------------


def test_socket_file_mode_0600(daemon: Daemon) -> None:
    mode = paths.socket_path().stat().st_mode
    assert stat.S_ISSOCK(mode)
    assert mode & 0o777 == 0o600


def test_client_add_then_list(client: Client) -> None:
    added = client.add(MAGNET)
    assert added["name"] == "fedora.iso"
    entries = client.list()["entries"]
    assert [entry["name"] for entry in entries] == ["fedora.iso"]
    assert entries[0]["status"] == "downloading"


def test_client_add_error_becomes_exception(client: Client) -> None:
    with pytest.raises(IPCError, match="btih"):
        client.add("magnet:?dn=bad")


def test_subscribe_streams_snapshots(daemon: Daemon, client: Client) -> None:
    with Client(paths.socket_path()) as adder:
        adder.add(MAGNET)
    stream = client.subscribe()
    first = next(stream)
    second = next(stream)
    assert [entry["name"] for entry in first["entries"]] == ["fedora.iso"]
    assert second["entries"] == first["entries"]


def test_unknown_op_gets_protocol_error(daemon: Daemon) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
        raw.connect(str(paths.socket_path()))
        raw.sendall(b'{"op": "nope"}\n')
        reply = proto.decode(raw.makefile("rb").readline())
    assert reply["ok"] is False
    assert "nope" in reply["error"]


def test_client_without_daemon_raises(state_home: Path) -> None:
    with pytest.raises(DaemonUnavailable):
        Client(paths.socket_path())


def test_second_daemon_refuses_lock(daemon: Daemon) -> None:
    from mt.daemon.daemon import AlreadyRunning

    second = Daemon()
    with pytest.raises(AlreadyRunning):
        second.start()


def test_state_survives_daemon_restart(state_home: Path) -> None:
    first = Daemon()
    first.start()
    with Client(paths.socket_path()) as client:
        client.add(MAGNET)
    first.stop()

    second = Daemon()
    second.start()
    try:
        with Client(paths.socket_path()) as client:
            names = [entry["name"] for entry in client.list()["entries"]]
        assert names == ["fedora.iso"]
    finally:
        second.stop()
