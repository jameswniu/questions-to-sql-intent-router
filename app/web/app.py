import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from app import db, requestlog, telemetry
from app.config import ROOT, Backend, settings
from app.identity import Principal, principal_for, principals
from app.llm import client as live
from app.pipeline import ask as run_pipeline
from app.redact import redact
from app.sources.scans import scan_image
from app.web import auth, dashboard, session
from app.web.ratelimit import RateLimiter
from app.web.stream import AskFn, Recorder, answer_stream

QUESTIONS_PER_MINUTE = 20
CSP = (
    "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)
NOT_FOUND = {"detail": "Not found"}
NOT_BUILT = "The web app has not been built. Run make frontend-build, or make up, which builds it in Docker."
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


def web_dist() -> Path:
    """Where the built browser app is: WEB_DIST, which the image sets, or frontend/dist after make frontend-build."""
    return Path(os.environ.get("WEB_DIST") or ROOT / "frontend" / "dist")


@cache
def _files(root: Path) -> StaticFiles:
    return RevalidatedFiles(directory=root, check_dir=False)


class BuiltAssets:
    """The browser app's scripts, stylesheet and fonts under /static, from the build in app.state.web_dist."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        root = cast(Path, scope["app"].state.web_dist) / "static"
        if not root.is_dir():
            await JSONResponse(NOT_FOUND, status_code=404)(scope, receive, send)
            return
        await _files(root)(scope, receive, send)


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
    # A live-mode setting that can't work stops the app here, rather than on the first question.
    live.default()
    yield
    await db.close_all()


app = FastAPI(title="claims-qa", lifespan=lifespan)
app.add_middleware(ProxyGate)
app.state.limiter = RateLimiter(QUESTIONS_PER_MINUTE, 60.0)
app.state.web_dist = web_dist()
app.mount("/static", BuiltAssets(), name="static")


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
    llm = live.default()
    if llm is None:
        return run_pipeline

    def ask_live(
        principal: Principal, question: str, session_id: str, *, backend: Backend = "none"
    ) -> AsyncIterator[object]:
        return run_pipeline(principal, question, session_id, backend=backend, llm=llm)

    return ask_live


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


def _index(request: Request) -> Response:
    """The browser app's one page. It draws the chat or the dashboard from the path, with data from /api."""
    try:
        html = (cast(Path, request.app.state.web_dist) / "index.html").read_bytes()
    except FileNotFoundError:
        return PlainTextResponse(NOT_BUILT, status_code=503)
    return _page(HTMLResponse(html, headers={"Cache-Control": "no-cache"}))


def page_session(request: Request) -> tuple[session.Session, bool]:
    """Who a page load asks as, and whether the session cookie needs setting. In demo mode a visitor without a
    session is signed in as the first demo user. Behind the proxy it is the proxy's user, or NotSignedIn."""
    cookie = session.decode(request.cookies.get(session.COOKIE))
    if identity_mode(request) != "demo":
        current = current_session(request)
    elif cookie is None:
        current = session.start(next(iter(principals())))
    else:
        current = cookie
    return current, current != cookie


@app.get("/", response_class=HTMLResponse)
async def chat(request: Request) -> Response:
    current, fresh = page_session(request)
    response = _index(request)
    if fresh:
        session.set_cookie(response, current, secure=request.url.scheme == "https")
    return response


class Person(BaseModel):
    user_id: str
    name: str
    title: str


class Me(Person):
    # The login every query this user causes runs on, which row-level security reads.
    db_role: str
    ops: bool


class SessionView(BaseModel):
    me: Me
    demo: bool
    # The users the demo picker offers. Behind the proxy there is no picker, and no one else is named.
    users: list[Person]


@app.get("/api/session")
async def session_view(request: Request, response: Response) -> SessionView:
    current, fresh = page_session(request)
    if fresh:
        session.set_cookie(response, current, secure=request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"
    me = principal_for(current.user_id)
    demo = identity_mode(request) == "demo"
    users = [Person(user_id=p.user_id, name=p.name, title=p.title) for p in principals().values()] if demo else []
    return SessionView(
        me=Me(user_id=me.user_id, name=me.name, title=me.title, db_role=me.db_role, ops=me.ops),
        demo=demo,
        users=users,
    )


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


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(operator)])
async def dashboard_page(
    request: Request, source: Annotated[Literal["ui", "eval", "replay"] | None, Query()] = None
) -> Response:
    # The page reads /api/dashboard with the same source, which is checked here too, so a filter the dashboard
    # doesn't have is refused before the page loads.
    return _index(request)


# A route dependency runs before the endpoint's own, so the ops tables are never read for someone else.
@app.get("/api/dashboard", dependencies=[Depends(operator)])
async def dashboard_view(
    response: Response,
    data: Annotated[dashboard.DashboardData, Depends(dashboard_data)],
    source: Annotated[Literal["ui", "eval", "replay"] | None, Query()] = None,
) -> dashboard.Page:
    # Aggregates only, with no question text.
    response.headers["Cache-Control"] = "no-store"
    return dashboard.page(data, source)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
