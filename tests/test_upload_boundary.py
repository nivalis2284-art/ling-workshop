from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from lingnexus.api.app import create_app


def test_unauthorized_upload_is_rejected_before_file_spooling(tmp_path, monkeypatch):
    written = []
    original = UploadFile.write

    async def observed(file, data):
        written.append(len(data))
        return await original(file, data)

    monkeypatch.setattr(UploadFile, "write", observed)
    with TestClient(
        create_app(data_dir=tmp_path, token="upload-test-token"), base_url="http://localhost:8765"
    ) as client:
        result = client.post(
            "/api/agents/unknown/attachments",
            files={"file": ("large.txt", b"x" * (2 * 1024 * 1024))},
        )
    assert result.status_code == 401
    assert written == []


def test_oversize_request_rejected_before_spooling(tmp_path, monkeypatch):
    written = []
    original = UploadFile.write

    async def observed(file, data):
        written.append(len(data))
        return await original(file, data)

    monkeypatch.setattr(UploadFile, "write", observed)
    with TestClient(
        create_app(data_dir=tmp_path, token="upload-test-token"), base_url="http://localhost:8765"
    ) as client:
        result = client.post(
            "/api/agents/unknown/attachments",
            headers={"Authorization": "Bearer upload-test-token"},
            files={"file": ("large.txt", b"x" * (27 * 1024 * 1024))},
        )
    assert result.status_code == 413
    assert written == []


def test_authorized_small_upload_still_works(tmp_path):
    auth = {"Authorization": "Bearer upload-test-token"}
    with TestClient(
        create_app(data_dir=tmp_path, token="upload-test-token"), base_url="http://localhost:8765"
    ) as client:
        agent = client.post("/api/agents", headers=auth, json={"name": "灵"}).json()["id"]
        result = client.post(
            f"/api/agents/{agent}/attachments",
            headers=auth,
            files={"file": ("small.txt", "真实的小文件".encode())},
        )
        assert result.status_code == 201
        assert result.json()["segments"][0]["text"] == "真实的小文件"


def test_chunked_upload_stops_before_consuming_entire_request(tmp_path, monkeypatch):
    import asyncio

    import httpx

    written, sent = [], []
    original = UploadFile.write

    async def observed(file, data):
        written.append(len(data))
        return await original(file, data)

    monkeypatch.setattr(UploadFile, "write", observed)

    async def check():
        app = create_app(data_dir=tmp_path, token="upload-test-token")

        async def body():
            crlf = bytes([13, 10])
            yield crlf.join(
                [
                    b"--boundary",
                    b'Content-Disposition: form-data; name="file"; filename="large.txt"',
                    b"Content-Type: text/plain",
                    b"",
                    b"",
                ]
            )
            for index in range(30):
                sent.append(index)
                yield b"x" * (1024 * 1024)
            yield crlf + b"--boundary--" + crlf

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as client:
            response = await client.post(
                "/api/agents/unknown/attachments",
                headers={
                    "Authorization": "Bearer upload-test-token",
                    "Content-Type": "multipart/form-data; boundary=boundary",
                },
                content=body(),
            )
        assert response.status_code == 413

    asyncio.run(check())
    assert len(sent) < 30
    assert sum(written) <= 25 * 1024 * 1024
