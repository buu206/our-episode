"""Reject oversized request bodies before multipart parsing creates temp files."""
import json
import re
from starlette.exceptions import HTTPException


class BodyLimitExceeded(HTTPException):
    def __init__(self):
        super().__init__(413, '请求内容超过允许大小。')


class BodyLimitMiddleware:
    def __init__(self, app, upload_bytes):
        self.app = app
        self.upload_limit = upload_bytes + 1024 * 1024  # multipart headers/boundaries

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in ('GET', 'HEAD', 'OPTIONS'):
            return await self.app(scope, receive, send)
        is_upload = scope['method'] == 'POST' and (
            scope['path'].endswith('/assets') or scope['path'] == '/api/v1/participant/submissions/upload')
        is_evidence = scope['method'] == 'PUT' and re.fullmatch(
            r'/api/v1/episodes/[^/]+/evidence', scope['path']) is not None
        limit = self.upload_limit if is_upload else 2 * 1024 * 1024 if is_evidence else 65536
        headers = dict(scope.get('headers', []))
        try:
            declared = int(headers.get(b'content-length', b'0'))
        except ValueError:
            declared = limit + 1
        if declared < 0 or declared > limit:
            return await self.reject(send)
        count = 0
        started = False
        exceeded = False

        async def limited_receive():
            nonlocal count, exceeded
            message = await receive()
            if message['type'] == 'http.request':
                count += len(message.get('body', b''))
                if count > limit:
                    exceeded = True
                    raise BodyLimitExceeded()
            return message

        async def tracked_send(message):
            nonlocal started
            # Framework body parsing may convert the receive exception into
            # its own 400 response. Preserve the deterministic 413 contract.
            if exceeded:
                if not started:
                    started = True
                    await self.reject(send)
                return
            if message['type'] == 'http.response.start':
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyLimitExceeded:
            if not started:
                await self.reject(send)

    @staticmethod
    async def reject(send):
        body = json.dumps({'error': {'code': 'REQUEST_TOO_LARGE', 'message': '上传或请求内容超过允许大小。'}}, ensure_ascii=False).encode()
        await send({'type': 'http.response.start', 'status': 413,
                    'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
        await send({'type': 'http.response.body', 'body': body})
