"""Reading GoPro sensor data (GPMF), with payloads built here."""
import struct

import numpy as np

from rbo import gopro, sync


def item(key, typ, size, rep, data: bytes) -> bytes:
    pad = (-len(data)) % 4
    return key.encode() + typ.encode() + bytes([size]) + struct.pack(">H", rep) + data + b"\0" * pad


def nest(key, *children) -> bytes:
    body = b"".join(children)
    return item(key, "\0", 1, len(body), body) if len(body) < 65536 else None


def sensor_stream(key, rows, scale, orin="ZXY"):
    raw = (np.asarray(rows) * scale).round().astype(">i2").tobytes()
    return nest("STRM",
                item("STNM", "c", len(key) + 1, 1, key.encode() + b"\0"),
                item("SCAL", "s", 2, 1, struct.pack(">h", scale)),
                item("ORIN", "c", 1, 3, orin.encode()),
                item(key, "s", 6, len(rows), raw))


def payload(gyro_rows, accl_rows):
    return nest("DEVC", item("DVNM", "c", 6, 1, b"HERO8\0"),
                sensor_stream("ACCL", accl_rows, 418),
                sensor_stream("GYRO", gyro_rows, 939))


def test_parse_scale_and_axes():
    g = [[0.1, -0.2, 0.3]] * 5
    a = [[9.81, 0.0, 0.5]] * 4
    st = {s["key"]: s for s in gopro.streams(payload(g, a))}
    assert set(st) == {"ACCL", "GYRO"}
    assert st["GYRO"]["data"].shape == (5, 3) and st["GYRO"]["orin"] == "ZXY"
    assert np.allclose(st["GYRO"]["data"][0], [0.1, -0.2, 0.3], atol=2e-3)
    # ZXY: first value is the camera's Z, second X, third Y; lower case = reversed
    cam = gopro._camera_axes(np.array([[1.0, 2.0, 3.0]]), "ZxY")
    assert cam.tolist() == [[-2.0, 3.0, 1.0]]


def test_fixed_point_and_text():
    assert gopro.values("c", 5, 1, b"GYRO\0") == "GYRO"
    q = gopro.values("q", 4, 1, struct.pack(">i", 3 * 65536 + 32768))
    assert float(q[0, 0]) == 3.5


def test_yaw_about_gravity_any_mount(monkeypatch):
    # camera on its side: gravity along camera X; it turns at 0.5 rad/s about that axis
    n, t = 200, np.arange(400) / 200
    turn = 0.5 * np.sin(2 * np.pi * t)
    pk = []
    for i in range(2):
        sl = slice(i * n, (i + 1) * n)
        g = np.column_stack([np.zeros(n), turn[sl], np.zeros(n)])   # camera X is stored second (ZXY)
        a = np.column_stack([np.zeros(n), np.full(n, 9.81), np.zeros(n)])
        pk.append((float(i), 1.0, payload(g, a)))
    monkeypatch.setattr(gopro, "packets", lambda path: pk)
    tt, yaw = gopro.yaw_rate("clip.mp4")
    assert len(tt) == 400 and abs(tt[0] - 0.0025) < 1e-6
    assert np.allclose(yaw, turn, atol=2e-3)


def test_not_a_gopro_file(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"not a video")
    assert not gopro.has_gpmf(f)
    assert sync.camera_gyro(f) is None
