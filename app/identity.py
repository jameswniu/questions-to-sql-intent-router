import os
from dataclasses import dataclass
from functools import cache

from psycopg.conninfo import make_conninfo

from app.config import load_yaml, settings


@dataclass(frozen=True)
class Principal:
    user_id: str
    name: str
    title: str
    db_role: str
    kind: str
    regions: tuple[str, ...]


class UnknownUser(LookupError):
    pass


@cache
def principals() -> dict[str, Principal]:
    doc = load_yaml("users.yaml")
    roles = doc["roles"]
    return {
        user["id"]: Principal(
            user_id=user["id"],
            name=user["name"],
            title=user["title"],
            db_role=user["role"],
            kind=roles[user["role"]]["kind"],
            regions=tuple(roles[user["role"]]["regions"]),
        )
        for user in doc["users"]
    }


def principal_for(user_id: str) -> Principal:
    try:
        return principals()[user_id]
    except KeyError:
        raise UnknownUser(user_id) from None


def role_conninfo(role: str, password: str) -> str:
    s = settings()
    return make_conninfo(
        host=s.db_host, port=s.db_port, dbname=s.db_name, user=role, password=password, application_name="claims-qa"
    )


def db_credentials(principal: Principal) -> str:
    # The on-behalf-of seam. In production this takes the user's OIDC token and exchanges it
    # for a short-lived database credential for the same role; here the password comes from env.
    return role_conninfo(principal.db_role, os.environ[f"PGPASS_{principal.db_role.upper()}"])
