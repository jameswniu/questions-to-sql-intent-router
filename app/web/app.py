import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from app import db, requestlog, telemetry
from app.config import settings
from app.identity import Principal, principal_for, principals
from app.pipeline import ask as run_pipeline
from app.redact import redact
from app.sources.scans import scan_image
from app.web import auth, dashboard, session
from app.web.ratelimit import RateLimiter
from app.web.stream import AskFn, Recorder, answer_stream

HERE = Path(__file__).parent
QUESTIONS_PER_MINUTE = 20
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
NOT_FOUND = {"detail": "Not found"}
SIGN_IN: dict[auth.Mode, str] = {
    "demo": "Choose who you are asking as at the top of the page, then try again.",
    "header": "You are not signed in, or your account has no access to this service.",
}
FEEDBACK = """
INSERT INTO ops.feedback (request_id, user_id, rating, note)
SELECT %(request_id)s::uuid, %(user_id)s::text, %(rating)s::smallint, %(note)s::text
WHERE EXISTS (SELECT 1 FROM ops.request_log WHERE request_id = %(request_id)s AND user_id = %(user_id)s)
"""


class NotSignedIn(Exception):
    pass


class NotAnOperator(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


class RevalidatedFiles(StaticFiles):
    """The browser checks static files for changes on every load, so a rebuilt page never runs a stale script."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class ProxyGate:
    """Behind the sign-in proxy, answers 401 to a request without the proxy's secret before its user header is read,
    so a client that reaches the app directly can't name a user. The health check and static files stay open."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not _open(scope["path"]):
            request = Request(scope)
            sent = request.headers.get(auth.SECRET_HEADER)
            secret = getattr(request.app.state, "proxy_secret", "")
            if identity_mode(request) == "header" and not auth.from_proxy(sent, secret):
                await PlainTextResponse(SIGN_IN["header"], status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _open(path: str) -> bool:
    return path == "/healthz" or path == "/static" or path.startswith("/static/")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.identity_mode = auth.identity_mode()
    app.state.proxy_secret = auth.proxy_secret()
    telemetry.configure()
    yield
    await db.close_all()


app = FastAPI(title="claims-qa", lifespan=lifespan)
app.add_middleware(ProxyGate)
app.state.limiter = RateLimiter(QUESTIONS_PER_MINUTE, 60.0)
app.mount("/static", RevalidatedFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


@app.exception_handler(NotSignedIn)
async def not_signed_in(request: Request, _: NotSignedIn) -> Response:
    return PlainTextResponse(SIGN_IN[identity_mode(request)], status_code=401)


@app.exception_handler(NotAnOperator)
async def not_an_operator(_: Request, __: NotAnOperator) -> Response:
    return PlainTextResponse("The dashboard is for operators only.", status_code=403)


@app.exception_handler(RateLimited)
async def too_many_questions(_: Request, exc: RateLimited) -> Response:
    wait = max(1, math.ceil(exc.retry_after))
    return PlainTextResponse(
        f"You have asked {QUESTIONS_PER_MINUTE} questions in the last minute. Wait {wait} seconds and try again.",
        status_code=429,
        headers={"Retry-After": str(wait)},
    )


def identity_mode(request: Request) -> auth.Mode:
    return cast(auth.Mode, request.app.state.identity_mode)


def current_session(request: Request) -> session.Session:
    found = session.decode(request.cookies.get(session.COOKIE))
    if identity_mode(request) == "header":
        # The proxy's header names the user; ProxyGate has already checked the request came through the proxy.
        # The cookie only carries the conversation, and only that user's.
        user = request.headers.get(auth.USER_HEADER)
        if user is None or user not in principals():
            raise NotSignedIn
        return found if found is not None and found.user_id == user else session.start(user)
    if found is None:
        raise NotSignedIn
    return found


Current = Annotated[session.Session, Depends(current_session)]


def current_principal(current: Current) -> Principal:
    return principal_for(current.user_id)


def operator(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not principal.ops:
        raise NotAnOperator
    return principal


def demo_only(request: Request) -> None:
    if identity_mode(request) != "demo":
        raise HTTPException(status_code=404)


def within_rate(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    wait = cast(RateLimiter, request.app.state.limiter).take(principal.user_id)
    if wait:
        raise RateLimited(wait)
    return principal


def pipeline() -> AskFn:
    return run_pipeline


def recorder() -> Recorder:
    return requestlog.write_request


async def dashboard_data(
    source: Annotated[Literal["ui", "eval", "replay"] | None, Query()] = None,
) -> dashboard.DashboardData:
    return await dashboard.load(source)


def _page(response: Response) -> Response:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/", response_class=HTMLResponse)
async def chat(request: Request) -> Response:
    demo = identity_mode(request) == "demo"
    cookie = session.decode(request.cookies.get(session.COOKIE))
    if not demo:
        current = current_session(request)
    elif cookie is None:
        current = session.start(next(iter(principals())))
    else:
        current = cookie
    users = list(principals().values()) if demo else []
    context = {"me": principal_for(current.user_id), "users": users, "demo": demo}
    response = templates.TemplateResponse(request, "chat.html", context)
    if current != cookie:
        session.set_cookie(response, current, secure=request.url.scheme == "https")
    return _page(response)


class Switch(BaseModel):
    user: str


@app.post("/session", status_code=204, dependencies=[Depends(demo_only)])
async def switch_user(body: Switch, request: Request) -> Response:
    # A stand-in for sign-in. In header mode the proxy's sign-in decides who is asking, and this route is a 404.
    if body.user not in principals():
        return JSONResponse(NOT_FOUND, status_code=404)
    response = Response(status_code=204)
    session.set_cookie(response, session.start(body.user), secure=request.url.scheme == "https")
    return response


class Question(BaseModel):
    q: str = Field(max_length=4000)


@app.post("/ask", response_class=EventSourceResponse)
async def ask(
    body: Question,
    current: Current,
    principal: Annotated[Principal, Depends(within_rate)],
    ask_fn: Annotated[AskFn, Depends(pipeline)],
    record: Annotated[Recorder, Depends(recorder)],
) -> AsyncIterator[ServerSentEvent]:
    # The question comes in the body, never the URL, so it stays out of access logs and browser history.
    events = answer_stream(ask_fn, principal, body.q, current.session_id, backend=settings().backend, record=record)
    async for event in events:
        yield event


@app.get("/evidence/scan/{doc_id}")
async def scan(doc_id: str, principal: Annotated[Principal, Depends(current_principal)]) -> Response:
    # The same answer for a scan that does not exist and one this user may not read, so ids cannot be probed.
    image = await scan_image(principal, doc_id)
    if image is None:
        return JSONResponse(NOT_FOUND, status_code=404)
    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    return Response(image, media_type="image/png", headers=headers)


class Feedback(BaseModel):
    request_id: UUID
    rating: Literal[1, -1]
    note: str | None = Field(default=None, max_length=1000)


@app.post("/feedback", status_code=204)
async def feedback(body: Feedback, principal: Annotated[Principal, Depends(current_principal)]) -> Response:
    # Only the user who asked can rate the answer, and a note is redacted the same way as a question.
    note = redact(body.note.strip()) if body.note and body.note.strip() else None
    params = {"request_id": body.request_id, "user_id": principal.user_id, "rating": body.rating, "note": note}
    await db.write(FEEDBACK, params)
    return Response(status_code=204)


# A route dependency runs before the endpoint's own, so the ops tables are never read for someone else.
@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(operator)])
async def dashboard_page(
    request: Request,
    data: Annotated[dashboard.DashboardData, Depends(dashboard_data)],
    source: Annotated[Literal["ui", "eval", "replay"] | None, Query()] = None,
) -> Response:
    # Aggregates only, with no question text.
    return _page(templates.TemplateResponse(request, "dashboard.html", dashboard.page(data, source)))


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
