import psycopg
import pytest
from psycopg.rows import TupleRow

from app.semantic.layer import DATE_COLUMNS, VIEWS, Layer

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]


def test_view_catalog_matches_the_database(superuser: Connection) -> None:
    rows = superuser.execute(
        "SELECT table_schema || '.' || table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema || '.' || table_name = ANY(%s)",
        (list(VIEWS),),
    ).fetchall()
    columns: dict[str, set[str]] = {}
    dates: dict[str, set[str]] = {}
    for view, column, data_type in rows:
        columns.setdefault(view, set()).add(column)
        if data_type == "date":
            dates.setdefault(view, set()).add(column)
    assert columns == {view: set(names) for view, names in VIEWS.items()}
    assert dates == {view: set(names) for view, names in DATE_COLUMNS.items()}


def test_analyst_reach_matches_the_aggregate_whitelist(layer: Layer, superuser: Connection) -> None:
    allowed: dict[str, list[str]] = dict(superuser.execute("SELECT name, dims FROM agg.allowed_measures").fetchall())
    reach = {
        name: set(layer.dimensions_for(measure, analyst=True))
        for name, measure in layer.measures.items()
        if measure.analyst
    }
    assert reach == {name: set(dims) for name, dims in allowed.items()}
